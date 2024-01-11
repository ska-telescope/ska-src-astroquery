import pyvo
import astropy.units
from astroquery.query import BaseQuery, BaseVOQuery
from astroquery.utils import commons, async_to_sync
from astroquery.utils.class_or_instance import class_or_instance
from astropy.coordinates import SkyCoord
from astropy import units as u
from . import conf

from astroquery.srcnet.exceptions import (handle_exceptions,
    QueryRegionSearchAreaAmbiguous, QueryRegionSearchAreaUndefined)

__all__ = ['SRCNet', 'SRCNetClass']  # specifies what to import


class SRCNetClass(BaseVOQuery, BaseQuery):
    srcnet_dm_api_base_address = conf.SRCNET_DM_API_ADDRESS
    srcnet_tap_service_url_base = conf.SRCNET_TAP_SERVICE_URL_BASE
    srcnet_datalink_service_url = conf.SRCNET_DATALINK_SERVICE_URL
    srcnet_ivoa_obscore_table_name = conf.SRCNET_IVOA_OBSCORE_TABLE_NAME
    srcnet_ivoa_obscore_ra_col_name = conf.SRCNET_IVOA_OBSCORE_RA_COL_NAME
    srcnet_ivoa_obscore_dec_col_name = conf.SRCNET_IVOA_OBSCORE_DEC_COL_NAME

    def __init__(self, *args, bearer_token=None):
        super().__init__()
        if bearer_token:
            self.bearer_token = bearer_token

    def _create_tap_query(self, query, sync):
        query = pyvo.dal.TAPQuery(
            baseurl=self.srcnet_tap_service_url_base,
            query=query,
            mode='sync' if sync else 'async',
            language='ADQL'
        )
        return query

    @class_or_instance
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

    @class_or_instance
    def query_object(self, object_name, radius=None, width=None, height=None,
                     columns='*', row_limit=100, sync=True, verbose=False):
        """ Search around a given object name.

        :param str object_name: The object name to be resolved.
        :param float radius: Search radius (deg).
        :param float width: Search width (deg).
        :param float height: Search height (deg).
        :param list columns: The table columns to include in the results.
        :param int row_limit: Maximum number of rows to return.
        :param bool sync: Run in synchronous mode?
        :param bool verbose: Run in verbose mode?

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
            sync=sync,
            verbose=verbose
        )

    @class_or_instance
    def query_region(self, coordinates, radius=None, width=None, height=None,
                     columns='*', row_limit=100, sync=True, verbose=False):
        """ Search around given coordinates.

        :param str coordinates: The coordinates to search around.
        :param float radius: Search radius (deg).
        :param float width: Search width (deg).
        :param float height: Search height (deg).
        :param str columns: The table columns to include in the results.
        :param int row_limit: Maximum number of rows to return.
        :param bool sync: Run in synchronous mode?
        :param bool verbose: Run in verbose mode?

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
                radius=(radius*u.deg).to('arcmin').value,
                row_limit=row_limit
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
                height=(width*u.deg).to('arcmin').value,
                row_limit=row_limit
            )
        else:
            raise QueryRegionSearchAreaUndefined

        return self.query_tap(query=query, sync=sync)

SRCNet = SRCNetClass()

