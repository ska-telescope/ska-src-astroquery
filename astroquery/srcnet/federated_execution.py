"""
FederatedExecutionClass — submit and track jobs on the SRCNet federated
compute pool, via the computing broker.

This module provides the :class:`FederatedExecutionClass`, the recommended
interface for running jobs on the federated pool. Obtain an instance via the
factory method::

    from astroquery.srcnet import SRCNet
    fe = SRCNet.get_federated_execution()

    # Submit a job (a plain dict — the broker's JobSubmitRequest shape)
    result = fe.execute({
        "workflow_type": "snakemake",
        "workflow_type_version": "7",
        "workflow_engine_parameters": {"--cores": "1"},
        "job_id": "my-job-0001",
    })
    print(result["state"])          # e.g. "PENDING"

    # Poll status
    status = fe.get_job(result["job_id"])
    print(status["state"])

The broker is the single point in SRCNet that can refuse a federated job
before it runs: it checks the owning project's credit state against the
central Accounting & Quota Service (AQS) at admission time and responds
``402 Payment Required`` for a project that is over its credit budget.
:meth:`FederatedExecutionClass.execute` surfaces that refusal as
:class:`~astroquery.srcnet.exceptions.CreditExceeded` (wrapped, like every
other error in this package, into a plain ``Exception`` by
:func:`~astroquery.srcnet.exceptions.handle_exceptions` — inspect ``str(e)``
rather than importing the exception class).
"""
from astroquery.srcnet.exceptions import (
    handle_exceptions,
    CreditExceeded,
)

__all__ = ["FederatedExecution", "FederatedExecutionClass"]


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
    def execute(self, job, timeout=30):
        """Submit a job to the SRCNet federated compute pool.

        Parameters
        ----------
        job : dict
            The job description, matching the computing broker's
            ``JobSubmitRequest`` body — at minimum ``workflow_type``,
            ``workflow_type_version``, ``workflow_engine_parameters``, and
            one of ``job_id`` / ``request_id``. See the broker's
            ``/v1/jobs`` OpenAPI schema for the full field list (e.g.
            ``workflow_url``, ``workflow_params``, ``data_location_hints``).
        timeout : float
            Request timeout in seconds (default 30).

        Returns
        -------
        dict
            The broker's ``JobSubmitResponse`` body: ``job_id``, ``state``,
            ``created``, ``candidate_sites``, ``dispatch_attempts``.

        Raises
        ------
        Exception
            If the owning project has no remaining credit budget, the
            broker responds ``402`` and this raises with a message
            identifying the credit-gate rejection (see
            :class:`~astroquery.srcnet.exceptions.CreditExceeded`). Any
            other HTTP or connection failure raises too — inspect ``str(e)``
            to tell them apart.

        Examples
        --------
        >>> fe = SRCNet.get_federated_execution()
        >>> result = fe.execute({
        ...     "workflow_type": "snakemake",
        ...     "workflow_type_version": "7",
        ...     "workflow_engine_parameters": {"--cores": "1"},
        ...     "job_id": "my-job-0001",
        ... })
        >>> result["state"]
        'PENDING'
        """
        url = "{api}/v1/jobs".format(api=self.srcnet_computing_broker_url)
        resp = self.session.post(url, json=job, headers=self._headers(), timeout=timeout)
        if resp.status_code == 402:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise CreditExceeded(detail)
        resp.raise_for_status()
        return resp.json()

    @handle_exceptions
    def get_job(self, job_id, timeout=20):
        """Fetch the current status of a previously submitted job.

        Parameters
        ----------
        job_id : str
            The broker job id, as returned by :meth:`execute`.
        timeout : float
            Request timeout in seconds (default 20).

        Returns
        -------
        dict
            The broker's ``JobStatusResponse`` body.

        Examples
        --------
        >>> fe = SRCNet.get_federated_execution()
        >>> fe.get_job("my-job-0001")["state"]
        'RUNNING'
        """
        url = "{api}/v1/jobs/{job_id}".format(
            api=self.srcnet_computing_broker_url, job_id=job_id
        )
        resp = self.session.get(url, headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    @handle_exceptions
    def cancel_job(self, job_id, timeout=20):
        """Request cancellation of a previously submitted job.

        Parameters
        ----------
        job_id : str
            The broker job id, as returned by :meth:`execute`.
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
