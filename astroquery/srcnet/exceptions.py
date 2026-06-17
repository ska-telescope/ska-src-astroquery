"""
Custom exceptions and error-handling utilities for the SRCNet client library.

All domain-specific exceptions inherit from :class:`CustomException` so the
:func:`handle_exceptions` decorator can distinguish them from unexpected
third-party errors and display the appropriate helpdesk prompt.

Exception hierarchy
-------------------
``CustomException``
  ├── ``NoAccessTokenFoundInResponse``
  ├── ``QueryRegionSearchAreaUndefined``
  ├── ``QueryRegionSearchAreaAmbiguous``
  ├── ``UnsupportedAccessProtocol``
  └── ``UnsupportedOIDCFlow``
"""
import requests
import traceback
from functools import wraps

from astropy import log


def handle_exceptions(func):
    """Decorator that catches all exceptions, logs them, shows a helpdesk table, then re-raises.

    Three tiers of handling:
    - ``requests.HTTPError`` — extracts the server response body for context.
    - ``CustomException`` — uses the structured ``.message`` attribute.
    - Anything else — formats the full traceback for diagnostics.

    The re-raised exception is always a plain ``Exception`` so callers do not
    need to import the original exception type.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            detail = "Error during request: {exception}, response: {response_text}".format(
                exception=e,
                response_text=e.response.text
            )
            log.critical(detail)
            _show_helpdesk(detail)
            raise Exception(detail)
        except CustomException as e:
            log.critical(e.message)
            _show_helpdesk(e.message)
            raise Exception(e.message)
        except Exception as e:
            log.critical(repr(e))
            detail = "General error occurred: {}, traceback: {}".format(
                repr(e), ''.join(traceback.format_tb(e.__traceback__)))
            _show_helpdesk(detail)
            raise Exception(detail)
    return wrapper


def _show_helpdesk(description: str, steps: str = "") -> None:
    """Best-effort helpdesk display — silently swallowed if IPython is unavailable."""
    try:
        from ._helpdesk import show_helpdesk_table
        show_helpdesk_table(description, steps=steps)
    except Exception:
        pass


class CustomException(Exception):
    """Base class for all SRCNet domain exceptions.

    Subclasses must set ``self.message`` before calling ``super().__init__``
    so that :func:`handle_exceptions` can display it without introspecting
    the exception args.
    """
    pass


class NoAccessTokenFoundInResponse(CustomException):
    """Raised when the OIDC token response does not contain an access token."""

    def __init__(self):
        self.message = "No access token found in response."
        super().__init__(self.message)


class QueryRegionSearchAreaUndefined(CustomException):
    """Raised when a region query is called without specifying a search area."""

    def __init__(self):
        self.message = "Must specify either a radius or both width and height."
        super().__init__(self.message)


class QueryRegionSearchAreaAmbiguous(CustomException):
    """Raised when both a radius and width/height are specified simultaneously."""

    def __init__(self):
        self.message = "Must specify one of either a radius or (both) width and height."
        super().__init__(self.message)


class UnsupportedAccessProtocol(CustomException):
    """Raised when a data-access URL uses a protocol the client cannot handle."""

    def __init__(self, protocol):
        self.message = "Unsupported access protocol: {protocol}".format(protocol=protocol)
        super().__init__(self.message)


class UnsupportedOIDCFlow(CustomException):
    """Raised when the requested OIDC flow is not implemented by the client."""

    def __init__(self, oidc_flow):
        self.message = "The {} flow is not supported".format(oidc_flow)
        super().__init__(self.message)
