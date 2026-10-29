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


def _soda_query_parameters(pos, circle, polygon, range_, time, band, pol):
    """The SODA query parameters (POS, BAND, TIME, POL) for :meth:`DataAccessClass.soda_cutout`, validated."""
    params = {}
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

    for key, value in (("BAND", band), ("TIME", time), ("POL", pol)):
        if value is not None:
            if "POS" not in params:
                raise ValueError(f"A positional parameter is required when using {key!r}")
            params[key] = value
    return params


def _soda_sync_service(datalink_results):
    """The access URL and dataset ID of the ``soda-sync`` service descriptor in a DataLink response."""
    for resource in datalink_results.votable.resources:
        if resource.type != "meta" or getattr(resource, "ID", None) != "soda-sync":
            continue
        access_url = next((param.value for param in resource.params if param.name == "accessURL"), None)
        dataset_id = next(
            (entry.value for group in resource.groups for entry in group.entries if entry.name == "ID"), None
        )
        return access_url, dataset_id
    return None, None


def _did_from_link_id(link_id):
    """The Rucio DID ``namespace:name`` in an SRCNet DataLink row ID, ``ivo://<authority>?<namespace>/<name>``.

    Returns ``""`` for an ID of another shape.
    """
    _, _, path = link_id.partition("?")
    namespace, _, name = path.partition("/")
    return f"{namespace}:{name}" if namespace and name else ""


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
            Replica selection among the sites that offer a synchronous SODA
            service next to the data: ``"random"`` (default) or
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
        # validated here, before any request and outside handle_exceptions, so bad arguments raise ValueError
        params = _soda_query_parameters(pos, circle, polygon, range_, time, band, pol)
        return self._soda_cutout(namespace, name, output_file, sort or "random", client_ip_address, params)

    @handle_exceptions
    def _soda_cutout(self, namespace, name, output_file, sort, client_ip_address, params):
        url_params = {
            "id": f"{namespace}:{name}",
            # A synchronous cutout needs only soda_sync next to the replica: the DM API requires every listed
            # service, so also asking for soda_async would leave only the few sites that run both. The legacy
            # DataLink (/rucio/links) reads must_include_soda instead.
            "colocated_services": "soda_sync",
            "must_include_soda": "True",
            "sort": sort,
        }
        if client_ip_address:
            url_params["client_ip_address"] = client_ip_address

        datalink_url = f"{self.srcnet_datalink_service_url}?{urlencode(url_params)}"
        log.debug(f"Using Datalink: {datalink_url}")
        # fetched here rather than by pyvo, so an error answer (JSON) is reported as such, not as a VOTable parse error
        resp = self.session.get(datalink_url)
        resp.raise_for_status()
        datalink_xml = DatalinkResults(parse_votable(io.BytesIO(resp.content)), url=datalink_url)

        soda_service, id_param = _soda_sync_service(datalink_xml)
        if not soda_service:
            raise ValueError("No synchronous SODA service (soda-sync) with an accessURL in the DataLink response.")
        if not id_param:
            raise ValueError("No dataset ID for the SODA service in the DataLink response.")

        params = {"ID": id_param, "RESPONSEFORMAT": "application/fits", **params}
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
        return output_file


#: Module-level singleton — points at the default (production) environment.
#: Requires :func:`~astroquery.srcnet.SRCNetClass.login` before use.
DataAccess = None  # populated by SRCNetClass.__init__ via the module-level SRCNet singleton
