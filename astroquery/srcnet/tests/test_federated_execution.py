"""
Tests for astroquery.srcnet.federated_execution module.

Covers:
  - JobDefinition: to_broker_request() (engine params, container image, in_datasets ->
    rucio_dids, out_dataset/accounting_scope -> informational workflow_params,
    task_name/job_name -> request_id/job_id), from_dict() (old design-study shape and
    this class's own field names)
  - Proxied session/token/URL properties delegate to the parent SRCNetClass
  - submit(): returns just job_id; execute(): returns the full response — both share the
    same 402 -> CreditExceeded / other-HTTP-failure / non-JSON-402-body handling
  - check_status(): returns just the state string
  - get_job(): success, HTTP failure
  - get_result(): terminal-state gating (raises for a non-terminal state), builds its
    entries from the logs response, and always reports data_access.supported == False
    (no Rucio-backed stage-out yet -- the "Battle API" gap)
  - cancel_job(): success
  - SRCNetClass.get_federated_execution() factory method returns the same
    cached instance, wired with the environment's computing_broker URL
"""
import pytest
from unittest.mock import MagicMock, patch

from astroquery.srcnet.federated_execution import FederatedExecutionClass, JobDefinition
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
# JobDefinition
# ─────────────────────────────────────────────────────────────────────────────

def test_to_broker_request_minimal_fields():
    job = JobDefinition(job_name="my-job-0001")
    body = job.to_broker_request()
    assert body["job_id"] == "my-job-0001"
    assert body["workflow_type"] == "snakemake"
    assert body["workflow_type_version"] == "7"
    assert body["workflow_engine_parameters"] == {}
    assert "request_id" not in body
    assert "workflow_params" not in body


def test_to_broker_request_task_name_becomes_request_id():
    job = JobDefinition(task_name="wf-EB12345-ContImaging", job_name="ws-0001")
    body = job.to_broker_request()
    assert body["request_id"] == "wf-EB12345-ContImaging"
    assert body["job_id"] == "ws-0001"


def test_to_broker_request_container_image_folds_into_engine_params():
    job = JobDefinition(job_name="j1", container_image="registry.skao.int/ska-sdp-imaging:1.2.3")
    body = job.to_broker_request()
    assert body["workflow_engine_parameters"]["--image"] == "registry.skao.int/ska-sdp-imaging:1.2.3"


def test_to_broker_request_container_image_does_not_override_explicit_image_param():
    job = JobDefinition(
        job_name="j1",
        container_image="ignored:latest",
        job_parameters={"--image": "explicit:latest"},
    )
    body = job.to_broker_request()
    assert body["workflow_engine_parameters"]["--image"] == "explicit:latest"


def test_to_broker_request_string_job_parameters_become_cmd():
    job = JobDefinition(job_name="j1", job_parameters="--algorithm wsclean --niter 5000")
    body = job.to_broker_request()
    assert body["workflow_engine_parameters"]["--cmd"] == "--algorithm wsclean --niter 5000"


def test_to_broker_request_in_datasets_become_real_rucio_dids():
    # Real: the broker's own data-locality lookup (services/data_locality.py,
    # requested_data_dids) reads workflow_params["rucio_dids"] for site preselection.
    job = JobDefinition(
        job_name="j1",
        in_datasets=["user.j.salgado:EB12345_raw", "user.j.salgado:calibration_data"],
    )
    body = job.to_broker_request()
    assert body["workflow_params"]["rucio_dids"] == [
        "user.j.salgado:EB12345_raw", "user.j.salgado:calibration_data"
    ]


def test_to_broker_request_out_dataset_and_accounting_scope_are_informational_only():
    # Not real: no output-dataset registration and no per-job accounting-scope override
    # exist in the broker (see module docstring) — still carried through, not dropped.
    job = JobDefinition(job_name="j1", out_dataset="user.j.salgado:job123", accounting_scope="sv-demo")
    body = job.to_broker_request()
    assert body["workflow_params"]["out_dataset"] == "user.j.salgado:job123"
    assert body["workflow_params"]["accounting_scope"] == "sv-demo"


def test_to_broker_request_metadata_merged_into_workflow_params():
    job = JobDefinition(job_name="j1", metadata={"workflow_id": "wf-1", "observation_id": "EB12345"})
    body = job.to_broker_request()
    assert body["workflow_params"]["workflow_id"] == "wf-1"
    assert body["workflow_params"]["observation_id"] == "EB12345"


def test_to_broker_request_data_location_hints_passed_through_top_level():
    hints = {"ip_address": "1.2.3.4"}
    job = JobDefinition(job_name="j1", data_location_hints=hints)
    body = job.to_broker_request()
    assert body["data_location_hints"] == hints


def test_from_dict_old_design_study_shape():
    job = JobDefinition.from_dict({
        "jobDefinition": {
            "taskName": "wf-EB12345-ContImaging",
            "jobName": "ws-20260130-001",
            "container_name": "registry.skao.int/ska-sdp-imaging:1.2.3",
            "jobParameters": "--algorithm wsclean --niter 5000",
            "inDatasets": ["user.j.salgado:EB12345_raw", "user.j.salgado:calibration_data"],
            "outDataset": ["user.j.salgado:job123"],
            "metadata": {"workflow_id": "wf-EB12345-ContImaging", "observation_id": "EB12345"},
        }
    })
    assert job.task_name == "wf-EB12345-ContImaging"
    assert job.job_name == "ws-20260130-001"
    assert job.container_image == "registry.skao.int/ska-sdp-imaging:1.2.3"
    assert job.job_parameters == "--algorithm wsclean --niter 5000"
    assert job.in_datasets == ["user.j.salgado:EB12345_raw", "user.j.salgado:calibration_data"]
    assert job.out_dataset == "user.j.salgado:job123"  # unwrapped from the study's list shape
    assert job.metadata == {"workflow_id": "wf-EB12345-ContImaging", "observation_id": "EB12345"}


def test_from_dict_accepts_inner_object_directly():
    job = JobDefinition.from_dict({"jobName": "j1", "job_parameters": {"--cores": "2"}})
    assert job.job_name == "j1"
    assert job.job_parameters == {"--cores": "2"}


def test_from_dict_accepts_this_class_own_field_names():
    job = JobDefinition.from_dict({"job_name": "j1", "task_name": "t1", "container_image": "img:latest"})
    assert job.job_name == "j1"
    assert job.task_name == "t1"
    assert job.container_image == "img:latest"


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
# submit() -- same wire behaviour as execute(), but returns just job_id
# ─────────────────────────────────────────────────────────────────────────────

def test_submit_accepts_a_job_definition_and_returns_just_the_job_id(fe, parent):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "job_id": "my-job-0001", "state": "PENDING", "created": True,
        "candidate_sites": ["int-stfc-1"], "dispatch_attempts": 0,
    }
    parent.session.post.return_value = resp

    job_id = fe.submit(JobDefinition(job_name="my-job-0001", job_parameters={"--cores": "1"}))

    assert job_id == "my-job-0001"
    args, kwargs = parent.session.post.call_args
    assert kwargs["json"]["job_id"] == "my-job-0001"
    assert kwargs["json"]["workflow_engine_parameters"] == {"--cores": "1"}


def test_submit_accepts_a_plain_dict_too(fe, parent):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"job_id": "my-job-0001", "state": "PENDING"}
    parent.session.post.return_value = resp

    assert fe.submit(_job()) == "my-job-0001"


def test_submit_over_budget_raises_same_as_execute(fe, parent):
    resp = MagicMock()
    resp.status_code = 402
    resp.json.return_value = {"detail": "project 'sv-demo' is over its credit budget (250/200 credits)"}
    parent.session.post.return_value = resp

    with pytest.raises(Exception, match="over its credit budget"):
        fe.submit(_job())


# ─────────────────────────────────────────────────────────────────────────────
# check_status()
# ─────────────────────────────────────────────────────────────────────────────

def test_check_status_returns_just_the_state(fe, parent):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"job_id": "my-job-0001", "state": "RUNNING"}
    parent.session.get.return_value = resp

    assert fe.check_status("my-job-0001") == "RUNNING"


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
# get_result()
# ─────────────────────────────────────────────────────────────────────────────

def test_get_result_raises_for_a_non_terminal_state(fe, parent):
    status_resp = MagicMock()
    status_resp.raise_for_status.return_value = None
    status_resp.json.return_value = {"job_id": "my-job-0001", "state": "RUNNING"}
    parent.session.get.return_value = status_resp

    with pytest.raises(Exception, match="has not finished yet"):
        fe.get_result("my-job-0001")


def test_get_result_builds_entries_from_logs_once_complete(fe, parent):
    status_resp = MagicMock()
    status_resp.raise_for_status.return_value = None
    status_resp.json.return_value = {"job_id": "my-job-0001", "state": "COMPLETE"}

    logs_resp = MagicMock()
    logs_resp.raise_for_status.return_value = None
    logs_resp.json.return_value = {
        "job_id": "my-job-0001",
        "run_id": "run-1",
        "state": "COMPLETE",
        "source": "run_dir",
        "backend_details": {"output_path": "scratch://storm2.test/outputs"},
        "stdout": {"available": True, "path": "/run/stdout.log", "content": "all good"},
        "stderr": {"available": False, "path": None, "content": None},
    }
    parent.session.get.side_effect = [status_resp, logs_resp]

    result = fe.get_result("my-job-0001")

    assert result["job_id"] == "my-job-0001"
    assert result["state"] == "COMPLETE"
    assert result["output_path"] == "scratch://storm2.test/outputs"
    assert result["entries"] == [
        {"semantic": "#log", "filename": "stdout", "content": "all good", "path": "/run/stdout.log"},
    ]
    # unavailable stderr is omitted, not included as an empty entry
    assert len(result["entries"]) == 1
    # data_access always reports unsupported today -- no Rucio-backed stage-out exists
    # regardless of how the job itself turned out (pending the "Battle API" gap).
    assert result["data_access"]["supported"] is False
    assert "Battle API" in result["data_access"]["reason"]

    logs_call_args, _ = parent.session.get.call_args_list[1]
    assert logs_call_args[0] == "http://broker.test/v1/jobs/my-job-0001/logs"


def test_get_result_handles_missing_backend_details(fe, parent):
    status_resp = MagicMock()
    status_resp.raise_for_status.return_value = None
    status_resp.json.return_value = {"job_id": "my-job-0001", "state": "FAILED"}

    logs_resp = MagicMock()
    logs_resp.raise_for_status.return_value = None
    logs_resp.json.return_value = {
        "job_id": "my-job-0001", "run_id": None, "state": "FAILED", "source": "unavailable",
    }
    parent.session.get.side_effect = [status_resp, logs_resp]

    result = fe.get_result("my-job-0001")

    assert result["output_path"] is None
    assert result["entries"] == []
    assert result["data_access"]["supported"] is False


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
