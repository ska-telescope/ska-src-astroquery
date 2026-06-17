"""
SRCNet Data Discovery TAP client.

Follows astroquery conventions — all query methods return `~astropy.table.Table`.

Examples
--------
Simple usage via the module singleton::

    from astroquery.srcnet import DataDiscovery

    # List available tables
    DataDiscovery.get_tables()

    # Cone search around a position
    from astropy.coordinates import SkyCoord
    import astropy.units as u

    results = DataDiscovery.query_region(SkyCoord(83.8, -5.4, unit="deg"), radius=0.5 * u.deg)

    # Raw ADQL
    DataDiscovery.query("SELECT TOP 10 * FROM ivoa.ObsCore")

    # Natural language → ADQL + execute
    adql, t = DataDiscovery.query_natural(
        "show the 10 most recent JCMT observations", verbose=True
    )

    # Natural language → ADQL only
    adql = DataDiscovery.nl_to_adql("how many observations per collection?")
    print(adql)

Switch environment::

    from astroquery.srcnet import conf
    conf.SRCNET_ENVIRONMENT = "development"
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

import pyvo
import requests
from astropy.coordinates import SkyCoord
from astropy.table import Table
import astropy.units as u
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ._helpdesk import srcnet_raise

__all__ = ["DataDiscovery", "DataDiscoveryClass"]


# ── NL → ADQL prompt ──────────────────────────────────────────────────────────

try:
    from .schemas import _TAP_OBSCORE_SCHEMA
except ImportError:
    _TAP_OBSCORE_SCHEMA = (
        "ivoa.ObsCore\n"
        "  obs_id             - observation identifier (string)\n"
        "  obs_collection     - data collection name\n"
        "  dataproduct_type   - 'image', 'cube', 'spectrum', etc.\n"
        "  calib_level        - 0=raw … 3=science-ready\n"
        "  target_name        - target / source name\n"
        "  facility_name      - telescope / facility\n"
        "  instrument_name    - instrument name\n"
        "  s_ra               - RA of field centre (degrees, ICRS)\n"
        "  s_dec              - Dec of field centre (degrees, ICRS)\n"
        "  s_fov              - field of view diameter (degrees)\n"
        "  t_min              - observation start time (MJD)\n"
        "  t_max              - observation end time (MJD)\n"
        "  t_exptime          - total exposure time (seconds)\n"
        "  em_min             - minimum wavelength (metres)\n"
        "  em_max             - maximum wavelength (metres)\n"
        "  access_url         - URL to download the data\n"
        "  access_format      - MIME type of the data product\n"
    )

_NL_TO_ADQL_PROMPT = (
    "You are an expert in ADQL (Astronomical Data Query Language), which is a superset\n"
    "of SQL used to query astronomical TAP services.\n"
    "\n"
    "The database exposes the standard IVOA ObsCore table.  The key table and its\n"
    "most useful columns are:\n"
    "\n"
    + _TAP_OBSCORE_SCHEMA
    + "\nADQL syntax notes:\n"
    "- Spatial cone search: CONTAINS(POINT('ICRS', s_ra, s_dec), CIRCLE('ICRS', ra, dec, radius_deg)) = 1\n"
    "- String wildcards: LIKE '%value%'\n"
    "- Limit rows: SELECT TOP N ... (ADQL does NOT support LIMIT — always use SELECT TOP N)\n"
    "- No JOIN needed — all columns are in ivoa.ObsCore.\n"
    "\n"
    "Translate the following question into a single ADQL query.\n"
    "Return ONLY the ADQL query — no explanation, no markdown, no comments.\n"
    "\n"
    "Question: {question}\n"
)


# ── Client class ──────────────────────────────────────────────────────────────

class DataDiscoveryClass:
    """
    Query the SRCNet Data Discovery TAP service.

    The convenience methods (``query_region``, ``query_name``,
    ``query_observations``, ``get_collections``) use the standard
    ``ivoa.ObsCore`` table, which is portable across IVOA-compliant TAP
    services.  Raw ADQL via :meth:`query` can target any table exposed by the
    service.  The ``get_artifacts`` method uses CAOM2-specific tables for
    file-level detail.

    Parameters
    ----------
    tap_url : str, optional
        TAP service base URL.  Defaults to the URL for the currently
        configured SRCNet environment.
    token : str, optional
        Bearer token for authenticated requests.
    """

    OBSCORE_TABLE = "ivoa.ObsCore"
    OBS_TABLE     = "caom2.Observation"
    PLANE_TABLE   = "caom2.Plane"
    ART_TABLE     = "caom2.Artifact"

    def __init__(
        self,
        tap_url: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        from . import _env_urls
        self._tap_url = (tap_url or _env_urls()["tap"]).rstrip("/")
        self._token = token
        self._tap: Optional[pyvo.dal.TAPService] = None

    # ── Internal ──────────────────────────────────────────────────────────────

    @property
    def tap(self) -> pyvo.dal.TAPService:
        """Lazily-instantiated :class:`~pyvo.dal.TAPService`."""
        if self._tap is None:
            self._tap = pyvo.dal.TAPService(self._tap_url)
            session = self._tap._session
            if self._token:
                session.headers["Authorization"] = f"Bearer {self._token}"
            _patch_redirect_session(session, self._tap_url)
            _mount_retries(session)
        return self._tap

    # ── Schema introspection ──────────────────────────────────────────────────

    def get_tables(self) -> Table:
        """
        Return the list of tables available in this TAP service.

        Returns
        -------
        `~astropy.table.Table`
            Columns: ``name``, ``description``.
        """
        # One request to the VOSI tableset, parsed directly — avoids pyvo's
        # per-table detail fetches (one request per table), which multiply the
        # chance of hitting a flaky TAP ingress. The retry-mounted session
        # handles transient connection drops on this single request.
        resp = self.tap._session.get(f"{self._tap_url}/tables")
        resp.raise_for_status()
        rows = _parse_tableset(resp.content)
        return Table(rows=rows) if rows else Table(names=["name", "description"])

    def get_columns(self, table: str) -> Table:
        """
        Return column definitions for *table*.

        Parameters
        ----------
        table : str
            Fully-qualified table name, e.g. ``"caom2.Observation"``.

        Returns
        -------
        `~astropy.table.Table`
            Columns: ``name``, ``datatype``, ``unit``, ``ucd``, ``description``.
        """
        cols = self.tap.tables[table].columns
        rows = [
            {
                "name":        c.name,
                "datatype":    c.datatype.content if hasattr(c.datatype, "content") else str(c.datatype),
                "unit":        str(c.unit or ""),
                "ucd":         str(c.ucd or ""),
                "description": c.description or "",
            }
            for c in cols
        ]
        return Table(rows=rows) if rows else Table(names=["name", "datatype", "unit", "ucd", "description"])

    # ── Low-level query ───────────────────────────────────────────────────────

    def query(self, adql: str, *, maxrec: Optional[int] = None) -> Table:
        """
        Execute an arbitrary ADQL statement synchronously.

        Parameters
        ----------
        adql : str
            ADQL query string.
        maxrec : int, optional
            Maximum rows to return.  Defaults to
            :attr:`~astroquery.srcnet.Conf.SRCNET_DEFAULT_MAXREC`.

        Returns
        -------
        `~astropy.table.Table`

        Examples
        --------
        >>> DataDiscovery.query("SELECT TOP 10 * FROM ivoa.ObsCore")
        >>> DataDiscovery.query("SELECT obs_collection, COUNT(*) AS n FROM ivoa.ObsCore GROUP BY obs_collection")
        """
        from . import conf
        maxrec = maxrec if maxrec is not None else conf.SRCNET_DEFAULT_MAXREC
        return self.tap.search(adql, maxrec=maxrec).to_table()

    # ── Convenience queries ───────────────────────────────────────────────────

    def get_collections(self, *, verbose: bool = False) -> Table:
        """
        Return all data collections and their observation counts.

        Parameters
        ----------
        verbose : bool
            If ``True``, print the generated ADQL before executing.

        Returns
        -------
        `~astropy.table.Table`
            Columns: ``obs_collection``, ``count``.
        """
        adql = (
            f"SELECT obs_collection, COUNT(*) AS count "
            f"FROM {self.OBSCORE_TABLE} "
            f"GROUP BY obs_collection "
            f"ORDER BY count DESC"
        )
        if verbose:
            print(f"[ADQL] {adql}")
        return self.query(adql, maxrec=500)

    def query_region(
        self,
        coordinates: SkyCoord,
        radius: u.Quantity,
        *,
        collection: Optional[str] = None,
        columns: str = "obs_id, obs_collection, facility_name, "
                       "s_ra, s_dec, em_min, em_max, t_exptime",
        maxrec: Optional[int] = None,
        verbose: bool = False,
    ) -> Table:
        """
        Cone search around *coordinates*.

        Parameters
        ----------
        coordinates : `~astropy.coordinates.SkyCoord`
            Centre of the search cone (ICRS).
        radius : `~astropy.units.Quantity`
            Search radius, e.g. ``0.5 * u.deg``.
        collection : str, optional
            Restrict to a specific data collection.
        columns : str, optional
            Comma-separated ADQL column list (default: key ObsCore columns).
        maxrec : int, optional
            Maximum rows to return.
        verbose : bool
            If ``True``, print the generated ADQL before executing.

        Returns
        -------
        `~astropy.table.Table`

        Examples
        --------
        >>> from astropy.coordinates import SkyCoord
        >>> import astropy.units as u
        >>> DataDiscovery.query_region(SkyCoord(83.8, -5.4, unit="deg"), radius=0.5 * u.deg)
        """
        ra  = coordinates.icrs.ra.deg
        dec = coordinates.icrs.dec.deg
        r   = radius.to(u.deg).value

        where = [
            f"CONTAINS(POINT('ICRS', s_ra, s_dec), "
            f"CIRCLE('ICRS', {ra}, {dec}, {r})) = 1"
        ]
        if collection:
            where.append(f"obs_collection = '{_esc(collection)}'")

        adql = (
            f"SELECT {columns} "
            f"FROM {self.OBSCORE_TABLE} "
            f"WHERE {' AND '.join(where)}"
        )
        if verbose:
            print(f"[ADQL] {adql}")
        return self.query(adql, maxrec=maxrec)

    def query_name(
        self,
        name: str,
        *,
        collection: Optional[str] = None,
        columns: str = "obs_id, obs_collection, facility_name, target_name, "
                       "s_ra, s_dec",
        maxrec: Optional[int] = None,
        verbose: bool = False,
    ) -> Table:
        """
        Search for observations by target name.

        Parameters
        ----------
        name : str
            Target name or partial name (case-insensitive substring match).
        collection : str, optional
            Restrict to a specific data collection.
        columns : str, optional
            Comma-separated ADQL column list.
        maxrec : int, optional
            Maximum rows to return.
        verbose : bool
            If ``True``, print the generated ADQL before executing.

        Returns
        -------
        `~astropy.table.Table`

        Examples
        --------
        >>> DataDiscovery.query_name("M31")
        >>> DataDiscovery.query_name("Crab", collection="JCMT")
        """
        where = [f"UPPER(target_name) LIKE UPPER('%{_esc(name)}%')"]
        if collection:
            where.append(f"obs_collection = '{_esc(collection)}'")

        adql = (
            f"SELECT {columns} "
            f"FROM {self.OBSCORE_TABLE} "
            f"WHERE {' AND '.join(where)}"
        )
        if verbose:
            print(f"[ADQL] {adql}")
        return self.query(adql, maxrec=maxrec)

    def query_observations(
        self,
        *,
        collection: Optional[str] = None,
        telescope: Optional[str] = None,
        instrument: Optional[str] = None,
        target_name: Optional[str] = None,
        columns: str = "obs_id, obs_collection, facility_name, "
                       "instrument_name, target_name, dataproduct_type",
        maxrec: Optional[int] = None,
        verbose: bool = False,
    ) -> Table:
        """
        Query ObsCore with optional keyword filters.

        All parameters are optional; omit them to retrieve all rows.

        Parameters
        ----------
        collection : str, optional
            Exact match on ``obs_collection``.
        telescope : str, optional
            Substring match on ``facility_name``.
        instrument : str, optional
            Substring match on ``instrument_name``.
        target_name : str, optional
            Substring match on ``target_name``.
        columns : str, optional
            Comma-separated ADQL column list.
        maxrec : int, optional
            Maximum rows to return.
        verbose : bool
            If ``True``, print the generated ADQL before executing.

        Returns
        -------
        `~astropy.table.Table`

        Examples
        --------
        >>> DataDiscovery.query_observations(collection="JCMT", instrument="SCUBA-2")
        >>> DataDiscovery.query_observations(target_name="Orion")
        """
        where: list[str] = []

        if collection:
            where.append(f"obs_collection = '{_esc(collection)}'")
        if telescope:
            where.append(f"facility_name LIKE '%{_esc(telescope)}%'")
        if instrument:
            where.append(f"instrument_name LIKE '%{_esc(instrument)}%'")
        if target_name:
            where.append(f"target_name LIKE '%{_esc(target_name)}%'")

        adql = f"SELECT {columns} FROM {self.OBSCORE_TABLE}"
        if where:
            adql += " WHERE " + " AND ".join(where)

        if verbose:
            print(f"[ADQL] {adql}")
        return self.query(adql, maxrec=maxrec)

    def get_artifacts(self, observation_id: str) -> Table:
        """
        Return all file artifacts associated with an observation.

        Uses a two-step lookup: first resolves ``obs_publisher_did`` values from
        ``ivoa.ObsCore`` for the given ``obs_id``, then fetches the corresponding
        CAOM2 artifacts via ``caom2.Plane.publisherID``.

        Parameters
        ----------
        observation_id : str
            The ``obs_id`` value as returned by any query method.

        Returns
        -------
        `~astropy.table.Table`
            Columns: ``uri``, ``productType``, ``releaseType``,
            ``contentType``, ``contentLength``.
        """
        _empty = Table(names=["uri", "productType", "releaseType",
                               "contentType", "contentLength"])

        # Step 1: resolve ObsCore obs_publisher_did (= CAOM2 Plane.publisherID)
        pub_rows = self.query(
            f"SELECT obs_publisher_did "
            f"FROM {self.OBSCORE_TABLE} "
            f"WHERE obs_id = '{_esc(observation_id)}'"
        )
        if len(pub_rows) == 0:
            return _empty

        in_clause = ", ".join(
            f"'{_esc(str(r['obs_publisher_did']))}'" for r in pub_rows
        )

        # Step 2: fetch artifacts via CAOM2 Plane.publisherID
        adql = (
            f"SELECT a.uri, a.productType, a.releaseType, "
            f"       a.contentType, a.contentLength "
            f"FROM {self.PLANE_TABLE} AS p "
            f"JOIN {self.ART_TABLE}   AS a ON p.planeID = a.planeID "
            f"WHERE p.publisherID IN ({in_clause})"
        )
        return self.query(adql)

    # ── NL → ADQL ─────────────────────────────────────────────────────────────

    def nl_to_adql(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        chatserver_url: Optional[str] = None,
        ollama_url: Optional[str] = None,
    ) -> str:
        """
        Translate a natural-language question into an ADQL query for the
        CAOM2 data model.

        Two backends are supported:

        * **Direct Ollama** (default) — calls a local ``ollama serve`` instance.
        * **CHATSERVER** — routes through the CHATSERVER REST API, which
          applies a TAP-specific system prompt.  Pass *chatserver_url* or set
          ``conf.SRCNET_CHATSERVER_URL``.

        Parameters
        ----------
        text : str
            Plain-English question, e.g.
            ``"count observations per telescope"``.
        model : str, optional
            Model name (default: ``conf.SRCNET_OLLAMA_MODEL``).
        chatserver_url : str, optional
            CHATSERVER base URL.  If given (or set via
            ``conf.SRCNET_CHATSERVER_URL``), the CHATSERVER backend is used
            instead of direct Ollama.
        ollama_url : str, optional
            Override the Ollama base URL (default: ``conf.SRCNET_OLLAMA_URL``).

        Returns
        -------
        str
            ADQL query string ready to pass to :meth:`query`.

        Raises
        ------
        RuntimeError
            If the backend is unreachable.

        Examples
        --------
        >>> adql = DataDiscovery.nl_to_adql("how many observations per collection?")
        >>> print(adql)
        SELECT obs_collection, COUNT(*) AS n FROM ivoa.ObsCore GROUP BY obs_collection ORDER BY n DESC
        """
        from . import conf
        cs_url = chatserver_url or conf.SRCNET_CHATSERVER_URL or None

        if cs_url:
            explicit = _detect_tables(text, {'caom2', 'ivoa', 'tap_schema'})
            msg = (f"[explicit_tables: {', '.join(explicit)}] " + text) if explicit else text
            try:
                resp = requests.post(
                    f"{cs_url.rstrip('/')}/chat",
                    json={"message": msg},
                    timeout=120,
                )
                resp.raise_for_status()
            except requests.exceptions.ConnectionError:
                srcnet_raise(
                    RuntimeError(f"Could not reach CHATSERVER at {cs_url}."),
                    steps=f"tap.nl_to_adql({text!r})",
                )
            raw = resp.json()
            adql = raw.get("adql") or _extract_adql(raw.get("answer") or raw.get("response", ""))
            _check_adql_placeholders(adql)
            return adql

        # Direct Ollama backend
        _ollama = (ollama_url or conf.SRCNET_OLLAMA_URL).rstrip("/")
        _model = model or conf.SRCNET_OLLAMA_MODEL
        # Use replace() instead of .format() — the schema block may contain
        # literal curly braces (e.g. JSON examples) that would raise KeyError.
        prompt = _NL_TO_ADQL_PROMPT.replace("{question}", text)
        try:
            resp = requests.post(
                f"{_ollama}/api/generate",
                json={"model": _model, "prompt": prompt, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            srcnet_raise(
                RuntimeError(f"Could not reach Ollama at {_ollama}."),
                steps=f"tap.nl_to_adql({text!r})",
            )
        raw = resp.json().get("response", "")
        adql = _extract_adql(raw)
        _check_adql_placeholders(adql)
        return adql

    def query_natural(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        chatserver_url: Optional[str] = None,
        ollama_url: Optional[str] = None,
        maxrec: Optional[int] = None,
        verbose: bool = False,
    ) -> Tuple[str, Table]:
        """
        Translate *text* to ADQL, then execute it against the TAP service.

        Parameters
        ----------
        text : str
            Plain-English question.
        model : str, optional
            Model name (default: ``conf.SRCNET_OLLAMA_MODEL``).
        chatserver_url : str, optional
            Route through CHATSERVER instead of direct Ollama.
        ollama_url : str, optional
            Override the Ollama base URL.
        maxrec : int, optional
            Maximum rows to return.
        verbose : bool
            Print the generated ADQL before executing.

        Returns
        -------
        tuple[str, `~astropy.table.Table`]
            ``(adql, table)`` — the generated query and its results.

        Examples
        --------
        >>> adql, t = DataDiscovery.query_natural(
        ...     "show 5 recent JCMT observations", verbose=True
        ... )
        [ADQL] SELECT TOP 5 ...
        """
        adql = self.nl_to_adql(
            text, model=model, chatserver_url=chatserver_url, ollama_url=ollama_url
        )
        if verbose:
            print(f"[ADQL] {adql}")
        return adql, self.query(adql, maxrec=maxrec)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_tableset(content: bytes) -> list:
    """Parse a VOSI tableset XML into ``[{"name", "description"}]``.

    Namespace-agnostic (matches on local tag names) so it works regardless of the
    VODataService namespace prefix the service uses.
    """
    rows: list = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return rows
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "table":
            continue
        name = desc = ""
        for child in el:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "name":
                name = (child.text or "").strip()
            elif tag == "description":
                desc = (child.text or "").strip()
        if name:
            rows.append({"name": name, "description": desc})
    return rows


def _mount_retries(session: requests.Session) -> None:
    """Retry transient connection failures and 5xx responses on *session*.

    TAP ingresses (especially preprod) occasionally reset the TLS handshake
    (``SSL: UNEXPECTED_EOF_WHILE_READING``) or return a 5xx. ``connect`` retries
    cover the TLS resets; ``backoff_factor`` adds exponential spacing so a flaky
    ingress doesn't fail a query outright.
    """
    retry = Retry(
        total=6, connect=6, read=6, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def _patch_redirect_session(session: requests.Session, tap_url: str) -> None:
    """Rewrite localhost redirect URLs to the public TAP hostname.

    Some TAP services (e.g. YouCAT) are deployed behind a reverse proxy but
    return job-redirect URLs with the internal hostname (localhost).  We fix
    the Location header via a response hook, which fires before requests
    resolves the redirect.
    """
    public_netloc = urlparse(tap_url).netloc

    def _fix_location(response, **kwargs):
        location = response.headers.get("Location", "")
        if location:
            parsed = urlparse(location)
            if parsed.hostname in ("localhost", "127.0.0.1"):
                response.headers["Location"] = urlunparse(
                    parsed._replace(netloc=public_netloc)
                )

    session.hooks["response"].append(_fix_location)


def _esc(s: str) -> str:
    """Minimal ADQL string-literal escaping."""
    return s.replace("'", "''")


_TABLE_RE = re.compile(r'\b([a-zA-Z_]\w*\.[a-zA-Z_]\w*)\b')


def _detect_tables(text: str, known_schemas: set) -> list:
    """Return schema.table strings found in text whose schema is in known_schemas."""
    return [t for t in _TABLE_RE.findall(text) if t.split('.')[0].lower() in known_schemas]


def _extract_adql(text: str) -> str:
    """Strip markdown fences and return the bare ADQL from a model response."""
    # sql/adql fenced block
    block = re.search(r"```(?:sql|adql)\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if block:
        return _fix_adql(block.group(1).strip())
    # python fenced block — extract ADQL from inside tap.query("""...""") or bare SELECT
    block = re.search(r"```.*?```", text, re.DOTALL)
    if block:
        inner = block.group(0)
        # pull triple-quoted string content (the ADQL lives there)
        tq = re.search(r'"""(.*?)"""', inner, re.DOTALL)
        if tq:
            candidate = tq.group(1).strip()
            if candidate.upper().startswith("SELECT"):
                return _fix_adql(candidate)
        # fall back to first SELECT line inside the block
        for line in inner.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("SELECT"):
                return _fix_adql(stripped)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SELECT"):
            return _fix_adql(stripped)
    return _fix_adql(text.strip())


def _fix_adql(adql: str) -> str:
    """Convert SQL LIMIT N → ADQL TOP N (ADQL does not support LIMIT)."""
    m = re.search(r'\bLIMIT\s+(\d+)\s*;?\s*$', adql, re.IGNORECASE)
    if m:
        n = m.group(1)
        adql = adql[:m.start()].rstrip().rstrip(';')
        adql = re.sub(r'\bSELECT\b', f'SELECT TOP {n}', adql, count=1, flags=re.IGNORECASE)
    return adql


def _check_adql_placeholders(adql: str) -> None:
    """Raise ValueError if the ADQL contains unresolved <placeholder> tokens.

    The LLM sometimes emits spatial filters like CIRCLE('ICRS', <ra_deg>,
    <dec_deg>, <radius_deg>) when the question doesn't supply coordinates.
    These are syntactically invalid in ADQL and cause cryptic parser errors
    from the TAP service.
    """
    placeholders = re.findall(r'<([a-zA-Z_][a-zA-Z0-9_ ]*)>', adql)
    if placeholders:
        names = ", ".join(f"<{p}>" for p in placeholders)
        raise ValueError(
            f"Generated ADQL contains unresolved placeholder(s): {names}.\n"
            "The model added a spatial constraint but your question did not "
            "supply coordinates.  Add a sky position to your query (e.g. "
            "\"near RA 10.5 Dec -20.3 within 0.5 degrees\") or rephrase to "
            "omit the spatial filter.\n"
            f"Generated ADQL was:\n  {adql}"
        )


# ── Module-level singleton (astroquery convention) ────────────────────────────

DataDiscovery = DataDiscoveryClass()
