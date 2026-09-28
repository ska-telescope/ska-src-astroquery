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


# ─────────────────────────────────────────────────────────────────────────────
# MAN-827 Q1-Q5 shortcuts: SearchFilters, _build_where, search, count_by,
# explain, execute_adql, count_adql, _add_namespace_filename_columns
# ─────────────────────────────────────────────────────────────────────────────

from astroquery.srcnet.data_discovery import (
    SearchFilters,
    _add_namespace_filename_columns,
    _decode_cursor,
    _encode_cursor,
)


class TestSearchFilters:
    def test_all_none_by_default(self):
        f = SearchFilters()
        assert f.coordinates is None
        assert f.radius is None
        assert f.obs_publisher_did is None
        assert f.dataproduct_type is None
        assert f.target_name is None
        assert f.collection is None
        assert f.facility is None
        assert f.instrument is None
        assert f.namespace is None
        assert f.order is None

    def test_add_filter_chains(self):
        f = SearchFilters().add_filter("collection", "JCMT").add_filter("dataproduct_type", "image")
        assert f.collection == "JCMT"
        assert f.dataproduct_type == "image"

    def test_add_filter_different_fields_example(self):
        # One example covering several distinct fields in one chain.
        f = (
            SearchFilters()
            .add_filter("collection", "JCMT")
            .add_filter("facility", "JCMT")
            .add_filter("instrument", "SCUBA-2")
            .add_filter("dataproduct_type", "image")
            .add_filter("target_name", "M31")
            .add_filter("namespace", "testing")
            .add_filter("filename", "foo.fits")
            .add_filter("obs_publisher_did", ["did:1", "did:2"])
        )
        assert f.collection == "JCMT"
        assert f.facility == "JCMT"
        assert f.instrument == "SCUBA-2"
        assert f.dataproduct_type == "image"
        assert f.target_name == "M31"
        assert f.namespace == "testing"
        assert f.filename == "foo.fits"
        assert f.obs_publisher_did == ["did:1", "did:2"]

    def test_add_filter_position_tuple(self):
        coords = SkyCoord(10.0, 20.0, unit="deg")
        f = SearchFilters().add_filter("position", (coords, 0.5 * u.deg))
        assert f.coordinates is coords
        assert f.radius == 0.5 * u.deg

    def test_add_filter_unknown_field_raises(self):
        with pytest.raises(ValueError, match="unknown filter field"):
            SearchFilters().add_filter("colection", "JCMT")  # typo'd field name

    def test_set_position_is_equivalent_to_add_filter(self):
        coords = SkyCoord(10.0, 20.0, unit="deg")
        f = SearchFilters().set_position(coords, 0.5 * u.deg)
        assert f.coordinates is coords
        assert f.radius == 0.5 * u.deg

    def test_set_position_returns_self_for_chaining(self):
        f = SearchFilters()
        assert f.set_position(SkyCoord(10.0, 20.0, unit="deg"), 0.5 * u.deg) is f

    def test_set_order_default_direction_is_asc(self):
        f = SearchFilters().set_order("t_min")
        assert f.order == ("t_min", "ASC")

    def test_set_order_desc(self):
        f = SearchFilters().set_order("t_min", "desc")
        assert f.order == ("t_min", "DESC")

    def test_set_order_chains_with_add_filter(self):
        f = SearchFilters().add_filter("collection", "JCMT").set_order("t_min", "DESC")
        assert f.collection == "JCMT"
        assert f.order == ("t_min", "DESC")

    def test_set_order_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction must be"):
            SearchFilters().set_order("t_min", "SIDEWAYS")

    def test_set_order_unknown_column_raises(self):
        with pytest.raises(ValueError, match="unknown ivoa.ObsCore column"):
            SearchFilters().set_order("not_a_real_column")


class TestBuildWhere:
    """_build_where() — the WHERE-clause builder shared by search/count_by/explain."""

    def test_none_filters_no_conditions(self, dd):
        assert dd._build_where(None) == []

    def test_empty_filters_no_conditions(self, dd):
        assert dd._build_where(SearchFilters()) == []

    def test_position_uses_s_region_not_s_ra_s_dec(self, dd):
        f = SearchFilters().set_position(SkyCoord(10.0, 20.0, unit="deg"), 0.5 * u.deg)
        where = dd._build_where(f)
        assert len(where) == 1
        assert "CONTAINS(s_region, CIRCLE('ICRS', 10.0, 20.0, 0.5)) = 1" == where[0]
        assert "s_ra" not in where[0]
        assert "s_dec" not in where[0]

    def test_position_requires_both_coordinates_and_radius(self, dd):
        f = SearchFilters().add_filter("position", (SkyCoord(10.0, 20.0, unit="deg"), None))  # no radius
        assert dd._build_where(f) == []

    def test_obs_publisher_did_in_list(self, dd):
        where = dd._build_where(SearchFilters().add_filter("obs_publisher_did", ["a", "b"]))
        assert where == ["obs_publisher_did IN ('a', 'b')"]

    def test_dataproduct_type_none_omitted(self, dd):
        assert dd._build_where(SearchFilters().add_filter("dataproduct_type", None)) == []

    def test_dataproduct_type_empty_string_matches_null_or_blank(self, dd):
        where = dd._build_where(SearchFilters().add_filter("dataproduct_type", ""))
        assert where == ["(dataproduct_type IS NULL OR dataproduct_type = '')"]

    def test_dataproduct_type_value_exact_match_ignoring_case(self, dd):
        where = dd._build_where(SearchFilters().add_filter("dataproduct_type", "image"))
        assert where == ["UPPER(dataproduct_type) = UPPER('image')"]

    def test_target_name_exact_not_substring(self, dd):
        where = dd._build_where(SearchFilters().add_filter("target_name", "M31"))
        assert where == ["UPPER(target_name) = UPPER('M31')"]
        assert "LIKE" not in where[0]

    def test_collection_facility_instrument_exact_match(self, dd):
        f = SearchFilters().add_filter("collection", "JCMT").add_filter("facility", "JCMT").add_filter("instrument", "SCUBA-2")
        where = dd._build_where(f)
        assert "UPPER(obs_collection) = UPPER('JCMT')" in where
        assert "UPPER(facility_name) = UPPER('JCMT')" in where
        assert "UPPER(instrument_name) = UPPER('SCUBA-2')" in where

    def test_namespace_is_prefix_match_on_obs_id(self, dd):
        where = dd._build_where(SearchFilters().add_filter("namespace", "testing"))
        assert where == ["obs_id LIKE 'testing:%'"]

    def test_filename_is_suffix_match_on_obs_id(self, dd):
        where = dd._build_where(SearchFilters().add_filter("filename", "foo.fits"))
        assert where == ["obs_id LIKE '%:foo.fits'"]

    def test_string_filters_escaped(self, dd):
        where = dd._build_where(SearchFilters().add_filter("target_name", "O'Brien"))
        assert "O''Brien" in where[0]

    def test_multiple_filters_all_present(self, dd):
        f = SearchFilters().add_filter("collection", "JCMT").add_filter("dataproduct_type", "image").add_filter("target_name", "M31")
        where = dd._build_where(f)
        assert len(where) == 3


class TestExplain:
    """explain() — same ADQL search() would run, without executing it."""

    def test_no_network_call(self, dd):
        dd.explain(SearchFilters().add_filter("collection", "JCMT"))
        dd._tap.search.assert_not_called()

    def test_returns_string(self, dd):
        assert isinstance(dd.explain(), str)

    def test_orders_by_publisher_did_without_position(self, dd):
        adql = dd.explain()
        assert "ORDER BY obs_publisher_did" in adql

    def test_no_order_by_with_position(self, dd):
        f = SearchFilters().set_position(SkyCoord(10.0, 20.0, unit="deg"), 0.5 * u.deg)
        adql = dd.explain(f)
        assert "ORDER BY" not in adql

    def test_top_is_exactly_page_size_not_page_size_plus_one(self, dd):
        # search() internally fetches page_size + 1 to detect another page;
        # explain() must show what a user would actually run, not that detail.
        adql = dd.explain(page_size=25)
        assert "TOP 25 " in adql

    def test_after_adds_keyset_condition(self, dd):
        adql = dd.explain(after="abc123")
        assert "obs_publisher_did > 'abc123'" in adql

    def test_after_ignored_with_position(self, dd):
        f = SearchFilters().set_position(SkyCoord(10.0, 20.0, unit="deg"), 0.5 * u.deg)
        adql = dd.explain(f, after="abc123")
        assert "abc123" not in adql

    def test_matches_search_where_clause(self, dd):
        # explain() and search() must never disagree -- same _build_where call.
        f = SearchFilters().add_filter("collection", "JCMT")
        explained = dd.explain(f, page_size=10)
        with patch.object(dd, "query", return_value=Table({"obs_publisher_did": []})) as mock_query:
            dd.search(f, page_size=10)
        executed_adql = mock_query.call_args[0][0]
        assert "WHERE UPPER(obs_collection) = UPPER('JCMT')" in explained
        assert "WHERE UPPER(obs_collection) = UPPER('JCMT')" in executed_adql


class TestSearch:
    """search() — Q1: one shortcut for any combination of filters, keyset-paged."""

    def _rows(self, n, start=0):
        return Table({
            "obs_publisher_did": [f"did{i:03d}" for i in range(start, start + n)],
            "obs_id": [f"ns{i}:file{i}.fits" for i in range(start, start + n)],
        })

    def test_returns_table(self, dd):
        with patch.object(dd, "query", return_value=self._rows(3)):
            result = dd.search()
        assert isinstance(result, Table)

    def test_requests_page_size_plus_one(self, dd):
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(page_size=10)
        assert mock_query.call_args[1]["maxrec"] == 11
        assert "TOP 11" in mock_query.call_args[0][0]

    def test_short_page_has_no_next_after(self, dd):
        # fewer than page_size + 1 rows back => this was the last page
        with patch.object(dd, "query", return_value=self._rows(3)):
            result = dd.search(page_size=10)
        assert len(result) == 3
        assert result.meta["next_after"] is None

    def test_full_extra_row_sets_next_after_and_trims(self, dd):
        # page_size + 1 rows back => there's another page; result trimmed to page_size
        with patch.object(dd, "query", return_value=self._rows(4)):
            result = dd.search(page_size=3)
        assert len(result) == 3
        assert result.meta["next_after"] == "did002"  # last row AFTER trimming

    def test_position_search_never_sets_next_after(self, dd):
        f = SearchFilters().set_position(SkyCoord(10.0, 20.0, unit="deg"), 0.5 * u.deg)
        with patch.object(dd, "query", return_value=self._rows(4)):
            result = dd.search(f, page_size=3)
        assert result.meta["next_after"] is None

    def test_position_search_requests_exactly_page_size_not_plus_one(self, dd):
        # A position search never exposes a next page (previous test), so the
        # +1-to-detect-another-page trick buys it nothing -- asking for one
        # more row than the caller requested was a real, needless bug once
        # spotted live (page_size=100 default produced "TOP 101").
        f = SearchFilters().set_position(SkyCoord(10.0, 20.0, unit="deg"), 0.5 * u.deg)
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(f, page_size=10)
        assert mock_query.call_args[1]["maxrec"] == 10
        assert "TOP 10 " in mock_query.call_args[0][0]
        assert "TOP 11" not in mock_query.call_args[0][0]

    def test_position_search_matches_explain_exactly(self, dd):
        # search() and explain() must agree on what TOP value a position
        # search actually runs -- they didn't before the fix above (explain()
        # already showed the plain page_size; search() silently asked for
        # page_size + 1 regardless of has_position).
        f = SearchFilters().set_position(SkyCoord(10.0, 20.0, unit="deg"), 0.5 * u.deg)
        explained = dd.explain(f, page_size=10)
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(f, page_size=10)
        assert mock_query.call_args[0][0] == explained

    def test_split_obs_id_default_adds_columns(self, dd):
        with patch.object(dd, "query", return_value=self._rows(2)):
            result = dd.search()
        assert "namespace" in result.colnames
        assert "filename" in result.colnames
        assert result["namespace"][0] == "ns0"
        assert result["filename"][0] == "file0.fits"

    def test_split_obs_id_false_skips_columns(self, dd):
        with patch.object(dd, "query", return_value=self._rows(2)):
            result = dd.search(split_obs_id=False)
        assert "namespace" not in result.colnames

    def test_with_total_count_makes_second_query(self, dd):
        count_table = Table({"num_records": [42]})
        page_table = self._rows(2)
        with patch.object(dd, "query", side_effect=[page_table, count_table]) as mock_query:
            result = dd.search(SearchFilters().add_filter("collection", "JCMT"), with_total_count=True)
        assert mock_query.call_count == 2
        count_adql = mock_query.call_args_list[1][0][0]
        assert "COUNT(*)" in count_adql
        assert "FROM (" not in count_adql  # no sub-select -- Argus rejects it
        assert result.meta["total_count"] == 42

    def test_without_total_count_makes_one_query(self, dd):
        with patch.object(dd, "query", return_value=self._rows(2)) as mock_query:
            dd.search()
        assert mock_query.call_count == 1

    def test_verbose_prints_adql(self, dd, capsys):
        with patch.object(dd, "query", return_value=self._rows(1)):
            dd.search(verbose=True)
        assert "[ADQL]" in capsys.readouterr().out

    def test_after_passed_through_as_keyset_cursor(self, dd):
        with patch.object(dd, "query", return_value=self._rows(1)) as mock_query:
            dd.search(after="did005")
        assert "obs_publisher_did > 'did005'" in mock_query.call_args[0][0]


class TestSearchUnbounded:
    """search(page_size=None) — opt out of pagination entirely, matching how
    query()/execute_adql() already behave by default (maxrec-only, no TOP)."""

    def _rows(self, n):
        return Table({
            "obs_publisher_did": [f"did{i:03d}" for i in range(n)],
            "obs_id": [f"ns{i}:file{i}.fits" for i in range(n)],
        })

    def test_no_top_in_adql(self, dd):
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(page_size=None)
        adql = mock_query.call_args[0][0]
        assert "TOP" not in adql

    def test_maxrec_is_none_delegates_to_query_default(self, dd):
        # query()'s own default (conf.SRCNET_DEFAULT_MAXREC) applies, the same
        # as execute_adql() -- search() shouldn't invent a second default cap.
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(page_size=None)
        assert mock_query.call_args[1]["maxrec"] is None

    def test_never_sets_next_after(self, dd):
        with patch.object(dd, "query", return_value=self._rows(50)):
            result = dd.search(page_size=None)
        assert result.meta["next_after"] is None

    def test_after_is_ignored(self, dd):
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(after="did005", page_size=None)
        assert "did005" not in mock_query.call_args[0][0]

    def test_does_not_trim_results(self, dd):
        # No page boundary to trim to -- every row query() returns comes back.
        with patch.object(dd, "query", return_value=self._rows(500)):
            result = dd.search(page_size=None)
        assert len(result) == 500

    def test_still_orders_deterministically_without_position(self, dd):
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(page_size=None)
        assert "ORDER BY obs_publisher_did" in mock_query.call_args[0][0]

    def test_with_total_count_still_works(self, dd):
        count_table = Table({"num_records": [500]})
        page_table = self._rows(3)
        with patch.object(dd, "query", side_effect=[page_table, count_table]):
            result = dd.search(page_size=None, with_total_count=True)
        assert result.meta["total_count"] == 500

    def test_explain_matches_search_exactly(self, dd):
        f = SearchFilters().add_filter("collection", "JCMT")
        explained = dd.explain(f, page_size=None)
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(f, page_size=None)
        assert mock_query.call_args[0][0] == explained


class TestCursorEncoding:
    """_encode_cursor()/_decode_cursor() -- the opaque composite keyset cursor
    a custom set_order() needs (order field's last value + obs_publisher_did
    tie-breaker), round-tripped through a plain string so search()'s cursor
    contract stays a single str either way."""

    def test_roundtrip_numeric(self):
        cursor = _encode_cursor(59000.5, "ivo://x/1")
        assert _decode_cursor(cursor) == (59000.5, "ivo://x/1")

    def test_roundtrip_string(self):
        cursor = _encode_cursor("JCMT-obs-1", "ivo://x/2")
        assert _decode_cursor(cursor) == ("JCMT-obs-1", "ivo://x/2")

    def test_numeric_value_stays_float_not_string(self):
        cursor = _encode_cursor(1.0, "ivo://x/3")
        order_value, _ = _decode_cursor(cursor)
        assert isinstance(order_value, float)

    def test_default_no_order_cursor_is_unencoded(self, dd):
        # Backward compatibility: the no-custom-order path still hands back
        # a plain, unencoded obs_publisher_did string, exactly as before this
        # cursor encoding existed.
        rows = Table({
            "obs_publisher_did": ["did001", "did002"],
            "obs_id": ["ns:file1.fits", "ns:file2.fits"],
        })
        with patch.object(dd, "query", return_value=rows):
            result = dd.search(page_size=1)
        assert result.meta["next_after"] == "did001"


class TestColumnsWithOrder:
    """_columns_with_order() -- auto-appends a custom set_order() field to
    the SELECT list so next_after can read it back and explain()/search()
    never silently disagree on what's selected."""

    def test_no_filters_returns_columns_unchanged(self, dd):
        cols = dd._columns_with_order(dd.DEFAULT_SEARCH_COLUMNS, None)
        assert cols == dd.DEFAULT_SEARCH_COLUMNS

    def test_no_order_returns_columns_unchanged(self, dd):
        f = SearchFilters().add_filter("collection", "JCMT")
        cols = dd._columns_with_order(dd.DEFAULT_SEARCH_COLUMNS, f)
        assert cols == dd.DEFAULT_SEARCH_COLUMNS

    def test_order_field_already_present_not_duplicated(self, dd):
        f = SearchFilters().set_order("obs_id")  # obs_id is in DEFAULT_SEARCH_COLUMNS
        cols = dd._columns_with_order(dd.DEFAULT_SEARCH_COLUMNS, f)
        assert cols == dd.DEFAULT_SEARCH_COLUMNS
        assert cols.count("obs_id") == 1

    def test_order_field_missing_is_appended(self, dd):
        f = SearchFilters().set_order("t_min")
        cols = dd._columns_with_order(dd.DEFAULT_SEARCH_COLUMNS, f)
        assert cols == f"{dd.DEFAULT_SEARCH_COLUMNS}, t_min"


class TestSearchCustomOrder:
    """set_order() combined with search()/explain() -- confirmed live against
    Argus that a plain ORDER BY isn't blocked by the OFFSET restriction, and
    that keyset pagination with a custom order needs the tie-breaking WHERE
    form (field > :v) OR (field = :v AND obs_publisher_did > :did), not the
    more compact row-value tuple form (rejected by Argus as a syntax error)."""

    def _rows(self, n, start=0):
        return Table({
            "obs_publisher_did": [f"did{i:03d}" for i in range(start, start + n)],
            "obs_id": [f"ns{i}:file{i}.fits" for i in range(start, start + n)],
            "t_min": [float(59000 + i) for i in range(start, start + n)],
        })

    def test_orders_by_custom_field_then_did(self, dd):
        f = SearchFilters().set_order("t_min", "DESC")
        adql = dd.explain(f, page_size=10)
        assert "ORDER BY t_min DESC, obs_publisher_did ASC" in adql

    def test_asc_is_default_direction(self, dd):
        f = SearchFilters().set_order("t_min")
        adql = dd.explain(f, page_size=10)
        assert "ORDER BY t_min ASC, obs_publisher_did ASC" in adql

    def test_no_after_no_tiebreak_condition(self, dd):
        f = SearchFilters().set_order("t_min", "DESC")
        adql = dd.explain(f, page_size=10)
        assert "WHERE" not in adql

    def test_after_adds_tiebreak_condition_asc(self, dd):
        f = SearchFilters().set_order("t_min", "ASC")
        cursor = _encode_cursor(59000.0, "did000")
        adql = dd.explain(f, after=cursor, page_size=10)
        assert "(t_min > 59000.0 OR (t_min = 59000.0 AND obs_publisher_did > 'did000'))" in adql

    def test_after_adds_tiebreak_condition_desc(self, dd):
        # DESC flips the comparison operator: next page is *smaller* t_min.
        f = SearchFilters().set_order("t_min", "DESC")
        cursor = _encode_cursor(59000.0, "did000")
        adql = dd.explain(f, after=cursor, page_size=10)
        assert "(t_min < 59000.0 OR (t_min = 59000.0 AND obs_publisher_did > 'did000'))" in adql

    def test_row_value_tuple_form_never_generated(self, dd):
        # Confirmed live: Argus rejects (field, obs_publisher_did) > (:v, :did)
        # as an ADQL syntax error -- must never be what gets generated.
        f = SearchFilters().set_order("t_min", "ASC")
        cursor = _encode_cursor(59000.0, "did000")
        adql = dd.explain(f, after=cursor, page_size=10)
        assert "(t_min, obs_publisher_did)" not in adql

    def test_select_list_includes_order_column(self, dd):
        f = SearchFilters().set_order("t_min", "DESC")
        adql = dd.explain(f, page_size=10)
        assert ", t_min" in adql.split(" FROM ")[0]

    def test_search_next_after_is_encoded_composite_cursor(self, dd):
        f = SearchFilters().set_order("t_min", "ASC")
        with patch.object(dd, "query", return_value=self._rows(4)):
            result = dd.search(f, page_size=3)
        assert _decode_cursor(result.meta["next_after"]) == (59002.0, "did002")

    def test_search_and_explain_agree_with_custom_order(self, dd):
        # Same as the default-order case (TestExplain.test_matches_search_where_clause):
        # explain() shows TOP page_size, search() fetches TOP page_size + 1
        # internally to detect another page -- that's the one documented
        # difference, everything else must match exactly.
        f = SearchFilters().add_filter("collection", "JCMT").set_order("t_min", "DESC")
        explained = dd.explain(f, page_size=10)
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(f, page_size=10)
        executed = mock_query.call_args[0][0]
        assert explained.replace("TOP 10 ", "TOP 11 ") == executed

    def test_search_and_explain_agree_with_custom_order_and_after(self, dd):
        f = SearchFilters().set_order("t_min", "ASC")
        cursor = _encode_cursor(59000.0, "did000")
        explained = dd.explain(f, after=cursor, page_size=10)
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            dd.search(f, after=cursor, page_size=10)
        executed = mock_query.call_args[0][0]
        assert explained.replace("TOP 10 ", "TOP 11 ") == executed

    def test_custom_order_unbounded_no_top_no_cursor(self, dd):
        f = SearchFilters().set_order("t_min", "DESC")
        with patch.object(dd, "query", return_value=self._rows(3)) as mock_query:
            result = dd.search(f, page_size=None)
        adql = mock_query.call_args[0][0]
        assert "TOP" not in adql
        assert "ORDER BY t_min DESC, obs_publisher_did ASC" in adql
        assert result.meta["next_after"] is None


class TestCountBy:
    """count_by() — Q2: grouped counts for the same filter set search() takes."""

    def test_default_group_by_dataproduct_type(self, dd):
        with patch.object(dd, "query", return_value=Table({"dataproduct_type": [], "num_records": []})) as mock_query:
            dd.count_by()
        adql = mock_query.call_args[0][0]
        assert "GROUP BY dataproduct_type" in adql

    def test_custom_group_by_fields(self, dd):
        with patch.object(dd, "query", return_value=Table({"a": [], "b": [], "num_records": []})) as mock_query:
            dd.count_by(group_by=["dataproduct_type", "facility_name"])
        adql = mock_query.call_args[0][0]
        assert "GROUP BY dataproduct_type, facility_name" in adql
        assert "SELECT dataproduct_type, facility_name, COUNT(*) AS num_records" in adql

    def test_filters_applied(self, dd):
        with patch.object(dd, "query", return_value=Table({"dataproduct_type": [], "num_records": []})) as mock_query:
            dd.count_by(filters=SearchFilters().add_filter("collection", "JCMT"))
        adql = mock_query.call_args[0][0]
        assert "WHERE UPPER(obs_collection) = UPPER('JCMT')" in adql

    def test_verbose_prints_adql(self, dd, capsys):
        with patch.object(dd, "query", return_value=Table({"dataproduct_type": [], "num_records": []})):
            dd.count_by(verbose=True)
        assert "[ADQL]" in capsys.readouterr().out


class TestExecuteAdql:
    """execute_adql() — Q5: run free-form ADQL, capped by max_rows (no OFFSET)."""

    def test_passes_adql_through_unchanged(self, dd):
        adql = "SELECT TOP 5 * FROM ivoa.ObsCore"
        with patch.object(dd, "query", return_value=Table({"obs_id": []})) as mock_query:
            dd.execute_adql(adql)
        assert mock_query.call_args[0][0] == adql

    def test_max_rows_passed_as_maxrec(self, dd):
        with patch.object(dd, "query", return_value=Table({"obs_id": []})) as mock_query:
            dd.execute_adql("SELECT * FROM ivoa.ObsCore", max_rows=25)
        assert mock_query.call_args[1]["maxrec"] == 25

    def test_verbose_prints_adql(self, dd, capsys):
        with patch.object(dd, "query", return_value=Table({"obs_id": []})):
            dd.execute_adql("SELECT * FROM ivoa.ObsCore", verbose=True)
        assert "[ADQL]" in capsys.readouterr().out


class TestCountAdql:
    """count_adql() — Q3: capped row count, by executing + measuring (no sub-select)."""

    def test_returns_one_row_with_num_records(self, dd):
        with patch.object(dd, "execute_adql", return_value=Table({"obs_id": ["a", "b", "c"]})):
            result = dd.count_adql("SELECT obs_id FROM ivoa.ObsCore")
        assert len(result) == 1
        assert result["num_records"][0] == 3

    def test_does_not_wrap_in_sub_select(self, dd):
        # Argus rejects sub-selects in FROM outright -- confirmed live.
        # count_adql must send the caller's ADQL to execute_adql unmodified,
        # never "SELECT COUNT(*) FROM (...)".
        adql = "SELECT obs_id FROM ivoa.ObsCore WHERE dataproduct_type = 'image'"
        with patch.object(dd, "execute_adql", return_value=Table({"obs_id": []})) as mock_execute:
            dd.count_adql(adql)
        assert mock_execute.call_args[0][0] == adql

    def test_max_rows_forwarded(self, dd):
        with patch.object(dd, "execute_adql", return_value=Table({"obs_id": []})) as mock_execute:
            dd.count_adql("SELECT obs_id FROM ivoa.ObsCore", max_rows=100)
        assert mock_execute.call_args[1]["max_rows"] == 100

    def test_zero_rows(self, dd):
        with patch.object(dd, "execute_adql", return_value=Table({"obs_id": []})):
            result = dd.count_adql("SELECT obs_id FROM ivoa.ObsCore")
        assert result["num_records"][0] == 0


class TestAddNamespaceFilenameColumns:
    """_add_namespace_filename_columns() — best-effort obs_id split."""

    def test_splits_on_first_colon(self):
        t = Table({"obs_id": ["testing:PTF10tce.fits"]})
        _add_namespace_filename_columns(t)
        assert t["namespace"][0] == "testing"
        assert t["filename"][0] == "PTF10tce.fits"

    def test_no_colon_falls_back_to_empty_namespace(self):
        t = Table({"obs_id": ["plainname.fits"]})
        _add_namespace_filename_columns(t)
        assert t["namespace"][0] == ""
        assert t["filename"][0] == "plainname.fits"

    def test_splits_on_first_colon_only(self):
        t = Table({"obs_id": ["ns:sub:file.fits"]})
        _add_namespace_filename_columns(t)
        assert t["namespace"][0] == "ns"
        assert t["filename"][0] == "sub:file.fits"

    def test_empty_table(self):
        t = Table({"obs_id": []})
        _add_namespace_filename_columns(t)  # must not raise
        assert "namespace" in t.colnames
        assert "filename" in t.colnames
