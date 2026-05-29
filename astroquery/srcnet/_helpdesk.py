"""
SRCNet Helpdesk integration.

Whenever a user-visible error occurs in any SRCNet module, call
``srcnet_raise(exc, steps=...)`` instead of a bare ``raise``.
It renders a pre-filled helpdesk ticket table in Jupyter (via
IPython.display.Markdown) then re-raises the exception so the
normal traceback is still shown.

Usage::

    from ._helpdesk import srcnet_raise

    try:
        ...
    except SomeError as exc:
        srcnet_raise(exc, steps="sd.query(...)")
"""
from __future__ import annotations

HELPDESK_URL = (
    "https://jira.skatelescope.org/servicedesk/customer/portal/859/create/3944"
)


def _env_cell() -> str:
    """Return a one-line string of all SRCNet service URLs for the current environment."""
    try:
        from . import _env_urls
        urls = _env_urls()
    except Exception:
        return "*(see SRCNet environment config)*"

    labels = {
        "tap":          "Data Discovery TAP",
        "software_tap": "Software Discovery TAP",
        "chat":         "Chat",
        "authn_api":    "Auth API",
        "dm_api":       "Data Management API",
    }
    parts = [
        f"{labels.get(k, k)}: `{v}`"
        for k, v in urls.items()
        if k in labels
    ]
    return "  ".join(parts)


def helpdesk_table_md(description: str, steps: str = "") -> str:
    """Return a Markdown helpdesk-ticket table as a string."""

    def _cell(s: str) -> str:
        return s.replace("\n", " ").replace("|", "\\|").strip()

    steps_cell = _cell(steps) if steps else "*(not available)*"

    return (
        "\n\n---\n\n"
        f"**Need help? [Open a SRCNet Helpdesk ticket]({HELPDESK_URL})**\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        "| Summary | SRCNet client error |\n"
        "| Urgency | *(select when submitting)* |\n"
        f"| Description of the Problem | `{_cell(description)}` |\n"
        f"| Environment | {_env_cell()} |\n"
        f"| Steps to Reproduce | {steps_cell} |\n"
        "| Area | Unassigned |\n"
    )


def show_helpdesk_table(description: str, steps: str = "") -> None:
    """
    Render the helpdesk table in the current output context.

    In a Jupyter/IPython session the table is rendered as formatted Markdown.
    Outside a notebook it is printed as plain text.
    """
    md = helpdesk_table_md(description, steps)
    try:
        from IPython.display import display, Markdown  # type: ignore
        display(Markdown(md))
    except Exception:
        print(md)


def srcnet_raise(exc: BaseException, steps: str = "") -> None:
    """Display a helpdesk table, then re-raise *exc*."""
    show_helpdesk_table(str(exc), steps=steps)
    raise exc
