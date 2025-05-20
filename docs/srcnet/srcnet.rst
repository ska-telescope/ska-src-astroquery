.. _astroquery.srcnet:

***********************************************************************************
SRCNet queries (`astroquery.srcnet`)
***********************************************************************************

Overview
--------

`astroquery.srcnet` provides the astroquery interface to the prototype SRCNet. It currently supports object/region
based querying and data retrieval.

Quickstart
----------

.. code-block:: bash

    python3 -m pip install astroquery --index-url https://gitlab.com/api/v4/projects/53653803/packages/pypi/simple

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> srcnet=SRCNet(verbose=True)
    >>> srcnet.login()                                                         # should only be run once every hour maximum
    >>> q1=srcnet.query_region(coordinates='82.1deg 12.58deg', radius=0.01)    # example query_region
    >>> q2=srcnet.query_object(object_name='PTF10tce', radius=0.01)            # example query_object
    >>> q3=srcnet.get_data(namespace='testing', name='PTF10tce.fits')          # example get_data

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

Install
-------

To install the package, install via pip from the remote package registry:

.. code-block:: bash

    python3 -m pip install astroquery --index-url https://gitlab.com/api/v4/projects/53653803/packages/pypi/simple

Examples
--------

The following examples presume that the user has previously logged in as above and has persisted their tokens.

query_region
^^^^^^^^^^^^

Query for results around a region.

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> srcnet=SRCNet()
    >>> srcnet.query_region(coordinates='82.1deg 12.58deg', radius=0.01)
    >>>
    >>> <Table length=1>
    >>> dataproduct_type dataproduct_subtype calib_level obs_collection       obs_id      ... em_ucd preview  source_table         dist
    >>>                                                                                   ...                                      deg
    >>>      object             object          int16        object           object      ... object  object     object          float64
    >>> ---------------- ------------------- ----------- -------------- ----------------- ... ------ ------- ------------- --------------------
    >>>            image                               2           RACS RACS-DR1_0528+12A ...                rucio.obscore 0.009205321609571323

query_object
^^^^^^^^^^^^

Resolve an object and query for results around it.

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> srcnet=SRCNet()
    >>> srcnet.query_object(object_name='PTF10tce', radius=0.01)
    >>>
    >>> <Table length=1>
    >>> dataproduct_type dataproduct_subtype calib_level   obs_collection   ... em_ucd preview  source_table          dist
    >>>                                                                     ...                                       deg
    >>>      object             object          int16          object       ... object  object     object           float64
    >>> ---------------- ------------------- ----------- ------------------ ... ------ ------- ------------- ---------------------
    >>>                                                1 collection_testing ...                rucio.obscore 7.176247592607064e-05

get_data
^^^^^^^^

Get data from the datalake given a namespace and name.

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> srcnet=SRCNet(verbose=True)
    >>> srcnet.get_data(namespace='testing', name='PTF10tce.fits')

    >>> INFO: Exchanged authn-api service token for data-management-api service [astroquery.srcnet.core]
    >>> DEBUG: Access token: <redacted>
    >>> DEBUG: Refresh token: <redacted>
    >>> DEBUG: Persisting access token to: /tmp/access_token [astroquery.srcnet.core]
    >>> DEBUG: Persisting refresh token to: /tmp/refresh_token [astroquery.srcnet.core]
    >>> DEBUG: Access token is valid, will not attempt token refresh. [astroquery.srcnet.core]
    >>> 8248KB downloaded

soda_cutout
^^^^^^^^^^^

Use the SODA service to request a cutout of the specified dataset within a circular region of the sky.

.. code-block:: python

    >>> from astroquery.srcnet import SRCNet
    >>> srcnet=SRCNet(verbose=True)
    >>> srcnet.soda_cutout(namespace='testing', name='PTF10tce.fits', circle=(351.986728, 8.778684, 0.1), output_file="output/soda-cutout-test.fits")

    >>> DEBUG: Using Datalink: https://datalink.ivoa.srcnet.skao.int/rucio/links?id=testing%3APTF10tce.fits&must_include_soda=True&sort=nearest_by_ip [astroquery.srcnet.core]
    >>> DEBUG: Extracted SODA Service: https://gatekeeper.srcdev.skao.int:443/soda/ska/datasets/soda [astroquery.srcnet.core]
    >>> DEBUG: Extracted ID: ivo://auth.example.org/datasets/fits?testing/5b/f5/PTF10tce.fits [astroquery.srcnet.core]
    >>> INFO: Requesting SODA cutout from https://gatekeeper.srcdev.skao.int:443/soda/ska/datasets/soda with params={'ID': 'ivo://auth.example.org/datasets/fits?testing/5b/f5/PTF10tce.fits', 'RESPONSEFORMAT': 'application/fits', 'POS': 'CIRCLE 351.986728 8.778684 0.1'} [astroquery.srcnet.core]
    >>> DEBUG: SODA cutout saved to 'output/soda-cutout-test.fits' [astroquery.srcnet.core]

Format Factory
^^^^^^^^^^^^^^

Use the preliminary Format Factory to automatically detect a dataset's type using its Rucio `Obscore metadata <https://gitlab.com/ska-telescope/src/src-mm/ska-src-mm-rucio-ivoa-integrations/-/blob/main/postgres-metadata/etc/init/dachs/02-rucio.sql?ref_type=heads#L12>`_, and access format-specific methods such as listing metadata and performing SODA cutouts (Renamed as ``subcube`` for 3D image cubes and ``cutout`` for 2D images).

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
