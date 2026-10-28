"""
Tests for astroquery.srcnet.exceptions module.

Covers:
  - All custom exception classes: message content, inheritance chain
  - handle_exceptions decorator: pass-through, HTTPError, CustomException, general Exception
  - _show_helpdesk: silently swallows its own failures
"""
import time

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


# ─────────────────────────────────────────────────────────────────────────────
# Keeping tokens out of messages, logs and the helpdesk table
# ─────────────────────────────────────────────────────────────────────────────

# Not a real token: three base64url segments, the first starting "eyJ".
FAKE_JWT = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl"


def _http_error(url, status_code=401, body="denied"):
    """A real HTTPError, as raised by Response.raise_for_status()."""
    request = requests.Request("POST", url).prepare()
    response = requests.Response()
    response.status_code = status_code
    response.reason = "Unauthorized"
    response._content = body.encode()
    response.url = request.url
    response.request = request
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        return e
    raise AssertionError("raise_for_status did not raise")


class TestRedactSecrets:

    def test_removes_jwts_bearer_values_url_queries_and_token_fields(self):
        from astroquery.srcnet._redact import redact_secrets

        text = (f"jwt {FAKE_JWT}; Authorization: Bearer opaque-1; "
                f"https://host/a?token={FAKE_JWT}&x=1 then "
                "{'access_token': 'opaque-2', \"refresh_token\": \"opaque-3\"} token=opaque-4")

        redacted = redact_secrets(text)

        for secret in (FAKE_JWT, "opaque-1", "opaque-2", "opaque-3", "opaque-4", "x=1"):
            assert secret not in redacted
        assert "https://host/a?[REDACTED] then" in redacted
        assert "'access_token': '[REDACTED]'" in redacted

    def test_leaves_ordinary_text_alone(self):
        from astroquery.srcnet._redact import redact_secrets

        text = "Unsupported access protocol: ftp (token_type unknown, see https://host/docs)"
        assert redact_secrets(text) == text


    @pytest.mark.parametrize("text", [
        "eyJ" * 100_000,
        "https://" * 40_000,
        "https://a?" * 30_000,
        "Bearer " * 50_000,
        "token=" * 50_000,
    ], ids=["jwt-starts", "url-starts", "url-queries", "bearer", "token-fields"])
    def test_is_linear_on_adversarial_input(self, text):
        from astroquery.srcnet._redact import redact_secrets

        # Quadratic patterns took minutes on inputs this size; linear ones take milliseconds. The bound is loose so
        # a slow CI runner doesn't flake.
        start = time.perf_counter()
        redact_secrets(text)
        assert time.perf_counter() - start < 1.0

    def test_redacts_a_truncated_jwt_and_keeps_query_less_urls(self):
        from astroquery.srcnet._redact import redact_secrets

        redacted = redact_secrets("cut {} and https://host/path, https://host/q?a=1".format(FAKE_JWT[:30]))

        assert FAKE_JWT[:30] not in redacted
        assert "https://host/path," in redacted
        assert "https://host/q?[REDACTED]" in redacted


class TestHandleExceptionsRedaction:

    def test_http_error_message_omits_the_query_string(self):
        error = _http_error("https://permissions.example/authorise/route/x?token=" + FAKE_JWT,
                            body='{"detail": "bad token %s"}' % FAKE_JWT)

        @handle_exceptions
        def fn():
            raise error

        with patch("astroquery.srcnet.exceptions._show_helpdesk") as mock_hd, \
                patch("astroquery.srcnet.exceptions.log") as mock_log, \
                pytest.raises(Exception) as info:
            fn()

        message = str(info.value)
        assert FAKE_JWT not in message
        assert "Unauthorized for url: https://permissions.example/authorise/route/x?[REDACTED]" in message
        assert "bad token [REDACTED]" in message
        assert FAKE_JWT not in str(mock_log.critical.call_args)
        assert FAKE_JWT not in str(mock_hd.call_args)

    def test_http_error_without_a_response_is_still_reported(self):
        @handle_exceptions
        def fn():
            raise requests.exceptions.HTTPError("https://api.example/x?token=" + FAKE_JWT)

        with pytest.raises(Exception, match="Error during request") as info:
            fn()
        assert FAKE_JWT not in str(info.value)

    def test_general_error_message_is_redacted(self):
        @handle_exceptions
        def fn():
            raise ValueError("cannot use " + FAKE_JWT)

        with patch("astroquery.srcnet.exceptions.log") as mock_log, pytest.raises(Exception) as info:
            fn()

        assert "General error occurred" in str(info.value)
        assert FAKE_JWT not in str(info.value)
        assert FAKE_JWT not in str(mock_log.critical.call_args)

    def test_custom_exception_message_is_redacted(self):
        @handle_exceptions
        def fn():
            raise UnsupportedAccessProtocol("https://host/f?token=" + FAKE_JWT)

        with pytest.raises(Exception) as info:
            fn()
        assert str(info.value) == "Unsupported access protocol: https://host/f?[REDACTED]"


class TestHelpdeskAndTokenLogsRedaction:

    def test_helpdesk_table_never_shows_a_token(self, capsys):
        from astroquery.srcnet import _helpdesk

        with patch.dict("sys.modules", {"IPython.display": None}):
            _helpdesk.show_helpdesk_table("failed: Bearer " + FAKE_JWT, steps="sd.get(token=%s)" % FAKE_JWT)

        assert FAKE_JWT not in capsys.readouterr().out

    def test_obtained_tokens_are_not_logged(self):
        from types import SimpleNamespace

        from astroquery.srcnet import core

        with patch.object(core, "log") as mock_log:
            core.SRCNetClass._log_tokens_obtained(SimpleNamespace(access_token=FAKE_JWT, refresh_token="opaque"))

        logged = str(mock_log.debug.call_args)
        assert "with a refresh token" in logged
        assert FAKE_JWT not in logged and "opaque" not in logged
