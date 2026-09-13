"""
FederatedExecutionClass — submit and track jobs on the SRCNet federated
compute pool, via the computing broker.

This module provides :class:`JobDefinition` (a job description) and
:class:`FederatedExecutionClass` (the recommended interface for running jobs
on the federated pool). Obtain a client via the factory method::

    from astroquery.srcnet import SRCNet
    SRCNet.login()                          # OIDC device flow -- the equivalent of an
                                             # old SRCNet design study's JobFactory.init(oidc_token=...):
                                             # this package exchanges/holds that token on SRCNet itself,
                                             # not on a separate factory object.
    fe = SRCNet.get_federated_execution()

    job = JobDefinition(
        task_name="wf-EB12345-ContImaging",
        job_name="ws-20260130-001",
        container_image="registry.skao.int/ska-sdp-imaging:1.2.3",
        job_parameters="--algorithm wsclean --niter 5000",
        in_datasets=["user.j.salgado:EB12345_raw", "user.j.salgado:calibration_data"],
        metadata={"workflow_id": "wf-EB12345-ContImaging", "observation_id": "EB12345"},
    )
    job_id = fe.submit(job)
    status = fe.check_status(job_id)        # -> "PENDING" / "RUNNING" / "COMPLETE" / ...
    result = fe.get_result(job_id)           # -> logs + output location once COMPLETE

**Where this sits relative to an older, unimplemented SRCNet design study** (a PanDA/Rucio
Global Execution API with ``JobFactory.init/submit/check_status/get_result`` and a
DataLink-shaped result document): this module targets the *real, existing* computing broker
(``ska-src-ef-computing-broker``), which runs a single Toil -> HTCondor -> pilot-pools path,
not PanDA/Harvester, and has no Rucio-backed output-dataset registration. The method names
and :class:`JobDefinition`'s fields intentionally mirror that study's shape so job
descriptions and calling code translate directly, but three things it assumed are only
partially or not yet real here -- called out explicitly, not silently faked:

- ``in_datasets`` **is real**: it maps onto the broker's own ``workflow_params["rucio_dids"]``,
  which real DMAPI-backed site preselection reads today (see
  ``services/data_locality.py``'s ``requested_data_dids``) -- data-aware placement, the same
  intent as the study's input datasets, just not phrased as "datasets" server-side.
- ``out_dataset`` and ``accounting_scope`` are **informational only**: carried through in
  ``workflow_params`` so they travel with the job (and are visible to anyone inspecting it
  server-side), but the broker does not register an output dataset anywhere, and the credit
  gate resolves the paying project from the bearer token's own group claims, never from the
  job body -- see ``services/credit_gate.py``. Setting ``accounting_scope`` here does not
  change who gets billed.
- :meth:`FederatedExecutionClass.get_result` returns the job's real, raw
  ``backend_details.output_path`` (wherever the run was configured to write its output) and
  its captured stdout/stderr -- not a per-file, DataLink-style listing with resolvable
  download URLs. There is no dataset-resolution step in the broker to build that from yet.
"""
from astroquery.srcnet.exceptions import (
    handle_exceptions,
    CreditExceeded,
)

__all__ = ["FederatedExecution", "FederatedExecutionClass", "JobDefinition"]

#: Broker job states that mean "still running" vs. terminal -- mirrors the broker's own
#: client SDK (``ska_src_ef_broker_client.client``), kept in sync manually since this
#: package doesn't depend on that client library.
_TERMINAL_STATES = frozenset({"COMPLETE", "FAILED", "CANCELED", "EXECUTOR_ERROR", "SYSTEM_ERROR"})


class JobDefinition:
    """A federated compute job description.

    Field names follow an earlier SRCNet Global Execution API design study (a PanDA/Rucio
    architecture SRCNet does not have) so job descriptions written against that study
    translate directly; :meth:`to_broker_request` renders the *real* computing broker's
    ``JobSubmitRequest`` body. See this module's own docstring for exactly which fields are
    fully real today (``in_datasets``) versus informational-only (``out_dataset``,
    ``accounting_scope``).

    Parameters
    ----------
    task_name : str, optional
        A label for the overall workflow/task this job belongs to. Several job runs can
        share one task. Rendered as the broker's ``request_id``.
    job_name : str, optional
        This specific run's identifier. Rendered as the broker's ``job_id``. At least one
        of ``task_name`` / ``job_name`` is required by the broker (its own validation).
    container_image : str, optional
        OCI image reference to run, e.g. ``"docker://harbor.test/wsclean:3.4"``. Folded into
        ``job_parameters["--image"]`` if not already set there.
    workflow_type : str
        The broker's workflow engine identifier, e.g. ``"snakemake"``. Required by the
        broker; defaults to ``"snakemake"``.
    workflow_type_version : str
        Version string for ``workflow_type``, e.g. ``"7"``. Required by the broker.
    job_parameters : dict or str, optional
        Extra ``workflow_engine_parameters`` for the run, e.g. ``{"--cores": "4"}``. A plain
        string (as in the old study's ``"--algorithm wsclean --niter 5000"``) is accepted
        for convenience and stored as ``{"--cmd": job_parameters}``.
    in_datasets : list of str, optional
        Input dataset identifiers, ``"scope:name"`` (matching this package's own Data Access
        namespace:name convention, and the broker's Rucio DID convention). **Real**: sent as
        ``workflow_params["rucio_dids"]``, which the broker's site preselection uses for
        data-aware placement.
    out_dataset : str, optional
        Output dataset identifier. **Informational only** — see module docstring; recorded
        in ``workflow_params["out_dataset"]`` but not registered or resolved by the broker.
    accounting_scope : str, optional
        The project/group this job's usage is intended to bill to. **Informational only** —
        see module docstring; the broker always resolves the paying project from the
        submitter's own bearer token, never from the job body.
    metadata : dict, optional
        Free-form metadata (e.g. ``workflow_id``, ``software_id``, ``observation_id``),
        merged into ``workflow_params``.
    data_location_hints : dict, optional
        The broker's own real request-local DMAPI replica-lookup hints (``ip_address``,
        ``colocated_services``) — sent as the broker's top-level ``data_location_hints``.

    Examples
    --------
    >>> job = JobDefinition(
    ...     task_name="wf-EB12345-ContImaging",
    ...     job_name="ws-20260130-001",
    ...     container_image="registry.skao.int/ska-sdp-imaging:1.2.3",
    ...     job_parameters="--algorithm wsclean --niter 5000",
    ...     in_datasets=["user.j.salgado:EB12345_raw"],
    ...     metadata={"observation_id": "EB12345"},
    ... )
    >>> job.to_broker_request()["workflow_engine_parameters"]["--image"]
    'registry.skao.int/ska-sdp-imaging:1.2.3'
    """

    def __init__(
        self,
        task_name=None,
        job_name=None,
        container_image=None,
        workflow_type="snakemake",
        workflow_type_version="7",
        job_parameters=None,
        in_datasets=None,
        out_dataset=None,
        accounting_scope=None,
        metadata=None,
        data_location_hints=None,
    ):
        self.task_name = task_name
        self.job_name = job_name
        self.container_image = container_image
        self.workflow_type = workflow_type
        self.workflow_type_version = workflow_type_version
        self.job_parameters = job_parameters
        self.in_datasets = list(in_datasets) if in_datasets else []
        self.out_dataset = out_dataset
        self.accounting_scope = accounting_scope
        self.metadata = dict(metadata) if metadata else {}
        self.data_location_hints = data_location_hints

    def to_broker_request(self):
        """Render this job as the real broker's ``JobSubmitRequest`` body (a plain dict)."""
        if isinstance(self.job_parameters, dict):
            engine_params = dict(self.job_parameters)
        elif self.job_parameters:
            engine_params = {"--cmd": str(self.job_parameters)}
        else:
            engine_params = {}
        if self.container_image:
            engine_params.setdefault("--image", self.container_image)

        workflow_params = dict(self.metadata)
        if self.in_datasets:
            # The broker's real data-locality lookup (services/data_locality.py,
            # requested_data_dids) reads exactly this key.
            workflow_params["rucio_dids"] = list(self.in_datasets)
        if self.out_dataset:
            workflow_params["out_dataset"] = self.out_dataset
        if self.accounting_scope:
            workflow_params["accounting_scope"] = self.accounting_scope

        body = {
            "workflow_type": self.workflow_type,
            "workflow_type_version": self.workflow_type_version,
            "workflow_engine_parameters": engine_params,
        }
        if workflow_params:
            body["workflow_params"] = workflow_params
        if self.task_name:
            body["request_id"] = self.task_name
        if self.job_name:
            body["job_id"] = self.job_name
        if self.data_location_hints:
            body["data_location_hints"] = self.data_location_hints
        return body

    @classmethod
    def from_dict(cls, data):
        """Build a :class:`JobDefinition` from a plain dict shaped like the old design
        study's ``jobDefinition`` (``taskName``, ``jobName``, ``container_name``,
        ``jobParameters``, ``inDatasets``, ``outDataset``, ``metadata``) -- for migrating
        job descriptions written against that study. Also accepts this class's own,
        already-Pythonic field names.

        Parameters
        ----------
        data : dict
            Either ``{"jobDefinition": {...}}`` or the inner object directly.

        Examples
        --------
        >>> job = JobDefinition.from_dict({
        ...     "jobDefinition": {
        ...         "taskName": "wf-EB12345-ContImaging",
        ...         "jobName": "ws-20260130-001",
        ...         "container_name": "registry.skao.int/ska-sdp-imaging:1.2.3",
        ...         "jobParameters": "--algorithm wsclean --niter 5000",
        ...         "inDatasets": ["user.j.salgado:EB12345_raw"],
        ...         "outDataset": ["user.j.salgado:job123"],
        ...         "metadata": {"observation_id": "EB12345"},
        ...     }
        ... })
        """
        jd = data.get("jobDefinition", data)
        out_dataset = jd.get("outDataset", jd.get("out_dataset"))
        if isinstance(out_dataset, (list, tuple)):
            out_dataset = out_dataset[0] if out_dataset else None
        return cls(
            task_name=jd.get("taskName", jd.get("task_name")),
            job_name=jd.get("jobName", jd.get("job_name")),
            container_image=jd.get("container_name", jd.get("container_image")),
            workflow_type=jd.get("workflow_type", "snakemake"),
            workflow_type_version=jd.get("workflow_type_version", "7"),
            job_parameters=jd.get("jobParameters", jd.get("job_parameters")),
            in_datasets=jd.get("inDatasets", jd.get("in_datasets")),
            out_dataset=out_dataset,
            accounting_scope=jd.get("accounting_scope"),
            metadata=jd.get("metadata"),
            data_location_hints=jd.get("data_location_hints"),
        )


class FederatedExecutionClass:
    """Client for the SRCNet computing broker (job submission and tracking).

    Do not instantiate directly. Use :meth:`SRCNetClass.get_federated_execution`::

        from astroquery.srcnet import SRCNet
        fe = SRCNet.get_federated_execution()

    All methods require authentication. Call :meth:`SRCNetClass.login` first
    if you have not already done so::

        SRCNet.login()
        fe = SRCNet.get_federated_execution()

    Parameters
    ----------
    srcnet_client : SRCNetClass
        The authenticated parent client. Token, session, and the broker's
        base URL are all read from this object.
    """

    def __init__(self, srcnet_client):
        self._srcnet = srcnet_client

    # ── Token/session proxies (required by the auth decorators) ───────────────

    @property
    def session(self):
        return self._srcnet.session

    @property
    def access_token(self):
        return self._srcnet.access_token

    @access_token.setter
    def access_token(self, value):
        self._srcnet.access_token = value

    @property
    def refresh_token(self):
        return self._srcnet.refresh_token

    @refresh_token.setter
    def refresh_token(self, value):
        self._srcnet.refresh_token = value

    @property
    def srcnet_computing_broker_url(self):
        return self._srcnet.srcnet_computing_broker_url

    def _decode_access_token(self):
        return self._srcnet._decode_access_token()

    def _persist_tokens(self):
        return self._srcnet._persist_tokens()

    def _headers(self):
        headers = {"Content-Type": "application/json", "accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = "Bearer {}".format(self.access_token)
        return headers

    # ── Public API ─────────────────────────────────────────────────────────────

    @handle_exceptions
    def submit(self, job, timeout=30):
        """Submit a job to the SRCNet federated compute pool.

        Parameters
        ----------
        job : JobDefinition or dict
            A :class:`JobDefinition`, or a plain dict already shaped like the broker's
            ``JobSubmitRequest`` body (``workflow_type``, ``workflow_type_version``,
            ``workflow_engine_parameters``, and one of ``job_id`` / ``request_id`` at
            minimum).
        timeout : float
            Request timeout in seconds (default 30).

        Returns
        -------
        str
            The broker's assigned ``job_id`` — pass this to :meth:`check_status` and
            :meth:`get_result`.

        Raises
        ------
        Exception
            If the owning project has no remaining credit budget, the broker responds
            ``402`` and this raises with a message identifying the credit-gate rejection
            (see :class:`~astroquery.srcnet.exceptions.CreditExceeded`). Any other HTTP or
            connection failure raises too — inspect ``str(e)`` to tell them apart.

        Examples
        --------
        >>> fe = SRCNet.get_federated_execution()
        >>> job = JobDefinition(job_name="my-job-0001", job_parameters={"--cores": "1"})
        >>> job_id = fe.submit(job)
        >>> fe.check_status(job_id)
        'PENDING'
        """
        response = self._submit(job, timeout=timeout)
        return response["job_id"]

    def _submit(self, job, timeout=30):
        body = job.to_broker_request() if isinstance(job, JobDefinition) else job
        url = "{api}/v1/jobs".format(api=self.srcnet_computing_broker_url)
        resp = self.session.post(url, json=body, headers=self._headers(), timeout=timeout)
        if resp.status_code == 402:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise CreditExceeded(detail)
        resp.raise_for_status()
        return resp.json()

    @handle_exceptions
    def execute(self, job, timeout=30):
        """Submit a job and return the broker's full response (not just ``job_id``).

        Equivalent to :meth:`submit`, kept for callers that want the broker's complete
        ``JobSubmitResponse`` body (``job_id``, ``state``, ``created``, ``candidate_sites``,
        ``dispatch_attempts``) in one call rather than a follow-up :meth:`check_status`.
        """
        return self._submit(job, timeout=timeout)

    @handle_exceptions
    def check_status(self, job_id, timeout=20):
        """Return this job's current state, e.g. ``"PENDING"``, ``"RUNNING"``,
        ``"COMPLETE"``, ``"FAILED"``.

        Parameters
        ----------
        job_id : str
            The broker job id, as returned by :meth:`submit`.
        timeout : float
            Request timeout in seconds (default 20).

        Examples
        --------
        >>> fe.check_status(job_id)
        'RUNNING'
        """
        return self.get_job(job_id, timeout=timeout)["state"]

    @handle_exceptions
    def get_job(self, job_id, timeout=20):
        """Fetch the full status record for a previously submitted job.

        Parameters
        ----------
        job_id : str
            The broker job id, as returned by :meth:`submit`.
        timeout : float
            Request timeout in seconds (default 20).

        Returns
        -------
        dict
            The broker's ``JobStatusResponse`` body — ``state``, ``status_reason``,
            ``last_error``, ``backend_details``, ``progress``, and more; see
            :meth:`check_status` for just the state, and :meth:`get_result` for the
            terminal-state output/logs view.
        """
        url = "{api}/v1/jobs/{job_id}".format(
            api=self.srcnet_computing_broker_url, job_id=job_id
        )
        resp = self.session.get(url, headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    @handle_exceptions
    def get_result(self, job_id, timeout=20):
        """Return what the broker can tell you about a job's output.

        **Not** a resolved, per-file listing with download URLs — the broker has no
        dataset-registration step to build that from yet (see this module's own
        docstring). This returns the job's real raw output location and its captured
        stdout/stderr, in a DataLink-*shaped* document so callers already written against
        that shape only need to adapt to weaker guarantees, not a different structure:

        .. code-block:: python

            {
                "job_id": "...",
                "state": "COMPLETE",
                "output_path": "scratch://storm2.test/outputs",  # or None
                "entries": [
                    {"semantic": "#log", "filename": "stdout", "content": "..."},
                    {"semantic": "#log", "filename": "stderr", "content": "..."},
                ],
            }

        Parameters
        ----------
        job_id : str
            The broker job id, as returned by :meth:`submit`.
        timeout : float
            Request timeout in seconds (default 20).

        Raises
        ------
        Exception
            If the job has not reached a terminal state yet (see
            :data:`_TERMINAL_STATES`) — call :meth:`check_status` first and only fetch a
            result once it is ``"COMPLETE"`` (or inspect the error for any other terminal
            state, e.g. ``"FAILED"``).
        """
        status = self.get_job(job_id, timeout=timeout)
        state = status["state"]
        if state not in _TERMINAL_STATES:
            raise RuntimeError(
                "job {} has not finished yet (state={}) — call check_status() until it "
                "reaches a terminal state before requesting a result".format(job_id, state)
            )

        url = "{api}/v1/jobs/{job_id}/logs".format(
            api=self.srcnet_computing_broker_url, job_id=job_id
        )
        resp = self.session.get(url, headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        logs = resp.json()

        backend_details = logs.get("backend_details") or {}
        entries = []
        for stream_name in ("stdout", "stderr"):
            stream = logs.get(stream_name)
            if stream and stream.get("available"):
                entries.append({
                    "semantic": "#log",
                    "filename": stream_name,
                    "content": stream.get("content"),
                    "path": stream.get("path"),
                })

        return {
            "job_id": job_id,
            "state": state,
            "output_path": backend_details.get("output_path"),
            "entries": entries,
        }

    @handle_exceptions
    def cancel_job(self, job_id, timeout=20):
        """Request cancellation of a previously submitted job.

        Parameters
        ----------
        job_id : str
            The broker job id, as returned by :meth:`submit`.
        timeout : float
            Request timeout in seconds (default 20).

        Returns
        -------
        dict
            The broker's ``JobCancelResponse`` body.
        """
        url = "{api}/v1/jobs/{job_id}/cancel".format(
            api=self.srcnet_computing_broker_url, job_id=job_id
        )
        resp = self.session.post(url, headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()


#: Module-level singleton — points at the default (production) environment.
#: Requires :func:`~astroquery.srcnet.SRCNetClass.login` before use.
FederatedExecution = None  # populated by SRCNetClass.__init__ via the module-level SRCNet singleton
