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
    filters = SearchFilters(collection="JCMT", dataproduct_type="image")
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
from dataclasses import dataclass
from typing import List, Optional, Tuple
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

@dataclass
class SearchFilters:
    """
    Shared filter object for :meth:`DataDiscoveryClass.search`,
    :meth:`~DataDiscoveryClass.count_by` and :meth:`~DataDiscoveryClass.explain`.

    Every field is optional and independent — set any combination, or none, and
    it applies equally to all three methods (they build the WHERE clause
    through the exact same internal helper, so they can never disagree on what
    a given filter set means).

    Deliberately does not include a ``project`` field: no such column exists
    yet on Argus's ``ivoa.ObsCore`` (confirmed live —
    ``Column: [dataproduct_subtype] does not exist``, the field DaCHS used to
    hold it), and it's still an open question where "project" would even live
    in CAOM for SKA data. Add it once that's answered rather than guess.

    All string filters match exact, ignoring case (``UPPER(col) = UPPER(...)``)
    — narrower than :meth:`DataDiscoveryClass.query_observations`'s substring
    (``LIKE '%...%'``) matching. That's a deliberate choice for these *new*
    methods; it does not change ``query_observations``/``query_name`` or their
    existing callers.

    Attributes
    ----------
    coordinates : `~astropy.coordinates.SkyCoord`, optional
        Cone-search centre (ICRS). Requires *radius* too.
    radius : `~astropy.units.Quantity`, optional
        Cone-search radius, e.g. ``0.5 * u.deg``.
    obs_publisher_did : list of str, optional
        Exact-match publisher DIDs (``IN (...)``).
    dataproduct_type : str, optional
        ``None`` = no filter. ``""`` = match a blank/NULL ``dataproduct_type``.
        Anything else = exact match, ignoring case.
    target_name : str, optional
        Exact match, ignoring case.
    collection : str, optional
        Exact match on ``obs_collection``, ignoring case.
    facility : str, optional
        Exact match on ``facility_name``, ignoring case.
    instrument : str, optional
        Exact match on ``instrument_name``, ignoring case.
    namespace : str, optional
        Rucio DID namespace, matched as a prefix on ``obs_id``
        (``obs_id LIKE 'namespace:%'``) — the same convention-dependent split
        the Gateway itself relies on today, not a real Argus column. Nothing
        in the ObsCore standard requires ``obs_id`` to encode this.
    filename : str, optional
        Rucio DID filename, matched as a suffix on ``obs_id``
        (``obs_id LIKE '%:filename'``) — same caveat as *namespace*.
    """

    coordinates: Optional[SkyCoord] = None
    radius: Optional[u.Quantity] = None
    obs_publisher_did: Optional[List[str]] = None
    dataproduct_type: Optional[str] = None
    target_name: Optional[str] = None
    collection: Optional[str] = None
    facility: Optional[str] = None
    instrument: Optional[str] = None
    namespace: Optional[str] = None
    filename: Optional[str] = None


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
                where.append(f"UPPER(dataproduct_type) = UPPER('{_esc(filters.dataproduct_type)}')")

        if filters.target_name:
            where.append(f"UPPER(target_name) = UPPER('{_esc(filters.target_name)}')")
        if filters.collection:
            where.append(f"UPPER(obs_collection) = UPPER('{_esc(filters.collection)}')")
        if filters.facility:
            where.append(f"UPPER(facility_name) = UPPER('{_esc(filters.facility)}')")
        if filters.instrument:
            where.append(f"UPPER(instrument_name) = UPPER('{_esc(filters.instrument)}')")
        if filters.namespace:
            where.append(f"obs_id LIKE '{_esc(filters.namespace)}:%'")
        if filters.filename:
            where.append(f"obs_id LIKE '%:{_esc(filters.filename)}'")

        return where

    def _has_position(self, filters: Optional[SearchFilters]) -> bool:
        return filters is not None and filters.coordinates is not None and filters.radius is not None

    def _search_adql(
        self,
        filters: Optional[SearchFilters],
        columns: str,
        after: Optional[str],
        top_n: int,
    ) -> str:
        """The ADQL both :meth:`search` and :meth:`explain` build — one
        function, so they can never disagree with each other."""
        where = self._build_where(filters)
        if after and not self._has_position(filters):
            where = where + [f"obs_publisher_did > '{_esc(after)}'"]

        adql = f"SELECT TOP {top_n} {columns} FROM {self.OBSCORE_TABLE}"
        if where:
            adql += " WHERE " + " AND ".join(where)
        if not self._has_position(filters):
            adql += " ORDER BY obs_publisher_did"
        return adql

    def search(
        self,
        filters: Optional[SearchFilters] = None,
        *,
        columns: Optional[str] = None,
        after: Optional[str] = None,
        page_size: int = 100,
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

        Parameters
        ----------
        filters : SearchFilters, optional
            Shared filter object — see :class:`SearchFilters`. ``None`` (the
            default) searches everything.
        columns : str, optional
            ADQL column list. Defaults to :attr:`DEFAULT_SEARCH_COLUMNS`.
        after : str, optional
            Keyset cursor — see above.
        page_size : int, optional
            Max rows this call returns.
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
            ``None`` if this was the last one. ``table.meta["total_count"]``
            is present only when *with_total_count* is true.

        Examples
        --------
        >>> filters = SearchFilters(collection="JCMT", dataproduct_type="image")
        >>> page1 = DataDiscovery.search(filters, page_size=50)
        >>> page2 = DataDiscovery.search(filters, page_size=50, after=page1.meta["next_after"])
        """
        columns = columns or self.DEFAULT_SEARCH_COLUMNS
        has_position = self._has_position(filters)
        # Fetch one extra row to learn whether another page exists, without a
        # second round trip or an OFFSET this service doesn't support; trimmed
        # back to page_size before returning.
        adql = self._search_adql(filters, columns, after, page_size + 1)

        if verbose:
            print(f"[ADQL] {adql}")

        table = self.query(adql, maxrec=page_size + 1)

        has_more = len(table) > page_size
        if has_more:
            table = table[:page_size]

        if split_obs_id and "obs_id" in table.colnames:
            _add_namespace_filename_columns(table)

        table.meta["next_after"] = (
            str(table["obs_publisher_did"][-1])
            if has_more and not has_position and len(table) and "obs_publisher_did" in table.colnames
            else None
        )

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
        page_size: int = 100,
    ) -> str:
        """
        Return the ADQL :meth:`search` would run for *filters*, without
        executing it (MAN-827 §5 requirement 5 — the "show query" modal).

        Built through the exact same :meth:`_search_adql` helper
        :meth:`search` uses, so this can never drift out of sync with what
        ``search(filters)`` actually does. The one difference: this shows
        ``TOP page_size``, not the ``page_size + 1`` :meth:`search` fetches
        internally to detect whether another page exists — that's an
        implementation detail of pagination, not something a user editing
        this ADQL in a "show query" modal should see.

        Parameters
        ----------
        filters : SearchFilters, optional
            Same filter object :meth:`search` takes.
        columns : str, optional
            Same as :meth:`search`.
        after : str, optional
            Same as :meth:`search`.
        page_size : int, optional
            Same as :meth:`search` — shown here as the real ``TOP`` value.

        Returns
        -------
        str

        Examples
        --------
        >>> DataDiscovery.explain(SearchFilters(collection="JCMT"))
        "SELECT ... FROM ivoa.ObsCore WHERE UPPER(obs_collection) = UPPER('JCMT') ORDER BY obs_publisher_did"
        """
        columns = columns or self.DEFAULT_SEARCH_COLUMNS
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
