import base64
import json
import os
import qrcode
import requests
import time
from functools import wraps

from astropy import log
from astroquery.query import BaseQuery, BaseVOQuery
from . import conf

from astroquery.srcnet.exceptions import (handle_exceptions,
    NoAccessTokenFoundInResponse,UnsupportedOIDCFlow)

__all__ = ['SRCNet', 'SRCNetClass']

_CHAT_HELP = """\
SRCNet Chat
===========

The chat service answers questions about software registered in the SKA
Software Discovery catalogue, CAOM2 observational data in the data discovery
TAP service, and how to use the astroquery.srcnet library.

Conversation history is preserved between calls so you can ask follow-up
questions without repeating context.  Call SRCNet._chat.reset() to start fresh.

─── Asking questions ────────────────────────────────────────────────────────

  SRCNet.chat("Show me all stable software that requires a GPU")
  SRCNet.chat("Which of those support amd64 architecture?")   # follow-up

  SRCNet.chat("How many observations are there per collection?")
  SRCNet.chat("Find JCMT observations of Orion from 2023")

  SRCNet.chat("How do I authenticate and download a file with SRCNet?")
  SRCNet.chat("What methods does DataAccessClass expose?")

  SRCNet._chat.reset()   # clear history

─── Software Discovery — without chat ───────────────────────────────────────

  from astroquery.srcnet import SoftwareDiscovery

  # Keyword filters
  t = SoftwareDiscovery.query_software(status="STABLE", requires_gpu=True)
  t = SoftwareDiscovery.query_software(science_category="Continuum Science")

  # Single entry by URI
  t = SoftwareDiscovery.get_software("ska:wsclean:docker-wsclean@3.4")

  # Search by Docker image name
  t = SoftwareDiscovery.query_by_image("wsclean")

  # Natural language → ADQL (inspect before running)
  adql = SoftwareDiscovery.nl_to_adql("list Docker images for continuum imaging")
  print(adql)

  # Natural language → ADQL + execute in one step
  adql, t = SoftwareDiscovery.query_natural(
      "show stable software that requires a GPU and supports amd64",
      verbose=True,   # prints the generated ADQL
  )

─── Data Discovery — without chat ───────────────────────────────────────────

  from astropy.coordinates import SkyCoord
  import astropy.units as u
  from astroquery.srcnet import DataDiscovery

  # Browse the archive
  DataDiscovery.get_collections()
  DataDiscovery.get_tables()

  # Cone search
  t = DataDiscovery.query_region(
      SkyCoord(83.8, -5.4, unit="deg"),
      radius=0.5 * u.deg,
  )

  # Target name search
  t = DataDiscovery.query_name("Orion", collection="JCMT")

  # Natural language → ADQL (inspect before running)
  adql = DataDiscovery.nl_to_adql("how many observations per collection?")
  print(adql)

  # Natural language → ADQL + execute in one step
  adql, t = DataDiscovery.query_natural(
      "show the 10 most recent JCMT observations with target name and exposure time",
      verbose=True,   # prints the generated ADQL
  )

─── Data Access — download, metadata, SODA cutouts ──────────────────────────

  SRCNet.login()                            # OIDC device flow — required once
  da = SRCNet.get_data_access()             # DataAccessClass

  # Download a file to the current directory
  da.get_data("testing", "PTF10tce.fits")

  # Retrieve JSON metadata
  meta = da.get_metadata("testing", "PTF10tce.fits")

  # Circular SODA spatial cutout
  da.soda_cutout("testing", "PTF10tce.fits", "output/cutout.fits",
                 circle=(351.9867, 8.7787, 0.1))

  # Spectral cutout within a spatial region
  da.soda_cutout("testing", "cube.fits", "output/subcube.fits",
                 circle=(83.8, -5.4, 0.5), band="0.0002 0.0003")

─── Suppress display / use results programmatically ─────────────────────────

  t = SRCNet.chat(
      "Give me all stable software with a Docker artifact",
      display=False,   # skip markdown rendering, just return the Table
  )
  if t is not None:
      print(t["uri", "status"])
"""


@handle_exceptions
def exchange_token_for_service(service):
    """ Decorator to exchange an existing access token for one with an audience
    corresponding to the required service.
    """
    def exchange_token(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if self.access_token and self.refresh_token:                                                                #FIXME: Can exchange token rather than using refresh flow in v1.8.3
                audience = self._decode_access_token().get('aud')
                if audience != service:
                    exchange_token_endpoint = \
                        "{api_url}/token/exchange/{service}".format(
                            api_url=self.srcnet_authn_api_address, service=service)
                    resp = self.session.get(exchange_token_endpoint, params={
                        "version": 'latest',
                        "try_use_cache": True,
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token
                    })
                    resp.raise_for_status()

                    log.info(
                        "Exchanged {from_service} service token for {to_service} service".format(
                            from_service=audience,
                            to_service=service
                        )
                    )

                    # parse new tokens
                    token = resp.json()
                    if not token.get('access_token'):
                        raise NoAccessTokenFoundInResponse
                    self.access_token = token.get('access_token')
                    self.refresh_token = token.get('refresh_token', None)

                    log.debug("Access token: {access_token}".format(
                        access_token=self.access_token))
                    log.debug("Refresh token: {refresh_token}".format(
                        refresh_token=self.refresh_token))

                    self._persist_tokens()
                else:
                    log.debug("Access token already exists for service, will not "
                              "attempt token exchange")
            else:
                log.debug("Either access token or refresh token are not set, will not "
                          "attempt token exchange.")
            return func(self, *args, **kwargs)
        return wrapper
    return exchange_token


@handle_exceptions
def refresh_token_if_expired(func):
    """ Decorator to try to refresh an access token if it's expired. """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if self.access_token and self.refresh_token:
            if self._decode_access_token().get('exp'):
                if self._decode_access_token().get('exp') - time.time() + 60 < 0:
                    log.info("Access token expired, attempting to refresh...")
                    refresh_token_endpoint = "{api_url}/refresh".format(
                        api_url=self.srcnet_authn_api_address)
                    resp = self.session.get(refresh_token_endpoint, params={
                        "refresh_token": self.refresh_token
                    })
                    resp.raise_for_status()

                    log.info("Refreshed token.")

                    # parse new tokens
                    token = resp.json()
                    if not token.get('access_token'):
                        raise NoAccessTokenFoundInResponse
                    self.access_token = token.get('access_token')
                    self.refresh_token = token.get('refresh_token', None)

                    log.debug("Access token: {access_token}".format(
                        access_token=self.access_token))
                    log.debug("Refresh token: {refresh_token}".format(
                        refresh_token=self.refresh_token))

                    self._persist_tokens()
                else:
                    log.debug("Access token is valid, will not attempt token refresh.")
        else:
            log.debug("Either access token or refresh token are not set, will not "
                      "attempt token refresh.")
        return func(self, *args, **kwargs)
    return wrapper


class SRCNetClass(BaseVOQuery, BaseQuery):

    def __init__(self, *args, access_token=None, refresh_token=None, access_token_path='/tmp/access_token',
                 refresh_token_path='/tmp/refresh_token', verbose=False, environment=None):
        super().__init__()

        from . import _env_urls, ENVIRONMENTS
        if environment is not None:
            if environment not in ENVIRONMENTS:
                raise ValueError(
                    f"Unknown environment {environment!r}. Valid options: {list(ENVIRONMENTS)}"
                )
            conf.SRCNET_ENVIRONMENT = environment
        urls = _env_urls()
        self.srcnet_authn_api_address      = urls["authn_api"]
        self.srcnet_dm_api_base_address    = urls["dm_api"]
        self.srcnet_data_access_tap_url    = urls["data_access_tap"]
        self.srcnet_datalink_service_url   = urls["datalink"]

        self.session = requests.Session()

        self.access_token_path = access_token_path
        self.refresh_token_path = refresh_token_path

        # check for access tokens in constructor, environment then persisted file (in that order)
        if access_token:
            pass
        elif os.environ.get('ACCESS_TOKEN', False):
            access_token = os.environ.get('ACCESS_TOKEN')
        elif os.path.isfile(access_token_path):
            with open(access_token_path, 'r') as f:
                access_token = f.read()
        self._access_token = access_token
        self._update_authorisation_requests_session()       # use this access token as the bearer token for requests

        # check for refresh tokens in constructor, environment then persisted file (in that order)
        if refresh_token:
            pass
        elif os.environ.get('REFRESH_TOKEN', False):
            refresh_token = os.environ.get('REFRESH_TOKEN')
        elif os.path.isfile(refresh_token_path):
            with open(refresh_token_path, 'r') as f:
                refresh_token = f.read()
        self._refresh_token = refresh_token

        if verbose:
            log.setLevel('DEBUG')
        else:
            log.setLevel('INFO')

        from .chat import SRCNetChat
        from .software_discovery import SoftwareDiscoveryClass
        from .data_discovery import DataDiscoveryClass
        from .data_access import DataAccessClass
        self._sd          = SoftwareDiscoveryClass(tap_url=urls["software_tap"])
        self._tap_client  = DataDiscoveryClass(tap_url=urls["tap"])
        self._chat        = SRCNetChat(self._sd, backend="chatserver",
                                       chatserver_url=urls["chat"])
        self._data_access = DataAccessClass(self)

    @property
    def access_token(self):
        return self._access_token

    @access_token.setter
    def access_token(self, new_access_token):
        self._access_token = new_access_token
        self._update_authorisation_requests_session()

    @property
    def refresh_token(self):
        return self._refresh_token

    @refresh_token.setter
    def refresh_token(self, new_refresh_token):
        self._refresh_token = new_refresh_token

    # ── Factory methods ───────────────────────────────────────────────────────

    def get_tap(self):
        """Return the DataDiscoveryClass configured for the current environment."""
        return self._tap_client

    def get_software_discovery(self):
        """Return the SoftwareDiscoveryClass configured for the current environment."""
        return self._sd

    def get_data_access(self):
        """Return a :class:`~astroquery.srcnet.DataAccessClass` for this environment.

        The returned object exposes download, metadata, and SODA cutout
        operations.  :meth:`login` must be called before using any of these
        methods if the services require authentication.

        Returns
        -------
        :class:`~astroquery.srcnet.DataAccessClass`

        Examples
        --------
        >>> SRCNet.login()
        >>> da = SRCNet.get_data_access()
        >>> da.get_data("testing", "PTF10tce.fits")
        >>> meta = da.get_metadata("testing", "PTF10tce.fits")
        >>> da.soda_cutout("testing", "PTF10tce.fits", "cutout.fits",
        ...                circle=(351.9867, 8.7787, 0.1))
        """
        return self._data_access

    def get_metadata(self, namespace, name):
        """Convenience proxy — delegates to :meth:`~DataAccessClass.get_metadata`."""
        return self._data_access.get_metadata(namespace, name)

    def soda_cutout(self, namespace, name, output_file=None, **kwargs):
        """Convenience proxy — delegates to :meth:`~DataAccessClass.soda_cutout`."""
        return self._data_access.soda_cutout(namespace, name, output_file, **kwargs)

    def get_chat(self, chatserver_url: str = None, **kwargs):
        """Return a SRCNetChat configured for the current environment.

        Parameters
        ----------
        chatserver_url : str, optional
            Override the chatserver URL. Defaults to the environment's chat URL.
        **kwargs
            Forwarded to :class:`~astroquery.srcnet.chat.SRCNetChat`.
        """
        from .chat import SRCNetChat
        from . import _env_urls
        cs_url = chatserver_url or _env_urls()["chat"]
        return SRCNetChat(self._sd, backend="chatserver", chatserver_url=cs_url, **kwargs)

    def _decode_access_token(self):
        """ Decode an access token.

        :return: The decoded token.
        :rtype: Dict
        """

        # Split by dot and get middle, payload, part;
        token_payload = self.access_token.split(".")[1]

        # Payload is base64 encoded, let's decode it to plain string
        # To make sure decoding will always work. We're adding max padding ("==")
        # to payload - it will be ignored if not needed.
        token_payload_decoded = str(base64.b64decode(token_payload + "=="), "utf-8")

        return json.loads(token_payload_decoded)

    def _login_via_device(self):
        """ Begin an OIDC device flow. "

        :return: A token.
        :rtype: Dict
        """

        login_endpoint = "{api_url}/login/device".format(api_url=self.srcnet_authn_api_address)
        token_endpoint = "{api_url}/token?device_code={{device_code}}".format(api_url=self.srcnet_authn_api_address)

        # redirect user to IAM
        device_authorization_response = self.session.get(login_endpoint)
        device_authorization_response.raise_for_status()

        # make an ascii qr code for the complete verification uri
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(device_authorization_response.json().get('verification_uri_complete'))

        # add instructional text for user if they don't want to use qr code
        user_instruction_text = ("Scan the QR code, or visit the following URL in your browser to authenticate:\n  " +
                                 "{verification_uri_complete}".format(
                                     verification_uri_complete=device_authorization_response.json().get('verification_uri_complete')))

        print()
        print("-" * 50)
        print()
        print(user_instruction_text)
        qr.print_ascii()
        print("-" * 50)
        print()

        # poll for user to complete authorisation process
        success = False
        max_attempts = 60
        for attempt in range(0, max_attempts):
            try:
                # the following will raise before the break if the authorization is still pending
                token_response = self.session.get(token_endpoint.format(
                    device_code=device_authorization_response.json().get('device_code')))
                token_response.raise_for_status()
                success = True
                break
            except Exception:
                try:
                    log.debug(token_response.json())
                except requests.exceptions.JSONDecodeError:
                    pass
            print("Polling for token... ({attempt}/{max_attempts})".format(
                attempt=attempt + 1, max_attempts=max_attempts), end='\r')
            time.sleep(5)
        print()
        print()
        if success:
            print("Successfully polled for token. You are now logged in.")
            print()
            return token_response.json()
        else:
            print("Failed to poll for token. Please try again.")
            print()
            return {}
        print()


    def _persist_tokens(self):
        """ Save access and refresh tokens.

        :return: Nothing.
        :rtype: None
        """
        if self.access_token_path:
            log.debug("Persisting access token to: {access_token_path}".format(
                access_token_path=self.access_token_path))
            with open(self.access_token_path, 'w') as f:
                f.write(self.access_token)
        if self.refresh_token_path:
            log.debug("Persisting refresh token to: {refresh_token_path}".format(
                refresh_token_path=self.refresh_token_path))
            with open(self.refresh_token_path, 'w') as f:
                f.write(self.refresh_token)

    def _update_authorisation_requests_session(self):
        """ Update the requests session header with the instance's bearer token.

        :return: Nothing.
        :rtype: None
        """
        self.session.headers.update({
            "Authorization": "Bearer {}".format(self.access_token)
        })

    def chat(self, message: str = None, **kwargs):
        """
        Conversational interface to SRCNet services.

        When called with no arguments, prints usage examples and returns ``None``.
        When called with a message, sends it to the SRCNet chat service and
        returns the resulting ``~astropy.table.Table`` (or ``None`` if no TAP
        query was needed).

        The chat service can answer questions about:

        - Software registered in the SKA Software Discovery catalogue
        - CAOM2 observational data in the data discovery TAP service
        - How to use the ``astroquery.srcnet`` library

        Parameters
        ----------
        message : str, optional
            Natural-language question.  Omit to print usage examples.
        **kwargs
            Forwarded to :class:`~astroquery.srcnet.SRCNetChat.__call__`
            (e.g. ``display=False`` to suppress rendering).

        Returns
        -------
        `~astropy.table.Table` or None

        Examples
        --------
        >>> SRCNet.chat()                                         # show help
        >>> SRCNet.chat("Show stable software that needs a GPU")  # query
        >>> SRCNet.chat("Which support amd64?")                   # follow-up
        >>> SRCNet._chat.reset()                                  # new session
        """
        if message is None:
            print(_CHAT_HELP)
            return None
        return self._chat(message, **kwargs)

    @handle_exceptions
    def login(self, requested_oidc_flow='device'):
        """ Log in using an OIDC flow.

        Updates authorisation in the instance's session header if successful.

        :return: Nothing.
        :rtype: None
        """
        # start an OIDC flow
        if requested_oidc_flow == 'device':
            resp = self._login_via_device()
        else:
            raise UnsupportedOIDCFlow(requested_oidc_flow)
        token = resp.get('token', {})

        # get the token from the resulting response
        if not token.get('access_token'):
            raise NoAccessTokenFoundInResponse
        self.access_token = token.get('access_token')
        self.refresh_token = token.get('refresh_token', None)

        log.debug("Access token: {access_token}".format(
            access_token=self.access_token))
        log.debug("Refresh token: {refresh_token}".format(
            refresh_token=self.refresh_token))

        self._persist_tokens()

SRCNet = SRCNetClass()

# Expose the DataAccessClass singleton so `from astroquery.srcnet import DataAccess` works.
from .data_access import DataAccessClass as _DataAccessClass  # noqa: E402
_DataAccessClass.__module__ = __name__  # tidy repr

