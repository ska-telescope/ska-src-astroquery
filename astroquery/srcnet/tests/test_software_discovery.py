"""
Tests for astroquery.srcnet.software_discovery module.

Covers:
  - Helper functions: _esc, _detect_tables, _fix_adql, _extract_adql,
    _patch_redirect_session
  - SoftwareDiscoveryClass: all public methods (query, query_software,
    get_software, query_by_image, get_tables, get_columns, nl_to_adql,
    query_natural) with mocked TAP/HTTP backends
"""
import pytest
from unittest.mock import MagicMock, patch

import requests
from astropy.table import Table

from astroquery.srcnet.software_discovery import (
    SoftwareDiscoveryClass,
    _detect_tables,
    _esc,
    _extract_adql,
    _fix_adql,
    _mount_retries,
    _parse_tableset,
    _patch_redirect_session,
)


class TestTablesetAndRetries:
    """_parse_tableset() and _mount_retries() — robustness helpers."""

    def test_parse_tableset_extracts_name_and_description(self):
        xml = (b'<vosi:tableset xmlns:vosi="http://x"><schema>'
               b'<table><name>sdm.a</name><description>A</description></table>'
               b'<table><name>sdm.b</name></table></schema></vosi:tableset>')
        rows = _parse_tableset(xml)
        assert {r["name"] for r in rows} == {"sdm.a", "sdm.b"}
        assert dict((r["name"], r["description"]) for r in rows)["sdm.a"] == "A"

    def test_parse_tableset_bad_xml_returns_empty(self):
        assert _parse_tableset(b"not xml at all") == []

    def test_mount_retries_configures_connect_retries(self):
        s = requests.Session()
        _mount_retries(s)
        retries = s.get_adapter("https://example.org").max_retries
        assert retries.connect and retries.connect >= 1
        assert retries.total and retries.total >= 1


# ─────────────────────────────────────────────────────────────────────────────
# _esc — ADQL string-literal escaping
# ─────────────────────────────────────────────────────────────────────────────

class TestEsc:
    """Single quotes in values must be doubled to prevent ADQL injection."""

    def test_plain_string_unchanged(self):
        assert _esc("hello") == "hello"

    def test_single_quote_doubled(self):
        assert _esc("O'Brien") == "O''Brien"

    def test_multiple_quotes_all_doubled(self):
        assert _esc("it's a 'test'") == "it''s a ''test''"

    def test_empty_string(self):
        assert _esc("") == ""


# ─────────────────────────────────────────────────────────────────────────────
# _detect_tables — find schema.table references in text
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectTables:
    def test_finds_sdm_table(self):
        assert "sdm.software" in _detect_tables("query sdm.software table", {"sdm"})

    def test_ignores_unknown_schema(self):
        assert _detect_tables("query ivoa.ObsCore", {"sdm"}) == []

    def test_finds_multiple_tables(self):
        result = _detect_tables("FROM sdm.software JOIN sdm.artifact", {"sdm"})
        assert "sdm.software" in result
        assert "sdm.artifact" in result

    def test_empty_text_returns_empty_list(self):
        assert _detect_tables("", {"sdm"}) == []

    def test_case_insensitive_schema_match(self):
        # Schema names from the text are lowercased for comparison.
        result = _detect_tables("query SDM.software", {"sdm"})
        assert len(result) > 0


# ─────────────────────────────────────────────────────────────────────────────
# _fix_adql — SQL LIMIT → ADQL TOP
# ─────────────────────────────────────────────────────────────────────────────

class TestFixAdqlSd:
    def test_unchanged_without_limit(self):
        q = "SELECT * FROM sdm.software"
        assert _fix_adql(q) == q

    def test_limit_becomes_top(self):
        result = _fix_adql("SELECT * FROM sdm.software LIMIT 10")
        assert "TOP 10" in result
        assert "LIMIT" not in result

    def test_limit_with_semicolon_removed(self):
        result = _fix_adql("SELECT * FROM sdm.software LIMIT 5;")
        assert "LIMIT" not in result
        assert "TOP 5" in result


# ─────────────────────────────────────────────────────────────────────────────
# _extract_adql — pull SELECT from LLM response text
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractAdqlSd:
    def test_sql_fenced_block(self):
        text = "```sql\nSELECT * FROM sdm.software\n```"
        assert "sdm.software" in _extract_adql(text)

    def test_adql_fenced_block(self):
        text = "```adql\nSELECT TOP 10 * FROM sdm.software\n```"
        assert "TOP 10" in _extract_adql(text)

    def test_plain_select_line_in_text(self):
        text = "The answer is:\nSELECT * FROM sdm.software WHERE s.status = 'STABLE'"
        assert "sdm.software" in _extract_adql(text)

    def test_python_triple_quote_block(self):
        text = '```python\nsd.query("""\nSELECT * FROM sdm.software\n""")\n```'
        result = _extract_adql(text)
        assert "SELECT" in result.upper()

    def test_fallback_returns_text_unchanged(self):
        # When no SELECT is found, the raw text should be returned.
        result = _extract_adql("no query here")
        assert "no query" in result


# ─────────────────────────────────────────────────────────────────────────────
# _patch_redirect_session — fix localhost Location headers
# ─────────────────────────────────────────────────────────────────────────────

class TestPatchRedirectSession:
    def test_hook_registered(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.example.com/tap/")
        assert len(session.hooks["response"]) == 1

    def test_localhost_redirect_rewritten_to_public_host(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.example.com/tap/")
        fake_resp = MagicMock()
        fake_resp.headers = {"Location": "http://localhost:8080/tap/sync"}
        session.hooks["response"][0](fake_resp)
        assert "tap.example.com" in fake_resp.headers["Location"]
        assert "localhost" not in fake_resp.headers["Location"]

    def test_127_0_0_1_redirect_rewritten(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.example.com/tap/")
        fake_resp = MagicMock()
        fake_resp.headers = {"Location": "http://127.0.0.1:9090/sync"}
        session.hooks["response"][0](fake_resp)
        assert "tap.example.com" in fake_resp.headers["Location"]

    def test_external_redirect_not_rewritten(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.example.com/tap/")
        fake_resp = MagicMock()
        original = "https://other.host/tap/sync"
        fake_resp.headers = {"Location": original}
        session.hooks["response"][0](fake_resp)
        assert fake_resp.headers["Location"] == original

    def test_no_location_header_unchanged(self):
        session = requests.Session()
        _patch_redirect_session(session, "https://tap.example.com/tap/")
        fake_resp = MagicMock()
        fake_resp.headers = {}  # no Location header
        # Should not raise
        session.hooks["response"][0](fake_resp)


# ─────────────────────────────────────────────────────────────────────────────
# SoftwareDiscoveryClass — with mocked TAP service
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sd():
    """SoftwareDiscoveryClass with an injected mock TAP service."""
    obj = SoftwareDiscoveryClass(tap_url="http://mock-tap/")
    mock_tap = MagicMock()
    mock_result = MagicMock()
    mock_result.to_table.return_value = Table({"uri": ["ska:foo:1.0"], "status": ["STABLE"]})
    mock_tap.search.return_value = mock_result
    obj._tap = mock_tap
    return obj


class TestSoftwareDiscoveryQuery:
    """query() — execute raw ADQL and return astropy Table."""

    def test_returns_astropy_table(self, sd):
        result = sd.query("SELECT * FROM sdm.software WHERE status = 'STABLE'")
        assert isinstance(result, Table)
        assert len(result) == 1

    def test_passes_adql_to_tap_search(self, sd):
        adql = "SELECT * FROM sdm.software"
        sd.query(adql)
        sd._tap.search.assert_called_once()
        assert sd._tap.search.call_args[0][0] == adql


class TestQuerySoftware:
    """query_software() — keyword-filtered convenience query."""

    def test_no_filters_no_where_clause(self, sd):
        sd.query_software()
        adql = sd._tap.search.call_args[0][0]
        assert "WHERE" not in adql

    def test_status_filter_adds_where(self, sd):
        sd.query_software(status="STABLE")
        adql = sd._tap.search.call_args[0][0]
        assert "s.status = 'STABLE'" in adql

    def test_uri_filter_adds_where(self, sd):
        sd.query_software(uri="ska:wsclean:2.0")
        adql = sd._tap.search.call_args[0][0]
        assert "ska:wsclean:2.0" in adql

    def test_science_category_adds_join_and_where(self, sd):
        sd.query_software(science_category="radio-astronomy")
        adql = sd._tap.search.call_args[0][0]
        assert "dsc.category LIKE" in adql
        assert "radio-astronomy" in adql
        # 1:N join requires DISTINCT
        assert "DISTINCT" in adql

    def test_function_category_adds_join_and_where(self, sd):
        sd.query_software(function_category="imaging")
        adql = sd._tap.search.call_args[0][0]
        assert "dfc.category LIKE" in adql
        assert "imaging" in adql

    def test_science_working_group_adds_join(self, sd):
        sd.query_software(science_working_group="VLBI")
        adql = sd._tap.search.call_args[0][0]
        assert "dswg.working_group LIKE" in adql
        assert "VLBI" in adql

    def test_requires_gpu_true_adds_join(self, sd):
        sd.query_software(requires_gpu=True)
        adql = sd._tap.search.call_args[0][0]
        assert "r.requires_gpu" in adql
        assert "resource_requirements" in adql

    def test_requires_gpu_false(self, sd):
        sd.query_software(requires_gpu=False)
        adql = sd._tap.search.call_args[0][0]
        assert "r.requires_gpu" in adql

    def test_uri_value_escaped(self, sd):
        # Single-quote injection must be escaped.
        sd.query_software(uri="bad' OR '1'='1")
        adql = sd._tap.search.call_args[0][0]
        assert "bad'' OR ''1''=''1" in adql

    def test_multiple_filters_combined_with_and(self, sd):
        sd.query_software(status="STABLE", uri="ska:foo:1.0")
        adql = sd._tap.search.call_args[0][0]
        assert " AND " in adql


class TestGetSoftware:
    """get_software() — single-entry lookup by URI."""

    def test_delegates_to_query_software(self, sd):
        with patch.object(sd, "query_software", return_value=Table({"uri": []})) as mock_qs:
            sd.get_software("ska:foo:1.0")
        mock_qs.assert_called_once_with(uri="ska:foo:1.0", maxrec=1)


class TestGetSoftwareFull:
    """get_software_full() — full profile across all sub-tables."""

    @pytest.fixture
    def sd_full(self):
        """SoftwareDiscoveryClass whose query() returns distinct per-call Tables."""
        obj = SoftwareDiscoveryClass(tap_url="http://mock-tap/")
        return obj

    def _make_table(self, col: str, value: str) -> Table:
        return Table({col: [value]})

    def test_returns_dict_with_expected_keys(self, sd_full):
        with patch.object(sd_full, "query", return_value=Table({"uri": ["ska:foo:1.0"]})):
            result = sd_full.get_software_full("ska:foo:1.0")

        assert set(result) == {
            "software", "artifacts", "requirements",
            "science_categories", "function_categories", "working_groups",
        }

    def test_all_values_are_tables(self, sd_full):
        with patch.object(sd_full, "query", return_value=Table({"col": ["v"]})):
            result = sd_full.get_software_full("ska:foo:1.0")

        for key, val in result.items():
            assert isinstance(val, Table), f"{key} is not a Table"

    def test_runs_six_queries(self, sd_full):
        with patch.object(sd_full, "query", return_value=Table({"col": ["v"]})) as mock_q:
            sd_full.get_software_full("ska:foo:1.0")
        assert mock_q.call_count == 6

    def test_software_query_uses_uri_filter(self, sd_full):
        calls = []
        def capture(adql, **kwargs):
            calls.append(adql)
            return Table({"col": ["v"]})

        with patch.object(sd_full, "query", side_effect=capture):
            sd_full.get_software_full("ska:wsclean:2.0")

        assert any("ska:wsclean:2.0" in c and "sdm.software" in c for c in calls)

    def test_artifacts_query_joins_artifact_table(self, sd_full):
        calls = []
        def capture(adql, **kwargs):
            calls.append(adql)
            return Table({"col": ["v"]})

        with patch.object(sd_full, "query", side_effect=capture):
            sd_full.get_software_full("ska:foo:1.0")

        assert any("sdm.artifact" in c for c in calls)

    def test_uri_value_is_escaped(self, sd_full):
        calls = []
        def capture(adql, **kwargs):
            calls.append(adql)
            return Table({"col": ["v"]})

        with patch.object(sd_full, "query", side_effect=capture):
            sd_full.get_software_full("ska:bad'uri:1.0")

        # Single quote must be doubled in every query
        assert all("bad''uri" in c for c in calls)


class TestQueryByImage:
    """query_by_image() — artifact location substring search."""

    def test_location_like_in_adql(self, sd):
        sd.query_by_image("wsclean")
        adql = sd._tap.search.call_args[0][0]
        assert "a.location LIKE '%wsclean%'" in adql

    def test_selects_expected_columns(self, sd):
        sd.query_by_image("wsclean")
        adql = sd._tap.search.call_args[0][0]
        assert "s.uri" in adql
        assert "a.kind" in adql


class TestGetTables:
    """get_tables() — schema introspection via a single VOSI tableset request."""

    _TABLESET = (
        b'<vosi:tableset xmlns:vosi="http://www.ivoa.net/xml/VOSITables/v1.0">'
        b'<schema><name>sdm</name>'
        b'<table><name>sdm.software</name><description>Software entries</description></table>'
        b'</schema></vosi:tableset>'
    )

    def _resp(self, content):
        r = MagicMock()
        r.content = content
        r.raise_for_status.return_value = None
        return r

    def test_returns_astropy_table(self, sd):
        sd._tap._session.get.return_value = self._resp(self._TABLESET)
        result = sd.get_tables()
        assert isinstance(result, Table)
        assert "sdm.software" in list(result["name"])
        assert "Software entries" in list(result["description"])

    def test_empty_service_returns_empty_table(self, sd):
        sd._tap._session.get.return_value = self._resp(
            b'<vosi:tableset xmlns:vosi="http://www.ivoa.net/xml/VOSITables/v1.0"/>'
        )
        result = sd.get_tables()
        assert len(result) == 0


class TestGetColumns:
    """get_columns() — column introspection for a named table."""

    def _mock_col(self, name, dtype="char", ucd="", description=""):
        col = MagicMock()
        col.name = name
        col.datatype.content = dtype
        col.unit = None
        col.ucd = ucd
        col.description = description
        return col

    def test_default_table_is_sdm_software(self, sd):
        col = self._mock_col("uri")
        sd._tap.tables = {"sdm.software": MagicMock(columns=[col])}
        result = sd.get_columns()
        assert "uri" in list(result["name"])

    def test_explicit_table_name_used(self, sd):
        col = self._mock_col("kind")
        sd._tap.tables = {"sdm.artifact": MagicMock(columns=[col])}
        result = sd.get_columns("sdm.artifact")
        assert "kind" in list(result["name"])

    def test_returns_astropy_table(self, sd):
        col = self._mock_col("uri", dtype="char", description="Software URI")
        sd._tap.tables = {"sdm.software": MagicMock(columns=[col])}
        result = sd.get_columns("sdm.software")
        assert isinstance(result, Table)
        assert set(result.colnames) >= {"name", "datatype", "description"}

    def test_multiple_columns_returned(self, sd):
        cols = [self._mock_col(n) for n in ("uri", "status", "release_date")]
        sd._tap.tables = {"sdm.software": MagicMock(columns=cols)}
        result = sd.get_columns("sdm.software")
        assert len(result) == 3


class TestNlToAdql:
    """nl_to_adql() — natural-language to ADQL translation."""

    def test_chatserver_path_returns_adql_field(self, sd):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"adql": "SELECT * FROM sdm.software", "answer": ""}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_resp):
            result = sd.nl_to_adql("list all stable software", chatserver_url="http://cs/")

        assert result == "SELECT * FROM sdm.software"

    def test_chatserver_falls_back_to_answer_field(self, sd):
        # When 'adql' is absent, extract from 'answer'.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "adql": None,
            "answer": "SELECT TOP 10 * FROM sdm.software",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_resp):
            result = sd.nl_to_adql("anything", chatserver_url="http://cs/")

        assert "sdm.software" in result

    def test_connection_error_to_chatserver_raises_runtime_error(self, sd):
        with (
            patch(
                "requests.post",
                side_effect=requests.exceptions.ConnectionError("unreachable"),
            ),
            pytest.raises(RuntimeError, match="CHATSERVER"),
        ):
            sd.nl_to_adql("test", chatserver_url="http://cs/")


class TestQueryNatural:
    """query_natural() — NL translation then TAP execution."""

    def test_returns_adql_and_table(self, sd):
        adql = "SELECT * FROM sdm.software"
        with (
            patch.object(sd, "nl_to_adql", return_value=adql),
            patch.object(sd, "query", return_value=Table({"uri": ["ska:x"]})),
        ):
            returned_adql, table = sd.query_natural("show all")

        assert returned_adql == adql
        assert isinstance(table, Table)

    def test_verbose_prints_adql(self, sd, capsys):
        adql = "SELECT * FROM sdm.software"
        with (
            patch.object(sd, "nl_to_adql", return_value=adql),
            patch.object(sd, "query", return_value=Table({"uri": []})),
        ):
            sd.query_natural("show all", verbose=True)

        out = capsys.readouterr().out
        assert "[ADQL]" in out

    def test_non_verbose_no_print(self, sd, capsys):
        adql = "SELECT * FROM sdm.software"
        with (
            patch.object(sd, "nl_to_adql", return_value=adql),
            patch.object(sd, "query", return_value=Table({"uri": []})),
        ):
            sd.query_natural("show all", verbose=False)

        assert "[ADQL]" not in capsys.readouterr().out
