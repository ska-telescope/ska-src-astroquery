"""
Tests for page-number (OFFSET) pagination in astroquery.srcnet.data_discovery:
search(page=...), explain(page=...), execute_adql(page=..., page_size=...) and
the OffsetNotSupported translation for Argus releases that reject OFFSET.
"""
from unittest.mock import MagicMock, patch

import pytest
import pyvo
from astropy.table import Table

from astroquery.srcnet.data_discovery import DataDiscoveryClass, SearchFilters, _page_offset
from astroquery.srcnet.exceptions import OffsetNotSupported


@pytest.fixture
def dd():
    obj = DataDiscoveryClass(tap_url="http://mock-tap/argus/")
    obj._tap = MagicMock()
    return obj


def _rows(n, start=0):
    return Table({
        "obs_publisher_did": [f"did{i:03d}" for i in range(start, start + n)],
        "obs_id": [f"ns{i}:file{i}.fits" for i in range(start, start + n)],
    })


# The error the production Argus and the CADC mirror return for OFFSET (checked live).
_ARGUS_NO_OFFSET = pyvo.dal.DALQueryError("invalid ADQL keyword: LIMIT")


class TestPageOffset:

    @pytest.mark.parametrize("page, size, expected", [(None, 10, 0), (1, 10, 0), (2, 10, 10), (5, 25, 100)])
    def test_offset(self, page, size, expected):
        assert _page_offset(page, size, None) == expected

    @pytest.mark.parametrize("page", [0, -1, 1.5, "2", True])
    def test_rejects_bad_page(self, page):
        with pytest.raises(ValueError, match="page must be a positive integer"):
            _page_offset(page, 10, None)

    def test_rejects_page_with_after(self):
        with pytest.raises(ValueError, match="can't be combined"):
            _page_offset(2, 10, "did001")

    def test_rejects_page_without_page_size(self):
        with pytest.raises(ValueError, match="needs a page_size"):
            _page_offset(2, None, None)

    @pytest.mark.parametrize("size", [0, -5, True])
    def test_rejects_bad_page_size(self, size):
        with pytest.raises(ValueError, match="page_size must be a positive integer"):
            _page_offset(2, size, None)


class TestExplainPage:

    def test_page_appends_offset_after_order_by(self, dd):
        adql = dd.explain(page=3, page_size=20)
        assert adql.endswith("ORDER BY obs_publisher_did ASC OFFSET 40")
        assert "SELECT TOP 20 " in adql

    def test_page_one_has_no_offset(self, dd):
        # page 1 must run unchanged on services without OFFSET.
        assert "OFFSET" not in dd.explain(page=1, page_size=20)
        assert dd.explain(page=1, page_size=20) == dd.explain(page_size=20)

    def test_page_with_custom_order(self, dd):
        f = SearchFilters().set_order("t_min", "DESC")
        adql = dd.explain(f, page=2, page_size=10)
        assert adql.endswith("ORDER BY t_min DESC, obs_publisher_did ASC OFFSET 10")
        assert "WHERE" not in adql  # page-number paging adds no keyset condition

    def test_page_and_after_rejected(self, dd):
        with pytest.raises(ValueError):
            dd.explain(page=2, after="did001")

    def test_no_network_call(self, dd):
        dd.explain(page=4, page_size=5)
        dd._tap.search.assert_not_called()


class TestSearchPage:

    def test_fetches_page_size_plus_one_with_offset(self, dd):
        with patch.object(dd, "query", return_value=_rows(3)) as mock_query:
            dd.search(page=3, page_size=10)
        adql = mock_query.call_args[0][0]
        assert "SELECT TOP 11 " in adql
        assert adql.endswith(" OFFSET 20")
        assert mock_query.call_args[1]["maxrec"] == 11

    def test_matches_explain_apart_from_top(self, dd):
        f = SearchFilters().add_filter("collection", "sv")
        explained = dd.explain(f, page=2, page_size=10)
        with patch.object(dd, "query", return_value=_rows(3)) as mock_query:
            dd.search(f, page=2, page_size=10)
        assert mock_query.call_args[0][0] == explained.replace("TOP 10 ", "TOP 11 ")

    def test_full_page_sets_next_page_and_trims(self, dd):
        with patch.object(dd, "query", return_value=_rows(4, start=6)):
            result = dd.search(page=3, page_size=3)
        assert len(result) == 3
        assert result.meta["page"] == 3
        assert result.meta["next_page"] == 4
        # the keyset cursor stays available from a page-number page too
        assert result.meta["next_after"] == "did008"

    def test_last_page_has_no_next_page(self, dd):
        with patch.object(dd, "query", return_value=_rows(2)):
            result = dd.search(page=5, page_size=3)
        assert result.meta["next_page"] is None
        assert result.meta["next_after"] is None

    def test_page_past_the_end_is_empty(self, dd):
        with patch.object(dd, "query", return_value=_rows(0)):
            result = dd.search(page=99, page_size=3)
        assert len(result) == 0
        assert result.meta["next_page"] is None

    def test_without_page_no_page_meta(self, dd):
        with patch.object(dd, "query", return_value=_rows(2)):
            result = dd.search(page_size=3)
        assert "page" not in result.meta
        assert "next_page" not in result.meta

    def test_total_pages_with_count(self, dd):
        def fake_query(adql, maxrec=None):
            if adql.startswith("SELECT COUNT(*)"):
                return Table({"num_records": [448]})
            return _rows(3)
        with patch.object(dd, "query", side_effect=fake_query):
            result = dd.search(page=2, page_size=50, with_total_count=True)
        assert result.meta["total_count"] == 448
        assert result.meta["total_pages"] == 9

    def test_total_pages_exact_multiple(self, dd):
        def fake_query(adql, maxrec=None):
            if adql.startswith("SELECT COUNT(*)"):
                return Table({"num_records": [100]})
            return _rows(3)
        with patch.object(dd, "query", side_effect=fake_query):
            result = dd.search(page=1, page_size=50, with_total_count=True)
        assert result.meta["total_pages"] == 2

    def test_count_has_no_offset(self, dd):
        with patch.object(dd, "query", return_value=Table({"num_records": [5]})) as mock_query:
            dd.search(page=2, page_size=2, with_total_count=True)
        count_adql = mock_query.call_args_list[1][0][0]
        assert "OFFSET" not in count_adql

    def test_selected_columns_still_paged(self, dd):
        with patch.object(dd, "query", return_value=_rows(4)) as mock_query:
            result = dd.search(columns=["obs_id"], page=2, page_size=3)
        assert mock_query.call_args[0][0].endswith(" OFFSET 3")
        assert result.colnames == ["obs_id"]
        assert result.meta["next_page"] == 3

    def test_unbounded_with_page_rejected(self, dd):
        with pytest.raises(ValueError, match="needs a page_size"):
            dd.search(page=2, page_size=None)

    def test_page_and_after_rejected_before_querying(self, dd):
        with patch.object(dd, "query") as mock_query, pytest.raises(ValueError):
            dd.search(page=2, after="did001")
        mock_query.assert_not_called()

    def test_service_without_offset_raises_clear_error(self, dd):
        with patch.object(dd, "query", side_effect=_ARGUS_NO_OFFSET), \
                pytest.raises(OffsetNotSupported, match="next_after"):
            dd.search(page=2, page_size=10)

    def test_page_one_runs_on_service_without_offset(self, dd):
        # page=1 sends no OFFSET, so nothing to translate; a real error passes through.
        with patch.object(dd, "query", side_effect=_ARGUS_NO_OFFSET), \
                pytest.raises(pyvo.dal.DALQueryError):
            dd.search(page=1, page_size=10)

    def test_other_query_errors_pass_through(self, dd):
        err = pyvo.dal.DALQueryError("column not found: foo")
        with patch.object(dd, "query", side_effect=err), pytest.raises(pyvo.dal.DALQueryError):
            dd.search(page=2, page_size=10)


class TestExecuteAdqlPage:

    ADQL = "SELECT obs_id FROM ivoa.ObsCore ORDER BY obs_id"

    def test_unpaged_unchanged(self, dd):
        with patch.object(dd, "query", return_value=_rows(2)) as mock_query:
            dd.execute_adql(self.ADQL, max_rows=7)
        mock_query.assert_called_once_with(self.ADQL, maxrec=7)

    def test_page_appends_offset_and_fetches_one_extra(self, dd):
        with patch.object(dd, "query", return_value=_rows(3)) as mock_query:
            dd.execute_adql(self.ADQL, page=3, page_size=20)
        assert mock_query.call_args[0][0] == self.ADQL + "\nOFFSET 40"
        assert mock_query.call_args[1]["maxrec"] == 21

    def test_page_one_has_no_offset(self, dd):
        with patch.object(dd, "query", return_value=_rows(3)) as mock_query:
            dd.execute_adql(self.ADQL, page=1, page_size=20)
        assert mock_query.call_args[0][0] == self.ADQL

    def test_trailing_semicolon_and_whitespace_stripped(self, dd):
        with patch.object(dd, "query", return_value=_rows(1)) as mock_query:
            dd.execute_adql(self.ADQL + " ;\n", page=2, page_size=5)
        assert mock_query.call_args[0][0] == self.ADQL + "\nOFFSET 5"

    def test_next_page_and_trim(self, dd):
        with patch.object(dd, "query", return_value=_rows(6)):
            result = dd.execute_adql(self.ADQL, page=2, page_size=5)
        assert len(result) == 5
        assert result.meta["page"] == 2
        assert result.meta["next_page"] == 3

    def test_last_page(self, dd):
        with patch.object(dd, "query", return_value=_rows(2)):
            result = dd.execute_adql(self.ADQL, page=2, page_size=5)
        assert result.meta["next_page"] is None

    def test_offset_survives_a_trailing_comment(self, dd):
        # A trailing "-- comment" must not swallow the OFFSET (page 2 would silently be page 1).
        with patch.object(dd, "query", return_value=_rows(1)) as mock_query:
            dd.execute_adql(self.ADQL + " -- sorted\n", page=2, page_size=5)
        sent = mock_query.call_args[0][0]
        assert sent.splitlines()[-1] == "OFFSET 5"
        assert sent == self.ADQL + " -- sorted\nOFFSET 5"

    @pytest.mark.parametrize("adql", [
        "SELECT obs_id FROM ivoa.ObsCore WHERE target_name = 'offset field' ORDER BY obs_id",
        "SELECT obs_id FROM ivoa.ObsCore WHERE target_name = 'it''s OFFSET 3' ORDER BY obs_id",
        'SELECT "offset" FROM ivoa.ObsCore ORDER BY obs_id',
        "SELECT obs_id FROM ivoa.ObsCore ORDER BY obs_id -- no OFFSET here",
    ])
    def test_offset_in_quotes_or_comments_is_not_a_clause(self, dd, adql):
        with patch.object(dd, "query", return_value=_rows(1)) as mock_query:
            dd.execute_adql(adql, page=2, page_size=5)
        assert mock_query.call_args[0][0].endswith("\nOFFSET 5")

    def test_query_with_own_offset_rejected(self, dd):
        with pytest.raises(ValueError, match="already has an OFFSET"):
            dd.execute_adql(self.ADQL + " offset 10", page=2, page_size=5)

    def test_page_without_page_size_rejected(self, dd):
        with pytest.raises(ValueError, match="needs page_size"):
            dd.execute_adql(self.ADQL, page=2)

    def test_page_size_without_page_rejected(self, dd):
        with pytest.raises(ValueError, match="only used with page"):
            dd.execute_adql(self.ADQL, page_size=5)

    def test_page_with_max_rows_rejected(self, dd):
        with pytest.raises(ValueError, match="can't be combined"):
            dd.execute_adql(self.ADQL, page=2, page_size=5, max_rows=10)

    def test_service_without_offset_raises_clear_error(self, dd):
        with patch.object(dd, "query", side_effect=_ARGUS_NO_OFFSET), pytest.raises(OffsetNotSupported):
            dd.execute_adql(self.ADQL, page=2, page_size=5)

    def test_count_adql_unaffected(self, dd):
        with patch.object(dd, "query", return_value=_rows(4)) as mock_query:
            result = dd.count_adql(self.ADQL, max_rows=100)
        assert result["num_records"][0] == 4
        assert "OFFSET" not in mock_query.call_args[0][0]
