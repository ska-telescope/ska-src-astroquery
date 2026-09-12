"""
Tests for astroquery.srcnet.federated_execution module.

Covers:
  - Proxied session/token/URL properties delegate to the parent SRCNetClass
  - execute(): success, 402 -> CreditExceeded (wrapped into plain Exception by
    handle_exceptions), other HTTP failures, non-JSON 402 body
  - get_job(): success, HTTP failure
  - cancel_job(): success
  - SRCNetClass.get_federated_execution() factory method returns the same
    cached instance, wired with the environment's computing_broker URL
"""
import pytest
from unittest.mock import MagicMock, patch

from astroquery.srcnet.federated_execution import FederatedExecutionClass
from astroquery.srcnet.core import SRCNetClass


class _FakeParent:
    """Minimal stand-in for SRCNetClass -- just what FederatedExecutionClass proxies."""

    def __init__(self, access_token=None, computing_broker_url="http://broker.test"):
        self.session = MagicMock()
        self._access_token = access_token
        self._refresh_token = None
        self.srcnet_computing_broker_url = computing_broker_url

    @property
    def access_token(self):
        return self._access_token

    @access_token.setter
    def access_token(self, value):
        self._access_token = value

    @property
    def refresh_token(self):
        return self._refresh_token

    @refresh_token.setter
    def refresh_token(self, value):
        self._refresh_token = value

    def _decode_access_token(self):
        return {}

    def _persist_tokens(self):
        pass


@pytest.fixture
def parent():
    return _FakeParent(access_token="qwerty")


@pytest.fixture
def fe(parent):
    return FederatedExecutionClass(parent)


# ─────────────────────────────────────────────────────────────────────────────
# Proxies
# ─────────────────────────────────────────────────────────────────────────────

def test_session_proxy(fe, parent):
    assert fe.session is parent.session


def test_access_token_proxy_get_and_set(fe, parent):
    assert fe.access_token == "qwerty"
    fe.access_token = "new_token"
    assert parent.access_token == "new_token"


def test_computing_broker_url_proxy(fe, parent):
    assert fe.srcnet_computing_broker_url == parent.srcnet_computing_broker_url


def test_headers_include_bearer_token(fe):
    headers = fe._headers()
    assert headers["Authorization"] == "Bearer qwerty"


def test_headers_omit_authorization_when_no_token(parent):
    parent.access_token = None
    fe = FederatedExecutionClass(parent)
    headers = fe._headers()
    assert "Authorization" not in headers


# ─────────────────────────────────────────────────────────────────────────────
# execute()
# ─────────────────────────────────────────────────────────────────────────────

def _job():
    return {
        "workflow_type": "snakemake",
        "workflow_type_version": "7",
        "workflow_engine_parameters": {"--cores": "1"},
        "job_id": "my-job-0001",
    }


def test_execute_success_returns_broker_response(fe, parent):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "job_id": "my-job-0001",
        "state": "PENDING",
        "created": True,
        "candidate_sites": ["int-stfc-1"],
        "dispatch_attempts": 0,
    }
    resp.raise_for_status.return_value = None
    parent.session.post.return_value = resp

    result = fe.execute(_job())

    assert result["state"] == "PENDING"
    parent.session.post.assert_called_once()
    args, kwargs = parent.session.post.call_args
    assert args[0] == "http://broker.test/v1/jobs"
    assert kwargs["json"] == _job()
    assert kwargs["headers"]["Authorization"] == "Bearer qwerty"


def test_execute_over_budget_raises_with_broker_detail(fe, parent):
    resp = MagicMock()
    resp.status_code = 402
    resp.json.return_value = {
        "detail": "project 'Survey' is over its credit budget (154/120 credits)"
    }
    parent.session.post.return_value = resp

    with pytest.raises(Exception, match="over its credit budget"):
        fe.execute(_job())

    # A 402 must not fall through to raise_for_status's generic HTTPError path.
    resp.raise_for_status.assert_not_called()


def test_execute_over_budget_with_non_json_body_falls_back_to_text(fe, parent):
    resp = MagicMock()
    resp.status_code = 402
    resp.json.side_effect = ValueError("not json")
    resp.text = "payment required"
    parent.session.post.return_value = resp

    with pytest.raises(Exception, match="payment required"):
        fe.execute(_job())


def test_execute_other_http_failure_propagates(fe, parent):
    import requests

    resp = MagicMock()
    resp.status_code = 400
    resp.text = "malformed payload"
    http_error = requests.exceptions.HTTPError("bad request")
    http_error.response = resp
    resp.raise_for_status.side_effect = http_error
    parent.session.post.return_value = resp

    with pytest.raises(Exception, match="malformed payload"):
        fe.execute(_job())


# ─────────────────────────────────────────────────────────────────────────────
# get_job() / cancel_job()
# ─────────────────────────────────────────────────────────────────────────────

def test_get_job_success(fe, parent):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"job_id": "my-job-0001", "state": "RUNNING"}
    parent.session.get.return_value = resp

    status = fe.get_job("my-job-0001")

    assert status["state"] == "RUNNING"
    args, kwargs = parent.session.get.call_args
    assert args[0] == "http://broker.test/v1/jobs/my-job-0001"


def test_cancel_job_success(fe, parent):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"job_id": "my-job-0001", "state": "CANCELED"}
    parent.session.post.return_value = resp

    result = fe.cancel_job("my-job-0001")

    assert result["state"] == "CANCELED"
    args, kwargs = parent.session.post.call_args
    assert args[0] == "http://broker.test/v1/jobs/my-job-0001/cancel"


# ─────────────────────────────────────────────────────────────────────────────
# SRCNetClass.get_federated_execution() factory method
# ─────────────────────────────────────────────────────────────────────────────

def test_factory_method_returns_federated_execution_class():
    srcnet = SRCNetClass("dummy", "dummy")
    fe = srcnet.get_federated_execution()
    assert isinstance(fe, FederatedExecutionClass)


def test_factory_method_returns_cached_instance():
    srcnet = SRCNetClass("dummy", "dummy")
    assert srcnet.get_federated_execution() is srcnet.get_federated_execution()


def test_factory_method_wired_to_environment_broker_url():
    srcnet = SRCNetClass("dummy", "dummy", environment="local")
    fe = srcnet.get_federated_execution()
    assert fe.srcnet_computing_broker_url == "http://localhost:8083"
