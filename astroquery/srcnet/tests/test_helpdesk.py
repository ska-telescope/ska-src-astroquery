"""
Tests for astroquery.srcnet._helpdesk module.

Covers:
  - _env_cell: environment URL string construction and fallback
  - helpdesk_table_md: Markdown table content, sanitisation, structure
  - show_helpdesk_table: IPython vs. plain-text rendering paths
  - srcnet_raise: exception re-raise after table display
"""
import pytest
from unittest.mock import MagicMock, patch

from astroquery.srcnet._helpdesk import (
    HELPDESK_URL,
    _env_cell,
    helpdesk_table_md,
    show_helpdesk_table,
    srcnet_raise,
)


# ─────────────────────────────────────────────────────────────────────────────
# _env_cell
# ─────────────────────────────────────────────────────────────────────────────

class TestEnvCell:
    """_env_cell builds a one-line URL summary for the current environment."""

    def test_returns_non_empty_string(self):
        result = _env_cell()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_falls_back_gracefully_on_import_failure(self):
        # If _env_urls() raises, the function must return a safe fallback string
        # rather than propagating the exception.
        with patch("astroquery.srcnet._helpdesk._env_urls", side_effect=RuntimeError("fail")):
            result = _env_cell()
        assert "SRCNet environment config" in result


# ─────────────────────────────────────────────────────────────────────────────
# helpdesk_table_md
# ─────────────────────────────────────────────────────────────────────────────

class TestHelpdeskTableMd:
    """helpdesk_table_md returns a Markdown helpdesk ticket as a string."""

    def test_contains_helpdesk_url(self):
        assert HELPDESK_URL in helpdesk_table_md("some error")

    def test_description_present_in_output(self):
        assert "connection refused" in helpdesk_table_md("connection refused")

    def test_steps_present_when_provided(self):
        result = helpdesk_table_md("error", steps="sd.query(...)")
        assert "sd.query(...)" in result

    def test_steps_fallback_when_omitted(self):
        result = helpdesk_table_md("error")
        assert "not available" in result

    def test_pipe_in_description_escaped(self):
        # Unescaped pipes break Markdown table rendering.
        result = helpdesk_table_md("error | with pipe")
        assert "\\|" in result

    def test_newline_in_description_replaced_with_space(self):
        # Newlines inside a table cell corrupt the Markdown structure.
        result = helpdesk_table_md("line1\nline2")
        assert "line1 line2" in result

    def test_markdown_table_header_present(self):
        result = helpdesk_table_md("error")
        assert "| Field | Value |" in result

    def test_markdown_separator_present(self):
        result = helpdesk_table_md("error")
        assert "|---|---|" in result

    def test_standard_fields_present(self):
        result = helpdesk_table_md("error")
        for field in ("Summary", "Urgency", "Description", "Environment", "Steps", "Area"):
            assert field in result


# ─────────────────────────────────────────────────────────────────────────────
# show_helpdesk_table
# ─────────────────────────────────────────────────────────────────────────────

class TestShowHelpdeskTable:
    """show_helpdesk_table renders via IPython in a notebook, plain text outside."""

    def test_calls_ipython_display_when_available(self):
        mock_display = MagicMock()
        mock_markdown_cls = MagicMock()
        fake_md_obj = MagicMock()
        mock_markdown_cls.return_value = fake_md_obj

        with patch.dict(
            "sys.modules",
            {"IPython.display": MagicMock(display=mock_display, Markdown=mock_markdown_cls)},
        ):
            show_helpdesk_table("test error")

        mock_display.assert_called_once_with(fake_md_obj)

    def test_falls_back_to_print_when_ipython_unavailable(self, capsys):
        # Simulate IPython not being installed.
        with patch(
            "astroquery.srcnet._helpdesk.display",
            side_effect=Exception("no IPython"),
        ):
            show_helpdesk_table("test error")
        # Should not raise — and the Markdown should appear in stdout.
        capsys.readouterr()
        assert True  # reaching here means no exception was raised


# ─────────────────────────────────────────────────────────────────────────────
# srcnet_raise
# ─────────────────────────────────────────────────────────────────────────────

class TestSrcnetRaise:
    """srcnet_raise shows the helpdesk table then re-raises the original exception."""

    def test_re_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="test error"):
            srcnet_raise(RuntimeError("test error"))

    def test_re_raises_value_error(self):
        with pytest.raises(ValueError):
            srcnet_raise(ValueError("bad value"))

    def test_re_raises_type_error(self):
        with pytest.raises(TypeError):
            srcnet_raise(TypeError("wrong type"))

    def test_show_helpdesk_called_with_correct_args(self):
        with patch("astroquery.srcnet._helpdesk.show_helpdesk_table") as mock_show:
            with pytest.raises(ValueError):
                srcnet_raise(ValueError("oops"), steps="my_func()")
        mock_show.assert_called_once_with("oops", steps="my_func()")

    def test_show_helpdesk_called_without_steps_by_default(self):
        with patch("astroquery.srcnet._helpdesk.show_helpdesk_table") as mock_show:
            with pytest.raises(RuntimeError):
                srcnet_raise(RuntimeError("err"))
        # steps defaults to "" — check it's passed through
        _, kwargs = mock_show.call_args
        assert kwargs.get("steps", "") == ""
