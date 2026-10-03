from unittest.mock import MagicMock

import pytest

from astroquery.srcnet.core import SRCNetClass
from astroquery.srcnet.data_access import DataAccessClass

DM_API = "https://dm.example/api/v1"
METADATA_URL = DM_API + "/metadata/srcnet_test.comm/eb_001.prod_001/PTF10tce.fits"


@pytest.fixture
def da():
    # No tokens, so the auth decorators skip refresh/exchange.
    srcnet = MagicMock(access_token=None, refresh_token=None,
                       srcnet_dm_api_base_address=DM_API)
    srcnet.session.get.return_value.json.return_value = {"bytes": 123}
    return DataAccessClass(srcnet)


def test_get_metadata_defaults_to_custom_metadata(da):
    assert da.get_metadata("srcnet_test.comm", "eb_001.prod_001/PTF10tce.fits") == {"bytes": 123}

    da.session.get.assert_called_once_with(METADATA_URL, params={"plugin": "POSTGRES_JSON"})


def test_get_metadata_did_column(da):
    da.get_metadata("srcnet_test.comm", "eb_001.prod_001/PTF10tce.fits", plugin="DID_COLUMN")

    da.session.get.assert_called_once_with(METADATA_URL, params={"plugin": "DID_COLUMN"})


def test_get_metadata_rejects_an_unknown_plugin(da):
    # The API would silently fall back to POSTGRES_JSON, so refuse it here.
    with pytest.raises(Exception, match="plugin must be one of POSTGRES_JSON, DID_COLUMN"):
        da.get_metadata("ns", "name", plugin="did_column")
    da.session.get.assert_not_called()


def test_srcnet_get_metadata_forwards_plugin():
    srcnet = SRCNetClass("dummy", "dummy")
    srcnet._data_access = MagicMock()

    srcnet.get_metadata("ns", "name", plugin="DID_COLUMN")

    srcnet._data_access.get_metadata.assert_called_once_with("ns", "name", plugin="DID_COLUMN")
