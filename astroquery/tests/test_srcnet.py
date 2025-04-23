import pytest
import os
import base64
import time
from unittest.mock import patch, MagicMock, mock_open, patch
from astroquery.srcnet.core import SRCNetClass, refresh_token_if_expired, NoAccessTokenFoundInResponse
from astropy.coordinates import SkyCoord
from astropy.coordinates.name_resolve import NameResolveError
from astroquery.srcnet.exceptions import NoAccessTokenFoundInResponse, UnsupportedOIDCFlow

@pytest.fixture
def mock_srcnet():
    srcnet = SRCNetClass("dummy", "dummy")
    srcnet.srcnet_ivoa_obscore_table_name = "mock_table"
    srcnet.srcnet_ivoa_obscore_ra_col_name = "ra"
    srcnet.srcnet_ivoa_obscore_dec_col_name = "dec"
    return srcnet

@pytest.mark.srcnet
def test_wrapper_with_access_and_refresh_tokens():
    srcnet = SRCNetClass("dummy", "dummy", access_token="qwerty", refresh_token="asdfgh")
    assert srcnet.access_token == "qwerty"
    assert srcnet.refresh_token == "asdfgh"

@pytest.mark.srcnet
def test_wrapper_with_refresh_token_if_expired():
    srcnet = SRCNetClass("dummy", "dummy", access_token="qwerty", refresh_token="asdfgh")
    expired_time = time.time() - 100  # expire token
    with patch.object(srcnet, "_decode_access_token", return_value={"exp": expired_time}), \
        patch.object(srcnet.session, "get") as mock_get, \
        patch.object(srcnet, "_persist_tokens") as mock_persist:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "access_token": "new_token",
            "refresh_token": "new_refresh"
        }
        @refresh_token_if_expired
        def dummy_method(self):
            return "called"
        result = dummy_method(srcnet)
        assert result == "called"
        assert srcnet.access_token == "new_token"
        assert srcnet.refresh_token == "new_refresh"
        mock_get.assert_called_once()
        mock_persist.assert_called_once()

@pytest.mark.srcnet
@patch.dict(os.environ, {"ACCESS_TOKEN": "env_access", "REFRESH_TOKEN": "env_refresh"})
def test_init_with_env_tokens():
    src = SRCNetClass("dummy", "dummy")
    assert src.access_token == "env_access"
    assert src.refresh_token == "env_refresh"

@pytest.mark.srcnet
def test_access_token_setter():
    srcnet = SRCNetClass("dummy", "dummy", access_token="initial_token")
    srcnet.access_token = "new_access_token"
    assert srcnet.access_token == "new_access_token"
    auth_header = srcnet.session.headers.get("Authorization")
    assert auth_header == "Bearer new_access_token"

@pytest.mark.srcnet
def test_access_token(mock_srcnet):
    mock_srcnet.access_token = "new_token"
    assert mock_srcnet.access_token == "new_token"
    assert mock_srcnet.session.headers["Authorization"] == "Bearer new_token"

@pytest.mark.srcnet
def test_refresh_token(mock_srcnet):
    mock_srcnet.refresh_token = "new_refresh"
    assert mock_srcnet.refresh_token == "new_refresh"

@pytest.mark.srcnet
def test_create_tap_query_sync(mock_srcnet):
    with patch("astroquery.srcnet.core.pyvo.dal.TAPQuery") as mock_tap:
        mock_srcnet._create_tap_query("SELECT obs_publisher_did, s_ra, s_dec", sync=True)
        mock_tap.assert_called_once_with(
            baseurl=mock_srcnet.srcnet_tap_service_url_base,
            query="SELECT obs_publisher_did, s_ra, s_dec",
            mode="sync",
            language="ADQL"
        )

@pytest.mark.srcnet
def test_create_tap_query_async(mock_srcnet):
    with patch("astroquery.srcnet.core.pyvo.dal.TAPQuery") as mock_tap:
        mock_srcnet._create_tap_query("SELECT obs_publisher_did, s_ra, s_dec", sync=False)
        mock_tap.assert_called_once_with(
            baseurl=mock_srcnet.srcnet_tap_service_url_base,
            query="SELECT obs_publisher_did, s_ra, s_dec",
            mode="async",
            language="ADQL"
        )

@pytest.mark.srcnet
def test_decode_access_token_custom():
    payload = '{"sub":"123456","preferred_username":"test","organisation_name":"SKA IAM Prototype"}'
    encoded_payload = base64.b64encode(payload.encode("utf-8")).decode("utf-8").rstrip("=")
    token = f"header.{encoded_payload}.signature"
    srcnet = SRCNetClass("dummy", "dummy", access_token=token)
    decoded = srcnet._decode_access_token()

    assert decoded["sub"] == "123456"
    assert decoded["preferred_username"] == "test"
    assert decoded["organisation_name"] == "SKA IAM Prototype"

@pytest.mark.srcnet
@patch("astroquery.srcnet.core.qrcode.QRCode")
@patch("astroquery.srcnet.core.time.sleep", return_value=None)
@patch("astroquery.srcnet.core.requests.Session.get")
def test_login_via_device_success(mock_get, mock_sleep, mock_qrcode):
    srcnet = SRCNetClass("dummy", "dummy")

    device_response = MagicMock()
    device_response.json.return_value = {
        "verification_uri_complete": "http://test.com/complete",
        "verification_uri": "http://test.com",
        "user_code": "qwerty",
        "device_code": "123456"
    }
    device_response.raise_for_status.return_value = None

    token_response = MagicMock()
    token_response.json.return_value = {"token": "mock_token"}
    token_response.raise_for_status.return_value = None

    mock_get.side_effect = [device_response, token_response]

    result = srcnet._login_via_device()
    assert result == {"token": "mock_token"}

@pytest.mark.srcnet
def test_persist_tokens():
    srcnet = SRCNetClass("dummy", "dummy", access_token="qwerty", refresh_token="asdfgh")
    with patch("builtins.open", mock_open()) as mock_file:
        srcnet._persist_tokens()
        mock_file.assert_any_call("/tmp/access_token", "w")
        mock_file().write.assert_any_call("qwerty")
        mock_file.assert_any_call("/tmp/refresh_token", "w")
        mock_file().write.assert_any_call("asdfgh")

@pytest.mark.srcnet
def test_update_authorisation_session():
    srcnet = SRCNetClass("dummy", "dummy", access_token="qwerty")
    srcnet._update_authorisation_requests_session()
    assert srcnet.session.headers["Authorization"] == "Bearer qwerty"

@pytest.mark.srcnet
def test_get_data_success(mock_srcnet):
    mock_service_token_response = MagicMock()
    mock_service_token_response.status_code = 200
    mock_service_token_response.json.return_value = {"access_token": "mock-access-token"}

    mock_locate_response = MagicMock()
    mock_locate_response.status_code = 200
    mock_locate_response.json.return_value = [{
        "identifier": "mock-rse",
        "replicas": ["https://mock-url.com/file.fits"],
        "associated_storage_area_id": "123456"
    }]

    mock_token_response = MagicMock()
    mock_token_response.status_code = 200
    mock_token_response.json.return_value = {
        "access_token": "mock-access-token"
    }

    mock_download_response = MagicMock()
    mock_download_response.status_code = 200
    mock_download_response.iter_content.return_value = [b"datachunk1", b"datachunk2"]

    with patch.object(mock_srcnet.session, "get", side_effect=[
        mock_service_token_response,
        mock_locate_response,     
        mock_token_response 
    ]), \
    patch("astroquery.srcnet.core.requests.get", return_value=mock_download_response), \
    patch("astroquery.srcnet.core.random.choice", return_value="https://mock-url.com/file.fits"), \
    patch("astroquery.srcnet.core.open", mock_open(), create=True), \
    patch("astroquery.srcnet.core.os.path.getsize", return_value=1024):

        mock_srcnet.srcnet_dm_api_base_address = "https://mock-dm-api.com"
        mock_srcnet.get_data(namespace="testing", name="file.fits")

        assert mock_download_response.iter_content.called


@pytest.mark.srcnet
def test_login_success_sets_tokens(mock_srcnet):
    fake_response = {
        "token": {
            "access_token": "mock_access",
            "refresh_token": "mock_refresh"
        }
    }

    with patch.object(mock_srcnet, "_login_via_device", return_value=fake_response), \
         patch.object(mock_srcnet, "_persist_tokens") as mock_persist:
        mock_srcnet.login()

    print("NOTE: CRITICAL log output above is expected in the three tests below.")

    assert mock_srcnet.access_token == "mock_access"
    assert mock_srcnet.refresh_token == "mock_refresh"
    mock_persist.assert_called_once()

@pytest.mark.srcnet
def test_login_missing_access_token_raises(mock_srcnet):
    incomplete_response = {"token": {"refresh_token": "mock_refresh"}}

    with patch.object(mock_srcnet, "_login_via_device", return_value=incomplete_response):
        with pytest.raises(Exception, match="No access token found in response."):
            mock_srcnet.login()

@pytest.mark.srcnet
def test_login_with_invalid_flow_raises(mock_srcnet):
    with pytest.raises(Exception, match="The invalid_flow flow is not supported"):
        mock_srcnet.login(requested_oidc_flow="invalid_flow")

@pytest.mark.srcnet
def test_query_tap_sync():
    srcnet = SRCNetClass("dummy", "dummy")
    mock_query = MagicMock()
    mock_result = MagicMock()
    mock_query.execute.return_value = mock_result

    with patch.object(srcnet, "_create_tap_query", return_value=mock_query) as mock_create:
        result = srcnet.query_tap("SELECT * FROM mock_table", sync=True)

    mock_create.assert_called_once_with(query="SELECT * FROM mock_table", sync=True)
    mock_query.execute.assert_called_once()
    mock_query.submit.assert_not_called()
    assert result == mock_result

@pytest.mark.srcnet
def test_query_tap_async():
    srcnet = SRCNetClass("dummy", "dummy")
    mock_query = MagicMock()
    mock_result = MagicMock()
    mock_query.submit.return_value = mock_result

    with patch.object(srcnet, "_create_tap_query", return_value=mock_query) as mock_create:
        result = srcnet.query_tap("SELECT * FROM mock_table", sync=False)

    mock_create.assert_called_once_with(query="SELECT * FROM mock_table", sync=False)
    mock_query.submit.assert_called_once()
    mock_query.execute.assert_not_called()
    assert result == mock_result

@pytest.mark.srcnet
def test_query_object(mock_srcnet):
    fake_coords = MagicMock()
    mock_result = MagicMock()

    with patch("astroquery.srcnet.core.SkyCoord.from_name", return_value=fake_coords) as mock_from_name, \
         patch.object(mock_srcnet, "query_region", return_value=mock_result) as mock_query_region:

        result = mock_srcnet.query_object(
            object_name="HH211",
            radius=1.0,
            columns=["ra", "dec"],
            row_limit=50,
            sync=False
        )
    mock_from_name.assert_called_once_with("HH211", frame="icrs")
    mock_query_region.assert_called_once_with(
        coordinates=fake_coords,
        radius=1.0,
        width=None,
        height=None,
        columns=["ra", "dec"],
        row_limit=50,
        sync=False
    )
    assert result == mock_result

@pytest.mark.srcnet
def test_query_object_name_unresolved(mock_srcnet):
    with patch("astroquery.srcnet.core.SkyCoord.from_name", side_effect=NameResolveError("Object not found")):
        with pytest.raises(Exception) as excinfo:
            mock_srcnet.query_object("nonexistent_object", radius=0.1)
        assert "Object not found" in str(excinfo.value)

@pytest.mark.srcnet
def test_query_region_with_radius(mock_srcnet):
    fake_coords = MagicMock()
    mock_result = MagicMock()
    fake_coords.ra.deg = 123.4
    fake_coords.dec.deg = -56.7

    with patch("astroquery.srcnet.core.commons.parse_coordinates", return_value=fake_coords), \
         patch.object(mock_srcnet, "query_tap", return_value=mock_result) as mock_qtap:

        result = mock_srcnet.query_region(
            "mock_coords",
            radius=0.1,
            columns=['ra', 'dec', 'flux'],
            sync=False
        )

    query = mock_qtap.call_args.kwargs["query"]
    sync_flag = mock_qtap.call_args.kwargs["sync"]

    radius_in_arcmin = 0.1 * 60

    assert "ra,dec,flux" in query
    assert "POINT('ICRS', 123.4, -56.7)" in query
    assert f"CIRCLE('ICRS', 123.4, -56.7, {radius_in_arcmin})" in query
    assert sync_flag is False
    assert result == mock_result

@pytest.mark.srcnet
def test_query_region_with_width_and_height(mock_srcnet):
    fake_coords = MagicMock()
    fake_coords.ra.deg = 432.1
    fake_coords.dec.deg = 76.5
    mock_result = MagicMock()

    with patch("astroquery.srcnet.core.commons.parse_coordinates", return_value=fake_coords), \
         patch.object(mock_srcnet, "query_tap", return_value=mock_result) as mock_qtap:

        result = mock_srcnet.query_region(
            "mock_coords",
            width=0.2,
            height=0.3,
            columns=['ra', 'dec', 'flux'],
            row_limit=50,
            sync=True
        )

    query = mock_qtap.call_args.kwargs["query"]
    sync_flag = mock_qtap.call_args.kwargs["sync"]

    width_arcmin = 0.2 * 60
    height_arcmin = 0.3 * 60

    assert "ra,dec,flux" in query
    assert "POINT('ICRS', 432.1, 76.5)" in query
    assert f"BOX('ICRS', 432.1, 76.5, {width_arcmin}, {height_arcmin})" in query
    assert sync_flag is True
    assert result == mock_result

@pytest.fixture
def mock_srcnet_soda(tmp_path):
    srcnet = SRCNetClass("dummy", "dummy")
    srcnet.srcnet_datalink_service_url = "https://mock-datalink.com"

    output_dir = tmp_path
    output_file = output_dir / "test_output.fits"

    mock_datalink = MagicMock()
    mock_resource = MagicMock()
    mock_resource.type = "meta"
    mock_resource.ID = "soda-sync"

    mock_param = MagicMock()
    mock_param.name = "accessURL"
    mock_param.value = "https://mock-soda.com/sync"

    mock_group_entry = MagicMock()
    mock_group_entry.name = "ID"
    mock_group_entry.value = "mock_dataset_id"

    mock_group = MagicMock(entries=[mock_group_entry])
    mock_resource.params = [mock_param]
    mock_resource.groups = [mock_group]

    mock_datalink.votable.resources = [mock_resource]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = [b"fits", b"data"]
    mock_response.raise_for_status.return_value = None

    with patch("astroquery.srcnet.core.DatalinkResults.from_result_url", return_value=mock_datalink), \
         patch.object(srcnet.session, "get", return_value=mock_response) as mock_get:

        yield srcnet, tmp_path, mock_get

@pytest.mark.srcnet
def test_cutout_with_circle(mock_srcnet_soda):
    srcnet, tmp_path, mock_get = mock_srcnet_soda
    output_file = tmp_path / "circle.fits"

    srcnet.soda_cutout(
        namespace="testing",
        name="testfile.fits",
        output_file=str(output_file),
        circle=(351.9, 8.7, 0.1)
    )

    assert output_file.exists()
    assert output_file.read_bytes() == b"fitsdata"
    assert mock_get.call_args.kwargs["params"]["POS"] == "CIRCLE 351.9 8.7 0.1"

@pytest.mark.srcnet
def test_cutout_with_range(mock_srcnet_soda):
    srcnet, tmp_path, mock_get = mock_srcnet_soda
    output_file = tmp_path / "circle.fits"

    srcnet.soda_cutout(
        namespace="testing",
        name="testfile.fits",    
        output_file=str(output_file),
        range_=(351.974, 351.998, 8.768, 8.791)
    )

    assert output_file.exists()
    assert output_file.read_bytes() == b"fitsdata"
    assert mock_get.call_args.kwargs["params"]["POS"] == "RANGE 351.974 351.998 8.768 8.791"

@pytest.mark.srcnet
def test_cutout_with_polygon(mock_srcnet_soda):
    srcnet, tmp_path, mock_get = mock_srcnet_soda
    output_file = tmp_path / "polygon.fits"

    srcnet.soda_cutout(
        namespace="testing",
        name="testfile.fits",
        output_file=str(output_file),
        polygon=[(351.986, 8.778), (351.989, 8.779), (351.987, 8.776)]
    )

    assert output_file.exists()
    assert output_file.read_bytes() == b"fitsdata"
    assert mock_get.call_args.kwargs["params"]["POS"] == "POLYGON 351.986 8.778 351.989 8.779 351.987 8.776"

@pytest.mark.srcnet
def test_cutout_with_band(mock_srcnet_soda):
    srcnet, tmp_path, mock_get = mock_srcnet_soda
    output_file = tmp_path / "band.fits"

    srcnet.soda_cutout(
        namespace="testing",
        name="testfile.fits",
        output_file=str(output_file),
        circle=(351.9, 8.7, 0.1),
        band="400 700"
    )

    assert output_file.exists()
    assert output_file.read_bytes() == b"fitsdata"
    assert mock_get.call_args.kwargs["params"]["BAND"] == "400 700"

@pytest.mark.srcnet
def test_cutout_with_time(mock_srcnet_soda):
    srcnet, tmp_path, mock_get = mock_srcnet_soda
    output_file = tmp_path / "time.fits"

    srcnet.soda_cutout(
        namespace="testing",
        name="testfile.fits",
        output_file=str(output_file),
        circle=(351.9, 8.7, 0.1),
        time="59000.0 59001.0"
    )

    assert output_file.exists()
    assert output_file.read_bytes() == b"fitsdata"
    assert mock_get.call_args.kwargs["params"]["TIME"] == "59000.0 59001.0"

@pytest.mark.srcnet
def test_cutout_with_pol(mock_srcnet_soda):
    srcnet, tmp_path, mock_get = mock_srcnet_soda
    output_file = tmp_path / "pol.fits"

    srcnet.soda_cutout(
        namespace="testing",
        name="testfile.fits",
        output_file=str(output_file),
        circle=(351.9, 8.7, 0.1),
        pol="I"
    )

    assert output_file.exists()
    assert output_file.read_bytes() == b"fitsdata"
    assert mock_get.call_args.kwargs["params"]["POL"] == "I"

@pytest.mark.srcnet
def test_cutout_pol_without_spatial(mock_srcnet_soda):
    """
    This tests using 'POL' (or BAND or TIME) without spatial cutout information. 
    The WARNING and CRITICAL logs are expected and indicates correct behaviour.
    """
    srcnet, tmp_path, _ = mock_srcnet_soda
    output_file = tmp_path / "no_spatial_info_with_pol.fits"

    with pytest.raises(Exception, match="A positional cutout is also required when using 'POL'"):
        srcnet.soda_cutout(
            namespace="testing",
            name="testfile.fits",
            output_file=str(output_file),
            pol="I"
        )

@pytest.mark.srcnet
def test_cutout_range_with_invalid_range(mock_srcnet_soda, caplog):
    """
    This tests that an invalid RANGE (3 instead of 4 elements) raises an exception.
    The CRITICAL log is expected and indicates correct behaviour.
    """
    srcnet, tmp_path, _ = mock_srcnet_soda
    output_file = tmp_path / "invalid_range.fits"

    with caplog.at_level("ERROR"):
        with pytest.raises(Exception, match="Error: RANGE parameters must be in the form"):
            srcnet.soda_cutout(
                namespace="testing",
                name="testfile.fits",
                output_file=str(output_file),
                range_=(351.974, 351.998, 8.768)
            )

