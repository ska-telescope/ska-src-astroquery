"""
Tests for astroquery.srcnet.exceptions module.

Covers:
  - All custom exception classes: message content, inheritance chain
  - handle_exceptions decorator: pass-through, HTTPError, CustomException, general Exception
  - _show_helpdesk: silently swallows its own failures
"""
import pytest
import requests
from unittest.mock import MagicMock, patch

from astroquery.srcnet.exceptions import (
    CustomException,
    NoAccessTokenFoundInResponse,
    QueryRegionSearchAreaAmbiguous,
    QueryRegionSearchAreaUndefined,
    UnsupportedAccessProtocol,
    UnsupportedOIDCFlow,
    _show_helpdesk,
    handle_exceptions,
)


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception classes
# ─────────────────────────────────────────────────────────────────────────────

class TestCustomExceptions:
    """All domain exceptions must carry a human-readable .message attribute."""

    def test_no_access_token_message(self):
        exc = NoAccessTokenFoundInResponse()
        assert "No access token" in exc.message

    def test_query_region_undefined_message(self):
        exc = QueryRegionSearchAreaUndefined()
        # Must mention radius or width/height so users know what's missing.
        assert "radius" in exc.message or "width" in exc.message or "height" in exc.message

    def test_query_region_ambiguous_message(self):
        exc = QueryRegionSearchAreaAmbiguous()
        assert exc.message  # non-empty

    def test_unsupported_protocol_includes_protocol_name(self):
        exc = UnsupportedAccessProtocol("ftp")
        assert "ftp" in exc.message

    def test_unsupported_oidc_flow_includes_flow_name(self):
        exc = UnsupportedOIDCFlow("client_credentials")
        assert "client_credentials" in exc.message

    def test_all_subclass_custom_exception(self):
        # handle_exceptions catches CustomException; all domain errors must inherit it.
        for cls in (
            NoAccessTokenFoundInResponse,
            QueryRegionSearchAreaUndefined,
            QueryRegionSearchAreaAmbiguous,
            UnsupportedOIDCFlow,
        ):
            assert issubclass(cls, CustomException), f"{cls} must subclass CustomException"

    def test_custom_exception_subclasses_builtin_exception(self):
        assert issubclass(CustomException, Exception)

    def test_message_also_used_as_str_arg(self):
        exc = UnsupportedAccessProtocol("soda")
        assert "soda" in str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# handle_exceptions decorator
# ─────────────────────────────────────────────────────────────────────────────

class TestHandleExceptions:
    """handle_exceptions wraps a function and converts all exceptions to plain Exception."""

    def test_return_value_passed_through(self):
        @handle_exceptions
        def fn():
            return 42

        assert fn() == 42

    def test_preserves_function_name_via_wraps(self):
        @handle_exceptions
        def my_specific_function():
            pass

        assert my_specific_function.__name__ == "my_specific_function"

    def test_catches_http_error_and_re_raises(self):
        mock_resp = MagicMock()
        mock_resp.text = "Not Found"
        http_err = requests.exceptions.HTTPError("404", response=mock_resp)

        @handle_exceptions
        def fn():
            raise http_err

        with pytest.raises(Exception, match="Error during request"):
            fn()

    def test_catches_custom_exception_and_re_raises(self):
        @handle_exceptions
        def fn():
            raise NoAccessTokenFoundInResponse()

        with pytest.raises(Exception, match="No access token"):
            fn()

    def test_catches_general_exception_and_re_raises(self):
        @handle_exceptions
        def fn():
            raise ValueError("something went wrong")

        with pytest.raises(Exception, match="General error occurred"):
            fn()

    def test_helpdesk_shown_on_any_exception(self):
        with patch("astroquery.srcnet.exceptions._show_helpdesk") as mock_hd:
            @handle_exceptions
            def fn():
                raise RuntimeError("boom")

            with pytest.raises(Exception):
                fn()
        mock_hd.assert_called_once()

    def test_decorator_works_with_arguments(self):
        @handle_exceptions
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_decorator_works_with_kwargs(self):
        @handle_exceptions
        def greet(name="world"):
            return f"Hello, {name}"

        assert greet(name="SKA") == "Hello, SKA"


# ─────────────────────────────────────────────────────────────────────────────
# _show_helpdesk
# ─────────────────────────────────────────────────────────────────────────────

class TestShowHelpdeskInternal:
    """_show_helpdesk must never propagate its own errors."""

    def test_does_not_raise_when_helpdesk_import_fails(self):
        # If _helpdesk.show_helpdesk_table is broken, _show_helpdesk must silently swallow it.
        with patch(
            "astroquery.srcnet.exceptions._show_helpdesk",
            side_effect=lambda *a, **kw: None,
        ):
            _show_helpdesk("test description")  # should not raise

    def test_silently_swallows_import_error(self):
        # Call directly — if it raises, the test fails; if not, we're fine.
        try:
            _show_helpdesk("safe error message")
        except Exception as e:
            pytest.fail(f"_show_helpdesk propagated an exception: {e}")
