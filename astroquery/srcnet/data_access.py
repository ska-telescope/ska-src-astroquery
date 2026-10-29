"""
DataAccessClass — data download, metadata retrieval, and SODA cutouts.

This module provides the :class:`DataAccessClass` which is the recommended
interface for accessing SRCNet data products.  Obtain an instance via the
factory method::

    from astroquery.srcnet import SRCNet
    da = SRCNet.get_data_access()

    # Download a file by namespace and name
    da.get_data("testing", "PTF10tce.fits")

    # Retrieve JSON metadata
    meta = da.get_metadata("testing", "PTF10tce.fits")

    # Links of a product or file found with DataDiscovery
    links = da.get_links(products["obs_publisher_did"][0])
    da.get_data(*links["rucio_did"][1].split(":", 1))

    # SODA spatial cutout written to disk
    da.soda_cutout("testing", "PTF10tce.fits", "output.fits",
                   circle=(351.9867, 8.7787, 0.1))
"""
import io
import os
import random
import time
import secrets

from astropy import log
from astropy.io.votable import parse as parse_votable
import requests
from pyvo.dal.adhoc import DatalinkResults
from urllib.parse import urlencode

from astroquery.srcnet._redact import redact_secrets
from astroquery.srcnet.exceptions import (
    DownloadFailed,
    handle_exceptions,
    UnsupportedAccessProtocol,
)
from astroquery.srcnet.core import (
    exchange_token_for_service,
    refresh_token_if_expired,
)

__all__ = ["DataAccess", "DataAccessClass"]

#: Connect and read timeouts, in seconds, for downloads from storage: a site that does not answer is skipped rather
#: than hung on (some storage doors drop connections from outside SRCNet).
DOWNLOAD_TIMEOUT_S = (15, 300)

#: Kinds of asynchronous data-movement job the Data Management API runs.
DATA_JOB_KINDS = ("move", "stage")

#: Network timeout, in seconds, of a job-status request.
JOB_STATUS_TIMEOUT_S = 60

#: Job states that mean "still running", for :meth:`DataAccessClass.wait_for_job`.
IN_PROGRESS_JOB_STATES = frozenset(
    {"PENDING", "RECEIVED", "STARTED", "RETRY", "PROGRESS", "WRAPPING", "MOVING", "VERIFYING", "PREPARING"}
)

#: Metadata plugins the Data Management API accepts for ``get_metadata``.
METADATA_PLUGINS = ("POSTGRES_JSON", "DID_COLUMN")


def _create_partial_file(directory):
    """Create a new partial-download file in *directory* and return ``(fd, path)``.

    The name is short and random (so it fits any filename length limit and cannot be an existing file), the file is
    created exclusively, and mode 0o666 lets the umask give it the usual permissions without touching the umask.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    while True:
        path = os.path.join(directory, ".download-{}.part".format(secrets.token_hex(8)))
        try:
            return os.open(path, flags, 0o666), path
        except FileExistsError:
            continue


def _did_from_link_id(link_id):
    """The Rucio DID ``namespace:name`` in an SRCNet DataLink row ID, ``ivo://<authority>?<namespace>/<name>``.

    Returns ``""`` for an ID of another shape.
    """
    _, _, path = link_id.partition("?")
    namespace, _, name = path.partition("/")
    return f"{namespace}:{name}" if namespace and name else ""


def _dids_body(dids):
    """``[{"namespace": ..., "name": ...}]`` for the Data Management API, from DIDs in any of the accepted forms."""
    if isinstance(dids, (str, tuple, dict)):
        dids = [dids]
    body = []
    for did in dids:
        if isinstance(did, str):
            namespace, _, name = did.partition(":")
        elif isinstance(did, tuple) and len(did) == 2:
            namespace, name = did
        elif isinstance(did, dict):
            namespace, name = did.get("namespace"), did.get("name")
        else:
            raise ValueError("a DID must be 'namespace:name', (namespace, name) or a dict, not {!r}".format(did))
        if not namespace or not name:
            raise ValueError("a DID needs a namespace and a name: {!r}".format(did))
        body.append({"namespace": str(namespace), "name": str(name)})
    if not body:
        raise ValueError("dids must name at least one data identifier")
    return body


def _positive_int(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("{what} must be a positive number of seconds, not {value!r}".format(what=what, value=value))
    return value


def _required_text(value, what):
    if not value or not str(value).strip():
        raise ValueError("{what} must not be empty".format(what=what))
    return str(value).strip()


def _job_kind(kind):
    if kind not in DATA_JOB_KINDS:
        raise ValueError("kind must be one of {kinds}, not {kind!r}".format(kinds=", ".join(DATA_JOB_KINDS), kind=kind))
    return kind


def _job_state(status):
    """The state in a job status: ``status`` while it runs, ``state`` (or ``status``) once it is done."""
    return str(status.get("status") or status.get("state") or "").upper()


def _default_output_path(name):
    """Map a data identifier name to a path under the working directory.

    The name's ``/``-separated parts become directories, without any
    normalisation, so distinct names always give distinct paths.  Refuses a
    name with an empty, ``.`` or ``..`` part (or a part containing a path
    separator), and a path that resolves outside the working directory, e.g.
    through an existing symlink.
    """
    parts = name.split("/")
    separators = [sep for sep in (os.sep, os.altsep) if sep]
    if any(part in ("", os.curdir, os.pardir) or any(sep in part for sep in separators)
           for part in parts):
        raise ValueError(
            "Cannot derive a local path from {name!r} (empty, '.' or '..' part); "
            "pass output_file= to choose a path.".format(name=name)
        )
    path = os.path.join(*parts)
    base = os.path.realpath(os.getcwd())
    if os.path.commonpath([base, os.path.realpath(path)]) != base:
        raise ValueError(
            "Refusing to write {name!r} outside the working directory (it resolves "
            "through a symlink); pass output_file= to choose a path.".format(name=name)
        )
    return path


class DataAccessClass:
    """Client for SRCNet data product access.

    Do not instantiate directly.  Use :meth:`SRCNetClass.get_data_access`::

        from astroquery.srcnet import SRCNet
        da = SRCNet.get_data_access()

    All methods require authentication.  Call :meth:`SRCNetClass.login` first
    if you have not already done so::

        SRCNet.login()
        da = SRCNet.get_data_access()

    Parameters
    ----------
    srcnet_client : SRCNetClass
        The authenticated parent client.  All token management, session, and
        URL configuration are read from this object.
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
    def srcnet_authn_api_address(self):
        return self._srcnet.srcnet_authn_api_address

    @property
    def srcnet_dm_api_base_address(self):
        return self._srcnet.srcnet_dm_api_base_address

    @property
    def srcnet_data_access_tap_url(self):
        return self._srcnet.srcnet_data_access_tap_url

    @property
    def srcnet_datalink_service_url(self):
        return self._srcnet.srcnet_datalink_service_url

    @property
    def access_token_path(self):
        return self._srcnet.access_token_path

    @property
    def refresh_token_path(self):
        return self._srcnet.refresh_token_path

    def _decode_access_token(self):
        return self._srcnet._decode_access_token()

    def _persist_tokens(self):
        return self._srcnet._persist_tokens()

    def _log_tokens_obtained(self):
        return self._srcnet._log_tokens_obtained()

    # ── Public API ─────────────────────────────────────────────────────────────

    @handle_exceptions
    # Refresh first: an expired token must be renewed before its audience decides whether to exchange it.
    @refresh_token_if_expired
    @exchange_token_for_service("data-management-api")
    def get_data(self, namespace, name, sort="random", ip_address=None,
                 output_file=None):
        """Locate and download a data product by its identifier.

        By default the file is written under the current working directory at
        the relative path given by *name*, creating any directories it names
        (``"eb_001/PTF10tce.fits"`` is saved as ``./eb_001/PTF10tce.fits``),
        so products whose names share a final component do not overwrite each
        other.  Progress is printed to stdout.

        Parameters
        ----------
        namespace : str
            Data identifier namespace, e.g. ``"testing"``.
        name : str
            Data identifier name, e.g. ``"PTF10tce.fits"``.  May contain
            ``/``.  Without *output_file*, a name with an empty, ``.`` or
            ``..`` part, or one that would resolve outside the working
            directory (e.g. through a symlink), is rejected before any request.
        sort : str
            Replica selection strategy: ``"random"`` (default, same as the
            Data Management API) or ``"nearest_by_ip"``.  ``"nearest_by_ip"``
            needs a GeoIP database on the server; if the server fails to
            sort by location (any 5xx), the request is retried with
            ``"random"`` and a warning is logged.
        ip_address : str, optional
            Client IP address used by the ``"nearest_by_ip"`` strategy.
            Defaults to the requesting client IP.
        output_file : str, optional
            Local path to write to.  Parent directories are created if
            needed.  Defaults to *name*, relative to the working directory.

        Returns
        -------
        str
            The local path the file was written to.

        Notes
        -----
        Each site holding the file is tried in the order the Data Management
        API returns them, until one serves it: a site whose storage token
        cannot be issued, whose storage does not answer within
        :data:`DOWNLOAD_TIMEOUT_S`, or whose transfer fails is skipped with a
        warning.  The file is written to a temporary ``.download-*.part``
        file beside *output_file* and renamed when complete (removed if the
        attempt fails).  If no site can serve it, the error lists each site's
        reason.

        Examples
        --------
        >>> da = SRCNet.get_data_access()
        >>> da.get_data("testing", "PTF10tce.fits")
        'PTF10tce.fits'
        """
        if output_file is None:
            output_file = _default_output_path(name)
        locate_endpoint = "{api}/data/locate/{ns}/{name}".format(
            api=self.srcnet_dm_api_base_address,
            ns=namespace,
            name=name,
        )
        params = {"sort": sort}
        if ip_address:
            params["ip_address"] = ip_address
        resp = self.session.get(locate_endpoint, params=params)
        if sort == "nearest_by_ip" and resp.status_code >= 500:
            log.warning(
                "Locating the nearest replica failed (HTTP {status}); "
                "retrying with sort='random'.".format(status=resp.status_code)
            )
            resp = self.session.get(locate_endpoint, params={"sort": "random"})
        resp.raise_for_status()
        location_response = resp.json()

        # Sites can fail independently (a storage-token error, an unreachable or misconfigured storage endpoint), so
        # each site holding the file is tried in the order the DM API gave, until one download succeeds.
        failures = []
        for entry in location_response:
            rse = entry.get("identifier")
            try:
                self._download_from_site(entry, namespace, name, output_file)
            except (requests.exceptions.RequestException, UnsupportedAccessProtocol, ValueError) as exc:
                reason = getattr(exc, "message", None) or str(exc)
                log.warning("Download from {rse} failed ({reason}); trying the next site.".format(rse=rse, reason=redact_secrets(reason)[:200]))
                failures.append("{rse}: {reason}".format(rse=rse, reason=redact_secrets(reason)[:200]))
                continue
            print("\n")
            return output_file
        raise DownloadFailed("{ns}:{name}".format(ns=namespace, name=name), failures)

    def _download_from_site(self, entry, namespace, name, output_file):
        """Download the file from one site's replica, via a partial file renamed on success.

        Raises on any failure, so the caller can try the next site.
        """
        rse = entry.get("identifier")
        replicas = entry.get("replicas") or []
        if not replicas:
            raise ValueError("no replica listed")
        access_url = random.choice(replicas)

        log.info("Downloading data from {rse} ({url})".format(rse=rse, url=redact_secrets(access_url)))
        if not (access_url.startswith("https") or access_url.startswith("davs")):
            raise UnsupportedAccessProtocol(access_url.split(":")[0])
        # davs:// is WebDAV over TLS, which requests only fetches as https://
        if access_url.startswith("davs://"):
            access_url = "https://" + access_url[len("davs://"):]

        token_endpoint = (
            "{api}/data/download/{storage_id}/{ns}/{name}".format(
                api=self.srcnet_dm_api_base_address,
                storage_id=entry.get("associated_storage_area_id"),
                ns=namespace,
                name=name,
            )
        )
        resp = self.session.get(token_endpoint)
        resp.raise_for_status()
        storage_token = resp.json().get("access_token")

        resp = requests.get(
            access_url,
            headers={"Authorization": "Bearer {}".format(storage_token)},
            stream=True,
            timeout=DOWNLOAD_TIMEOUT_S,
        )
        resp.raise_for_status()
        directory = os.path.dirname(output_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # A new, uniquely named partial file beside the destination: it cannot clobber an existing file or collide
        # with another download, and it is removed if this attempt fails.
        fd, partial = _create_partial_file(directory or os.curdir)
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024):
                    f.write(chunk)
                    f.flush()
                    print("{}KB downloaded".format(round(f.tell() / 1024)), end="\r")
            os.replace(partial, output_file)
        except BaseException:
            os.unlink(partial)
            raise
    def get_metadata(self, namespace, name, plugin="POSTGRES_JSON"):
        """Retrieve JSON metadata for a data product.

        Parameters
        ----------
        namespace : str
            Data identifier namespace, e.g. ``"testing"``.
        name : str
            Data identifier name, e.g. ``"PTF10tce.fits"``.
        plugin : str
            Which Rucio metadata to return:

            - ``"POSTGRES_JSON"`` (default) -- custom metadata registered for
              the product.  A product with none gives a 404 ("No metadata
              found").
            - ``"DID_COLUMN"`` -- Rucio's own fields for the identifier, such
              as ``bytes``, ``adler32``, ``md5`` and ``did_type``.

        Returns
        -------
        dict
            Metadata as returned by the Data Management API.

        Raises
        ------
        ValueError
            If *plugin* is not one of the values above.  (The API would
            otherwise silently fall back to ``"POSTGRES_JSON"``.)

        Examples
        --------
        >>> da = SRCNet.get_data_access()
        >>> meta = da.get_metadata("testing", "PTF10tce.fits", plugin="DID_COLUMN")
        >>> print(meta["bytes"], meta["adler32"])
        """
        # Validated outside handle_exceptions, which would turn the ValueError
        # into a plain Exception.
        if plugin not in METADATA_PLUGINS:
            raise ValueError(
                "plugin must be one of {plugins}, not {plugin!r}".format(
                    plugins=", ".join(METADATA_PLUGINS), plugin=plugin)
            )
        return self._get_metadata(namespace, name, plugin)

    @handle_exceptions
    # Refresh first: an expired token must be renewed before its audience decides whether to exchange it.
    @refresh_token_if_expired
    @exchange_token_for_service("data-management-api")
    def _get_metadata(self, namespace, name, plugin):
        url = "{api}/metadata/{ns}/{name}".format(
            api=self.srcnet_dm_api_base_address, ns=namespace, name=name
        )
        resp = self.session.get(url, params={"plugin": plugin})
        resp.raise_for_status()
        return resp.json()

    # ── Data movement ─────────────────────────────────────────────────────────

    def move_data(self, dids, to_storage_area, lifetime=3600, parent_namespace=None):
        """Replicate data to another storage area.

        Asks the Data Management API to add Rucio replication rules so that a
        copy of each DID is made at *to_storage_area*.  This runs as a job on
        the server; poll it with :meth:`get_job_status` or
        :meth:`wait_for_job` (``kind="move"``).

        Parameters
        ----------
        dids : list
            The data to replicate: ``"namespace:name"`` strings,
            ``(namespace, name)`` pairs, or ``{"namespace": ..., "name": ...}``
            dicts.
        to_storage_area : str
            UUID of the destination storage area, as listed by the Site
            Capabilities API (e.g. a replica's ``associated_storage_area_id``).
        lifetime : int, optional
            Lifetime of the new replicas, in seconds (default one hour).
        parent_namespace : str, optional
            Namespace of the container the DIDs are grouped in; defaults to the
            first DID's.

        Returns
        -------
        str
            The job ID.

        Examples
        --------
        >>> job = da.move_data(["srcnet_test.comm:test_eb_001.test_prod_001/PTF10tce.fits"],
        ...                    to_storage_area="f26ff4e4-30af-4339-88ed-bb0cb7037ca0", lifetime=86400)
        >>> da.wait_for_job(job, kind="move")
        """
        params = {"to_storage_area_uuid": _required_text(to_storage_area, "to_storage_area"),
                  "lifetime": _positive_int(lifetime, "lifetime")}
        if parent_namespace:
            params["parent_namespace"] = parent_namespace
        return self._submit_data_job("move", _dids_body(dids), params)

    def stage_data(self, dids, to_storage_area=None, lifetime=3600, parent_namespace=None, approach=None):
        """Stage data in a storage area, e.g. next to the compute that will use it.

        Like :meth:`move_data`, but through the Data Management API's staging
        workflow; poll the job with ``kind="stage"``.

        Parameters
        ----------
        dids : list
            As for :meth:`move_data`.
        to_storage_area : str, optional
            UUID of the storage area to stage into; the server chooses if
            omitted.
        lifetime : int, optional
            Lifetime of the staged copies, in seconds (default one hour).
        parent_namespace : str, optional
            As for :meth:`move_data`.
        approach : str, optional
            Staging approach, one of those the destination's staging service
            supports.

        Returns
        -------
        str
            The job ID.
        """
        params = {"lifetime": _positive_int(lifetime, "lifetime")}
        if to_storage_area:
            params["to_storage_area_uuid"] = to_storage_area
        if parent_namespace:
            params["parent_namespace"] = parent_namespace
        if approach:
            params["approach"] = approach
        return self._submit_data_job("stage", _dids_body(dids), params)

    def get_job_status(self, job_id, kind="move"):
        """The status of a :meth:`move_data` (``kind="move"``) or :meth:`stage_data` (``kind="stage"``) job.

        Returns
        -------
        dict
            As the Data Management API reports it: while the job runs,
            ``{"status": ..., "info": ...}``; when it is done, its result
            (e.g. a ``state`` of ``READY`` or ``ERROR``, or a ``status`` of
            ``FAILED`` with an ``error_message``).
        """
        return self._get_data_job(_job_kind(kind), _required_text(job_id, "job_id"))

    def wait_for_job(self, job_id, kind="move", timeout_s=3600, poll_s=10):
        """Poll a data-movement job until it is no longer running, and return its final status.

        Raises
        ------
        TimeoutError
            If the job is still running after *timeout_s* seconds.
        """
        kind, job_id = _job_kind(kind), _required_text(job_id, "job_id")
        deadline = time.monotonic() + timeout_s
        status = None
        while True:
            # The deadline is checked before every request, and each request and sleep is bounded by the time left,
            # so neither an unresponsive server nor a long poll_s can outlast the limit.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("{kind} job {job} still running after {t} s: {status}".format(
                    kind=kind, job=job_id, t=timeout_s, status=status))
            status = self._get_data_job(kind, job_id, timeout=min(remaining, JOB_STATUS_TIMEOUT_S))
            if _job_state(status) not in IN_PROGRESS_JOB_STATES:
                return status
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_s, remaining))

    @handle_exceptions
    @refresh_token_if_expired
    @exchange_token_for_service("data-management-api")
    def _submit_data_job(self, kind, body, params):
        resp = self.session.post("{api}/data/{kind}".format(api=self.srcnet_dm_api_base_address, kind=kind),
                                 params=params, json=body)
        resp.raise_for_status()
        return resp.json()["job_id"]

    @handle_exceptions
    @refresh_token_if_expired
    @exchange_token_for_service("data-management-api")
    def _get_data_job(self, kind, job_id, timeout=JOB_STATUS_TIMEOUT_S):
        resp = self.session.get("{api}/data/{kind}/{job}".format(
            api=self.srcnet_dm_api_base_address, kind=kind, job=job_id), timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def get_links(self, identifier, colocated_services="", sort="random", client_ip_address=None):
        """The DataLink links of a data product or one of its files.

        This is the step from the science-metadata catalogue to the data: pass
        an identifier found with :class:`~astroquery.srcnet.data_discovery.DataDiscoveryClass`,
        and use the ``rucio_did`` of a file row with :meth:`get_data`.

        Parameters
        ----------
        identifier : str
            The product's ``obs_publisher_did`` (``ivo://...``, from ``search``
            or ``get_product_summary``), a file's ``uri`` (``skao:...``, from
            ``get_files`` or ``get_artifacts``), or a Rucio DID
            ``namespace:name``.
        colocated_services : str, optional
            Comma-separated service types a replica's site must offer, e.g.
            ``"soda_sync,soda_async"``.  Empty (default) accepts any replica;
            the DataLink service's own default, ``"product_streamer"``, is
            offered by no site yet.
        sort : str, optional
            Replica order: ``"random"`` (default) or ``"nearest_by_ip"``.
        client_ip_address : str, optional
            Client IP for ``"nearest_by_ip"``.

        Returns
        -------
        `~astropy.table.Table`
            One row per link, with the DataLink columns (``ID``,
            ``access_url``, ``semantics``, ``description``, ``content_type``,
            ``content_length``, ...) and ``rucio_did``: the ``namespace:name``
            of the file a row links to, for :meth:`get_data`.  For a product,
            the ``#this`` row stands for the whole product and the files are
            the ``#child`` rows, one per file and site; its ``rucio_did`` is
            empty.  Access URLs are storage (``davs://``) addresses that need a
            storage token, which :meth:`get_data` obtains.

        Examples
        --------
        >>> products = DataDiscovery.get_product_summary(obs_id)
        >>> links = da.get_links(products["obs_publisher_did"][0])
        >>> files = sorted(set(links["rucio_did"]) - {""})
        >>> da.get_data(*files[0].split(":", 1))
        """
        identifier = str(identifier).strip()
        if not identifier:
            raise ValueError("identifier must not be empty")
        return self._get_links(identifier, colocated_services, sort, client_ip_address)

    @handle_exceptions
    def _get_links(self, identifier, colocated_services, sort, client_ip_address):
        # lowercase id: DataLink before 0.1.7 (production today) only reads that; later versions accept any case
        params = {"id": identifier, "colocated_services": colocated_services, "sort": sort}
        if client_ip_address:
            params["client_ip_address"] = client_ip_address
        url = f"{self.srcnet_datalink_service_url}?{urlencode(params)}"
        # fetched here rather than by pyvo, so an error answer (JSON) is reported as such, not as a VOTable parse error
        resp = self.session.get(url)
        resp.raise_for_status()
        table = DatalinkResults(parse_votable(io.BytesIO(resp.content)), url=url).to_table()
        has_children = any(str(semantics) == "#child" for semantics in table["semantics"])
        table["rucio_did"] = [
            "" if has_children and str(row["semantics"]) == "#this" else _did_from_link_id(str(row["ID"]))
            for row in table
        ]
        return table

    @handle_exceptions
    def soda_cutout(
        self,
        namespace,
        name,
        output_file,
        sort=None,
        client_ip_address=None,
        pos=None,
        circle=None,
        polygon=None,
        range_=None,
        time=None,
        band=None,
        pol=None,
    ):
        """Download a spatial or spectral cutout via the IVOA SODA protocol.

        A positional parameter (*pos*, *circle*, *polygon*, or *range_*) is
        required unless *band*, *time*, or *pol* alone is sufficient for the
        service (service-dependent behaviour).

        Parameters
        ----------
        namespace : str
            Data identifier namespace, e.g. ``"testing"``.
        name : str
            Filename, e.g. ``"PTF10tce.fits"``.
        output_file : str
            Destination path for the cutout FITS file,
            e.g. ``"output/cutout.fits"``.
        sort : str, optional
            SODA service selection strategy: ``"random"`` (default) or
            ``"nearest_by_ip"``, which fails while the Data Management API has
            no IP-geolocation database.
        client_ip_address : str, optional
            Override the client IP used for nearest-by-IP selection.
        pos : str, optional
            Generic SODA POS string, e.g. ``"CIRCLE 351.9 8.77 0.1"``.
        circle : tuple, optional
            ``(longitude, latitude, radius)`` in degrees,
            e.g. ``(351.9867, 8.7787, 0.1)``.
        polygon : list of tuple, optional
            List of ``(longitude, latitude)`` vertex pairs.
        range_ : tuple, optional
            ``(lon1, lon2, lat1, lat2)`` bounding box in degrees.
        time : str, optional
            MJD time interval, e.g. ``"55123.456 55123.466"``.
            Requires a positional parameter.
        band : str, optional
            Wavelength interval in metres, e.g. ``"-Inf 300"``.
            Requires a positional parameter.
        pol : str, optional
            Polarization state, e.g. ``"Q"``.
            Requires a positional parameter.

        Examples
        --------
        >>> da = SRCNet.get_data_access()

        >>> # Circular spatial cutout
        >>> da.soda_cutout("testing", "PTF10tce.fits", "cutout.fits",
        ...                circle=(351.9867, 8.7787, 0.1))

        >>> # Spectral cutout within a spatial region
        >>> da.soda_cutout("testing", "cube.fits", "subcube.fits",
        ...                circle=(83.8, -5.4, 0.5), band="0.0002 0.0003")
        """
        if sort is None:
            sort = "random"

        url_params = {
            "id": f"{namespace}:{name}",
            # the current DataLink service selects replicas at sites offering these services; the legacy one
            # (/rucio/links) reads must_include_soda instead
            "colocated_services": "soda_sync,soda_async",
            "must_include_soda": "True",
            "sort": sort,
        }
        if client_ip_address:
            url_params["client_ip_address"] = client_ip_address

        datalink_url = f"{self.srcnet_datalink_service_url}?{urlencode(url_params)}"
        log.debug(f"Using Datalink: {datalink_url}")

        datalink_xml = DatalinkResults.from_result_url(datalink_url, session=self.session)

        soda_service = None
        id_param = None
        for resource in datalink_xml.votable.resources:
            if resource.type == "meta":
                if getattr(resource, "ID", None) == "soda-sync":
                    soda_service = None
                    id_param = None
                for param in resource.params:
                    if param.name == "accessURL":
                        soda_service = param.value
                if resource.groups:
                    for group in resource.groups:
                        for entry in group.entries:
                            if entry.name == "ID":
                                id_param = entry.value
                break

        if not soda_service:
            raise ValueError("SODA service accessURL not found in Datalink response.")
        if not id_param:
            raise ValueError("Dataset ID not found in Datalink response.")

        log.debug(f"SODA service: {soda_service}")
        log.debug(f"Dataset ID: {id_param}")

        params = {"ID": id_param, "RESPONSEFORMAT": "application/fits"}

        if pos:
            params["POS"] = pos
        elif circle:
            if len(circle) != 3:
                raise ValueError("circle must be (longitude, latitude, radius)")
            params["POS"] = "CIRCLE {} {} {}".format(*circle)
        elif polygon:
            coords = []
            for lon, lat in polygon:
                coords += [str(lon), str(lat)]
            params["POS"] = "POLYGON " + " ".join(coords)
        elif range_:
            if len(range_) != 4:
                raise ValueError("range_ must be (lon1, lon2, lat1, lat2)")
            params["POS"] = "RANGE {} {} {} {}".format(*range_)
        else:
            log.warning("No positional region set for SODA cutout.")

        for key, value, label in [
            ("BAND", band, "BAND"),
            ("TIME", time, "TIME"),
            ("POL", pol, "POL"),
        ]:
            if value is not None:
                if "POS" not in params:
                    raise ValueError(
                        f"A positional parameter is required when using {label!r}"
                    )
                params[key] = value

        log.info(f"Requesting SODA cutout from {soda_service} with params={params}")
        response = self.session.get(soda_service, params=params, stream=True)
        response.raise_for_status()

        output_dir = os.path.dirname(output_file)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(output_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=4096):
                f.write(chunk)
                f.flush()
        print("\n")
        log.debug(f"SODA cutout saved to '{output_file}'")


#: Module-level singleton — points at the default (production) environment.
#: Requires :func:`~astroquery.srcnet.SRCNetClass.login` before use.
DataAccess = None  # populated by SRCNetClass.__init__ via the module-level SRCNet singleton
