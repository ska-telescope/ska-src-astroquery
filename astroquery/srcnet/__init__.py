from astropy import config as _config


# ── Environment definitions ───────────────────────────────────────────────────

#: Entry-point URLs for each SRCNet deployment environment.
#: Switch via ``conf.SRCNET_ENVIRONMENT`` or by setting the
#: ``SRCNET_ENVIRONMENT`` config item.
ENVIRONMENTS = {
    "production": {
        "authn_api":       "https://authn.srcnet.skao.int/api/v1",
        "dm_api":          "https://data-management.srcnet.skao.int/api/v1",
        "tap":             "https://science-metadata.srcnet.skao.int/argus/",
        "data_access_tap": "https://dachs.ivoa.srcnet.skao.int/tap",
        "datalink":        "https://datalink.ivoa.srcnet.skao.int/rucio/links",
        "software_tap":    "https://software-discovery.srcnet.skao.int/tap/",
        "chat":            "https://chat.srcnet.skao.int",
    },
    "preprod": {
        "authn_api":       "https://authn.srcdev.skao.int/api/v1",
        "dm_api":          "https://data-management.srcdev.skao.int/api/v1",
        "tap":             "https://ws.cadc-ccda.hia-iha.nrc-cnrc.gc.ca/argus",
        "data_access_tap": "https://dachs.ivoa.srcnet.skao.int/tap",
        "datalink":        "https://datalink.ivoa.srcdev.skao.int/rucio/links",
        "software_tap":    "https://software-discovery.ral-preprod.uksrc.org/tap/",
        "chat":            "https://chat.srcdev.skao.int",
    },
}

_DEFAULT_ENV = "production"


class Conf(_config.ConfigNamespace):
    """
    Configuration parameters for `astroquery.srcnet`.
    """
    SRCNET_ENVIRONMENT = _config.ConfigItem(
        _DEFAULT_ENV,
        'SRCNet deployment environment. '
        '"production" (default) — live SRCNet services. '
        '"preprod" — pre-production / staging services. '
        'All service URLs are derived from this setting.'
    )
    SRCNET_SOFTWARE_DISCOVERY_TAP_URL = _config.ConfigItem(
        '',
        'Override URL for the Software Discovery TAP service. '
        'Empty = use the URL for the configured SRCNET_ENVIRONMENT.'
    )
    SRCNET_DATA_DISCOVERY_TAP_URL = _config.ConfigItem(
        '',
        'Override URL for the Data Discovery (CAOM2) TAP service. '
        'Empty = use the URL for the configured SRCNET_ENVIRONMENT.'
    )
    SRCNET_OLLAMA_URL = _config.ConfigItem(
        'http://localhost:11434',
        'Base URL for the local Ollama instance used by nl_to_adql().'
    )
    SRCNET_OLLAMA_MODEL = _config.ConfigItem(
        'deepseek-coder-v2',
        'Default Ollama model for nl_to_adql().'
    )
    SRCNET_CHATSERVER_URL = _config.ConfigItem(
        '',
        'CHATSERVER REST API base URL. Empty = use direct Ollama instead.'
    )
    SRCNET_DEFAULT_MAXREC = _config.ConfigItem(
        2000,
        'Default maximum number of rows returned per query.'
    )
    SRCNET_QUERY_TIMEOUT = _config.ConfigItem(
        60,
        'Synchronous query timeout in seconds.'
    )


conf = Conf()


def _env_urls():
    """Return the URL dict for the currently configured environment."""
    return ENVIRONMENTS.get(conf.SRCNET_ENVIRONMENT, ENVIRONMENTS[_DEFAULT_ENV])


from .core import SRCNet, SRCNetClass
from .format_factory import SKAFormatFactory, Cube, Image, Spectra, Visibility
from .software_discovery import SoftwareDiscovery, SoftwareDiscoveryClass
from .data_discovery import DataDiscovery, DataDiscoveryClass
from .data_access import DataAccessClass
from .chat import SRCNetChat

#: Module-level DataAccess singleton — wraps the default SRCNet instance.
#: Requires :func:`SRCNet.login` before calling data-access methods.
DataAccess = SRCNet.get_data_access()

__all__ = [
    'SRCNet', 'SRCNetClass',
    'conf', 'ENVIRONMENTS',
    'SKAFormatFactory', 'Cube', 'Image', 'Spectra', 'Visibility',
    'SoftwareDiscovery', 'SoftwareDiscoveryClass',
    'DataDiscovery', 'DataDiscoveryClass',
    'DataAccess', 'DataAccessClass',
    'SRCNetChat',
]
