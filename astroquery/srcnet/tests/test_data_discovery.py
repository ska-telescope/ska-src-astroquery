"""
Tests for astroquery.srcnet.data_discovery module.

Covers:
  - Helper functions: _esc, _detect_tables, _fix_adql, _extract_adql,
    _patch_redirect_session
  - DataDiscoveryClass: query, get_tables, get_columns, get_collections,
    query_region, query_name, query_observations, get_artifacts,
    nl_to_adql, query_natural — all with mocked TAP/HTTP backends
"""
import pytest
from unittest.mock import MagicMock, patch

import requests
from astropy.coordinates import SkyCoord
from astropy.table import Table
import astropy.units as u

from astroquery.srcnet.data_discovery import (
    DataDiscoveryClass,
    _detect_tables,
    _esc,
    _extract_adql,
    _fix_adql,
    _patch_redirect_session,
)


# ─────────────────────────────────────────────────────────────────────────────
# _esc
# ─────────────────────────────────────────────────────────────────────────────

class TestEscDd:
    def test_plain_string_unchanged(self):
        assert _esc("JCMT") == "JCMT"

    def test_single_quote_doubled(self):
        assert _esc("L'Aquila") == "L''Aquila"

    def test_multiple_quotes(self):
        result = _esc("it's 'here'")
        assert result == "it''s ''here''"


# ─────────────────────────────────────────────────────────────────────────────
# _detect_tables
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectTablesDd:
    def test_finds_ivoa_table(self):
        assert "ivoa.ObsCore" in _detect_tables("FROM ivoa.ObsCore", {"ivoa"})

    def test_finds_caom2_table(self):
        assert "caom2.Observation" in _detect_tables("FROM caom2.Observation", {"caom2"})

    def test_ignores_unknown_schema(self):
        assert _detect_tables("FROM sdm.software", {"ivoa"}) == []

    def test_empty_text(self):
        assert _detect_tables("", {"ivoa"}) == []


# ─────────────────────────────────────────────────────────────────────────────
# _fix_adql
# ─────────────────────────────────────────────────────────────────────────────

class TestFixAdqlDd:
    def test_no_limit_unchanged(self):
        q = "SELECT * FROM ivoa.ObsCore"
        assert _fix_adql(q) == q

    def test_limit_becomes_top(self):
        result = _fix_adql("SELECT * FROM ivoa.ObsCore LIMIT 20")
        assert "TOP 20" in result
        assert "LIMIT" not in result

    def test_limit_with_semicolon(self):
        result = _fix_adql("SELECT * FROM ivoa.ObsCore LIMIT 5;")
        assert "LIMIT" not in result
        assert "TOP 5" in result


# ─────────────────────────────────────────────────────────────────────────────
# _extract_adql
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractAdqlDd:
    def test_sql_fenced_block(self):
        text = "```sql\nSELECT * FROM ivoa.ObsCore\n```"
        assert "ivoa.ObsCore" in _extract_adql(text)

    def test_adql_fenced_block(self):
        text = "```adql\nSELECT TOP 5 * FROM ivoa.ObsCore\n```"
        assert "TOP 5" in _extract_adql(text)

    def test_plain_select_line(self):
        text = "Here is the query:\nSELECT * FROM ivoa.ObsCore WHERE target_name = 'Orion'"
        assert "ivoa.ObsCore" in _extract_adql(text)

    def test_python_triple_quote_block(self):
        text = '```python\ntap.query("""\nSELECT * FROM ivoa.ObsCore\n""")\n```'
        result = _extract_adql(text)
        assert "SELECT" in result.upper()

    def test_fallback_raw_text(self):
        # When no SELECT present, return text as-is (after _fix_adql).
        result = _extract_adql("plain text without sql")
        assert "plain text" in result


# ─────────────────────────────────────────────────────────────────────────────
# _patch_redirect_session
# ─────────────────────────────────────────────────────────────────────────────

class TestPatchRedirectSessionDd:
    def test_hook_registered(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.srcnet.skao.int/argus/")
        assert len(session.hooks["response"]) == 1

    def test_localhost_rewritten(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.srcnet.skao.int/argus/")
        fake_resp = MagicMock()
        fake_resp.headers = {"Location": "http://localhost:9090/tap/async/1"}
        session.hooks["response"][0](fake_resp)
        assert "tap.srcnet.skao.int" in fake_resp.headers["Location"]

    def test_external_host_not_rewritten(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.srcnet.skao.int/argus/")
        fake_resp = MagicMock()
        original = "https://other.host/tap/async/1"
        fake_resp.headers = {"Location": original}
        session.hooks["response"][0](fake_resp)
        assert fake_resp.headers["Location"] == original

    def test_no_location_header_no_error(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.srcnet.skao.int/argus/")
        fake_resp = MagicMock()
        fake_resp.headers = {}
        session.hooks["response"][0](fake_resp)  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# DataDiscoveryClass — mocked TAP service
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dd():
    """DataDiscoveryClass with an injected mock TAP service."""
    obj = DataDiscoveryClass(tap_url="http://mock-tap/argus/")
    mock_tap = MagicMock()
    mock_result = MagicMock()
    mock_result.to_table.return_value = Table({
        "obs_id": ["obs001"],
        "obs_collection": ["JCMT"],
        "facility_name": ["JCMT"],
    })
    mock_tap.search.return_value = mock_result
    obj._tap = mock_tap
    return obj


class TestDataDiscoveryQuery:
    """query() — execute raw ADQL."""

    def test_returns_table(self, dd):
        result = dd.query("SELECT TOP 10 * FROM ivoa.ObsCore")
        assert isinstance(result, Table)
        assert len(result) == 1

    def test_adql_passed_to_tap_search(self, dd):
        adql = "SELECT * FROM ivoa.ObsCore WHERE obs_collection = 'JCMT'"
        dd.query(adql)
        assert dd._tap.search.call_args[0][0] == adql


class TestGetCollections:
    """get_collections() — grouped count query."""

    def test_returns_table(self, dd):
        result = dd.get_collections()
        assert isinstance(result, Table)

    def test_adql_contains_group_by(self, dd):
        dd.get_collections()
        adql = dd._tap.search.call_args[0][0]
        assert "GROUP BY obs_collection" in adql

    def test_verbose_prints_adql(self, dd, capsys):
        dd.get_collections(verbose=True)
        assert "[ADQL]" in capsys.readouterr().out

    def test_non_verbose_no_print(self, dd, capsys):
        dd.get_collections(verbose=False)
        assert "[ADQL]" not in capsys.readouterr().out


class TestQueryRegion:
    """query_region() — ADQL cone search."""

    def _coord(self):
        return SkyCoord(83.8, -5.4, unit="deg")

    def test_returns_table(self, dd):
        result = dd.query_region(self._coord(), radius=0.5 * u.deg)
        assert isinstance(result, Table)

    def test_adql_contains_contains(self, dd):
        dd.query_region(self._coord(), radius=0.5 * u.deg)
        adql = dd._tap.search.call_args[0][0]
        assert "CONTAINS" in adql
        assert "CIRCLE" in adql

    def test_adql_contains_ra_dec_values(self, dd):
        coord = SkyCoord(83.8, -5.4, unit="deg")
        dd.query_region(coord, radius=1.0 * u.deg)
        adql = dd._tap.search.call_args[0][0]
        assert "83.8" in adql
        assert "-5.4" in adql

    def test_collection_filter_added(self, dd):
        dd.query_region(self._coord(), radius=0.5 * u.deg, collection="JCMT")
        adql = dd._tap.search.call_args[0][0]
        assert "obs_collection = 'JCMT'" in adql

    def test_radius_converted_to_degrees(self, dd):
        # 30 arcmin = 0.5 deg
        dd.query_region(self._coord(), radius=30 * u.arcmin)
        adql = dd._tap.search.call_args[0][0]
        assert "0.5" in adql

    def test_verbose_prints_adql(self, dd, capsys):
        dd.query_region(self._coord(), radius=0.5 * u.deg, verbose=True)
        assert "[ADQL]" in capsys.readouterr().out


class TestQueryName:
    """query_name() — target-name substring search."""

    def test_returns_table(self, dd):
        result = dd.query_name("Orion")
        assert isinstance(result, Table)

    def test_adql_contains_like_clause(self, dd):
        dd.query_name("Orion")
        adql = dd._tap.search.call_args[0][0]
        assert "LIKE" in adql
        assert "Orion" in adql

    def test_collection_filter_added(self, dd):
        dd.query_name("Orion", collection="JCMT")
        adql = dd._tap.search.call_args[0][0]
        assert "obs_collection = 'JCMT'" in adql

    def test_name_value_escaped(self, dd):
        dd.query_name("O'Brien")
        adql = dd._tap.search.call_args[0][0]
        assert "O''Brien" in adql

    def test_verbose_prints_adql(self, dd, capsys):
        dd.query_name("Crab", verbose=True)
        assert "[ADQL]" in capsys.readouterr().out


class TestQueryObservations:
    """query_observations() — optional multi-filter query."""

    def test_no_filters_no_where(self, dd):
        dd.query_observations()
        adql = dd._tap.search.call_args[0][0]
        assert "WHERE" not in adql

    def test_collection_filter(self, dd):
        dd.query_observations(collection="JCMT")
        adql = dd._tap.search.call_args[0][0]
        assert "obs_collection = 'JCMT'" in adql

    def test_telescope_filter_uses_like(self, dd):
        dd.query_observations(telescope="JCMT")
        adql = dd._tap.search.call_args[0][0]
        assert "facility_name LIKE '%JCMT%'" in adql

    def test_instrument_filter_uses_like(self, dd):
        dd.query_observations(instrument="SCUBA-2")
        adql = dd._tap.search.call_args[0][0]
        assert "instrument_name LIKE '%SCUBA-2%'" in adql

    def test_target_name_filter_uses_like(self, dd):
        dd.query_observations(target_name="Orion")
        adql = dd._tap.search.call_args[0][0]
        assert "target_name LIKE '%Orion%'" in adql

    def test_multiple_filters_combined_with_and(self, dd):
        dd.query_observations(collection="JCMT", telescope="JCMT")
        adql = dd._tap.search.call_args[0][0]
        assert " AND " in adql

    def test_verbose_mode_prints_adql(self, dd, capsys):
        dd.query_observations(collection="JCMT", verbose=True)
        assert "[ADQL]" in capsys.readouterr().out


class TestGetArtifacts:
    """get_artifacts() — two-step ObsCore + CAOM2 query."""

    def test_returns_empty_table_when_obs_not_found(self, dd):
        # Step 1 returns no rows → empty artifact table.
        dd._tap.search.return_value.to_table.return_value = Table(
            names=["obs_publisher_did"]
        )
        result = dd.get_artifacts("missing_obs")
        assert len(result) == 0

    def test_second_query_uses_caom2_tables(self, dd):
        # Step 1 finds a publisher DID.
        calls = []
        tables = [
            Table({"obs_publisher_did": ["caom2:JCMT/obs001/science"]}),
            Table({"uri": ["ad:JCMT/file1.fits"], "productType": ["science"],
                   "releaseType": ["public"], "contentType": ["image/fits"],
                   "contentLength": [1024]}),
        ]

        def search_side_effect(adql, **kw):
            m = MagicMock()
            m.to_table.return_value = tables[len(calls)]
            calls.append(adql)
            return m

        dd._tap.search.side_effect = search_side_effect
        dd.get_artifacts("obs001")
        # Second query must target CAOM2 tables.
        assert any("caom2.Plane" in q or "caom2.Artifact" in q for q in calls)


class TestNlToAdqlDd:
    """nl_to_adql() — natural-language to ADQL via chatserver or Ollama."""

    def test_chatserver_path_returns_adql(self, dd):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"adql": "SELECT * FROM ivoa.ObsCore", "answer": ""}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_resp):
            result = dd.nl_to_adql("show all", chatserver_url="http://cs/")

        assert result == "SELECT * FROM ivoa.ObsCore"

    def test_chatserver_falls_back_to_answer_field(self, dd):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "adql": None,
            "answer": "SELECT TOP 10 * FROM ivoa.ObsCore",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_resp):
            result = dd.nl_to_adql("show all", chatserver_url="http://cs/")

        assert "ivoa.ObsCore" in result

    def test_chatserver_connection_error_raises(self, dd):
        with (
            patch(
                "requests.post",
                side_effect=requests.exceptions.ConnectionError(),
            ),
            pytest.raises(RuntimeError, match="CHATSERVER"),
        ):
            dd.nl_to_adql("show all", chatserver_url="http://cs/")

    def test_explicit_table_tags_added_when_table_in_question(self, dd):
        # If the question already mentions a TAP table, tag it explicitly.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"adql": "SELECT * FROM ivoa.ObsCore"}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_resp) as mock_post:
            dd.nl_to_adql("show data from ivoa.ObsCore", chatserver_url="http://cs/")

        sent_msg = mock_post.call_args[1]["json"]["message"]
        assert "explicit_tables" in sent_msg


class TestQueryNaturalDd:
    """query_natural() — NL translation then TAP execution."""

    def test_returns_adql_and_table(self, dd):
        adql = "SELECT * FROM ivoa.ObsCore"
        with (
            patch.object(dd, "nl_to_adql", return_value=adql),
            patch.object(dd, "query", return_value=Table({"obs_id": ["001"]})),
        ):
            returned_adql, table = dd.query_natural("show observations")

        assert returned_adql == adql
        assert isinstance(table, Table)

    def test_verbose_prints_adql(self, dd, capsys):
        with (
            patch.object(dd, "nl_to_adql", return_value="SELECT * FROM ivoa.ObsCore"),
            patch.object(dd, "query", return_value=Table({"obs_id": []})),
        ):
            dd.query_natural("show obs", verbose=True)

        assert "[ADQL]" in capsys.readouterr().out
