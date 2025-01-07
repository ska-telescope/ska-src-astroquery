import base64
import json
import os
import qrcode
import random
import requests
import sys
import textwrap
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
        user_instruction_text = ("Scan the QR code, or using a browser on another device, visit " +
                                 "{verification_uri} and enter code {user_code}".format(
                                     verification_uri=device_authorization_response.json().get('verification_uri'),
                                     user_code=device_authorization_response.json().get('user_code')))

        wrapped_string = textwrap.fill(user_instruction_text, width=50)
        print()
        print("-" * 50)
        print()
        print(wrapped_string)
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
            except Exception as e:
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

    @handle_exceptions
    @exchange_token_for_service('data-management-api')
    @refresh_token_if_expired
    def get_data(self, namespace, name, sort='nearest_by_ip', ip_address=None):
        """ Locate and download data given a data identifier.

        :param str namespace: The data identifier's namespace.
        :param str name: The data identifier's name.
        :param str sort: The sorting algorithm to use (random || nearest_by_ip)
        :param str ip_address: The ip address to geolocate the nearest replica to. Leave blank to use the requesting
            client ip (sort == nearest_by_ip only)

        :return: Nothing
        :rtype: None
        """
        # query DM API to get a list of data replicas for this namespace/name and pick the first
        locate_data_endpoint = "{api_url}/data/locate/{namespace}/{name}?sort={sort}&ip_address={ip_address}".format(
            api_url=self.srcnet_dm_api_base_address, namespace=namespace, name=name, sort=sort,
            ip_address=ip_address if ip_address else "")
        resp = self.session.get(locate_data_endpoint)
        resp.raise_for_status()
        location_response = resp.json()

        # pick the first replica from the first site in the response (ordered if sorting algorithm is used)
        position = 0
        rse = location_response[position].get('identifier')
        replicas = location_response[position].get('replicas')
        associated_storage_area_id = location_response[position].get('associated_storage_area_id')

        # pick a random replica from this site (only relevant if multiple exist)
        access_url = random.choice(replicas)

        # download the data
        log.info("Downloading data from {rse} ({access_url})".format(rse=rse, access_url=access_url))
        if access_url.startswith('https') or access_url.startswith('davs'):
            pass
        else:
            raise UnsupportedAccessProtocol(access_url.split(':')[0])

        # get the storage read access token for the associated storage area
        get_download_token_namespace_endpoint = "{api_url}/data/download/{storage_area_uuid}/{namespace}/{name}".format(
            api_url=self.srcnet_dm_api_base_address, storage_area_uuid=associated_storage_area_id, namespace=namespace,
            name=name)
        resp = self.session.get(get_download_token_namespace_endpoint)
        resp.raise_for_status()
        storage_access_token = resp.json().get('access_token')

        # download data using this access token
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

    @handle_exceptions
    def soda_cutout(self, soda_url, dataset_path, file_name, filtering_parameter, 
                    ra, dec, radius, output_path, output_filename):
        """ Perform SODA cutout

        :param str soda_url (URL for the SODA service)
        :param str dataset_path.
        :param str file_name.
        :param str filtering_parameter (CIRCLE).
        :param float ra (ICRS in deg).
        :param float dec (ICRS in deg).
        :param float radius (deg).
        :param str output_path.
        :param str output_filename.

        :return: The query response.
        :rtype: FITS file
        """

        # Get an SKA IAM token
        if not self.access_token:
            log.info("No valid access token found. Logging in...")
            self.login()

        # Only "CIRCLE" works for now
        if filtering_parameter == "CIRCLE":
            circle_str = f"{ra} {dec} {radius}"
        else:
            raise ValueError(f"Unsupported filtering parameter: {filtering_parameter}")

        dataset_path_and_file_name = f"{dataset_path}/{file_name}"
        full_id = f"ivo://test.skao/datasets/fits?{dataset_path_and_file_name}"

        # Make the request to SODA
        params = {
            "ID": full_id,
            filtering_parameter: circle_str,     # e.g. "CIRCLE": "351.986728 8.778684 0.1"
            "RESPONSE_FORMAT": "application/fits"
        }

        log.info(f"Requesting SODA cutout from {soda_url} with params={params}")
        response = self.session.get(soda_url, params=params, stream=True)
        response.raise_for_status()

        # Write the SODA response to the output file
        # Should it use get_data method here?
        os.makedirs(output_path, exist_ok=True)
        output_path_and_filename = os.path.join(output_path, output_filename)
        with open(output_path_and_filename, "wb") as f:
            for chunk in response.iter_content(chunk_size=4096):
                f.write(chunk)
                f.flush()
        print('\n')

        log.info(f"SODA cutout saved to '{output_path_and_filename}'")

        return output_path_and_filename

SRCNet = SRCNetClass()

