"""
DataAccessClass data movement: move_data / stage_data submit jobs to the Data
Management API, get_job_status / wait_for_job follow them.
"""
from unittest.mock import MagicMock, patch

import pytest

from astroquery.srcnet.data_access import DataAccessClass

DM_API = "https://dm.example/api/v1"
AREA = "f26ff4e4-30af-4339-88ed-bb0cb7037ca0"
DID = "srcnet_test.comm:test_eb_001.test_prod_001/PTF10tce.fits"


@pytest.fixture
def da():
    # No tokens, so the auth decorators skip refresh/exchange.
    return DataAccessClass(MagicMock(access_token=None, refresh_token=None, srcnet_dm_api_base_address=DM_API))


def _json(data):
    resp = MagicMock(status_code=200)
    resp.json.return_value = data
    return resp


class TestSubmit:

    def test_move_data_posts_the_dids_and_returns_the_job_id(self, da):
        da.session.post.return_value = _json({"job_id": "job-1"})

        assert da.move_data([DID], to_storage_area=AREA, lifetime=86400) == "job-1"

        call = da.session.post.call_args
        assert call.args[0] == DM_API + "/data/move"
        assert call.kwargs["params"] == {"to_storage_area_uuid": AREA, "lifetime": 86400}
        assert call.kwargs["json"] == [{"namespace": "srcnet_test.comm", "name": "test_eb_001.test_prod_001/PTF10tce.fits"}]

    @pytest.mark.parametrize("dids", [
        DID,
        ("srcnet_test.comm", "test_eb_001.test_prod_001/PTF10tce.fits"),
        {"namespace": "srcnet_test.comm", "name": "test_eb_001.test_prod_001/PTF10tce.fits"},
        [("srcnet_test.comm", "test_eb_001.test_prod_001/PTF10tce.fits")],
    ], ids=["string", "pair", "dict", "list-of-pairs"])
    def test_every_did_form_gives_the_same_body(self, da, dids):
        da.session.post.return_value = _json({"job_id": "j"})
        da.move_data(dids, to_storage_area=AREA)
        assert da.session.post.call_args.kwargs["json"] == [
            {"namespace": "srcnet_test.comm", "name": "test_eb_001.test_prod_001/PTF10tce.fits"}]

    def test_optional_parameters_are_sent_only_when_given(self, da):
        da.session.post.return_value = _json({"job_id": "j"})

        da.move_data([DID], to_storage_area=AREA, parent_namespace="srcnet_test.comm")
        assert da.session.post.call_args.kwargs["params"]["parent_namespace"] == "srcnet_test.comm"

        da.stage_data([DID])
        call = da.session.post.call_args
        assert call.args[0] == DM_API + "/data/stage"
        assert call.kwargs["params"] == {"lifetime": 3600}

        da.stage_data([DID], to_storage_area=AREA, approach="local", lifetime=60)
        assert da.session.post.call_args.kwargs["params"] == {"lifetime": 60, "to_storage_area_uuid": AREA, "approach": "local"}

    @pytest.mark.parametrize(("call", "message"), [
        (lambda da: da.move_data([], to_storage_area=AREA), "at least one"),
        (lambda da: da.move_data(["no-namespace"], to_storage_area=AREA), "needs a namespace and a name"),
        (lambda da: da.move_data([42], to_storage_area=AREA), "a DID must be"),
        (lambda da: da.move_data([DID], to_storage_area=" "), "to_storage_area must not be empty"),
        (lambda da: da.move_data([DID], to_storage_area=AREA, lifetime=0), "lifetime must be a positive"),
        (lambda da: da.stage_data([DID], lifetime="3600"), "lifetime must be a positive"),
        (lambda da: da.get_job_status("job", kind="remove"), "kind must be one of move, stage"),
        (lambda da: da.get_job_status(""), "job_id must not be empty"),
    ])
    def test_invalid_input_is_refused_before_any_request(self, da, call, message):
        with pytest.raises(ValueError, match=message):
            call(da)
        da.session.post.assert_not_called()
        da.session.get.assert_not_called()


class TestFollow:

    def test_get_job_status_returns_what_the_api_reports(self, da):
        da.session.get.return_value = _json({"status": "STARTED", "info": "moving"})

        assert da.get_job_status("job-1", kind="stage") == {"status": "STARTED", "info": "moving"}
        assert da.session.get.call_args.args[0] == DM_API + "/data/stage/job-1"

    def test_wait_for_job_polls_until_the_job_is_done(self, da):
        da.session.get.side_effect = [
            _json({"status": "PENDING", "info": None}),
            _json({"status": "MOVING", "info": "1/1"}),
            _json({"state": "READY"}),
        ]
        with patch("astroquery.srcnet.data_access.time.sleep") as sleep:
            assert da.wait_for_job("job-1", poll_s=5) == {"state": "READY"}

        assert da.session.get.call_count == 3
        sleep.assert_called_with(5)

    def test_a_failed_job_is_returned_not_waited_on(self, da):
        da.session.get.return_value = _json({"status": "FAILED", "error_message": "no quota"})
        with patch("astroquery.srcnet.data_access.time.sleep") as sleep:
            assert da.wait_for_job("job-1", kind="stage")["error_message"] == "no quota"
        sleep.assert_not_called()

    def test_wait_for_job_times_out(self, da):
        da.session.get.return_value = _json({"status": "STARTED", "info": None})
        # deadline at 15: polls at 0 and 8, then the check at 16 is past it
        clock = iter([0, 0, 5, 8, 16])
        with patch("astroquery.srcnet.data_access.time.sleep"), \
                patch("astroquery.srcnet.data_access.time.monotonic", side_effect=lambda: next(clock)):
            with pytest.raises(TimeoutError, match="still running after 15 s"):
                da.wait_for_job("job-1", timeout_s=15, poll_s=3)

    def test_waiting_never_outlasts_the_limit(self, da):
        """Each status request's timeout and each sleep are bounded by the time left, however long poll_s is."""
        da.session.get.side_effect = [_json({"status": "STARTED"}), _json({"state": "READY"})]
        clock = iter([0, 0, 2, 2])
        with patch("astroquery.srcnet.data_access.time.sleep") as sleep, \
                patch("astroquery.srcnet.data_access.time.monotonic", side_effect=lambda: next(clock)):
            assert da.wait_for_job("job-1", timeout_s=5, poll_s=600) == {"state": "READY"}

        assert [c.kwargs["timeout"] for c in da.session.get.call_args_list] == [5, 3]
        sleep.assert_called_once_with(3)

    def test_get_job_status_has_a_network_timeout(self, da):
        da.session.get.return_value = _json({"status": "STARTED"})
        da.get_job_status("job-1")
        assert da.session.get.call_args.kwargs["timeout"] == 60
