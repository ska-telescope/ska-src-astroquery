import traceback
from functools import wraps


def handle_exceptions(func):
    """ Decorator to handle exceptions. """
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except CustomException as e:
            raise Exception(message=e.message)
        except Exception as e:
            detail = "General error occurred: {}, traceback: {}".format(
                repr(e), ''.join(traceback.format_tb(e.__traceback__)))
            raise Exception(detail)
    return wrapper


class CustomException(Exception):
    """ Class that all custom exceptions must inherit in order for exception to be caught by the
    handle_exceptions decorator.
    """
    pass


class QueryRegionSearchAreaUndefined(CustomException):
    def __init__(self):
        self.message = "Must specify either a radius or both width and height."
        super().__init__(self.message)


class QueryRegionSearchAreaAmbiguous(CustomException):
    def __init__(self):
        self.message = "Must specify one of either a radius or (both) width and height."
        super().__init__(self.message)


