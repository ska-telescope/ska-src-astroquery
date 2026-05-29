import pytest
import os
import base64
import time
from unittest.mock import patch, MagicMock, mock_open
from astroquery.srcnet.core import SRCNetClass, refresh_token_if_expired

@pytest.fixture
def mock_srcnet():
    srcnet = SRCNetClass("dummy", "dummy")
    return srcnet

def test_wrapper_with_access_and_refresh_tokens():
    srcnet = SRCNetClass("dummy", "dummy", access_token="qwerty", refresh_token="asdfgh")
    assert srcnet.access_token == "qwerty"
    assert srcnet.refresh_token == "asdfgh"

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

@patch.dict(os.environ, {"ACCESS_TOKEN": "env_access", "REFRESH_TOKEN": "env_refresh"})
def test_init_with_env_tokens():
    src = SRCNetClass("dummy", "dummy")
    assert src.access_token == "env_access"
    assert src.refresh_token == "env_refresh"

def test_access_token_setter():
    srcnet = SRCNetClass("dummy", "dummy", access_token="initial_token")
    srcnet.access_token = "new_access_token"
    assert srcnet.access_token == "new_access_token"
    auth_header = srcnet.session.headers.get("Authorization")
    assert auth_header == "Bearer new_access_token"

def test_access_token(mock_srcnet):
    mock_srcnet.access_token = "new_token"
    assert mock_srcnet.access_token == "new_token"
    assert mock_srcnet.session.headers["Authorization"] == "Bearer new_token"

def test_refresh_token(mock_srcnet):
    mock_srcnet.refresh_token = "new_refresh"
    assert mock_srcnet.refresh_token == "new_refresh"

def test_decode_access_token_custom():
    payload = '{"sub":"123456","preferred_username":"test","organisation_name":"SKA IAM Prototype"}'
    encoded_payload = base64.b64encode(payload.encode("utf-8")).decode("utf-8").rstrip("=")
    token = f"header.{encoded_payload}.signature"
    srcnet = SRCNetClass("dummy", "dummy", access_token=token)
    decoded = srcnet._decode_access_token()

    assert decoded["sub"] == "123456"
    assert decoded["preferred_username"] == "test"
    assert decoded["organisation_name"] == "SKA IAM Prototype"

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

def test_persist_tokens():
    srcnet = SRCNetClass("dummy", "dummy", access_token="qwerty", refresh_token="asdfgh")
    with patch("builtins.open", mock_open()) as mock_file:
        srcnet._persist_tokens()
        mock_file.assert_any_call("/tmp/access_token", "w")
        mock_file().write.assert_any_call("qwerty")
        mock_file.assert_any_call("/tmp/refresh_token", "w")
        mock_file().write.assert_any_call("asdfgh")

def test_update_authorisation_session():
    srcnet = SRCNetClass("dummy", "dummy", access_token="qwerty")
    srcnet._update_authorisation_requests_session()
    assert srcnet.session.headers["Authorization"] == "Bearer qwerty"

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

def test_login_missing_access_token_raises(mock_srcnet):
    incomplete_response = {"token": {"refresh_token": "mock_refresh"}}

    with patch.object(mock_srcnet, "_login_via_device", return_value=incomplete_response):
        with pytest.raises(Exception, match="No access token found in response."):
            mock_srcnet.login()

def test_login_with_invalid_flow_raises(mock_srcnet):
    with pytest.raises(Exception, match="The invalid_flow flow is not supported"):
        mock_srcnet.login(requested_oidc_flow="invalid_flow")


