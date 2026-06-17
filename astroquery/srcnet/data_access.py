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

    # SODA spatial cutout written to disk
    da.soda_cutout("testing", "PTF10tce.fits", "output.fits",
                   circle=(351.9867, 8.7787, 0.1))
"""
import os
import random

from astropy import log
from pyvo.dal.adhoc import DatalinkResults
from urllib.parse import urlencode

from astroquery.srcnet.exceptions import (
    handle_exceptions,
    UnsupportedAccessProtocol,
)
from astroquery.srcnet.core import (
    exchange_token_for_service,
    refresh_token_if_expired,
)

__all__ = ["DataAccess", "DataAccessClass"]


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

    # ── Public API ─────────────────────────────────────────────────────────────

    @handle_exceptions
    @exchange_token_for_service("data-management-api")
    @refresh_token_if_expired
    def get_data(self, namespace, name, sort="nearest_by_ip", ip_address=None):
        """Locate and download a data product by its identifier.

        The file is written to the current working directory under *name*.
        Progress is printed to stdout.

        Parameters
        ----------
        namespace : str
            Data identifier namespace, e.g. ``"testing"``.
        name : str
            Data identifier name, e.g. ``"PTF10tce.fits"``.
        sort : str
            Replica selection strategy: ``"nearest_by_ip"`` (default) or
            ``"random"``.
        ip_address : str, optional
            Client IP address used by the ``"nearest_by_ip"`` strategy.
            Defaults to the requesting client IP.

        Examples
        --------
        >>> da = SRCNet.get_data_access()
        >>> da.get_data("testing", "PTF10tce.fits")
        """
        locate_endpoint = (
            "{api}/data/locate/{ns}/{name}"
            "?sort={sort}&ip_address={ip}".format(
                api=self.srcnet_dm_api_base_address,
                ns=namespace,
                name=name,
                sort=sort,
                ip=ip_address if ip_address else "",
            )
        )
        resp = self.session.get(locate_endpoint)
        resp.raise_for_status()
        location_response = resp.json()

        position = 0
        rse = location_response[position].get("identifier")
        replicas = location_response[position].get("replicas")
        storage_area_id = location_response[position].get("associated_storage_area_id")

        access_url = random.choice(replicas)

        log.info("Downloading data from {rse} ({url})".format(rse=rse, url=access_url))
        if not (access_url.startswith("https") or access_url.startswith("davs")):
            raise UnsupportedAccessProtocol(access_url.split(":")[0])

        token_endpoint = (
            "{api}/data/download/{storage_id}/{ns}/{name}".format(
                api=self.srcnet_dm_api_base_address,
                storage_id=storage_area_id,
                ns=namespace,
                name=name,
            )
        )
        resp = self.session.get(token_endpoint)
        resp.raise_for_status()
        storage_token = resp.json().get("access_token")

        resp = __import__("requests").get(
            access_url,
            headers={"Authorization": "Bearer {}".format(storage_token)},
            stream=True,
        )
        resp.raise_for_status()
        with open(name, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024):
                print(
                    "{}KB downloaded".format(round(os.path.getsize(name) / 1024), ),
                    end="\r",
                )
                f.write(chunk)
                f.flush()
        print("\n")

    @handle_exceptions
    @exchange_token_for_service("data-management-api")
    @refresh_token_if_expired
    def get_metadata(self, namespace, name):
        """Retrieve JSON metadata for a data product.

        Parameters
        ----------
        namespace : str
            Data identifier namespace, e.g. ``"testing"``.
        name : str
            Data identifier name, e.g. ``"PTF10tce.fits"``.

        Returns
        -------
        dict
            Metadata as returned by the Data Management API.

        Examples
        --------
        >>> da = SRCNet.get_data_access()
        >>> meta = da.get_metadata("testing", "PTF10tce.fits")
        >>> print(meta["size"])
        """
        url = "{api}/metadata/{ns}/{name}?plugin=POSTGRES_JSON".format(
            api=self.srcnet_dm_api_base_address, ns=namespace, name=name
        )
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

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
            SODA service selection strategy: ``"nearest_by_ip"`` (default) or
            ``"random"``.
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
            sort = "nearest_by_ip"

        url_params = {
            "id": f"{namespace}:{name}",
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
