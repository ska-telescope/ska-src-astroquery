import base64
import json
import os
import random
import requests
import time
from functools import wraps

import pyvo
import astropy.units
from astropy import log
from astroquery.query import BaseQuery, BaseVOQuery
from astroquery.utils import commons, async_to_sync
from astroquery.utils.class_or_instance import class_or_instance
from astropy.coordinates import SkyCoord
from astropy import units as u
from pyvo.dal.adhoc import DatalinkResults
from . import conf

from astroquery.srcnet.exceptions import (handle_exceptions,
    NoAccessTokenFoundInResponse,QueryRegionSearchAreaAmbiguous,
    QueryRegionSearchAreaUndefined, UnsupportedAccessProtocol, UnsupportedOIDCFlow)

__all__ = ['SRCNet', 'SRCNetClass']  # specifies what to import


@handle_exceptions
def exchange_token_for_service(service):
    """ Decorator to exchange an existing access token for one with an audience
    corresponding to the required service.
    """
    def exchange_token(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if self.access_token and self.refresh_token:
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
                log.debug("No access token or refresh token are set, will not attempt "
                          "token exchange.")
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
            log.debug("No access token or refresh token are set, will not attempt token refresh.")
        return func(self, *args, **kwargs)
    return wrapper


class SRCNetClass(BaseVOQuery, BaseQuery):
    srcnet_authn_api_address = conf.SRCNET_AUTHN_API_ADDRESS
    srcnet_dm_api_base_address = conf.SRCNET_DM_API_ADDRESS
    srcnet_tap_service_url_base = conf.SRCNET_TAP_SERVICE_URL_BASE
    srcnet_datalink_service_url = conf.SRCNET_DATALINK_SERVICE_URL
    srcnet_ivoa_obscore_table_name = conf.SRCNET_IVOA_OBSCORE_TABLE_NAME
    srcnet_ivoa_obscore_ra_col_name = conf.SRCNET_IVOA_OBSCORE_RA_COL_NAME
    srcnet_ivoa_obscore_dec_col_name = conf.SRCNET_IVOA_OBSCORE_DEC_COL_NAME

    def __init__(self, *args, access_token=None, refresh_token=None, access_token_path='/tmp/access_token',
                 refresh_token_path='/tmp/refresh_token', verbose=False):
        super().__init__()

        self.session = requests.Session()

        # persist access and refresh tokens, if set
        if access_token_path and not access_token:
            if os.path.isfile(access_token_path):
                with open(access_token_path, 'r') as f:
                    access_token = f.read()
            self.access_token_path = access_token_path

        if refresh_token_path and not refresh_token:
            if os.path.isfile(refresh_token_path):
                with open(refresh_token_path, 'r') as f:
                    refresh_token = f.read()
            self.refresh_token_path = refresh_token_path

        self._access_token = access_token
        self._refresh_token = refresh_token
        self._update_authorisation_requests_session()

        if verbose:
            log.setLevel('DEBUG')
        else:
            log.setLevel('INFO')

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

    def _create_tap_query(self, query, sync):
        """ Constructs a TAP query but doesn't execute/submit it."

        :param str query: An ADQL query.
        :param bool sync: Run in synchronous mode?

        :return: The query.
        :rtype: pyvo.dal.TAPQuery
        """
        query = pyvo.dal.TAPQuery(
            baseurl=self.srcnet_tap_service_url_base,
            query=query,
            mode='sync' if sync else 'async',
            language='ADQL'
        )
        return query

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

    def _login_via_auth_code(self):
        """ Begin an OIDC authorization_code login flow. "

        :return: A token.
        :rtype: Dict
        """

        login_endpoint = "{api_url}/login".format(api_url=self.srcnet_authn_api_address)
        token_endpoint = "{api_url}/token".format(api_url=self.srcnet_authn_api_address)

        # redirect user to IAM
        resp = self.session.get(login_endpoint)
        resp.raise_for_status()
        print("To login, please sign in here: {}\n".format(resp.json().get(
            'authorization_uri')))

        # retrieve the code from IAM and exchange for a token
        print("After you have signed in, please enter the returned authorisation code "
              "and state.")
        code = input("Enter code: ")
        state = input("Enter state: ")
        resp = self.session.get(token_endpoint, params={
            "code": code,
            "state": state
        })
        resp.raise_for_status()
        return resp.json()

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

    @handle_exceptions
    @exchange_token_for_service('data-management-api')
    @refresh_token_if_expired
    def get_data(self, namespace, name):
        # query DM API to locate file
        locate_data_endpoint = "{api_url}/data/locate/{namespace}/{name}?sort=random".format(
            api_url=self.srcnet_dm_api_base_address, namespace=namespace, name=name)
        resp = self.session.get(locate_data_endpoint)
        resp.raise_for_status()
        access_url = random.choice(list(resp.json().items()))[1][0]

        ''' 
        # This uses the Datalink service.
        #
        # query the datalink service for the identifier
        datalink_url = '{datalink_base_url}?id={namespace}:{name}'.format(
            datalink_base_url=self.srcnet_datalink_service_url,
            namespace=namespace,
            name=name
        )

        datalink = DatalinkResults.from_result_url(datalink_url)

        # take the link with semantic "#this"
        this = next(datalink.bysemantics("#this"))

        # get the physical file path (on storage) from this link
        access_url = this.access_url
        '''
        if access_url.startswith('https') or access_url.startswith('davs'):
            pass
        else:
            raise UnsupportedAccessProtocol(access_url.split(':')[0])


        # get a token for storage
        get_download_token_namespace_endpoint = "{api_url}/data/download/{namespace}".format(
            api_url=self.srcnet_dm_api_base_address, namespace=namespace)
        resp = self.session.get(get_download_token_namespace_endpoint)
        resp.raise_for_status()

        storage_access_token = resp.json().get('access_token')

        # download
        resp = requests.get(access_url, headers={
            'Authorization': 'Bearer {access_token}'.format(access_token=storage_access_token)
        }, stream=True)
        resp.raise_for_status()
        with open(name, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024):
                print("{}KB downloaded".format(round(os.path.getsize(name) / 1024), 0), end='\r')
                f.write(chunk)
                f.flush()
        print('\n')

    @handle_exceptions
    def login(self, requested_oidc_flow='authorization_code'):
        """ Log in using an OIDC flow.

        Updates authorisation in the instance's session header if successful.

        :return: Nothing.
        :rtype: None
        """
        # start an OIDC flow
        if requested_oidc_flow == 'authorization_code':
            resp = self._login_via_auth_code()
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

    @handle_exceptions
    def query_tap(self, query, sync=True):
        """ Run a TAP query either synchronously or asynchronously.

        :param str query: An ADQL query.
        :param bool sync: Run in synchronous mode?

        :return: The query response.
        :rtype: pyvo.dal.TAPResults or requests.Response
        """
        query = self._create_tap_query(query=query, sync=sync)
        if sync:
            rtn = query.execute()
        else:
            rtn = query.submit()
        return rtn

    @handle_exceptions
    def query_object(self, object_name, radius=None, width=None, height=None,
                     columns='*', row_limit=100, sync=True):
        """ Search around a given object name.

        :param str object_name: The object name to be resolved.
        :param float radius: Search radius (deg).
        :param float width: Search width (deg).
        :param float height: Search height (deg).
        :param list columns: The table columns to include in the results.
        :param int row_limit: Maximum number of rows to return.
        :param bool sync: Run in synchronous mode?

        :return: The query response.
        :rtype: pyvo.dal.TAPResults or requests.Response
        """
        coordinates = SkyCoord.from_name(object_name, frame="icrs")
        return self.query_region(
            coordinates=coordinates,
            radius=radius,
            width=width,
            height=height,
            columns=columns,
            row_limit=row_limit,
            sync=sync
        )

    @handle_exceptions
    def query_region(self, coordinates, radius=None, width=None, height=None,
                     columns='*', row_limit=100, sync=True):
        """ Search around given coordinates.

        :param str coordinates: The coordinates to search around.
        :param float radius: Search radius (deg).
        :param float width: Search width (deg).
        :param float height: Search height (deg).
        :param str columns: The table columns to include in the results.
        :param int row_limit: Maximum number of rows to return.
        :param bool sync: Run in synchronous mode?

        :return: The query response.
        :rtype: pyvo.dal.TAPResults or requests.Response
        """
        parsed_coordinates = commons.parse_coordinates(coordinates)
        if all([radius, width, height]):
            raise QueryRegionSearchAreaAmbiguous
        if radius:
            query = """
                    SELECT
                      {columns},
                      DISTANCE(
                        POINT('ICRS', {ra_column_name}, {dec_column_name}),
                        POINT('ICRS', {ra}, {dec})
                      ) AS dist
                    FROM
                      {table_name}
                    WHERE
                      1 = CONTAINS(
                        POINT('ICRS', {ra_column_name}, {dec_column_name}),
                        CIRCLE('ICRS', {ra}, {dec}, {radius})
                      )
                    ORDER BY
                      dist ASC
                    """.format(
                columns='{}.*'.format(self.srcnet_ivoa_obscore_table_name)
                    if columns == '*' else ','.join(columns),
                ra_column_name=self.srcnet_ivoa_obscore_ra_col_name,
                dec_column_name=self.srcnet_ivoa_obscore_dec_col_name,
                ra=parsed_coordinates.ra.deg,
                dec=parsed_coordinates.dec.deg,
                table_name=self.srcnet_ivoa_obscore_table_name,
                radius=(radius*u.deg).to('arcmin').value
            )
        elif width and height:
            query = """
                    SELECT
                      {columns},
                      DISTANCE(
                        POINT('ICRS', {ra_column_name}, {dec_column_name}),
                        POINT('ICRS', {ra}, {dec})
                      ) as dist
                    FROM
                      {table_name}
                    WHERE
                      1 = CONTAINS(
                        POINT('ICRS', {ra_column_name}, {dec_column_name}),
                        BOX('ICRS', {ra}, {dec}, {width}, {height})
                      )
                    ORDER BY
                      dist ASC
                    """.format(
                columns='{}.*'.format(self.srcnet_ivoa_obscore_table_name)
                    if not columns else ','.join(columns),
                ra_column_name=self.srcnet_ivoa_obscore_ra_col_name,
                dec_column_name=self.srcnet_ivoa_obscore_dec_col_name,
                ra=parsed_coordinates.ra.deg,
                dec=parsed_coordinates.dec.deg,
                table_name=self.srcnet_ivoa_obscore_table_name,
                width=(width*u.deg).to('arcmin').value,
                height=(width*u.deg).to('arcmin').value
            )
        else:
            raise QueryRegionSearchAreaUndefined

        return self.query_tap(query=query, sync=sync)


SRCNet = SRCNetClass()

