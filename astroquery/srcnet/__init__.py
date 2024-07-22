from astropy import config as _config


class Conf(_config.ConfigNamespace):
    """
    Configuration parameters for `astroquery.srcnet`.
    """
    SRCNET_AUTHN_API_ADDRESS = _config.ConfigItem(
        'https://authn.srcdev.skao.int/api/v1',
        'Base address of the SRCNet AuthN API'
        )
    SRCNET_DM_API_ADDRESS = _config.ConfigItem(
        'https://data-management.srcdev.skao.int/api/v1',
        'Base address of the SRCNet Data Management API'
        )
    SRCNET_TAP_SERVICE_URL_BASE = _config.ConfigItem(
        'https://dachs.ivoa.srcdev.skao.int/__system__/tap/run',
        'SRCNet TAP service URL'
    )
    SRCNET_DATALINK_SERVICE_URL = _config.ConfigItem(
        'https://datalink.ivoa.srcdev.skao.int/rucio/links',
        'SRCNET Datalink service URL'
    )
    SRCNET_IVOA_OBSCORE_TABLE_NAME = _config.ConfigItem(
        'ivoa.obscore',
        'ObsCore table name'
    )
    SRCNET_IVOA_OBSCORE_RA_COL_NAME = _config.ConfigItem(
        'S_ra',
        'RA column name in IVOA ObsCore table'
    )
    SRCNET_IVOA_OBSCORE_DEC_COL_NAME = _config.ConfigItem(
        'S_dec',
        'DEC column name in IVOA ObsCore table'
    )

conf = Conf()

from .core import SRCNet, SRCNetClass

__all__ = ['SRCNet', 'SRCNetClass', 'conf']
