.. _astroquery.srcnet:

***********************************************************************************
SRCNet queries (`astroquery.srcnet`)
***********************************************************************************

Overview
--------

`astroquery.srcnet` provides the astroquery interface to the SRCNet platform.
It supports querying CAOM2 observational data and the software discovery
registry via TAP services, as well as authenticated data download and
SODA cutouts through a dedicated :class:`~astroquery.srcnet.DataAccessClass`.

The library follows the **astroquery factory pattern**: a module-level
singleton ``SRCNet`` is provided for convenience; factory methods
(:meth:`~astroquery.srcnet.SRCNetClass.get_tap`,
:meth:`~astroquery.srcnet.SRCNetClass.get_software_discovery`,
:meth:`~astroquery.srcnet.SRCNetClass.get_data_access`,
:meth:`~astroquery.srcnet.SRCNetClass.get_chat`) return purpose-built clients
configured for the current environment.

Quickstart
----------

.. code-block:: bash

    python3 -m pip install astroquery --index-url https://gitlab.com/api/v4/projects/53653803/packages/pypi/simple

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> from astropy.coordinates import SkyCoord
    >>> import astropy.units as u

    >>> # Data discovery (no login required)
    >>> tap = SRCNet.get_tap()
    >>> tap.get_collections()
    >>> tap.query_region(SkyCoord(82.1, 12.58, unit="deg"), radius=0.01 * u.deg)

    >>> # Software discovery (no login required)
    >>> sd = SRCNet.get_software_discovery()
    >>> sd.query_software(status="STABLE", requires_gpu=True)

    >>> # Data access (login required)
    >>> SRCNet.login()
    >>> da = SRCNet.get_data_access()
    >>> da.get_data(namespace="testing", name="PTF10tce.fits")
    >>> da.soda_cutout("testing", "PTF10tce.fits", "cutout.fits",
    ...                circle=(351.9867, 8.7787, 0.1))

Authentication
--------------

For authenticated requests, there is a ``login`` function. This function will guide you through an auth flow to identify
yourself to the APIs required to perform the requests, e.g.

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> srcnet=SRCNet(verbose=True)
    >>> srcnet.login()

    --------------------------------------------------

    Scan the QR code, or using a browser on another
    device, visit https://ska-iam.stfc.ac.uk/device
    and enter code XXXYYY

    ▄▄▄▄▄▄▄ ▄   ▄   ▄ ▄▄▄ ▄▄▄▄▄▄▄
    █ ▄▄▄ █ ▄██▀▀▄▄▄█ ▄▄  █ ▄▄▄ █
    █ ███ █ █▀▀█▄▀▀██▀▀▄  █ ███ █
    █▄▄▄▄▄█ ▄ █ █ ▄ ▄▀█▀▄ █▄▄▄▄▄█
    ▄▄▄▄  ▄ ▄ ██▄▄█ ▀█▄▀ ▄  ▄▄▄ ▄
    ▄  ▀▄▀▄█▄▄█▄▄█▀ █▄▄▀ ▀█▀▀ ▄▄▀
    ▄▀▄▄▄ ▄ ▄██▄ ██▀▄▄▀  ▀▀▀▀▄▄ ▀
    ▄█▄▄▄▀▄█▄█▄▀▀ ▀▀ ▄ █  ██  ███
    ▄█▄▄  ▄▄▀█  █▄▀▀▀  ▀█▄ ▄█▄ █
    ▄▀▀█ █▄▄▄ ▄ █▄▄█▄█  ▀ ▄█ ▀█▀
    ▄▀█▄▀▄▀██▀█▄ ▄▀▄██▄▄█▄██▄█
    ▄▄▄▄▄▄▄ ▀ █▄    █▄███ ▄ ██▀█▀
    █ ▄▄▄ █  ▀▄▄ █▄ ▀▄  █▄▄▄█▀▄▄
    █ ███ █ ██ ▀█▄█▀█▀▄█   █▀▀▄▀█
    █▄▄▄▄▄█ █▀  ▄▄█ █ ▄██ ██ █ █

    --------------------------------------------------

    Polling for token... (3/60)

    Successfully polled for token. You are now logged in.

    DEBUG: Access token: <redacted>
    DEBUG: Refresh token: <redacted> [astroquery.srcnet.core]
    DEBUG: Persisting access token to: /tmp/access_token [astroquery.srcnet.core]
    DEBUG: Persisting refresh token to: /tmp/refresh_token [astroquery.srcnet.core]

Due to an ongoing issue in Indigo IAM v1.8.2 [1]_, it is strongly advisable to keep hold of the ``access_token`` and
``refresh_token`` that result from this command **and any subsequent commands**. One consequence of this issue is that
subsequent calls to the ``login`` function within the first access token's lifetime (default=1 hour) will yield a
HTTP 500; you have to wait until the first access token has expired before you can run this function again.

To circumvent this, and for convenience, access tokens and refresh tokens can be persisted between sessions by passing
an ``access_token_path`` and ``refresh_token_path`` to the ``SRCNet`` constructor, pointing to where the access tokens
and refresh tokens will be stored locally (default is ``/tmp/access_token`` and ``/tmp/refresh_token`` respectively).
Setting these to ``None`` will disable token persistence. The ``access_token`` and ``refresh_token`` can also be passed
to the ``SRCNet()`` constructor (``access_token`` and ``refresh_token`` parameters respectively) directly if required,
or via the environment variables ``ACCESS_TOKEN`` and ``REFRESH_TOKEN`` respectively. The order in which these
locations are checked are first the constructor, the environment and then finally local paths.

Due to token exchanges that occur within the client, if you're passing a token in through the constructor or
environment **this token may become invalid**. This is another consequence of [1]_.

If persisting tokens, be aware that they are stored in plaintext locally. This is especially a concern for the refresh
token, which if compromised can be used to generate new access tokens on-demand for the entirety of the refresh
token's lifetime.

Authorisation
--------------

Authenticating does not necessarily mean that you will be allowed to perform a given request, rather this is
determined by the action you're trying perform and your group membership on Indigo IAM.

Environments
------------

All service URLs are derived from the ``SRCNET_ENVIRONMENT`` configuration item.
The default is ``"operational"``; switch to ``"development"`` to target the
pre-production deployment:

.. code-block:: python

    >>> from astroquery.srcnet import conf
    >>> conf.SRCNET_ENVIRONMENT = "development"   # all subsequent calls use dev URLs

Available environments and their entry points are listed in
``astroquery.srcnet.ENVIRONMENTS``.

Install
-------

To install the package, install via pip from the remote package registry:

.. code-block:: bash

    python3 -m pip install astroquery --index-url https://gitlab.com/api/v4/projects/53653803/packages/pypi/simple

Format Factory
^^^^^^^^^^^^^^

Automatically detect a dataset's data type and access format-specific methods such as cutouts and metadata inspection.

Note: The dataset must have its Rucio metadata key ``dataproduct_type`` set correctly (e.g. ``cube``, ``image``, ``spectra``, or ``visibility``).

To use the Format Factory, import the ``SKAFormatFactory`` class.

If you wish to list the available methods for a specific data type before loading a dataset (e.g., ``Cube().show_methods()``), you must also explicitly import the corresponding data type class (Cube, Image, Spectra, or Visibility) first.
This is optional -- if you load a dataset using ``SKAFormatFactory.get(...)``, the correct class is instantiated automatically.

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet, SKAFormatFactory, Cube, Image, Spectra, Visibility

    >>> Image().show_methods()

    Available methods for Image:
    - cutout
    - show_metadata
    - fits_header_info (placeholder)

    >>> SRCNet.login()

    Successfully polled for token. You are now logged in.

    >>> data = SKAFormatFactory.get("magenta", "HD163296_13CO_2-1.fits")

    INFO: Exchanged authn-api service token for data-management-api service [astroquery.srcnet.core]
    Detected data type of magenta:HD163296_13CO_2-1.fits: cube

    >>> data.show_methods()

    Available methods for Cube:
    - subcube
    - show_metadata
    - fits_header_info (placeholder)

    >>> data.show_metadata()

    Metadata for magenta:HD163296_13CO_2-1.fits
    s_ra                : 269.08
    test                : 2
    s_dec               : -21.95
    s_fov               : 0.1
    obs_id              : magenta:HD163296_13CO_2-1.fits
    testing             : {"key1": {"level2": "value2"}}
    access_url          : https://ivoa.datalink.srcdev.skao.int/rucio/links?id=magenta:HD163296_13CO_2-1.fits
    access_format       : application/x-votable+xml
    facility_name       : ALMA
    obs_collection      : collection_magenta_test
    dataproduct_type    : cube
    obs_publisher_did   : magenta

    >>> data.subcube(circle=(269.08, -21.95, 0.01),output_file="output.fits")

    INFO: Requesting SODA cutout from https://gatekeeper.srcdev.skao.int:443/soda/ska/datasets/soda with params={'ID': 'ivo://auth.example.org/datasets/fits?magenta/33/7b/HD163296_13CO_2-1.fits', 'RESPONSEFORMAT': 'application/fits', 'POS': 'CIRCLE 269.08 -21.95 0.01'} [astroquery.srcnet.core]


Data Access
-----------

``DataAccessClass`` provides authenticated access to SRCNet data products.
Obtain an instance through the factory method:

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> SRCNet.login()
    >>> da = SRCNet.get_data_access()

All methods require a valid login session.  Tokens are refreshed automatically.

get_metadata
^^^^^^^^^^^^

Retrieve the JSON metadata record for a data product stored in the SRCNet
Data Management API.

.. code-block:: python

    >>> meta = da.get_metadata("testing", "PTF10tce.fits")
    >>> print(meta["size"], meta["checksum"])

The returned dictionary contains at minimum ``size`` (bytes), ``checksum``,
and ``replicas`` (list of storage locations), plus any custom attributes
registered for the file.

get_data
^^^^^^^^

Download a data product to the current working directory.  The nearest
replica is selected automatically by default.

.. code-block:: python

    >>> da.get_data("testing", "PTF10tce.fits")

    >>> # Explicitly select a random replica
    >>> da.get_data("testing", "PTF10tce.fits", sort="random")

``sort`` controls replica selection:

- ``"nearest_by_ip"`` (default) — geographically closest SRC site
- ``"random"`` — random replica

soda_cutout
^^^^^^^^^^^

Request a sub-region of a data product via the
`IVOA SODA 1.0 <https://www.ivoa.net/documents/SODA/>`_ protocol.  The
service endpoint is discovered from the file's Datalink record; the result
is streamed to ``output_file``.

.. code-block:: python

    >>> # Circular spatial cutout (0.1° radius)
    >>> da.soda_cutout(
    ...     "testing", "PTF10tce.fits", "output/cutout_circle.fits",
    ...     circle=(351.9867, 8.7787, 0.1),   # (lon, lat, radius_deg)
    ... )

    >>> # Polygon cutout
    >>> da.soda_cutout(
    ...     "testing", "PTF10tce.fits", "output/cutout_polygon.fits",
    ...     polygon=[(351.9, 8.77), (352.0, 8.70), (352.05, 8.60)],
    ... )

    >>> # Spectral cutout within a circular region (cube data)
    >>> da.soda_cutout(
    ...     "testing", "example_cube.fits", "output/subcube.fits",
    ...     circle=(83.8221, -5.3911, 0.5),
    ...     band="0.0002 0.0003",   # wavelength interval in metres
    ... )

**Positional parameters** — at least one is required:

+----------------+--------------------------------------+----------------------------+
| Parameter      | Format                               | Example                    |
+================+======================================+============================+
| ``circle``     | ``(lon, lat, radius_deg)``           | ``(351.99, 8.78, 0.1)``    |
+----------------+--------------------------------------+----------------------------+
| ``polygon``    | ``[(lon, lat), …]``                  | ``[(351.9, 8.7), …]``      |
+----------------+--------------------------------------+----------------------------+
| ``range_``     | ``(lon1, lon2, lat1, lat2)``         | ``(351.8, 352.1, 8.6, 8.9)``|
+----------------+--------------------------------------+----------------------------+
| ``pos``        | SODA POS string                      | ``"CIRCLE 351.99 8.78 0.1"``|
+----------------+--------------------------------------+----------------------------+

**Spectral / temporal filters** (combine with a positional parameter):

+----------+-------------------------------+---------------------------+
| Parameter| Format                        | Example                   |
+==========+===============================+===========================+
| ``band`` | wavelength interval (metres)  | ``"0.0002 0.0003"``       |
+----------+-------------------------------+---------------------------+
| ``time`` | MJD interval                  | ``"55123.456 55123.466"`` |
+----------+-------------------------------+---------------------------+
| ``pol``  | polarization state            | ``"Q"``                   |
+----------+-------------------------------+---------------------------+

End-to-end workflow
^^^^^^^^^^^^^^^^^^^

A typical session: discover an observation with the TAP interface, locate its
artifacts, then download or cut out the product of interest.

.. code-block:: python

    >>> from astropy.coordinates import SkyCoord
    >>> import astropy.units as u

    >>> tap = SRCNet.get_tap()
    >>> da  = SRCNet.get_data_access()

    >>> # 1. Find observations near a target
    >>> target = SkyCoord(83.8221, -5.3911, unit="deg")   # Orion Nebula
    >>> obs = tap.query_region(target, radius=1.0 * u.deg)

    >>> # 2. List file artifacts for the first observation
    >>> artifacts = tap.get_artifacts(str(obs["obs_id"][0]))

    >>> # 3. Download the first science artifact
    >>> uri = str(artifacts["uri"][0])   # e.g. "testing:PTF10tce.fits"
    >>> ns, fname = uri.split(":", 1)
    >>> da.get_data(ns, fname)


Software Discovery
------------------

``SoftwareDiscoveryClass`` queries the SRCNet software registry via the
``sdm.software`` TAP service.  Use the module-level singleton
``SoftwareDiscovery`` for the default endpoint, or instantiate the class
directly for custom URLs.

The database uses a **normalized schema** — ``sdm.software`` is the base table,
with categories, resource requirements, and artifacts in separate joined tables.

No login is required for read-only queries:

.. code-block:: python

    >>> from astroquery.srcnet import SoftwareDiscovery

get_columns
^^^^^^^^^^^

Return the column definitions of the ``sdm.software`` TAP table.

.. code-block:: python

    >>> SoftwareDiscovery.get_columns()
    <Table length=6>
    name         datatype  unit  ucd  description
    ...

query_software
^^^^^^^^^^^^^^

Query registered software with optional keyword filters on status, science
category, function category, working group, and GPU requirement.

.. code-block:: python

    >>> t = SoftwareDiscovery.query_software(columns="uri, status, description")

    >>> t = SoftwareDiscovery.query_software(
    ...     status="STABLE",
    ...     science_category="Continuum Science",
    ...     requires_gpu=False,
    ... )

get_software
^^^^^^^^^^^^

Retrieve a single software entry by its full SKA URI.

.. code-block:: python

    >>> t = SoftwareDiscovery.get_software("ska:sextractor:docker-sextractor@2.25.0")
    >>> t["uri", "status", "description"]

query_by_image
^^^^^^^^^^^^^^

Search for software whose artifact location contains a given Docker image name or partial reference.

.. code-block:: python

    >>> t = SoftwareDiscovery.query_by_image("wsclean")
    >>> t["uri", "location", "cpu_architecture"]

query (raw ADQL)
^^^^^^^^^^^^^^^^

Execute an arbitrary ADQL statement against the software TAP service for
queries that span multiple normalized tables.

.. code-block:: python

    >>> t = SoftwareDiscovery.query("""
    ...     SELECT DISTINCT s.uri, s.status, s.description,
    ...                     r.min_memory, a.location, a.cpu_architecture
    ...     FROM sdm.software AS s
    ...     LEFT JOIN sdm.resource_requirements AS r ON r.software_id = s.id
    ...     JOIN sdm.artifact AS a ON a.software_id = s.id
    ...     WHERE s.status = 'STABLE'
    ...       AND r.requires_gpu = FALSE
    ...       AND r.min_memory <= 4
    ...     ORDER BY s.uri
    ... """)

For science category filtering use ``EXISTS`` subqueries:

.. code-block:: python

    >>> t = SoftwareDiscovery.query("""
    ...     SELECT DISTINCT s.uri, s.status, s.description
    ...     FROM sdm.software AS s
    ...     WHERE EXISTS (
    ...         SELECT 1 FROM sdm.discovery AS d
    ...         JOIN sdm.discovery_science_category AS dsc ON dsc.discovery_id = d.id
    ...         WHERE d.software_id = s.id
    ...         AND dsc.category LIKE '%Continuum%'
    ...     )
    ... """)

nl_to_adql (software)
^^^^^^^^^^^^^^^^^^^^^

Translate a plain-English question into an ADQL query for the ``sdm.software``
schema using the SRCNet remote chat service.

.. code-block:: python

    >>> adql = SoftwareDiscovery.nl_to_adql(
    ...     "list all stable software that requires a GPU"
    ... )
    >>> print(adql)
    SELECT DISTINCT s.uri, s.status, s.description
    FROM sdm.software AS s
    LEFT JOIN sdm.resource_requirements AS r ON r.software_id = s.id
    WHERE s.status = 'STABLE' AND r.requires_gpu = TRUE

query_natural (software)
^^^^^^^^^^^^^^^^^^^^^^^^

Translate a natural-language question into ADQL and execute it against the
software TAP service in a single call.

.. code-block:: python

    >>> adql, t = SoftwareDiscovery.query_natural(
    ...     "show all Docker images for continuum imaging, sorted by URI",
    ...     verbose=True,   # prints the generated ADQL before running
    ... )
    [ADQL] SELECT DISTINCT s.uri, a.location ...

    >>> adql, t = SoftwareDiscovery.query_natural(
    ...     "how many entries are there per status category?"
    ... )


Data Discovery (CAOM2 TAP)
--------------------------

``DataDiscoveryClass`` queries the SRCNet data archive via the CAOM2 data
model.  The main tables are ``caom2.Observation``, ``caom2.Plane``, and
``caom2.Artifact``.

.. code-block:: python

    >>> from astroquery.srcnet import DataDiscovery

get_tables
^^^^^^^^^^

List all tables available in the data discovery TAP service.

.. code-block:: python

    >>> DataDiscovery.get_tables()

get_columns
^^^^^^^^^^^

Inspect column definitions for a given CAOM2 table.

.. code-block:: python

    >>> DataDiscovery.get_columns("caom2.Observation")
    >>> DataDiscovery.get_columns("caom2.Plane")

get_collections
^^^^^^^^^^^^^^^

Return all data collections present in the archive with their observation counts.

.. code-block:: python

    >>> DataDiscovery.get_collections()

query_region
^^^^^^^^^^^^

Cone search: find observations within a given angular radius of sky coordinates.

.. code-block:: python

    >>> from astropy.coordinates import SkyCoord
    >>> import astropy.units as u

    >>> results = DataDiscovery.query_region(
    ...     SkyCoord(83.8221, -5.3911, unit="deg"),
    ...     radius=0.5 * u.deg,
    ... )
    >>> results = DataDiscovery.query_region(
    ...     SkyCoord(83.8221, -5.3911, unit="deg"),
    ...     radius=1.0 * u.deg,
    ...     collection="JCMT",
    ... )

query_name
^^^^^^^^^^

Find observations by target name using a case-insensitive substring match.

.. code-block:: python

    >>> t = DataDiscovery.query_name("M31")
    >>> t = DataDiscovery.query_name("Crab", collection="JCMT")

query_observations
^^^^^^^^^^^^^^^^^^

Query observations with optional keyword filters on collection, telescope,
instrument, and target name — no ADQL required.

.. code-block:: python

    >>> t = DataDiscovery.query_observations(
    ...     collection="JCMT",
    ...     instrument="SCUBA-2",
    ...     target_name="Orion",
    ... )

get_artifacts
^^^^^^^^^^^^^

Return all file artifacts (URIs, product types, and sizes) associated with a
given observation ID.

.. code-block:: python

    >>> t = DataDiscovery.get_artifacts("scuba2_00001_20230101T000000")
    >>> t["uri", "productType", "contentLength"]

query (raw ADQL)
^^^^^^^^^^^^^^^^

Execute an arbitrary ADQL statement against the CAOM2 TAP service.

.. code-block:: python

    >>> t = DataDiscovery.query("""
    ...     SELECT o.collection, COUNT(*) AS n
    ...     FROM caom2.Observation AS o
    ...     GROUP BY o.collection
    ...     ORDER BY n DESC
    ... """)

nl_to_adql (data)
^^^^^^^^^^^^^^^^^

Translate a plain-English question into a valid CAOM2 ADQL query using the
SRCNet remote chat service — no local model setup required.

.. code-block:: python

    >>> adql = DataDiscovery.nl_to_adql("how many observations per telescope?")
    >>> print(adql)
    SELECT telescope_name, COUNT(*) AS n
    FROM caom2.Observation
    GROUP BY telescope_name ORDER BY n DESC

query_natural (data)
^^^^^^^^^^^^^^^^^^^^

Translate a natural-language question into ADQL and execute it against the
CAOM2 TAP service in a single call.

.. code-block:: python

    >>> adql, t = DataDiscovery.query_natural(
    ...     "show 5 recent JCMT observations of Orion",
    ...     verbose=True,   # prints the generated ADQL before running
    ... )
    [ADQL] SELECT TOP 5 ...

    >>> adql, t = DataDiscovery.query_natural(
    ...     "find all observations within 1 degree of RA=83.8, Dec=-5.4"
    ... )


Chat Interface
--------------

``SRCNet.chat()`` is a multi-turn conversational interface backed by the SRCNet
remote chat service.  No local model installation or server start-up is needed.

Call with no arguments to print usage instructions and Python examples:

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> SRCNet.chat()

Call with a question to get an answer and, where applicable, a live
``astropy.table.Table`` fetched from the TAP service:

chat (software discovery)
^^^^^^^^^^^^^^^^^^^^^^^^^^

Ask questions about registered software in plain English — the chat generates
and executes the ADQL on your behalf.

.. code-block:: python

    >>> SRCNet.chat("Show me all stable software that requires a GPU")
    >>> SRCNet.chat("Which of those have a Docker image for amd64?")   # follow-up
    >>> SRCNet.chat("What is WSClean and how do I run it?")
    >>> SRCNet.chat("How many entries are there per status category?")

chat (data discovery)
^^^^^^^^^^^^^^^^^^^^^

Ask questions about observational data — the chat translates your question into
a CAOM2 ADQL query and returns the results.

.. code-block:: python

    >>> SRCNet.chat("How many JCMT observations are there in total?")
    >>> SRCNet.chat("Break that down by instrument")   # follow-up
    >>> SRCNet.chat("Find observations of Orion within 1 degree")

chat (library usage)
^^^^^^^^^^^^^^^^^^^^

Ask how to use the ``astroquery.srcnet`` library — the chat reads the actual
source code and returns working Python examples.

.. code-block:: python

    >>> SRCNet.chat("How do I authenticate and download a file?")
    >>> SRCNet.chat("What does the query_natural method do for DataDiscovery?")
    >>> SRCNet.chat("Show me how to create a DataDiscovery object and run a natural language query")

Reset the session
^^^^^^^^^^^^^^^^^

Clear conversation history to start a new independent session.

.. code-block:: python

    >>> SRCNet._chat.reset()

Suppress display / use results programmatically
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Pass ``display=False`` to skip markdown rendering and work with the returned
Table directly.

.. code-block:: python

    >>> t = SRCNet.chat(
    ...     "Give me all stable software with a Docker artifact",
    ...     display=False,
    ... )
    >>> if t is not None:
    ...     print(t["uri", "status"])


Development
-----------

The following assumes that development is against the mirrored GitLab registry.

Because the astroquery contributing guide states that external dependencies are not desirable, the client
functionality that comes with each API (i.e. the calls to each API REST interface) has to be duplicated here.

Install
^^^^^^^

For easy development, clone the repository and install with package symlinks so you can change the code and run
without reinstalling the package:

.. code-block:: bash

    ska-src-astroquery$ python3 -m pip install -e .

On commit to main the Python package will be created by the CI pipeline. For this to build, you must first delete the
existing package with the same version before commit otherwise the job will fail. Alternatively you can change the
package version number.

Docs
^^^^

To manually build the docs first install ``sphinx-astropy``:

.. code-block:: bash

    $ python3 -m pip install sphinx-astropy

Then run the sphinx ``html`` Make target:

.. code-block:: bash

    ska-src-astroquery$ cd docs && make html

The docs are automatically made into a GitLab page by the CI pipeline.

Footnotes
---------

.. [1] Fixed in v1.8.3 but not yet deployed.
