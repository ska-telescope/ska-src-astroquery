"""
Conversational interface for ``astroquery.srcnet``.

Routes through the CHATSERVER REST API, which classifies each query and applies
the appropriate domain-specific system prompt (TAP, software discovery, pipeline,
or library usage).

Typical usage::

    from astroquery.srcnet import SoftwareDiscovery, SRCNetChat

    chat = SRCNetChat(SoftwareDiscovery, backend="chatserver",
                      chatserver_url="http://localhost:8001")

    chat("What software is available for continuum imaging?")
    chat("Show me all stable tools that require a GPU")
    chat.reset()   # clear conversation history
"""

from __future__ import annotations

import json as _json
import os
import re
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from astropy.table import Table
import requests

from ._helpdesk import srcnet_raise

if TYPE_CHECKING:
    from .software_discovery import SoftwareDiscoveryClass

_SRC_DIR = Path(__file__).parent
_DEFAULT_MODEL = "deepseek-coder-v2"
_ANTHROPIC_MODEL = "claude-opus-4-7-20251101"

try:
    from .schemas import _SDM_SCHEMA
except ImportError:
    _SDM_SCHEMA = "sdm.software\n  (run 'make regen-schema' in ska-src-chat to populate)\n"

__all__ = ["SRCNetChat"]


# ── System prompt ─────────────────────────────────────────────────────────────

_LIBRARY_FILES = [
    "core.py",
    "software_discovery.py",
    "data_discovery.py",
    "format_factory.py",
]

_SYSTEM_PREAMBLE = """\
You are an expert assistant for the `astroquery.srcnet` module
and the SKA SRCNet services.

You can help with:

1. **Module usage** — how to initialise `SRCNetClass`, authenticate with OIDC,
   query and download data, use `SoftwareDiscoveryClass` and `DataDiscoveryClass`.
   Include working Python examples in your answers.

2. **Software Discovery queries** — finding, filtering, and aggregating entries
   in the `sdm.software` TAP table.  Call the `run_adql` or `get_schema` tools
   for live data — never invent rows.

3. **Data Discovery queries** — searching CAOM2 observations, planes, and
   artifacts.  Call `run_adql` for live data.

---

## Library source code

"""

_SCHEMA_BLOCK = (
    "---\n\n"
    "## TAP service — sdm.* (normalized schema)\n\n"
    + _SDM_SCHEMA
    + "\nADQL conventions: single-quoted strings, `%` wildcards, `SELECT TOP N`, "
    "`SELECT DISTINCT` for 1:N joins.\n"
    "Use JOINs (not EXISTS subqueries) to filter by category columns — "
    "correlated subqueries are not supported by this TAP backend.\n"
)

# Tool instructions for Ollama / CHATSERVER (text-based JSON tool calls)
_TOOL_PROMPT = """

---

## Tools

For questions requiring live data from the TAP service, respond with **only** a
JSON block:

To run an ADQL query:
```json
{"tool": "run_adql", "adql": "<ADQL SELECT statement>", "maxrec": 100}
```

To inspect the column schema:
```json
{"tool": "get_schema"}
```

After receiving a tool result, continue your answer in plain text.
For library/usage questions that don't need live data, answer directly.
"""

# Anthropic native-tool definitions
_ANTHROPIC_TOOLS = [
    {
        "name": "run_adql",
        "description": (
            "Execute an ADQL query against the software discovery TAP service "
            "and return the results as a markdown table."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "adql":   {"type": "string",  "description": "ADQL SELECT statement"},
                "maxrec": {"type": "integer", "description": "Maximum rows (default 100)"},
            },
            "required": ["adql"],
        },
    },
    {
        "name": "get_schema",
        "description": "Return the column definitions of the sdm.software TAP table.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _build_system_prompt() -> str:
    parts = [_SYSTEM_PREAMBLE]
    for filename in _LIBRARY_FILES:
        path = _SRC_DIR / filename
        if path.exists():
            parts.append(f"\n### {filename}\n\n```python\n{path.read_text()}\n```")
    parts.append("\n" + _SCHEMA_BLOCK)
    return "\n".join(parts)


# ── Tool-call parsing (Ollama / CHATSERVER) ───────────────────────────────────

def _parse_tool_call(text: str) -> Optional[dict]:
    block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = block.group(1) if block else None
    if candidate is None and '"tool"' in text:
        m = re.search(r'\{[^{}]*"tool"[^{}]*\}', text, re.DOTALL)
        candidate = m.group(0) if m else None
    if candidate is None:
        return None
    try:
        data = _json.loads(candidate)
        return data if "tool" in data else None
    except _json.JSONDecodeError:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _table_to_text(table: Table, max_rows: int = 20) -> str:
    if len(table) == 0:
        return "(no rows returned)"
    truncated = table[:max_rows]
    lines = [" | ".join(truncated.colnames)]
    lines.append(" | ".join("---" for _ in truncated.colnames))
    for row in truncated:
        lines.append(" | ".join(str(row[c]) for c in truncated.colnames))
    if len(table) > max_rows:
        lines.append(f"… ({len(table)} rows total, showing first {max_rows})")
    return "\n".join(lines)


def _display(text: str, table: Optional[Table]) -> None:
    try:
        from IPython.display import display, Markdown
        if text:
            display(Markdown(text))
        if table is not None:
            display(table)
    except ImportError:
        if text:
            print(text)
        if table is not None:
            table.pprint_all()


def _stream_print(chunk: str) -> None:
    """Write a streaming chunk immediately to stdout (works in terminal and Jupyter)."""
    import sys
    sys.stdout.write(chunk)
    sys.stdout.flush()


def _stream_finish(full_text: str) -> None:
    """After streaming completes, replace raw token output with rendered Markdown in Jupyter."""
    try:
        from IPython.display import clear_output, display, Markdown
        clear_output(wait=True)
        display(Markdown(full_text))
    except ImportError:
        pass  # terminal: output already printed token-by-token


# ── Main class ────────────────────────────────────────────────────────────────

class SRCNetChat:
    """
    Conversational interface over ``astroquery.srcnet``.

    Parameters
    ----------
    sd : SoftwareDiscoveryClass
        A configured software discovery client used to execute TAP queries.
    backend : str
        One of ``"anthropic"`` (default), ``"ollama"``, or ``"chatserver"``.
    anthropic_api_key : str, optional
        Anthropic API key.  Falls back to ``ANTHROPIC_API_KEY`` env var.
        Required when *backend* is ``"anthropic"``.
    ollama_url : str, optional
        Base URL of the Ollama REST service (``backend="ollama"``).
        Defaults to the SRCNet environment chat URL.
    ollama_model : str, optional
        Ollama model name (default ``"deepseek-coder-v2"``).
    chatserver_url : str, optional
        CHATSERVER base URL.  Required when *backend* is ``"chatserver"``.
    verbose : bool
        Print generated ADQL queries and backend category labels.
    """

    def __init__(
        self,
        sd: "SoftwareDiscoveryClass",
        *,
        backend: str = "anthropic",
        anthropic_api_key: Optional[str] = None,
        ollama_url: Optional[str] = None,
        ollama_model: str = _DEFAULT_MODEL,
        chatserver_url: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        from . import _env_urls
        self._sd            = sd
        self._verbose       = verbose
        self._backend       = backend
        self._history: list[dict] = []
        self._last_table: Optional[Table] = None

        base_prompt = _build_system_prompt()

        if backend == "anthropic":
            key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise ValueError(
                    "Anthropic API key required. Pass anthropic_api_key= or set ANTHROPIC_API_KEY."
                )
            try:
                import anthropic as _anthropic
            except ImportError:
                raise ImportError(
                    "Install the Anthropic SDK: pip install anthropic"
                )
            self._anthropic_client = _anthropic.Anthropic(api_key=key)
            self._system_prompt = base_prompt

        elif backend == "ollama":
            self._ollama_url    = (ollama_url or _env_urls()["chat"]).rstrip("/")
            self._ollama_model  = ollama_model
            self._system_prompt = base_prompt + _TOOL_PROMPT

        elif backend == "chatserver":
            cs = (chatserver_url or "").rstrip("/")
            if not cs:
                raise ValueError(
                    "chatserver_url is required when backend='chatserver'."
                )
            self._chatserver_url = cs
            self._session_id     = str(uuid.uuid4())
            self._system_prompt  = base_prompt + _TOOL_PROMPT  # unused by server, kept for consistency

        else:
            raise ValueError(
                f"Unknown backend {backend!r}. Choose 'anthropic', 'ollama', or 'chatserver'."
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def __call__(
        self,
        message: str,
        *,
        display: bool = True,
        stream: Optional[bool] = None,
    ) -> Optional[Table]:
        """
        Send a natural-language message and return the resulting Table (if any).

        Parameters
        ----------
        message : str
            Any question — library usage, software discovery, or data query.
        display : bool
            Render the response in the notebook / terminal (default ``True``).
        stream : bool, optional
            Stream tokens as they are generated.  Defaults to ``True`` when the
            ``"chatserver"`` backend is in use, ``False`` otherwise.  Streaming
            prints each token immediately; in Jupyter the raw text is replaced
            with rendered Markdown once the response is complete.

        Returns
        -------
        `~astropy.table.Table` or None
        """
        self._history.append({"role": "user", "content": message})
        self._last_table = None

        use_stream = stream if stream is not None else (self._backend == "chatserver")

        if use_stream and self._backend == "chatserver":
            reply = self._run_chatserver_stream()
        else:
            reply = self._run_loop()

        self._history.append({"role": "assistant", "content": reply})
        if display:
            if use_stream and self._backend == "chatserver":
                # Text was already streamed; only display the table if present
                _display("", self._last_table)
            else:
                _display(reply, self._last_table)
        return self._last_table

    def reset(self) -> None:
        """Clear conversation history to start a fresh session."""
        self._history.clear()
        self._last_table = None
        if self._backend == "chatserver":
            try:
                requests.delete(
                    f"{self._chatserver_url}/chat/{self._session_id}", timeout=10
                )
            except Exception:
                pass
            self._session_id = str(uuid.uuid4())

    # ── Backend dispatch ──────────────────────────────────────────────────────

    def _run_loop(self) -> str:
        if self._backend == "anthropic":
            return self._run_loop_anthropic()
        if self._backend == "chatserver":
            return self._run_loop_chatserver()
        return self._run_loop_ollama()

    def _run_loop_ollama(self) -> str:
        messages = [{"role": "system", "content": self._system_prompt}]
        messages += [{"role": m["role"], "content": m["content"]} for m in self._history]

        while True:
            try:
                resp = requests.post(
                    f"{self._ollama_url}/api/chat",
                    json={"model": self._ollama_model, "messages": messages, "stream": False},
                    timeout=300,
                )
                resp.raise_for_status()
            except requests.exceptions.ConnectionError:
                srcnet_raise(
                    RuntimeError(f"Could not reach Ollama at {self._ollama_url}."),
                    steps=f"chat({self._history[-1]['content']!r})",
                )

            content = resp.json().get("message", {}).get("content", "")
            call = _parse_tool_call(content)
            if call is None:
                return content

            messages.append({"role": "assistant", "content": content})
            result = self._dispatch(call["tool"], call)
            messages.append({
                "role": "user",
                "content": f"Tool result:\n{result}\n\nNow answer the original question.",
            })

    def _run_loop_chatserver(self) -> str:
        # CHATSERVER API: POST /chat {"message": str, "session_id": str}
        # Returns: {"answer": str, "category": str, "session_id": str}
        # Server maintains conversation history keyed by session_id.
        message_to_send = self._history[-1]["content"]

        while True:
            try:
                resp = requests.post(
                    f"{self._chatserver_url}/chat",
                    json={"message": message_to_send, "session_id": self._session_id},
                    timeout=300,
                )
                resp.raise_for_status()
            except requests.exceptions.ConnectionError:
                srcnet_raise(
                    RuntimeError(f"Could not reach CHATSERVER at {self._chatserver_url}."),
                    steps=f"chat({message_to_send!r})",
                )

            data = resp.json()
            content = data.get("answer", "")
            category = data.get("category", "")
            if self._verbose and category:
                print(f"[{category}]")

            call = _parse_tool_call(content)
            if call is None:
                return content

            # Dispatch tool and send result as a follow-up message in the same session
            result = self._dispatch(call["tool"], call)
            message_to_send = (
                f"Tool result:\n{result}\n\nNow answer the original question."
            )

    def _run_chatserver_stream(self) -> str:
        """Call /chat/stream and print tokens as they arrive."""
        message_to_send = self._history[-1]["content"]

        try:
            resp = requests.post(
                f"{self._chatserver_url}/chat/stream",
                json={"message": message_to_send, "session_id": self._session_id},
                timeout=300,
                stream=True,
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            srcnet_raise(
                RuntimeError(f"Could not reach CHATSERVER at {self._chatserver_url}."),
                steps=f"chat({message_to_send!r})",
            )

        category = resp.headers.get("X-Category", "")
        if self._verbose and category:
            print(f"[{category}]\n")

        chunks: list[str] = []
        for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
            if chunk:
                chunks.append(chunk)
                _stream_print(chunk)

        full_text = "".join(chunks)
        _stream_finish(full_text)
        return full_text

    def _run_loop_anthropic(self) -> str:

        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in self._history
        ]

        while True:
            resp = self._anthropic_client.messages.create(
                model=_ANTHROPIC_MODEL,
                max_tokens=8192,
                system=self._system_prompt,
                tools=_ANTHROPIC_TOOLS,
                messages=messages,
            )

            if resp.stop_reason == "end_turn":
                return "\n".join(
                    b.text for b in resp.content if hasattr(b, "text")
                )

            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    result = self._dispatch(
                        block.name,
                        {"adql": block.input.get("adql", ""), "maxrec": block.input.get("maxrec")},
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                return "\n".join(
                    b.text for b in resp.content if hasattr(b, "text")
                )

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "run_adql":
            return self._tool_run_adql(args.get("adql", ""), args.get("maxrec"))
        if name == "get_schema":
            return self._tool_get_schema()
        return f"Unknown tool: {name}"

    def _tool_run_adql(self, adql: str, maxrec: Optional[int]) -> str:
        if self._verbose:
            print(f"[ADQL] {adql}")
        try:
            table = self._sd.query(adql, maxrec=maxrec if maxrec is not None else 100)
            self._last_table = table
            return f"Query returned {len(table)} rows.\n\n{_table_to_text(table)}"
        except Exception as exc:
            return f"Query error: {exc}"

    def _tool_get_schema(self) -> str:
        try:
            cols = self._sd.get_columns()
            return f"Columns in sdm.software:\n\n{_table_to_text(cols, max_rows=50)}"
        except Exception as exc:
            return f"Schema fetch error: {exc}"
