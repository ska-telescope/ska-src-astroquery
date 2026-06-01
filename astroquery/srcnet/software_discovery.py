"""
SRCNet Software Discovery TAP client.

Follows astroquery conventions — methods return `~astropy.table.Table`.

Examples
--------
Quick query via the module singleton::

    from astroquery.srcnet import SoftwareDiscovery

    # Keyword-filtered query
    t = SoftwareDiscovery.query_software(
        status="STABLE",
        science_category="Continuum Science",
    )

    # Single entry by URI
    t = SoftwareDiscovery.get_software("ska:sextractor:docker-sextractor@2.25.0")

    # Raw ADQL
    t = SoftwareDiscovery.query("SELECT * FROM sdm.software WHERE status = 'STABLE'")

    # Natural language → ADQL + execute
    adql, t = SoftwareDiscovery.query_natural(
        "show stable software that requires a GPU", verbose=True
    )

    # Natural language → ADQL only
    adql = SoftwareDiscovery.nl_to_adql("list all Docker images for continuum imaging")
    print(adql)

Switch environment::

    from astroquery.srcnet import conf
    conf.SRCNET_ENVIRONMENT = "development"
"""

from __future__ import annotations

import re
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

import pyvo
import requests
from astropy.table import Table

from ._helpdesk import srcnet_raise

__all__ = ["SoftwareDiscovery", "SoftwareDiscoveryClass"]


# ── NL → ADQL prompt ──────────────────────────────────────────────────────────

try:
    from .schemas import _SDM_SCHEMA
except ImportError:
    _SDM_SCHEMA = (
        "sdm.software (alias: s)  — base table\n"
        "  id, uri, description, status (ALPHA/BETA/TESTING/STABLE/DEPRECATED),\n"
        "  release_date, changelog\n"
        "\n"
        "sdm.resource_requirements (alias: r)  — 1:1 with software\n"
        "  JOIN: LEFT JOIN sdm.resource_requirements AS r ON r.software_id = s.id\n"
        "  requires_gpu (boolean), min_memory (GB), recommended_memory (GB), min_cpu_cores (int)\n"
        "\n"
        "sdm.artifact (alias: a)  — 1:N with software\n"
        "  JOIN: JOIN sdm.artifact AS a ON a.software_id = s.id\n"
        "  kind (DOCKER/SINGULARITY/CONDA/PIP), location, cpu_architecture\n"
        "\n"
        "sdm.discovery_science_category (alias: dsc)  — reach via sdm.discovery bridge\n"
        "  JOIN: JOIN sdm.discovery AS d ON d.software_id = s.id\n"
        "         JOIN sdm.discovery_science_category AS dsc ON dsc.discovery_id = d.id\n"
        "  category (e.g. Continuum Science, Spectral Line Science, VLBI)\n"
        "\n"
        "sdm.discovery_function_category (alias: dfc)  — reach via sdm.discovery bridge\n"
        "  JOIN: JOIN sdm.discovery AS d ON d.software_id = s.id\n"
        "         JOIN sdm.discovery_function_category AS dfc ON dfc.discovery_id = d.id\n"
        "  category (e.g. Imaging, Calibration, Source Finding, Simulation)\n"
        "\n"
        "sdm.discovery_science_working_group (alias: dswg)  — reach via sdm.discovery bridge\n"
        "  JOIN: JOIN sdm.discovery AS d ON d.software_id = s.id\n"
        "         JOIN sdm.discovery_science_working_group AS dswg ON dswg.discovery_id = d.id\n"
        "  working_group\n"
    )

_NL_TO_ADQL_PROMPT = (
    "You are an expert in ADQL (Astronomical Data Query Language).\n"
    "\n"
    "The Software Discovery database uses a normalized multi-table schema.\n"
    "Do NOT query sdm.software as if it has a flat structure — use the JOINs below.\n"
    "\n"
    + _SDM_SCHEMA
    + "\nADQL notes:\n"
    "- String literals: single quotes — WHERE s.status = 'STABLE'\n"
    "- Wildcards: LIKE '%keyword%'\n"
    "- Booleans: use string literals — WHERE r.requires_gpu = 'true' or = 'false'; never 1/0 or TRUE/FALSE\n"
    "- String enums are case-sensitive and uppercase: s.status = 'STABLE' not 'stable'; valid values: ALPHA, BETA, TESTING, STABLE, DEPRECATED\n"
    "- Row limit: SELECT TOP N ...\n"
    "- Use SELECT DISTINCT when JOINing 1:N tables (artifact, discovery)\n"
    "- Use JOINs (not EXISTS subqueries) for category filters — correlated subqueries are not supported\n"
    "\n"
    "Translate the following question into a single ADQL query.\n"
    "Return ONLY the ADQL query — no explanation, no markdown, no comments.\n"
    "\n"
    "Question: {question}\n"
)


# ── Client class ──────────────────────────────────────────────────────────────

class SoftwareDiscoveryClass:
    """
    Query the SRCNet Software Discovery TAP service.

    Attributes
    ----------
    TABLE : str
        Fully-qualified TAP table name (``sdm.software``).

    Parameters
    ----------
    tap_url : str, optional
        TAP service base URL.  Defaults to the URL for the currently
        configured SRCNet environment.
    token : str, optional
        Bearer token for authenticated TAP services.
    """

    TABLE = "sdm.software"

    def __init__(
        self,
        tap_url: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        from . import _env_urls
        self._tap_url = tap_url or _env_urls()["software_tap"]
        self._token = token
        self._tap: Optional[pyvo.dal.TAPService] = None

    # ── Internal ──────────────────────────────────────────────────────────────

    @property
    def tap(self) -> pyvo.dal.TAPService:
        """Lazily-instantiated pyvo :class:`~pyvo.dal.TAPService`."""
        if self._tap is None:
            self._tap = pyvo.dal.TAPService(self._tap_url)
            session = self._tap._session
            if self._token:
                session.headers["Authorization"] = f"Bearer {self._token}"
            _patch_redirect_session(session, self._tap_url)
        return self._tap

    # ── Low-level query ───────────────────────────────────────────────────────

    def query(self, adql: str, *, maxrec: Optional[int] = None) -> Table:
        """
        Execute an arbitrary ADQL statement synchronously.

        Parameters
        ----------
        adql : str
            ADQL query string, e.g.
            ``"SELECT * FROM sdm.software WHERE status = 'STABLE'"``.
        maxrec : int, optional
            Maximum rows to return.  Defaults to
            :attr:`~astroquery.srcnet.Conf.SRCNET_DEFAULT_MAXREC`.

        Returns
        -------
        `~astropy.table.Table`
        """
        from . import conf
        maxrec = maxrec if maxrec is not None else conf.SRCNET_DEFAULT_MAXREC
        return self.tap.search(adql, maxrec=maxrec).to_table()

    # ── Schema introspection ──────────────────────────────────────────────────

    def get_tables(self) -> Table:
        """
        Return all tables available in this TAP service.

        Returns
        -------
        `~astropy.table.Table`
            Columns: ``name``, ``description``.
        """
        tables = self.tap.tables
        rows = [
            {"name": t.name, "description": t.description or ""}
            for t in tables.values()
        ]
        return Table(rows=rows) if rows else Table(names=["name", "description"])

    def get_columns(self, table: Optional[str] = None) -> Table:
        """
        Return the column definitions for *table*.

        Parameters
        ----------
        table : str, optional
            Fully-qualified TAP table name, e.g. ``"sdm.artifact"``.
            Defaults to :attr:`TABLE` (``"sdm.software"``).

        Returns
        -------
        `~astropy.table.Table`
            Columns: ``name``, ``datatype``, ``unit``, ``ucd``, ``description``.
        """
        target = table if table is not None else self.TABLE
        cols = self.tap.tables[target].columns
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
        return Table(rows=rows)

    # ── Convenience queries ───────────────────────────────────────────────────

    def query_software(
        self,
        *,
        uri: Optional[str] = None,
        status: Optional[str] = None,
        science_category: Optional[str] = None,
        function_category: Optional[str] = None,
        science_working_group: Optional[str] = None,
        requires_gpu: Optional[bool] = None,
        columns: str = "*",
        maxrec: Optional[int] = None,
    ) -> Table:
        """
        Query :attr:`TABLE` with optional keyword filters.

        All parameters are optional; omit them to retrieve all rows.

        Parameters
        ----------
        uri : str, optional
            Exact URI match, e.g. ``"ska:sextractor:docker-sextractor@2.25.0"``.
        status : str, optional
            One of ``ALPHA``, ``BETA``, ``TESTING``, ``STABLE``, ``DEPRECATED``.
        science_category : str, optional
            Substring search in ``sdm.discovery_science_category.category``.
        function_category : str, optional
            Substring search in ``sdm.discovery_function_category.category``.
        science_working_group : str, optional
            Substring search in ``sdm.discovery_science_working_group.working_group``.
        requires_gpu : bool, optional
            Filter by GPU requirement (``True`` / ``False``).
        columns : str, optional
            Comma-separated list of ``sdm.software`` columns to return
            (default ``"*"``).
        maxrec : int, optional
            Maximum rows to return.

        Returns
        -------
        `~astropy.table.Table`

        Examples
        --------
        >>> from astroquery.srcnet import SoftwareDiscovery
        >>> t = SoftwareDiscovery.query_software(
        ...     status="STABLE",
        ...     science_category="Continuum Science",
        ... )
        """
        joins: list[str] = []
        where: list[str] = []

        if uri is not None:
            where.append(f"s.uri = '{_esc(uri)}'")
        if status is not None:
            where.append(f"s.status = '{_esc(status)}'")

        if requires_gpu is not None:
            joins.append("LEFT JOIN sdm.resource_requirements AS r ON r.software_id = s.id")
            where.append(f"r.requires_gpu = {'TRUE' if requires_gpu else 'FALSE'}")

        # Each category filter gets its own discovery JOIN with a unique alias
        # to avoid correlated subqueries (not supported by all TAP services).
        if science_category is not None:
            joins.append("JOIN sdm.discovery AS d_sc ON d_sc.software_id = s.id")
            joins.append("JOIN sdm.discovery_science_category AS dsc ON dsc.discovery_id = d_sc.id")
            where.append(f"dsc.category LIKE '%{_esc(science_category)}%'")

        if function_category is not None:
            joins.append("JOIN sdm.discovery AS d_fc ON d_fc.software_id = s.id")
            joins.append("JOIN sdm.discovery_function_category AS dfc ON dfc.discovery_id = d_fc.id")
            where.append(f"dfc.category LIKE '%{_esc(function_category)}%'")

        if science_working_group is not None:
            joins.append("JOIN sdm.discovery AS d_swg ON d_swg.software_id = s.id")
            joins.append("JOIN sdm.discovery_science_working_group AS dswg ON dswg.discovery_id = d_swg.id")
            where.append(f"dswg.working_group LIKE '%{_esc(science_working_group)}%'")

        needs_distinct = any("sdm.discovery" in j for j in joins)
        if needs_distinct:
            col_clause = "DISTINCT s.*" if columns == "*" else f"DISTINCT {columns}"
        else:
            col_clause = columns

        adql = f"SELECT {col_clause} FROM {self.TABLE} AS s"
        if joins:
            adql += " " + " ".join(joins)
        if where:
            adql += " WHERE " + " AND ".join(where)

        return self.query(adql, maxrec=maxrec)

    def get_software(self, uri: str) -> Table:
        """
        Retrieve a single software entry by its full URI.

        Parameters
        ----------
        uri : str
            Full SKA software URI, e.g.
            ``"ska:sextractor:docker-sextractor@2.25.0"``.

        Returns
        -------
        `~astropy.table.Table`
            Zero or one rows from ``sdm.software`` only.
            Use :meth:`get_software_full` to include artifacts and categories.
        """
        return self.query_software(uri=uri, maxrec=1)

    def get_software_full(self, uri: str) -> dict:
        """
        Retrieve all available information for a single software entry.

        Runs one query per sub-table to avoid row duplication from 1:N JOINs.

        Parameters
        ----------
        uri : str
            Full SKA software URI, e.g.
            ``"ska:sextractor:docker-sextractor@2.25.0"``.

        Returns
        -------
        dict
            Keys: ``software``, ``artifacts``, ``requirements``,
            ``science_categories``, ``function_categories``, ``working_groups``.
            Each value is an `~astropy.table.Table`.

        Examples
        --------
        >>> profile = sd.get_software_full("ska:sextractor:docker-sextractor@2.25.0")
        >>> profile["software"]
        >>> profile["artifacts"]
        """
        esc_uri = _esc(uri)

        software = self.query(
            f"SELECT * FROM sdm.software AS s WHERE s.uri = '{esc_uri}'",
            maxrec=1,
        )
        artifacts = self.query(
            f"SELECT a.kind, a.location, a.cpu_architecture"
            f" FROM sdm.artifact AS a"
            f" JOIN sdm.software AS s ON a.software_id = s.id"
            f" WHERE s.uri = '{esc_uri}'"
        )
        requirements = self.query(
            f"SELECT r.requires_gpu, r.min_memory, r.recommended_memory, r.min_cpu_cores"
            f" FROM sdm.resource_requirements AS r"
            f" JOIN sdm.software AS s ON r.software_id = s.id"
            f" WHERE s.uri = '{esc_uri}'",
            maxrec=1,
        )
        science_categories = self.query(
            f"SELECT DISTINCT dsc.category"
            f" FROM sdm.discovery_science_category AS dsc"
            f" JOIN sdm.discovery AS d ON dsc.discovery_id = d.id"
            f" JOIN sdm.software AS s ON d.software_id = s.id"
            f" WHERE s.uri = '{esc_uri}'"
        )
        function_categories = self.query(
            f"SELECT DISTINCT dfc.category"
            f" FROM sdm.discovery_function_category AS dfc"
            f" JOIN sdm.discovery AS d ON dfc.discovery_id = d.id"
            f" JOIN sdm.software AS s ON d.software_id = s.id"
            f" WHERE s.uri = '{esc_uri}'"
        )
        working_groups = self.query(
            f"SELECT DISTINCT dswg.working_group"
            f" FROM sdm.discovery_science_working_group AS dswg"
            f" JOIN sdm.discovery AS d ON dswg.discovery_id = d.id"
            f" JOIN sdm.software AS s ON d.software_id = s.id"
            f" WHERE s.uri = '{esc_uri}'"
        )

        return {
            "software":           software,
            "artifacts":          artifacts,
            "requirements":       requirements,
            "science_categories": science_categories,
            "function_categories": function_categories,
            "working_groups":     working_groups,
        }

    def query_by_image(self, image: str) -> Table:
        """
        Search for software entries whose artifact location contains *image*.

        Parameters
        ----------
        image : str
            Docker image name or partial reference to search for.

        Returns
        -------
        `~astropy.table.Table`
            Columns: ``uri``, ``status``, ``description``,
            ``kind``, ``location``, ``cpu_architecture``.
        """
        adql = (
            f"SELECT DISTINCT s.uri, s.status, s.description,"
            f" a.kind, a.location, a.cpu_architecture"
            f" FROM {self.TABLE} AS s"
            f" JOIN sdm.artifact AS a ON a.software_id = s.id"
            f" WHERE a.location LIKE '%{_esc(image)}%'"
        )
        return self.query(adql)

    # ── NL → ADQL ─────────────────────────────────────────────────────────────

    def nl_to_adql(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        chatserver_url: Optional[str] = None,
    ) -> str:
        """
        Translate a natural-language question into an ADQL query for
        the ``sdm.software`` schema.

        Parameters
        ----------
        text : str
            Plain-English question, e.g.
            ``"list all Docker images for continuum imaging"``.
        model : str, optional
            Model name (legacy Ollama path only).
        chatserver_url : str, optional
            CHATSERVER base URL.  If given (or set via
            ``conf.SRCNET_CHATSERVER_URL``), the CHATSERVER backend is used.

        Returns
        -------
        str
            ADQL query string ready to pass to :meth:`query`.

        Examples
        --------
        >>> adql = SoftwareDiscovery.nl_to_adql("show all stable GPU software")
        >>> print(adql)
        """
        from . import conf
        cs_url = chatserver_url or conf.SRCNET_CHATSERVER_URL or None

        explicit = _detect_tables(text, {'sdm'})
        msg = (f"[explicit_tables: {', '.join(explicit)}] " + text) if explicit else text

        if cs_url:
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
                    steps=f"sd.nl_to_adql({text!r})",
                )
            raw = resp.json()
            return raw.get("adql") or _extract_adql(raw.get("answer") or raw.get("response", ""))

        from . import _env_urls
        ollama_url = _env_urls()["chat"]
        model = model or "deepseek-coder-v2"
        # Use replace() instead of .format() — the schema block may contain
        # literal curly braces (e.g. JSON examples) that would raise KeyError.
        prompt = _NL_TO_ADQL_PROMPT.replace("{question}", text)
        try:
            resp = requests.post(
                f"{ollama_url.rstrip('/')}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            srcnet_raise(
                RuntimeError(f"Could not reach the SRCNet chat service at {ollama_url}."),
                steps=f"sd.nl_to_adql({text!r})",
            )
        return _extract_adql(resp.json().get("response", ""))

    def query_natural(
        self,
        text: str,
        *,
        model: Optional[str] = None,
        chatserver_url: Optional[str] = None,
        maxrec: Optional[int] = None,
        verbose: bool = False,
    ) -> Tuple[str, Table]:
        """
        Translate *text* to ADQL, then execute it against the TAP service.

        Parameters
        ----------
        text : str
            Plain-English question, e.g.
            ``"show stable software that requires a GPU"``.
        model : str, optional
            Model name (legacy Ollama path only).
        chatserver_url : str, optional
            Route through CHATSERVER instead of direct Ollama.
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
        >>> adql, t = SoftwareDiscovery.query_natural(
        ...     "show stable software that requires a GPU", verbose=True
        ... )
        """
        adql = self.nl_to_adql(text, model=model, chatserver_url=chatserver_url)
        if verbose:
            print(f"[ADQL] {adql}")
        return adql, self.query(adql, maxrec=maxrec)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Minimal ADQL string-literal escaping: replace ``'`` with ``''``."""
    return s.replace("'", "''")


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


_TABLE_RE = re.compile(r'\b([a-zA-Z_]\w*\.[a-zA-Z_]\w*)\b')


def _detect_tables(text: str, known_schemas: set) -> list:
    """Return schema.table strings found in text whose schema is in known_schemas."""
    return [t for t in _TABLE_RE.findall(text) if t.split('.')[0].lower() in known_schemas]


def _fix_adql(adql: str) -> str:
    """Convert SQL LIMIT N → ADQL TOP N."""
    m = re.search(r'\bLIMIT\s+(\d+)\s*;?\s*$', adql, re.IGNORECASE)
    if m:
        n = m.group(1)
        adql = adql[:m.start()].rstrip().rstrip(';')
        adql = re.sub(r'\bSELECT\b', f'SELECT TOP {n}', adql, count=1, flags=re.IGNORECASE)
    return adql


def _extract_adql(text: str) -> str:
    """Strip markdown fences and return the bare ADQL from a model response."""
    block = re.search(r"```(?:sql|adql)\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if block:
        return _fix_adql(block.group(1).strip())
    block = re.search(r"```.*?```", text, re.DOTALL)
    if block:
        inner = block.group(0)
        tq = re.search(r'"""(.*?)"""', inner, re.DOTALL)
        if tq:
            candidate = tq.group(1).strip()
            if candidate.upper().startswith("SELECT"):
                return _fix_adql(candidate)
        for line in inner.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("SELECT"):
                return _fix_adql(stripped)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SELECT"):
            return _fix_adql(stripped)
    return _fix_adql(text.strip())


# ── Module-level singleton (astroquery convention) ────────────────────────────

SoftwareDiscovery = SoftwareDiscoveryClass()
