"""
UserStorageClass — read and manage files in a site's user storage (CANFAR Cavern).

Each SRCNet site running the CANFAR science platform keeps its users' files in a
Cavern service, an IVOA VOSpace 2.0 server at ``https://<canfar host>/cavern``.
Obtain a client for one site via the factory method::

    from astroquery.srcnet import SRCNet
    SRCNet.login()
    SRCNet.list_user_storage_services()          # which sites have one
    us = SRCNet.get_user_storage("ESPSRC")

    us.list(us.home)                             # your home folder
    us.list("home/<user>/results")
    us.info("home/<user>/results/cube.fits")
    us.get("home/<user>/results/cube.fits", "cube.fits")

    us.mkdir("home/<user>/inbox")
    us.put("local.fits", "home/<user>/inbox/local.fits")
    us.move("home/<user>/inbox/local.fits", "home/<user>/results")
    us.set_public("home/<user>/results/local.fits")
    us.delete("home/<user>/inbox")

Sites are found in Site Capabilities: a ``cavern`` service if the site registers
one, otherwise the site's ``canfar`` science-portal host with ``/cavern``.
"""
import base64
import json
import os
import secrets
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse

from astropy.table import Table

from astroquery.srcnet._redact import redact_secrets
from astroquery.srcnet.core import exchange_token_for_service, refresh_token_if_expired
from astroquery.srcnet.exceptions import handle_exceptions

__all__ = ["UserStorageClass", "list_user_storage_services"]

#: Connect and read timeouts, in seconds, for Cavern metadata requests (listing, folders, jobs).
USER_STORAGE_TIMEOUT_S = (15, 60)

#: Connect and read timeouts, in seconds, for moving file contents (downloads and uploads).
TRANSFER_TIMEOUT_S = (15, 300)

#: Token audience for Site Capabilities, used to find each site's Cavern.
SITE_CAPABILITIES_SERVICE = "site-capabilities-api"

_VOS = "http://www.ivoa.net/xml/VOSpace/v2.0"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_CORE = "ivo://ivoa.net/vospace/core#"
_UWS = "http://www.ivoa.net/xml/UWS/v1.0"
ET.register_namespace("vos", _VOS)
ET.register_namespace("xsi", _XSI)
#: Security method asking for URLs that accept the bearer token (Cavern advertises ivo://ivoa.net/sso#token).
_TOKEN_SECURITY = "ivo://ivoa.net/sso#token"

#: VOSpace node type for each :meth:`UserStorageClass.list` ``type``.
_NODE_TYPES = {"folder": "vos:ContainerNode", "file": "vos:DataNode", "link": "vos:LinkNode"}

#: VOSpace properties shown as columns by :meth:`UserStorageClass.list`.
_PROPERTIES = {
    "length": _CORE + "length",
    "date": _CORE + "date",
    "content_type": _CORE + "type",
    "is_public": _CORE + "ispublic",
}


def _node_path(path):
    """Normalise *path* to a relative VOSpace path (``home/<user>/…``).

    Accepts ``home/x``, ``/home/x`` or a full ``vos://<authority>/home/x`` URI.
    Empty, ``.`` and ``..`` segments are refused rather than resolved.
    """
    if path is None:
        raise ValueError("path is required, e.g. 'home/<user>'.")
    path = str(path)
    if path.startswith("vos://"):
        path = urlparse(path).path
    parts = [p for p in path.strip("/").split("/")] if path.strip("/") else []
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"path {path!r} has an empty, '.' or '..' segment.")
    return "/".join(parts)


def _url_path(path):
    """*path* quoted for use in a URL, segment by segment."""
    return "/".join(quote(p, safe="") for p in path.split("/")) if path else ""


def _node_type(node):
    t = node.get(f"{{{_XSI}}}type", "")
    return {"vos:ContainerNode": "folder", "vos:DataNode": "file", "vos:LinkNode": "link"}.get(t, t or "unknown")


def _properties(node):
    return {p.get("uri"): (p.text or "") for p in node.findall(f"{{{_VOS}}}properties/{{{_VOS}}}property")}


def _node_row(node):
    """One :meth:`UserStorageClass.list` row for a ``vos:node`` element."""
    props = _properties(node)
    path = _node_path(node.get("uri", ""))
    length = props.get(_PROPERTIES["length"])
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "type": _node_type(node),
        "size": int(length) if length and length.isdigit() else -1,
        "modified": props.get(_PROPERTIES["date"], ""),
        "content_type": props.get(_PROPERTIES["content_type"], ""),
        "is_public": props.get(_PROPERTIES["is_public"], "").lower() == "true",
    }


def _cavern_url(service):
    """Base URL of a Site Capabilities service entry, tolerating the shapes sites use.

    Seen in production: ``prefix`` with or without ``://``, ``path`` with or without
    a leading slash, and the path packed into ``host``.
    """
    prefix = (service.get("prefix") or "https").split(":")[0]
    host, _, host_path = (service.get("host") or "").strip().strip("/").partition("/")
    port = service.get("port")
    if port and ":" not in host and int(port) != {"https": 443, "http": 80}.get(prefix):
        host = f"{host}:{port}"
    path = "/".join(p.strip("/") for p in (host_path, service.get("path") or "") if p and p.strip("/"))
    return "/".join(x for x in (f"{prefix}://{host}", path) if x)


def _hostname(service):
    """Just the host name of a service entry, dropping any path packed into ``host``."""
    return (service.get("host") or "").strip().strip("/").split("/")[0]


def _storage_services(nodes):
    """Rows (node, site, url, registered) for every site with a Cavern or a CANFAR portal."""
    rows = []
    for node in nodes:
        for site in node.get("sites", []):
            # CANFAR and Cavern are registered as local services of a site's compute entries.
            computes = [c for c in site.get("compute", []) if isinstance(c, dict) and not c.get("is_force_disabled")]
            services = [s for c in computes for s in c.get("associated_local_services", []) if isinstance(s, dict)]
            services = [s for s in services if not s.get("is_force_disabled")]
            caverns = [s for s in services if s.get("type") == "cavern"]
            portals = [s for s in services if s.get("type") == "canfar"]
            # Registered Caverns first, then the one CANFAR runs beside each science portal: some
            # sites register no Cavern, and one registered entry points at the storage web page.
            urls = [(_cavern_url(s), True) for s in caverns]
            urls += [(f"{_cavern_url({**s, 'host': _hostname(s), 'path': ''})}/cavern", False) for s in portals]
            seen = set()
            for url, registered in urls:
                if url not in seen:
                    seen.add(url)
                    rows.append({"node": node.get("name", ""), "site": site.get("name", ""),
                                 "url": url, "registered": registered})
    return rows


def list_user_storage_services(srcnet_client):
    """Sites' user storage (Cavern) services, from Site Capabilities.

    See :meth:`SRCNetClass.list_user_storage_services`.
    """
    return _SiteCapabilities(srcnet_client).storage_services()


class _SrcnetTokenProxy:
    """Token/session proxies the auth decorators need, read from the parent SRCNet client."""

    def __init__(self, srcnet_client):
        self._srcnet = srcnet_client

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


class _SiteCapabilities(_SrcnetTokenProxy):

    @property
    def url(self):
        return self._srcnet.srcnet_site_capabilities_url

    @handle_exceptions
    @refresh_token_if_expired
    @exchange_token_for_service(SITE_CAPABILITIES_SERVICE)
    def nodes(self):
        resp = self.session.get(f"{self.url}/nodes", params={"include_inactive": "false"},
                                timeout=USER_STORAGE_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()

    def storage_services(self):
        rows = _storage_services(self.nodes())
        return Table(rows=rows, names=("node", "site", "url", "registered")) if rows else \
            Table(names=("node", "site", "url", "registered"), dtype=(str, str, str, bool))

    def find(self, site):
        """The Cavern URL for *site* (a node or site name, case-insensitive).

        Candidates are tried in order (registered first) and the first one whose
        VOSpace ``/capabilities`` answers is used."""
        wanted = site.lower()
        services = self.storage_services()
        matches = [str(r["url"]) for r in services if wanted in (r["node"].lower(), r["site"].lower())]
        if not matches:
            known = sorted(set(services["node"]))
            raise ValueError(f"No user storage found for {site!r} in Site Capabilities. Sites with one: {known}")
        for url in dict.fromkeys(matches):
            if _is_vospace(self.session, url):
                return url
        raise ValueError(f"None of {site}'s user storage addresses answers as a VOSpace service: {matches}")


def _is_vospace(session, url):
    """Whether *url* answers like a VOSpace service (its public ``/capabilities``)."""
    try:
        resp = session.get(f"{url}/capabilities", headers={"Authorization": None}, timeout=(10, 20))
    except Exception:  # noqa: BLE001 - any connection problem means "not this one"
        return False
    return resp.ok and "ivo://ivoa.net/std/VOSpace" in resp.text


class UserStorageClass(_SrcnetTokenProxy):
    """Client for one site's user storage (CANFAR Cavern, IVOA VOSpace 2.0).

    Do not instantiate directly.  Use :meth:`SRCNetClass.get_user_storage`::

        from astroquery.srcnet import SRCNet
        SRCNet.login()
        us = SRCNet.get_user_storage("ESPSRC")

    Requests carry the SRCNet login's bearer token; Cavern accepts it whatever
    service audience it was last exchanged for (checked live). Paths are relative
    to the VOSpace root, e.g. ``home/<user>/results``; :attr:`home` is your own
    folder, which exists only at sites whose science portal you have used.

    Parameters
    ----------
    srcnet_client : SRCNetClass
        The authenticated parent client.
    url : str
        Base URL of the Cavern service, e.g. ``https://canfar.example.org/cavern``.
    token_service : str, optional
        Exchange the login token for this service's audience before each call.
        ``None`` (the default) sends the token as it is.
    """

    def __init__(self, srcnet_client, url, token_service=None):
        super().__init__(srcnet_client)
        self.url = url.rstrip("/")
        self.token_service = token_service

    def __repr__(self):
        return f"<UserStorageClass {self.url}>"

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _headers(self):
        """Authorization header for Cavern, after refreshing (and exchanging, if configured) the token."""
        refresh_token_if_expired(lambda s: None)(self)
        if self.token_service:
            exchange_token_for_service(self.token_service)(lambda s: None)(self)
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    @property
    def username(self):
        """Your username, from the login token (``preferred_username``), or ``None``."""
        token = self.access_token
        if not token or token.count(".") != 2:
            return None
        payload = token.split(".")[1]
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except ValueError:
            return None
        return claims.get("preferred_username")

    @property
    def home(self):
        """Path of your home folder, ``home/<username>``."""
        if not self.username:
            raise ValueError("No username in the login token; call SRCNet.login() first, or give the path.")
        return f"home/{self.username}"

    # ── Public API ────────────────────────────────────────────────────────────

    def list(self, path=None):
        """List a folder.

        Parameters
        ----------
        path : str, optional
            Folder to list, e.g. ``home/<user>/results``. Defaults to :attr:`home`.

        Returns
        -------
        `~astropy.table.Table`
            One row per entry: ``name``, ``path``, ``type`` (``folder``/``file``/``link``),
            ``size`` (bytes, -1 if unknown), ``modified``, ``content_type``, ``is_public``.
        """
        path = _node_path(self.home if path is None else path)
        try:
            node = self._get_node(path)
        except FileNotFoundError:
            if path == self.home:
                raise FileNotFoundError(
                    f"You have no home folder in the user storage at {self.url}. It is created the first time "
                    "you use this site's CANFAR science portal."
                ) from None
            raise
        rows = [_node_row(child) for child in node.findall(f"{{{_VOS}}}nodes/{{{_VOS}}}node")]
        names = ("name", "path", "type", "size", "modified", "content_type", "is_public")
        if not rows:
            return Table(names=names, dtype=(str, str, str, int, str, str, bool))
        return Table(rows=rows, names=names)

    def info(self, path):
        """Metadata of one file or folder, as a dict (same fields as a :meth:`list` row)."""
        return _node_row(self._get_node(_node_path(path)))

    def exists(self, path):
        """Whether *path* exists (and is visible to you)."""
        try:
            self._get_node(_node_path(path))
            return True
        except FileNotFoundError:
            return False

    def get(self, path, output_file=None):
        """Download a file.

        Parameters
        ----------
        path : str
            File to download, e.g. ``home/<user>/results/cube.fits``.
        output_file : str, optional
            Where to save it. Defaults to the file's name in the current directory.
            Written to a temporary ``.part`` file first and renamed when complete.

        Returns
        -------
        str
            The path written.
        """
        path = _node_path(path)
        output_file = output_file or path.rsplit("/", 1)[-1]
        resp = self._request("GET", f"{self.url}/files/{_url_path(path)}", stream=True, timeout=TRANSFER_TIMEOUT_S)
        directory = os.path.dirname(output_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        partial = os.path.join(directory or os.curdir, f".download-{secrets.token_hex(8)}.part")
        try:
            with open(partial, "xb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            os.replace(partial, output_file)
        except BaseException:
            if os.path.exists(partial):
                os.unlink(partial)
            raise
        return output_file

    def put(self, local_file, path, *, overwrite=False):
        """Upload a local file.

        Parameters
        ----------
        local_file : str
            The file to upload.
        path : str
            Destination in the user storage, e.g. ``home/<user>/inbox/cube.fits``.
            Its folder must exist (see :meth:`mkdir`).
        overwrite : bool, optional
            Replace an existing file. Off by default: an existing *path* raises
            ``FileExistsError``.

        Returns
        -------
        str
            The destination path.
        """
        path = _node_path(path)
        if not os.path.isfile(local_file):
            raise FileNotFoundError(f"No such local file: {local_file}")
        if not overwrite and self.exists(path):
            raise FileExistsError(f"{path} already exists in the user storage; pass overwrite=True to replace it.")
        # VOSpace transfer negotiation: the service answers with the URL(s) to PUT the bytes to.
        endpoints = self._negotiate(path, "pushToVoSpace")
        errors = []
        for endpoint in endpoints:
            with open(local_file, "rb") as f:
                resp = self.session.put(endpoint, data=f, headers=self._headers(), timeout=TRANSFER_TIMEOUT_S)
            if resp.ok:
                return path
            errors.append(f"{resp.status_code} {resp.text[:120].strip()}")
        raise OSError(f"Upload of {path} failed at every endpoint: {'; '.join(errors) or 'none offered'}")

    def mkdir(self, path, *, parents=False, exist_ok=False):
        """Create a folder.

        Parameters
        ----------
        path : str
            Folder to create, e.g. ``home/<user>/inbox``.
        parents : bool, optional
            Also create missing parent folders.
        exist_ok : bool, optional
            Don't raise if the folder already exists.

        Returns
        -------
        str
            The folder's path.
        """
        path = _node_path(path)
        if parents:
            parts = path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if not self.exists(parent):
                    self.mkdir(parent)
        body = self._node_xml(path, "vos:ContainerNode")
        resp = self._request("PUT", f"{self.url}/nodes/{_url_path(path)}", data=body,
                             headers={"Content-Type": "text/xml"}, expected=(200, 201, 409))
        if resp.status_code == 409:
            if exist_ok and self.info(path)["type"] == "folder":
                return path
            raise FileExistsError(f"{path} already exists in the user storage.")
        return path

    def delete(self, path, *, recursive=False, timeout=600):
        """Delete a file or folder.

        A folder must be empty unless *recursive* is true, which deletes it with
        everything in it (as a server-side job, waited for up to *timeout* seconds).

        Returns
        -------
        str
            The deleted path.
        """
        path = _node_path(path)
        if path.count("/") < 2:
            raise ValueError(f"Refusing to delete {path!r}: give a path inside your home folder, e.g. home/<user>/x.")
        if recursive and self.info(path)["type"] == "folder":
            job = self._request("POST", f"{self.url}/async-delete", data={"target": self._vos_uri(path)},
                                allow_redirects=False, expected=(303,))
            self._run_job(job.headers["Location"], timeout)
        else:
            resp = self._request("DELETE", f"{self.url}/nodes/{_url_path(path)}", expected=(200, 204, 400, 409))
            # Cavern answers 400 "container node '…' is not empty"; VOSpace allows 409 too.
            if resp.status_code == 409 or (resp.status_code == 400 and "not empty" in resp.text):
                raise OSError(
                    f"{path} is a folder that isn't empty; pass recursive=True to delete it and its contents.")
            if resp.status_code == 400:
                raise OSError(f"User storage refused to delete {path}: {resp.text[:300].strip()}")
        return path

    def move(self, path, destination, *, timeout=600):
        """Move or rename a file or folder.

        If *destination* is an existing folder, *path* is moved into it;
        otherwise *path* is renamed to *destination*. Runs as a server-side job,
        waited for up to *timeout* seconds.

        Returns
        -------
        str
            The new path.
        """
        path, destination = _node_path(path), _node_path(destination)
        try:
            into = self.info(destination)["type"] == "folder"
        except FileNotFoundError:
            into = False
        else:
            if not into:
                raise FileExistsError(f"{destination} already exists in the user storage.")
        job = self._request("POST", f"{self.url}/transfers",
                            data=self._transfer_xml(self._vos_uri(path), self._vos_uri(destination), keep_bytes=False),
                            headers={"Content-Type": "text/xml"}, allow_redirects=False, expected=(303,))
        self._run_job(job.headers["Location"], timeout)
        return f"{destination}/{path.rsplit('/', 1)[-1]}" if into else destination

    def set_public(self, path, public=True):
        """Make a file or folder readable by anyone (``public=True``) or only by you.

        Returns
        -------
        dict
            The entry's updated metadata (see :meth:`info`).
        """
        path = _node_path(path)
        node_type = _NODE_TYPES[self.info(path)["type"]]
        body = self._node_xml(path, node_type, {_PROPERTIES["is_public"]: "true" if public else "false"})
        self._request("POST", f"{self.url}/nodes/{_url_path(path)}", data=body, headers={"Content-Type": "text/xml"})
        return self.info(path)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _request(self, method, url, *, expected=None, headers=None, timeout=USER_STORAGE_TIMEOUT_S, **kwargs):
        """One request with the login token; maps 401/403/404 to Python errors.

        *expected* lists status codes to return as they are rather than raise for."""
        all_headers = {**self._headers(), **(headers or {})}
        resp = self.session.request(method, url, headers=all_headers, timeout=timeout, **kwargs)
        if expected and resp.status_code in expected:
            return resp
        if resp.status_code == 404:
            raise FileNotFoundError(f"Not found in user storage: {redact_secrets(url)}")
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"User storage refused access ({resp.status_code}) to {redact_secrets(url)}: "
                f"{resp.text[:200].strip()}"
            )
        if not resp.ok:
            raise OSError(f"User storage error {resp.status_code} for {method} {redact_secrets(url)}: "
                          f"{resp.text[:300].strip()}")
        return resp

    def _get_node(self, path):
        resp = self._request("GET", f"{self.url}/nodes/{_url_path(path)}", params={"detail": "max"})
        try:
            return ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ValueError(f"User storage returned something that isn't a VOSpace node for {path!r}.") from exc

    @property
    def _vos_root(self):
        """``vos://<authority>``: this service's VOSpace URI prefix, read from the root node."""
        if getattr(self, "_vos_root_cache", None) is None:
            uri = self._get_node("").get("uri", "")
            parsed = urlparse(uri)
            if parsed.scheme != "vos" or not parsed.netloc:
                raise ValueError(f"User storage at {self.url} reported no VOSpace URI for its root.")
            self._vos_root_cache = f"vos://{parsed.netloc}"
        return self._vos_root_cache

    def _vos_uri(self, path):
        return f"{self._vos_root}/{path}"

    def _node_xml(self, path, node_type, properties=None):
        """A VOSpace node document, for creating a node or changing its properties."""
        node = ET.Element(f"{{{_VOS}}}node", {f"{{{_XSI}}}type": node_type, "uri": self._vos_uri(path)})
        props = ET.SubElement(node, f"{{{_VOS}}}properties")
        for uri, value in (properties or {}).items():
            ET.SubElement(props, f"{{{_VOS}}}property", {"uri": uri, "readOnly": "false"}).text = value
        if node_type == "vos:ContainerNode":
            ET.SubElement(node, f"{{{_VOS}}}nodes")
        return ET.tostring(node, encoding="UTF-8", xml_declaration=True)

    @staticmethod
    def _transfer_xml(target, direction, *, keep_bytes=None, protocol=None):
        """A VOSpace 2.1 transfer document."""
        transfer = ET.Element(f"{{{_VOS}}}transfer", {"version": "2.1"})
        ET.SubElement(transfer, f"{{{_VOS}}}target").text = target
        ET.SubElement(transfer, f"{{{_VOS}}}direction").text = direction
        if keep_bytes is not None:
            ET.SubElement(transfer, f"{{{_VOS}}}keepBytes").text = "true" if keep_bytes else "false"
        if protocol:
            # one protocol entry for the token, one without (pre-authorised URLs)
            proto = ET.SubElement(transfer, f"{{{_VOS}}}protocol", {"uri": protocol})
            ET.SubElement(proto, f"{{{_VOS}}}securityMethod", {"uri": _TOKEN_SECURITY})
            ET.SubElement(transfer, f"{{{_VOS}}}protocol", {"uri": protocol})
        return ET.tostring(transfer, encoding="UTF-8", xml_declaration=True)

    def _negotiate(self, path, direction):
        """Endpoint URLs the service offers for moving *path*'s bytes in *direction*."""
        protocol = _CORE + ("httpsput" if direction == "pushToVoSpace" else "httpsget")
        resp = self._request("POST", f"{self.url}/synctrans",
                             data=self._transfer_xml(self._vos_uri(path), direction, protocol=protocol),
                             headers={"Content-Type": "text/xml"}, allow_redirects=False, expected=(200, 303))
        hops = 0
        while resp.status_code == 303 and hops < 5:
            resp = self._request("GET", resp.headers["Location"], allow_redirects=False, expected=(200, 303))
            hops += 1
        try:
            doc = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise OSError(f"User storage gave no transfer document for {path}.") from exc
        endpoints = [e.text.strip() for e in doc.iter(f"{{{_VOS}}}endpoint") if e.text and e.text.strip()]
        if not endpoints:
            raise OSError(f"User storage offered no endpoint to transfer {path}.")
        return endpoints

    def _run_job(self, job_url, timeout):
        """Start a UWS job and wait until it finishes; raise with the service's message if it fails."""
        self._request("POST", f"{job_url}/phase", data={"PHASE": "RUN"}, allow_redirects=False, expected=(200, 303))
        deadline = time.monotonic() + timeout
        while True:
            doc = ET.fromstring(self._request("GET", job_url, params={"WAIT": 6}).content)
            phase = (doc.findtext(f"{{{_UWS}}}phase") or "").upper()
            if phase == "COMPLETED":
                return
            if phase in ("ERROR", "ABORTED"):
                message = doc.findtext(f"{{{_UWS}}}errorSummary/{{{_UWS}}}message") or "no message"
                raise OSError(f"User storage job {phase.lower()}: {message.strip()}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"User storage job still {phase or 'unknown'} after {timeout} s: {job_url}")
