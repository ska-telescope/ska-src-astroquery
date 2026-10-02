import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from astroquery.srcnet.data_access import DataAccessClass

DM_API = "https://dm.example/api/v1"
LOCATE = DM_API + "/data/locate/srcnet_test.comm/eb_001.prod_001/PTF10tce.fits"
LOCATION = [{
    "identifier": "RSE_A",
    "replicas": ["https://storage.example/PTF10tce.fits"],
    "associated_storage_area_id": "area-1",
}]


def _response(status_code=200, json_data=None, chunks=()):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.iter_content.return_value = list(chunks)
    return resp


@pytest.fixture
def da():
    # No tokens, so the auth decorators skip refresh/exchange.
    srcnet = MagicMock(access_token=None, refresh_token=None,
                       srcnet_dm_api_base_address=DM_API)
    return DataAccessClass(srcnet)


def _get_data(da, session_responses, **kwargs):
    da.session.get.side_effect = session_responses
    with patch("requests.get", return_value=_response(chunks=[b"abc", b"def"])):
        return da.get_data("srcnet_test.comm", "eb_001.prod_001/PTF10tce.fits", **kwargs)


def test_get_data_defaults_to_random_and_omits_ip(da, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _get_data(da, [_response(json_data=LOCATION),
                   _response(json_data={"access_token": "storage-token"})])

    locate_call = da.session.get.call_args_list[0]
    assert locate_call.args == (LOCATE,)
    assert locate_call.kwargs["params"] == {"sort": "random"}


def test_get_data_writes_basename_of_a_name_with_slashes(da, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _get_data(da, [_response(json_data=LOCATION),
                          _response(json_data={"access_token": "storage-token"})])

    assert path == "PTF10tce.fits"
    assert (tmp_path / "PTF10tce.fits").read_bytes() == b"abcdef"


def test_get_data_output_file_creates_parent_directories(da, tmp_path):
    target = os.path.join(tmp_path, "downloads", "nested", "out.fits")
    path = _get_data(da, [_response(json_data=LOCATION),
                          _response(json_data={"access_token": "storage-token"})],
                     output_file=target)

    assert path == target
    with open(target, "rb") as f:
        assert f.read() == b"abcdef"


def test_get_data_nearest_by_ip_falls_back_to_random_on_server_error(da, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _get_data(da, [_response(status_code=500),
                   _response(json_data=LOCATION),
                   _response(json_data={"access_token": "storage-token"})],
              sort="nearest_by_ip", ip_address="192.0.2.1")

    first, retry = da.session.get.call_args_list[:2]
    assert first.kwargs["params"] == {"sort": "nearest_by_ip", "ip_address": "192.0.2.1"}
    assert retry.args == (LOCATE,)
    assert retry.kwargs["params"] == {"sort": "random"}


def test_get_data_does_not_retry_a_client_error(da, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    not_found = _response(status_code=404)
    not_found.text = '{"detail": "not found"}'
    not_found.raise_for_status.side_effect = requests.HTTPError("404", response=not_found)
    da.session.get.side_effect = [not_found]

    with pytest.raises(Exception, match="404"):
        da.get_data("srcnet_test.comm", "eb_001.prod_001/PTF10tce.fits", sort="nearest_by_ip")
    assert da.session.get.call_count == 1
