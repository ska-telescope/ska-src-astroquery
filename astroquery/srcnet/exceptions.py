import requests
import traceback
from functools import wraps

from astropy import log


def handle_exceptions(func):
    """ Decorator to handle exceptions. """
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
            raise Exception(detail)
        except CustomException as e:
            log.critical(e.message)
            raise Exception(e.message)
        except Exception as e:
            log.critical(repr(e))
            detail = "General error occurred: {}, traceback: {}".format(
                repr(e), ''.join(traceback.format_tb(e.__traceback__)))
            raise Exception(detail)
    return wrapper


class CustomException(Exception):
    """ Class that all custom exceptions must inherit in order for exception to be caught by the
    handle_exceptions decorator.
    """
    pass


class NoAccessTokenFoundInResponse(CustomException):
    def __init__(self):
        self.message = "No access token found in response."
        super().__init__(self.message)


class QueryRegionSearchAreaUndefined(CustomException):
    def __init__(self):
        self.message = "Must specify either a radius or both width and height."
        super().__init__(self.message)


class QueryRegionSearchAreaAmbiguous(CustomException):
    def __init__(self):
        self.message = "Must specify one of either a radius or (both) width and height."
        super().__init__(self.message)


class UnsupportedAccessProtocol(CustomException):
    def __init__(self, protocol):
        self.message = "Unsupported access protocol: {protocol}".format(protocol=protocol)
        super().__init__(self.message)


class UnsupportedOIDCFlow(CustomException):
    def __init__(self, oidc_flow):
        self.message = "The {} flow is not supported".format(oidc_flow)
        super().__init__(self.message)



