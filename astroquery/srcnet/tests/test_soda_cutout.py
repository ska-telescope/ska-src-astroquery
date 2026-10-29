"""
DataAccessClass.soda_cutout: the DataLink request, its errors, choosing the
synchronous SODA service, argument validation, and the cutout download.
"""
import os
from unittest.mock import MagicMock

import pytest
import requests

from astroquery.srcnet.data_access import DataAccessClass

DATALINK = "https://datalink.example/api/v1/links"
SODA_SYNC = "https://gatekeeper.example/soda/sync"
DATASET_ID = "ivo://skao.int?srcnet_test.comm/eb.prod/PTF10tce.fits"


def _service(resource_id, access_url):
    return (
        f'<RESOURCE type="meta" ID="{resource_id}" utype="adhoc:service">'
        f'<PARAM name="accessURL" datatype="char" arraysize="*" value="{access_url}"/>'
        '<GROUP name="inputParams">'
        f'<PARAM name="ID" datatype="char" arraysize="*" value="{DATASET_ID}"/>'
        '</GROUP></RESOURCE>'
    )


def _datalink(*services):
    """A DataLink response with one #this row and the given service descriptors, in order."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3"><RESOURCE type="results"><TABLE>'
        '<FIELD name="ID" datatype="char" arraysize="*"/><FIELD name="access_url" datatype="char" arraysize="*"/>'
        '<FIELD name="service_def" datatype="char" arraysize="*"/><FIELD name="error_message" datatype="char" arraysize="*"/>'
        '<FIELD name="semantics" datatype="char" arraysize="*"/>'
        f'<DATA><TABLEDATA><TR><TD>{DATASET_ID}</TD><TD>https://x/f.fits</TD><TD></TD><TD></TD><TD>#this</TD></TR>'
        '</TABLEDATA></DATA></TABLE></RESOURCE>' + "".join(services) + '</VOTABLE>'
    ).encode()


def _ok(content=b"", chunks=()):
    resp = MagicMock(status_code=200, content=content)
    resp.iter_content.return_value = list(chunks)
    return resp


@pytest.fixture
def da():
    return DataAccessClass(MagicMock(srcnet_datalink_service_url=DATALINK))


def test_asks_datalink_only_for_sites_with_synchronous_soda(da, tmp_path):
    """The DM API requires every listed service, so asking for soda_async too would exclude most sites."""
    da.session.get.side_effect = [_ok(_datalink(_service("soda-sync", SODA_SYNC))), _ok(chunks=[b"FITS"])]

    da.soda_cutout("ns", "f.fits", str(tmp_path / "cut.fits"), circle=(1, 2, 0.1))

    datalink_url = da.session.get.call_args_list[0].args[0]
    assert datalink_url.startswith(DATALINK + "?")
    assert "colocated_services=soda_sync&" in datalink_url
    assert "sort=random" in datalink_url


def test_the_cutout_is_requested_from_soda_sync_and_saved(da, tmp_path):
    da.session.get.side_effect = [_ok(_datalink(_service("soda-sync", SODA_SYNC))), _ok(chunks=[b"SIMPLE", b"=T"])]
    output = tmp_path / "out" / "cut.fits"

    assert da.soda_cutout("ns", "f.fits", str(output), circle=(1, 2, 0.1), band="0.1 0.2") == str(output)

    soda_call = da.session.get.call_args_list[1]
    assert soda_call.args[0] == SODA_SYNC
    assert soda_call.kwargs["params"] == {
        "ID": DATASET_ID, "RESPONSEFORMAT": "application/fits", "POS": "CIRCLE 1 2 0.1", "BAND": "0.1 0.2",
    }
    assert output.read_bytes() == b"SIMPLE=T"


def test_soda_sync_is_chosen_whatever_comes_first(da, tmp_path):
    """Other service descriptors (product streamer, async SODA) may precede soda-sync."""
    da.session.get.side_effect = [
        _ok(_datalink(_service("product-streamer", "https://streamer.example"), _service("soda-async", "https://async.example"),
                      _service("soda-sync", SODA_SYNC))),
        _ok(chunks=[b"x"]),
    ]

    da.soda_cutout("ns", "f.fits", str(tmp_path / "cut.fits"), circle=(1, 2, 0.1))

    assert da.session.get.call_args_list[1].args[0] == SODA_SYNC


def test_no_soda_sync_service_is_a_clear_error(da, tmp_path):
    da.session.get.return_value = _ok(_datalink(_service("soda-async", "https://async.example")))

    with pytest.raises(Exception, match="No synchronous SODA service"):
        da.soda_cutout("ns", "f.fits", str(tmp_path / "cut.fits"), circle=(1, 2, 0.1))


def test_a_datalink_error_reports_its_detail(da, tmp_path):
    """DataLink answers errors in JSON; they used to surface as an XML parse error."""
    detail = '{"detail":"No replicas with requested and enabled colocated services found for this DID."}'
    failed = MagicMock(status_code=404, text=detail)
    failed.raise_for_status.side_effect = requests.HTTPError("404 Client Error", response=failed)
    da.session.get.return_value = failed

    with pytest.raises(Exception, match="No replicas with requested and enabled colocated services"):
        da.soda_cutout("ns", "f.fits", str(tmp_path / "cut.fits"), circle=(1, 2, 0.1))
    assert not os.path.exists(tmp_path / "cut.fits")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"circle": (1, 2)}, "circle must be"),
        ({"range_": (1, 2, 3)}, "range_ must be"),
        ({"band": "0.1 0.2"}, "positional parameter is required when using 'BAND'"),
    ],
)
def test_invalid_arguments_are_refused_before_any_request(da, tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        da.soda_cutout("ns", "f.fits", str(tmp_path / "cut.fits"), **kwargs)
    da.session.get.assert_not_called()
