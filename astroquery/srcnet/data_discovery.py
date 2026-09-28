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

    # Typed shortcuts (no ADQL to write) -- any combination of filters
    filters = SearchFilters().add_filter("collection", "JCMT").add_filter("dataproduct_type", "image")
    results = DataDiscovery.search(filters)
    counts = DataDiscovery.count_by(["dataproduct_type"], filters)
    adql = DataDiscovery.explain(filters)  # what search() would run, unexecuted

Switch environment::

    from astroquery.srcnet import conf
    conf.SRCNET_ENVIRONMENT = "development"
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import pyvo
import requests
from astropy.coordinates import SkyCoord
from astropy.table import Table
import astropy.units as u
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ._helpdesk import srcnet_raise

__all__ = ["DataDiscovery", "DataDiscoveryClass", "SearchFilters"]


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


def _parse_obscore_columns(schema_text: str) -> frozenset:
    """Real ``ivoa.ObsCore`` column names, parsed out of the same schema block
    used for the NL prompt above -- so :meth:`SearchFilters.set_order`'s
    validation reflects the actual, live-introspected schema (see
    ``schemas.py``'s own header: auto-generated from a live TAP query), not a
    second, hand-maintained list that could drift from it.
    """
    columns: list = []
    in_block = False
    for line in schema_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ivoa.ObsCore") and not stripped.startswith("ivoa.ObsCore_radio"):
            in_block = True
            continue
        if in_block:
            if not stripped:
                break
            match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)", stripped)
            if match:
                columns.append(match.group(1))
    return frozenset(columns)


#: Every real column on ivoa.ObsCore -- valid targets for
#: :meth:`SearchFilters.set_order`.
_OBSCORE_COLUMNS = _parse_obscore_columns(_TAP_OBSCORE_SCHEMA)

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


# ── Shared filter object ───────────────────────────────────────────────────────

class SearchFilters:
    """
    Shared filter object for :meth:`DataDiscoveryClass.search`,
    :meth:`~DataDiscoveryClass.count_by` and :meth:`~DataDiscoveryClass.explain`.

    Construct empty and add filters incrementally with :meth:`add_filter`
    (chainable — each call returns *self*), rather than setting every field as
    a constructor keyword. Every field is optional and independent — add any
    combination, or none, and it applies equally to all three methods (they
    build the WHERE clause through the exact same internal helper, so they can
    never disagree on what a given filter set means).

    Examples
    --------
    >>> filters = (
    ...     SearchFilters()
    ...     .add_filter("collection", "JCMT")
    ...     .add_filter("dataproduct_type", "image")
    ...     .add_filter("obs_publisher_did", ["did:1", "did:2"])
    ...     .set_order("t_exptime", "DESC")
    ... )
    >>> filters = SearchFilters().set_position(SkyCoord(83.8, -5.4, unit="deg"), 0.5 * u.deg)

    Deliberately does not accept a ``project`` filter: no such column exists
    yet on Argus's ``ivoa.ObsCore`` (confirmed live —
    ``Column: [dataproduct_subtype] does not exist``, the field DaCHS used to
    hold it), and it's still an open question where "project" would even live
    in CAOM for SKA data. Add it once that's answered rather than guess.

    All string filters match exact, ignoring case (``UPPER(col) = UPPER(...)``)
    — narrower than :meth:`DataDiscoveryClass.query_observations`'s substring
    (``LIKE '%...%'``) matching. That's a deliberate choice for these *new*
    methods; it does not change ``query_observations``/``query_name`` or their
    existing callers.

    ``UPPER(col)`` *can* cost more than a plain ``col = 'value'`` comparison on
    a TAP service backed by a real database, since it prevents the query
    planner from using a plain index on *col* (production Argus currently
    holds zero rows, so this never shows up there). Tested against a populated
    Argus deployment (CADC's ``ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/argus``),
    paired timing runs gave contradictory results in both directions (single
    query pairs ranged from the ``UPPER()`` form being ~5x slower to it being
    faster) — response-time variance on that shared, third-party service
    turned out to be larger than whatever effect ``UPPER()`` has on its own,
    so treat this as an untested-but-plausible optimization for a given
    deployment, not a guaranteed speedup. Pass ``case_sensitive=True`` to
    :meth:`add_filter` on ``dataproduct_type``, ``target_name``,
    ``collection``, ``facility`` or ``instrument`` (the only fields
    ``UPPER()``-wrapped by default) to opt out and get a plain
    ``col = 'value'`` comparison instead, when you know the value in your data
    is consistently cased (e.g. collection codes like ``"HST"``/``"JCMT"``
    usually are) and want to rule ``UPPER()`` out as a cost on your own
    deployment.

    Filter fields (each set via ``add_filter(field, value)``)
    ------------------------------------------------------------
    position : ``(coordinates, radius)`` tuple, or use :meth:`set_position`
        Cone search — ``coordinates`` an `~astropy.coordinates.SkyCoord`,
        ``radius`` an `~astropy.units.Quantity`, e.g. ``0.5 * u.deg``.
    obs_publisher_did : list of str
        Exact-match publisher DIDs (``IN (...)``).
    dataproduct_type : str
        ``""`` = match a blank/NULL ``dataproduct_type``; never set = no
        filter; anything else = exact match, ignoring case.
    target_name : str
        Exact match, ignoring case.
    collection : str
        Exact match on ``obs_collection``, ignoring case.
    facility : str
        Exact match on ``facility_name``, ignoring case.
    instrument : str
        Exact match on ``instrument_name``, ignoring case.
    namespace : str
        Rucio DID namespace, matched as a prefix on ``obs_id``
        (``obs_id LIKE 'namespace:%'``) — the same convention-dependent split
        the Gateway itself relies on today, not a real Argus column. Nothing
        in the ObsCore standard requires ``obs_id`` to encode this.
    filename : str
        Rucio DID filename, matched as a suffix on ``obs_id``
        (``obs_id LIKE '%:filename'``) — same caveat as *namespace*.
    """

    #: Fields settable via :meth:`add_filter`. ``"position"`` takes a
    #: ``(coordinates, radius)`` tuple; every other one takes a plain value.
    FIELDS = frozenset({
        "position", "obs_publisher_did", "dataproduct_type", "target_name",
        "collection", "facility", "instrument", "namespace", "filename",
    })

    #: The only fields ``UPPER()``-wrapped by default (i.e. the only ones
    #: ``case_sensitive`` on :meth:`add_filter` has any effect on).
    #: ``obs_publisher_did`` (``IN (...)``) and ``namespace``/``filename``
    #: (``LIKE``, no ``UPPER()``) are already case-sensitive; ``position``
    #: isn't a string comparison at all.
    CASE_INSENSITIVE_FIELDS = frozenset({
        "dataproduct_type", "target_name", "collection", "facility", "instrument",
    })

    def __init__(self) -> None:
        self._position: Optional[Tuple[SkyCoord, u.Quantity]] = None
        self._obs_publisher_did: Optional[List[str]] = None
        self._values: Dict[str, str] = {}
        self._case_sensitive: Dict[str, bool] = {}
        self._order: Optional[Tuple[str, str]] = None

    def add_filter(self, field: str, value, *, case_sensitive: bool = False) -> "SearchFilters":
        """
        Set one filter. Returns *self*, so calls chain.

        Parameters
        ----------
        field : str
            One of :attr:`FIELDS` — see the class docstring for what each
            one matches.
        value :
            ``(coordinates, radius)`` for ``"position"``; a list of str for
            ``"obs_publisher_did"``; a plain str for everything else.
        case_sensitive : bool, optional
            Only meaningful for :attr:`CASE_INSENSITIVE_FIELDS` (``dataproduct_type``,
            ``target_name``, ``collection``, ``facility``, ``instrument``), which
            default to a case-insensitive ``UPPER(col) = UPPER('value')`` match.
            Pass ``True`` to get a plain ``col = 'value'`` comparison instead,
            which may be faster on a TAP service backed by a real database
            (``UPPER(col)`` can prevent using a plain index on *col* — see the
            class docstring for what live testing against a populated Argus
            deployment did and didn't confirm about this), at the cost of no
            longer matching a differently-cased value.

        Raises
        ------
        ValueError
            If *field* isn't one of :attr:`FIELDS` — deliberately strict:
            a builder that silently no-ops on a typo'd field name (``"colection"``
            for ``"collection"``) would produce a query that quietly matches
            more than intended, which is worse than failing loudly. Also raised
            if ``case_sensitive`` is passed for a field it has no effect on —
            it's not a filter in its own right, so silently accepting it there
            would misleadingly suggest it did something.
        """
        if case_sensitive and field not in self.CASE_INSENSITIVE_FIELDS:
            raise ValueError(
                f"case_sensitive only applies to {', '.join(sorted(self.CASE_INSENSITIVE_FIELDS))}; "
                f"{field!r} has no case-insensitive default to opt out of"
            )
        if field == "position":
            coordinates, radius = value
            self._position = (coordinates, radius)
        elif field == "obs_publisher_did":
            self._obs_publisher_did = list(value)
        elif field in self.FIELDS:
            self._values[field] = value
            if field in self.CASE_INSENSITIVE_FIELDS:
                self._case_sensitive[field] = case_sensitive
        else:
            raise ValueError(f"unknown filter field {field!r}; valid fields: {', '.join(sorted(self.FIELDS))}")
        return self

    def is_case_sensitive(self, field: str) -> bool:
        """Whether *field* was last set with ``case_sensitive=True``. Only
        meaningful for :attr:`CASE_INSENSITIVE_FIELDS`; ``False`` for anything
        else (including a field that was never set)."""
        return self._case_sensitive.get(field, False)

    def set_position(self, coordinates: SkyCoord, radius: u.Quantity) -> "SearchFilters":
        """Convenience for ``add_filter("position", (coordinates, radius))``. Returns *self*."""
        return self.add_filter("position", (coordinates, radius))

    def set_order(self, field: str, direction: str = "ASC") -> "SearchFilters":
        """
        Order results by *field* instead of the default ``obs_publisher_did``.
        Returns *self*, so this chains with :meth:`add_filter` too.

        Combinable with :meth:`DataDiscoveryClass.search`'s keyset pagination:
        confirmed live against Argus that the tie-breaking form this needs —
        ``field > :v OR (field = :v AND obs_publisher_did > :did)`` — works,
        even though the more compact row-value form
        (``(field, obs_publisher_did) > (:v, :did)``) does not (rejected as
        an ADQL syntax error). ``obs_publisher_did`` is always added as a
        secondary sort key, so ordering stays deterministic even when *field*
        has duplicate values across rows.

        Parameters
        ----------
        field : str
            Any real ``ivoa.ObsCore`` column name.
        direction : str, optional
            ``"ASC"`` (default) or ``"DESC"``.

        Raises
        ------
        ValueError
            If *field* isn't a real ``ivoa.ObsCore`` column, or *direction*
            isn't ``"ASC"``/``"DESC"``.
        """
        direction = direction.upper()
        if direction not in ("ASC", "DESC"):
            raise ValueError(f"direction must be 'ASC' or 'DESC', got {direction!r}")
        if field not in _OBSCORE_COLUMNS:
            raise ValueError(f"unknown ivoa.ObsCore column {field!r} for set_order")
        self._order = (field, direction)
        return self

    # ── Read-only views used internally by _build_where/_search_adql/search() ──
    # (kept as properties, not a public dict, so that internal code is unchanged
    # from the dataclass-attribute version this replaces.)

    @property
    def coordinates(self) -> Optional[SkyCoord]:
        return self._position[0] if self._position else None

    @property
    def radius(self) -> Optional[u.Quantity]:
        return self._position[1] if self._position else None

    @property
    def obs_publisher_did(self) -> Optional[List[str]]:
        return self._obs_publisher_did

    @property
    def dataproduct_type(self) -> Optional[str]:
        return self._values.get("dataproduct_type")

    @property
    def target_name(self) -> Optional[str]:
        return self._values.get("target_name")

    @property
    def collection(self) -> Optional[str]:
        return self._values.get("collection")

    @property
    def facility(self) -> Optional[str]:
        return self._values.get("facility")

    @property
    def instrument(self) -> Optional[str]:
        return self._values.get("instrument")

    @property
    def namespace(self) -> Optional[str]:
        return self._values.get("namespace")

    @property
    def filename(self) -> Optional[str]:
        return self._values.get("filename")

    @property
    def order(self) -> Optional[Tuple[str, str]]:
        """``(field, direction)`` set via :meth:`set_order`, or ``None``."""
        return self._order


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

    #: Default columns for :meth:`search` — the 13-column set the Gateway
    #: selects today (MAN-827 §1/§2), minus ``dataproduct_subtype``: confirmed
    #: live against Argus that it does not exist on ``ivoa.ObsCore``
    #: (``validateColumnNonAlias: Column: [dataproduct_subtype] does not
    #: exist``) — DaCHS used it to hold the project name; Argus has no
    #: equivalent yet (see :class:`SearchFilters`).
    DEFAULT_SEARCH_COLUMNS = (
        "obs_publisher_did, target_name, obs_id, dataproduct_type, calib_level, "
        "obs_collection, access_url, access_format, facility_name, "
        "instrument_name, s_ra, s_dec"
    )

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

    # ── Typed shortcuts (MAN-827 §6) ──────────────────────────────────────────
    #
    # search/count_by/explain share one filter object (SearchFilters) and one
    # WHERE-clause builder (_build_where), so "what would this filter set
    # match" can never drift between what search() executes and what
    # explain() shows. Three real Argus ADQL-dialect limits, each confirmed
    # live against the real service (not assumed from MAN-827's own
    # description of the *current*, DaCHS-backed Gateway), shape all of this:
    #
    #   1. OFFSET is rejected outright ("invalid ADQL keyword: LIMIT") --
    #      MAN-827's own page/page_size contract (TOP + OFFSET, matching what
    #      DaCHS accepts today) does not carry over to Argus. search() uses
    #      keyset pagination on obs_publisher_did instead (see its docstring).
    #   2. Sub-selects in FROM are rejected outright ("sub-select not
    #      supported in FROM clause") -- the current Gateway's own
    #      SELECT COUNT(*) FROM (...) trick for counting a free-form query
    #      does not work here either. count_adql() executes and measures
    #      instead of wrapping (see its docstring).
    #   3. DISTANCE() is rejected outright ("DISTANCE not supported"), even
    #      for two literal points -- so a position filter can't compute
    #      angular_separation or sort "nearest first" the way
    #      query_region()/the current Gateway do. search() falls back to no
    #      further paging for a position search rather than claim an
    #      ordering it can't produce.

    def _build_where(self, filters: Optional[SearchFilters]) -> List[str]:
        """ANDed WHERE conditions for *filters* — shared by :meth:`search`,
        :meth:`count_by` and :meth:`explain`."""
        if filters is None:
            return []
        where: List[str] = []

        if filters.coordinates is not None and filters.radius is not None:
            ra = filters.coordinates.icrs.ra.deg
            dec = filters.coordinates.icrs.dec.deg
            r = filters.radius.to(u.deg).value
            # s_region, not s_ra/s_dec: s_region is indexed on Argus, s_ra/s_dec
            # are not (MAN-827 §2). CONTAINS(s_region, ...) parses and executes
            # fine against Argus today (confirmed live) even though the table
            # is currently empty, so this can't yet be verified against real
            # rows -- query_region() above is left on s_ra/s_dec so it keeps
            # behaving exactly as before.
            where.append(f"CONTAINS(s_region, CIRCLE('ICRS', {ra}, {dec}, {r})) = 1")

        if filters.obs_publisher_did:
            in_list = ", ".join(f"'{_esc(d)}'" for d in filters.obs_publisher_did)
            where.append(f"obs_publisher_did IN ({in_list})")

        if filters.dataproduct_type is not None:
            if filters.dataproduct_type == "":
                where.append("(dataproduct_type IS NULL OR dataproduct_type = '')")
            else:
                where.append(_exact_match_where(
                    "dataproduct_type", filters.dataproduct_type,
                    filters.is_case_sensitive("dataproduct_type"),
                ))

        if filters.target_name:
            where.append(_exact_match_where(
                "target_name", filters.target_name, filters.is_case_sensitive("target_name"),
            ))
        if filters.collection:
            where.append(_exact_match_where(
                "obs_collection", filters.collection, filters.is_case_sensitive("collection"),
            ))
        if filters.facility:
            where.append(_exact_match_where(
                "facility_name", filters.facility, filters.is_case_sensitive("facility"),
            ))
        if filters.instrument:
            where.append(_exact_match_where(
                "instrument_name", filters.instrument, filters.is_case_sensitive("instrument"),
            ))
        if filters.namespace:
            where.append(f"obs_id LIKE '{_esc(filters.namespace)}:%'")
        if filters.filename:
            where.append(f"obs_id LIKE '%:{_esc(filters.filename)}'")

        return where

    def _has_position(self, filters: Optional[SearchFilters]) -> bool:
        return filters is not None and filters.coordinates is not None and filters.radius is not None

    def _columns_with_order(self, columns: str, filters: Optional[SearchFilters]) -> str:
        """Make sure a custom :meth:`~SearchFilters.set_order` field is present
        in the SELECT list — needed both to read the cursor value back out of
        the result for ``next_after``, and so ``ORDER BY`` never silently
        sorts by a column the caller can't see in what came back. Shared by
        :meth:`search` and :meth:`explain` so what the latter shows always
        matches what the former actually selects."""
        if filters is None or filters.order is None:
            return columns
        field = filters.order[0]
        existing = {c.strip() for c in columns.split(",")}
        if field in existing:
            return columns
        return f"{columns}, {field}"

    def _search_adql(
        self,
        filters: Optional[SearchFilters],
        columns: str,
        after: Optional[str],
        top_n: Optional[int],
    ) -> str:
        """The ADQL both :meth:`search` and :meth:`explain` build — one
        function, so they can never disagree with each other.

        ``top_n=None`` means unbounded: no ``TOP`` clause at all (the caller
        relies on ``maxrec`` alone, same as :meth:`query`/:meth:`execute_adql`)
        — *after* is meaningless without a page boundary, so it's ignored in
        that case rather than silently building a WHERE clause nothing will
        ever page through.

        A position filter drops ordering entirely (see :meth:`search`'s
        docstring on why). Otherwise: no :meth:`SearchFilters.set_order` means
        the existing ``ORDER BY obs_publisher_did ASC`` default, unchanged; a
        custom order adds ``obs_publisher_did`` as a secondary sort key for
        determinism, and — when paginating (*after* given, *top_n* not
        ``None``) — needs the cursor's *order_value* half (see
        :func:`_decode_cursor`) to build a tie-breaking condition, since
        ``obs_publisher_did`` alone can no longer bound "everything after this
        row" once the primary sort is on a different, possibly-repeated field.
        """
        where = self._build_where(filters)
        has_position = self._has_position(filters)
        order = None if filters is None else filters.order
        paginating = top_n is not None

        if has_position:
            order_by = None
        elif order is not None:
            field, direction = order
            order_by = f"{field} {direction}, obs_publisher_did ASC"
            if after and paginating:
                order_value, did_value = _decode_cursor(after)
                cmp_op = ">" if direction == "ASC" else "<"
                literal = _sql_literal(order_value)
                where = where + [
                    f"({field} {cmp_op} {literal} OR "
                    f"({field} = {literal} AND obs_publisher_did > '{_esc(did_value)}'))"
                ]
        else:
            order_by = "obs_publisher_did ASC"
            if after and paginating:
                where = where + [f"obs_publisher_did > '{_esc(after)}'"]

        select = f"SELECT {columns}" if top_n is None else f"SELECT TOP {top_n} {columns}"
        adql = f"{select} FROM {self.OBSCORE_TABLE}"
        if where:
            adql += " WHERE " + " AND ".join(where)
        if order_by:
            adql += f" ORDER BY {order_by}"
        return adql

    def search(
        self,
        filters: Optional[SearchFilters] = None,
        *,
        columns: Optional[str] = None,
        after: Optional[str] = None,
        page_size: Optional[int] = 100,
        split_obs_id: bool = True,
        with_total_count: bool = False,
        verbose: bool = False,
    ) -> Table:
        """
        One shortcut covering every combination of the Gateway's filters
        (MAN-827 §5 requirement 1) — position, collection, facility,
        instrument, target name, data-product type, a publisher-DID list, and
        Rucio namespace/filename, all ANDed, any subset set or none.

        Pagination is keyset-based (*after* / ``obs_publisher_did``), **not**
        the ``page``/``page_size`` OFFSET scheme MAN-827 proposes — confirmed
        live against Argus that ``OFFSET`` is rejected outright
        (``invalid ADQL keyword: LIMIT``), so page-N pagination the way the
        current DaCHS-backed Gateway does it isn't possible against this
        service today. Pass the previous call's ``table.meta["next_after"]``
        back in as *after* to get the next page; it's ``None`` once there are
        no more rows.

        A position filter (*filters.coordinates* set) drops the keyset order:
        ``angular_separation``/"nearest first" isn't available either —
        ``DISTANCE()`` is rejected server-side (confirmed live, even for two
        literal points: ``DISTANCE not supported``) — so a position search
        just returns up to *page_size* rows with no further pages, rather
        than claim an ordering this service can't produce.

        Pagination is opt-in, not the only mode: pass ``page_size=None`` for
        an unbounded search — no ``TOP`` at all, just ``maxrec`` as a plain
        safety cap (:attr:`~astroquery.srcnet.Conf.SRCNET_DEFAULT_MAXREC`,
        the same default :meth:`query`/:meth:`execute_adql` use), with no
        pagination bookkeeping at all (``next_after`` is always ``None``, and
        *after* is ignored — there's no page boundary for it to mean anything
        against). Reach for this when you just want everything matching the
        filters and don't care about paging through a UI-sized page at a
        time — the default ``page_size=100`` exists for the latter case
        (MAN-827's own contract for this shortcut is literally "a page of
        rows"), not because every caller needs pagination.

        A custom order (:meth:`SearchFilters.set_order`) works alongside
        keyset pagination — see that method's docstring for exactly how; the
        cursor in ``next_after`` becomes an opaque string carrying both the
        order field's value and the ``obs_publisher_did`` tie-breaker, still
        just a string to pass back in as *after*.

        Parameters
        ----------
        filters : SearchFilters, optional
            Shared filter object — see :class:`SearchFilters`. ``None`` (the
            default) searches everything.
        columns : str, optional
            ADQL column list. Defaults to :attr:`DEFAULT_SEARCH_COLUMNS`.
        after : str, optional
            Keyset cursor — see above. Ignored when *page_size* is ``None``.
        page_size : int or None, optional
            Max rows this call returns. ``None`` = unbounded (see above).
        split_obs_id : bool, optional
            Also add ``namespace``/``filename`` columns, split from ``obs_id``
            on the first ``:`` — the same convention-dependent split the
            Gateway's own code does today (MAN-827 §3 finding 3); Argus does
            not expose these as native columns yet.
        with_total_count : bool, optional
            Also run a second, filter-scoped ``COUNT(*)`` (no sub-select — see
            :meth:`count_adql`'s docstring for why that matters here) and
            stash it in ``table.meta["total_count"]``. Off by default: MAN-827
            itself notes the Gateway already treats counting as a separate
            call from paging, so a slow count never blocks the first page.
        verbose : bool, optional
            Print the generated ADQL before executing.

        Returns
        -------
        `~astropy.table.Table`
            ``table.meta["next_after"]`` is the cursor for the next page, or
            ``None`` if this was the last one (always ``None`` when
            *page_size* is ``None``). ``table.meta["total_count"]`` is
            present only when *with_total_count* is true.

        Examples
        --------
        >>> filters = SearchFilters().add_filter("collection", "JCMT").add_filter("dataproduct_type", "image")
        >>> page1 = DataDiscovery.search(filters, page_size=50)
        >>> page2 = DataDiscovery.search(filters, page_size=50, after=page1.meta["next_after"])
        >>> everything = DataDiscovery.search(filters, page_size=None)  # unbounded
        """
        columns = self._columns_with_order(columns or self.DEFAULT_SEARCH_COLUMNS, filters)
        has_position = self._has_position(filters)
        order = None if filters is None else filters.order
        unbounded = page_size is None
        # Fetch one extra row to learn whether another page exists, without a
        # second round trip or an OFFSET this service doesn't support; trimmed
        # back to page_size before returning. Only for the keyset-paginated
        # case: a position search never exposes a next page (next_after is
        # forced to None below regardless -- see the class docstring), so
        # asking for one more row there would just fetch something we always
        # throw away -- likewise unbounded mode has no "next page" to detect
        # at all. fetch_n is also what explain() must show for parity -- it
        # takes the same has_position/unbounded-dependent value, so what
        # explain() displays and what search() actually runs never diverge.
        fetch_n = None if unbounded else (page_size if has_position else page_size + 1)
        adql = self._search_adql(filters, columns, after, fetch_n)

        if verbose:
            print(f"[ADQL] {adql}")

        table = self.query(adql, maxrec=fetch_n)

        has_more = (not unbounded) and (not has_position) and len(table) > page_size
        if has_more:
            table = table[:page_size]

        if split_obs_id and "obs_id" in table.colnames:
            _add_namespace_filename_columns(table)

        if has_more and len(table) and "obs_publisher_did" in table.colnames:
            if order is not None and order[0] in table.colnames:
                table.meta["next_after"] = _encode_cursor(table[order[0]][-1], table["obs_publisher_did"][-1])
            else:
                table.meta["next_after"] = str(table["obs_publisher_did"][-1])
        else:
            table.meta["next_after"] = None

        if with_total_count:
            count_where = self._build_where(filters)
            count_adql = f"SELECT COUNT(*) AS num_records FROM {self.OBSCORE_TABLE}"
            if count_where:
                count_adql += " WHERE " + " AND ".join(count_where)
            count_table = self.query(count_adql, maxrec=1)
            table.meta["total_count"] = int(count_table["num_records"][0]) if len(count_table) else 0

        return table

    def count_by(
        self,
        group_by: Optional[List[str]] = None,
        filters: Optional[SearchFilters] = None,
        *,
        verbose: bool = False,
    ) -> Table:
        """
        Row counts grouped by one or more fields, for the same filter set
        :meth:`search` accepts (MAN-827 §5 requirement 3 — the
        search-catalogue type tabs and ADQL template 3).

        Parameters
        ----------
        group_by : list of str, optional
            ObsCore column names to group by. Defaults to
            ``["dataproduct_type"]``, matching MAN-827's stated default.
        filters : SearchFilters, optional
            Same filter object :meth:`search` takes.
        verbose : bool, optional
            Print the generated ADQL before executing.

        Returns
        -------
        `~astropy.table.Table`
            One row per group, plus ``num_records``.

        Examples
        --------
        >>> DataDiscovery.count_by(["dataproduct_type", "facility_name"])
        """
        group_by = group_by or ["dataproduct_type"]
        cols = ", ".join(group_by)
        where = self._build_where(filters)

        adql = f"SELECT {cols}, COUNT(*) AS num_records FROM {self.OBSCORE_TABLE}"
        if where:
            adql += " WHERE " + " AND ".join(where)
        adql += f" GROUP BY {cols} ORDER BY num_records DESC"

        if verbose:
            print(f"[ADQL] {adql}")
        return self.query(adql, maxrec=500)

    def explain(
        self,
        filters: Optional[SearchFilters] = None,
        *,
        columns: Optional[str] = None,
        after: Optional[str] = None,
        page_size: Optional[int] = 100,
    ) -> str:
        """
        Return the ADQL :meth:`search` would run for *filters*, without
        executing it (MAN-827 §5 requirement 5 — the "show query" modal).

        Built through the exact same :meth:`_search_adql` helper
        :meth:`search` uses, so this can never drift out of sync with what
        ``search(filters)`` actually does. The one difference: for a
        keyset-paginated (non-position) search this shows ``TOP page_size``,
        not the ``page_size + 1`` :meth:`search` fetches internally to detect
        whether another page exists — that's an implementation detail of
        pagination, not something a user editing this ADQL in a "show query"
        modal should see. Pass ``page_size=None`` to see the unbounded form
        (no ``TOP`` at all) that ``search(filters, page_size=None)`` runs.

        Parameters
        ----------
        filters : SearchFilters, optional
            Same filter object :meth:`search` takes.
        columns : str, optional
            Same as :meth:`search`.
        after : str, optional
            Same as :meth:`search`.
        page_size : int or None, optional
            Same as :meth:`search` — shown here as the real ``TOP`` value
            (or no ``TOP`` at all when ``None``).

        Returns
        -------
        str

        Examples
        --------
        >>> DataDiscovery.explain(SearchFilters().add_filter("collection", "JCMT"))
        "SELECT ... FROM ivoa.ObsCore WHERE UPPER(obs_collection) = UPPER('JCMT') ORDER BY obs_publisher_did ASC"
        """
        columns = self._columns_with_order(columns or self.DEFAULT_SEARCH_COLUMNS, filters)
        return self._search_adql(filters, columns, after, page_size)

    def execute_adql(
        self,
        adql: str,
        *,
        max_rows: Optional[int] = None,
        verbose: bool = False,
    ) -> Table:
        """
        Run free-form ADQL (MAN-827's Q5 / the ADQL tab), capped at
        *max_rows*.

        No ``page``/``page_size`` OFFSET parameter: confirmed live against
        Argus that ``OFFSET`` is rejected outright (``invalid ADQL keyword:
        LIMIT``), so page-N pagination over arbitrary free-form ADQL isn't
        possible against this service today — and unlike :meth:`search`,
        there's no ``obs_publisher_did``-style keyset fallback available
        here either, because an arbitrary caller-supplied query has no
        column astroquery can assume is present, sortable, or unique.

        Parameters
        ----------
        adql : str
            ADQL query string. If it already has its own ``TOP``, that wins;
            *max_rows* only caps rows via ``maxrec`` on top of whatever the
            query itself returns.
        max_rows : int, optional
            Row cap, via TAP's own ``maxrec``. Defaults to
            :attr:`~astroquery.srcnet.Conf.SRCNET_DEFAULT_MAXREC` (same
            default :meth:`query` uses).
        verbose : bool, optional
            Print *adql* before executing.

        Returns
        -------
        `~astropy.table.Table`

        Examples
        --------
        >>> DataDiscovery.execute_adql("SELECT TOP 10 * FROM ivoa.ObsCore", max_rows=10)
        """
        if verbose:
            print(f"[ADQL] {adql}")
        return self.query(adql, maxrec=max_rows)

    def count_adql(
        self,
        adql: str,
        *,
        max_rows: Optional[int] = None,
        verbose: bool = False,
    ) -> Table:
        """
        Row count for a free-form ADQL query (MAN-827's Q3 / the ADQL tab's
        total), capped at *max_rows* — matching MAN-827's own stated
        semantics for this query type ("One row: num_records, capped at the
        query's TOP"), not an unbounded ``COUNT(*)``.

        Deliberately does **not** wrap *adql* in ``SELECT COUNT(*) FROM
        (...)``, the way the current DaCHS-backed Gateway does this today:
        confirmed live against Argus that sub-selects in the ``FROM`` clause
        are rejected outright (``sub-select not supported in FROM clause``).
        Rewriting an arbitrary caller's ``SELECT`` clause via string surgery
        to work around that would reintroduce exactly the ADQL-built-by-
        string-interpolation risk MAN-827 itself calls out (§4) — and would
        still silently give the wrong answer for any query with its own
        ``GROUP BY``, where ``COUNT(*)`` over the original column list
        doesn't mean "how many result rows". Executing the query and
        measuring the real row count sidesteps both problems, and is exactly
        the "capped at TOP" semantics MAN-827 describes — just arrived at by
        running the query instead of wrapping it.

        Parameters
        ----------
        adql : str
            ADQL query string.
        max_rows : int, optional
            Row cap passed to :meth:`execute_adql`.
        verbose : bool, optional
            Print *adql* before executing.

        Returns
        -------
        `~astropy.table.Table`
            One row: ``num_records``.

        Examples
        --------
        >>> DataDiscovery.count_adql("SELECT * FROM ivoa.ObsCore WHERE dataproduct_type = 'image'")
        """
        table = self.execute_adql(adql, max_rows=max_rows, verbose=verbose)
        return Table(rows=[{"num_records": len(table)}])

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


def _exact_match_where(column: str, value: str, case_sensitive: bool) -> str:
    """``column = 'value'`` (case-sensitive) or ``UPPER(column) = UPPER('value')``
    (default) — the latter can cost more against a TAP service backed by a
    real database, since ``UPPER(col)`` can't use a plain index on *col*, but
    see :class:`SearchFilters` for what live testing did and didn't confirm
    about this."""
    if case_sensitive:
        return f"{column} = '{_esc(value)}'"
    return f"UPPER({column}) = UPPER('{_esc(value)}')"


def _sql_literal(value) -> str:
    """ADQL literal for *value* -- quoted only if it's actually a string.
    Used for the custom-order keyset tie-break in :meth:`DataDiscoveryClass._search_adql`,
    where the column's real type (numeric vs char) has to be respected or the
    comparison is either a syntax error or silently wrong."""
    if isinstance(value, str):
        return f"'{_esc(value)}'"
    return repr(float(value))


#: Separator for the composite (order_value, obs_publisher_did) cursor a
#: custom-ordered search() page needs -- U+001F (unit separator), chosen
#: because it can't appear in a DID or a real column value typed at a keyboard.
_CURSOR_SEP = "\x1f"


def _encode_cursor(order_value, did_value) -> str:
    """Opaque keyset cursor carrying both a custom order field's last value
    and the obs_publisher_did tie-breaker -- needed once :meth:`SearchFilters.set_order`
    is in play, since obs_publisher_did alone no longer determines row order.
    The plain (no custom order) case still uses a bare obs_publisher_did
    string as its cursor, unchanged -- this encoding is only used when there's
    a second value to carry."""
    tag = "s" if isinstance(order_value, str) else "n"
    return f"{tag}{_CURSOR_SEP}{order_value}{_CURSOR_SEP}{did_value}"


def _decode_cursor(cursor: str) -> Tuple[object, str]:
    """Inverse of :func:`_encode_cursor` -- returns ``(order_value, did_value)``."""
    tag, order_repr, did_value = cursor.split(_CURSOR_SEP, 2)
    order_value = order_repr if tag == "s" else float(order_repr)
    return order_value, did_value


def _add_namespace_filename_columns(table: Table) -> None:
    """Best-effort split of ``obs_id`` into ``namespace``/``filename`` columns,
    in place — mirrors the Gateway's own ``extract_filenames_and_namespaces``
    fallback (MAN-827 §3 finding 3: nothing in the ObsCore standard requires
    ``obs_id`` to encode ``namespace:filename``; this is only as reliable as
    that convention holds for a given row). A row with no ``:`` gets an empty
    ``namespace`` and the whole ``obs_id`` as ``filename``, same as the
    Gateway's own fallback for a missing separator.
    """
    namespaces, filenames = [], []
    for obs_id in table["obs_id"]:
        text = str(obs_id)
        if ":" in text:
            ns, _, fn = text.partition(":")
        else:
            ns, fn = "", text
        namespaces.append(ns)
        filenames.append(fn)
    table["namespace"] = namespaces
    table["filename"] = filenames


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
