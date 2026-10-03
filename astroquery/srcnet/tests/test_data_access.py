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


def test_get_data_keeps_the_directories_of_a_name_with_slashes(da, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _get_data(da, [_response(json_data=LOCATION),
                          _response(json_data={"access_token": "storage-token"})])

    assert path == os.path.join("eb_001.prod_001", "PTF10tce.fits")
    assert (tmp_path / "eb_001.prod_001" / "PTF10tce.fits").read_bytes() == b"abcdef"


def test_get_data_names_sharing_a_filename_do_not_overwrite(da, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for eb, content in (("eb_001", b"first"), ("eb_002", b"second")):
        da.session.get.side_effect = [_response(json_data=LOCATION),
                                      _response(json_data={"access_token": "storage-token"})]
        with patch("requests.get", return_value=_response(chunks=[content])):
            da.get_data("ns", eb + "/image.fits")

    assert (tmp_path / "eb_001" / "image.fits").read_bytes() == b"first"
    assert (tmp_path / "eb_002" / "image.fits").read_bytes() == b"second"


@pytest.mark.parametrize("name", [
    "../escape.fits",
    "a/../../escape.fits",
    "/abs/escape.fits",
    # Normalising these would alias another identifier's path.
    "eb_001/sub/../image.fits",
    "eb_001/./image.fits",
    "eb_001//image.fits",
    "eb_001/",
])
def test_get_data_refuses_a_name_with_an_empty_dot_or_dotdot_part(da, tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="Cannot derive a local path"):
        da.get_data("ns", name)
    da.session.get.assert_not_called()


def test_get_data_refuses_a_path_through_a_symlink_outside(da, tmp_path, monkeypatch):
    workdir, outside = tmp_path / "work", tmp_path / "outside"
    workdir.mkdir()
    outside.mkdir()
    (workdir / "eb_001").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(workdir)

    with pytest.raises(Exception, match="outside the working directory"):
        da.get_data("ns", "eb_001/image.fits")
    da.session.get.assert_not_called()
    assert not (outside / "image.fits").exists()


def test_get_data_allows_a_symlink_that_stays_inside(da, tmp_path, monkeypatch):
    (tmp_path / "real").mkdir()
    (tmp_path / "eb_001").symlink_to(tmp_path / "real", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    da.session.get.side_effect = [_response(json_data=LOCATION),
                                  _response(json_data={"access_token": "storage-token"})]
    with patch("requests.get", return_value=_response(chunks=[b"x"])):
        da.get_data("ns", "eb_001/image.fits")

    assert (tmp_path / "real" / "image.fits").read_bytes() == b"x"


def test_get_data_output_file_allows_any_name(da, tmp_path):
    target = os.path.join(tmp_path, "out.fits")
    da.session.get.side_effect = [_response(json_data=LOCATION),
                                  _response(json_data={"access_token": "storage-token"})]
    with patch("requests.get", return_value=_response(chunks=[b"x"])):
        assert da.get_data("ns", "../escape.fits", output_file=target) == target


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


def test_get_data_fetches_a_davs_replica_over_https(da, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    location = [dict(LOCATION[0], replicas=["davs://storage.example:1094/area/PTF10tce.fits"])]
    da.session.get.side_effect = [_response(json_data=location),
                                  _response(json_data={"access_token": "storage-token"})]
    with patch("requests.get", return_value=_response(chunks=[b"abc"])) as storage_get:
        da.get_data("srcnet_test.comm", "eb_001.prod_001/PTF10tce.fits")

    assert storage_get.call_args.args == ("https://storage.example:1094/area/PTF10tce.fits",)
    assert storage_get.call_args.kwargs["headers"] == {"Authorization": "Bearer storage-token"}
