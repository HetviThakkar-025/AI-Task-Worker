"""Tool definitions exposed to the LLM and a dispatcher that executes them.

Tool failures are returned as "ERROR: ..." strings rather than raised: for the
agent an error is just another observation to react to.
"""
from __future__ import annotations

import io

from pypdf import PdfReader

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from agent.browser import BrowserSession
from agent.reliability import TOOL_TIMEOUT_S, with_retry

MAX_PDF_CHARS = 3000

TOOL_SCHEMAS = [
    {
        "name": "browser_goto",
        "description": "Navigate the browser to a URL (absolute, or a path like '/portal' on the company environment). Returns the new page state.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL or path to open"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_state",
        "description": "Return the current page state: URL, title, visible errors, visible text and the numbered interactive elements. Action tools already return this, so only call it if you need a refresh.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_click",
        "description": "Click the interactive element with the given id from the latest page state. Returns the new page state.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Element id, e.g. 7 for [7]"}},
            "required": ["id"],
        },
    },
    {
        "name": "browser_type",
        "description": "Clear the input/textarea with the given id and type text into it. Does not submit. Returns the new page state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Element id"},
                "text": {"type": "string", "description": "Text to enter"},
            },
            "required": ["id", "text"],
        },
    },
    {
        "name": "browser_select",
        "description": "Choose an option in the select element with the given id, matched by option value or visible text. Returns the new page state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Element id"},
                "value": {"type": "string", "description": "Option value or visible text"},
            },
            "required": ["id", "value"],
        },
    },
    {
        "name": "read_pdf",
        "description": "Download a PDF (absolute URL or path) and return its extracted text. Use this for PDF links instead of browser_goto.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL or path of the PDF"}},
            "required": ["url"],
        },
    },
    # --- handled by the agent loop, not the browser ---
    {
        "name": "remember",
        "description": "Save an important fact discovered during the task (e.g. an extracted value) so it stays available in later steps.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short name for the fact"},
                "value": {"type": "string", "description": "The fact's value, exactly as needed"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "ask_user",
        "description": "Ask the user a question when information is ambiguous or missing and cannot be found in the environment, or when approval is needed before a risky action. Returns the user's answer.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "A clear, specific question"}},
            "required": ["question"],
        },
    },
    {
        "name": "request_approval",
        "description": "Ask the human operator to approve an action BEFORE doing it. Use before any irreversible action (paying, deleting, sending, cancelling) or any high-value action. Returns approved or denied.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "The exact action you intend to take"},
                "reason": {"type": "string", "description": "Why approval is needed (what is irreversible or high-value)"},
            },
            "required": ["action", "reason"],
        },
    },
    {
        "name": "finish",
        "description": "End the task. Call only when the goal is achieved (success=true) or it definitely cannot be achieved (success=false). Summary must state what was done and the evidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Short summary of what was done and the evidence of completion"},
                "success": {"type": "boolean", "description": "Whether the goal was achieved"},
            },
            "required": ["summary", "success"],
        },
    },
]

LOOP_TOOLS = {"remember", "ask_user", "request_approval", "finish"}


def read_pdf(session: BrowserSession, url: str) -> str:
    target = session.resolve_url(url)
    resp = session.context.request.get(target)
    if not resp.ok:
        raise ValueError(f"GET {target} returned HTTP {resp.status}")
    body = resp.body()
    if not body.startswith(b"%PDF"):
        raise ValueError(f"{target} is not a PDF (content-type: {resp.headers.get('content-type', '?')})")
    reader = PdfReader(io.BytesIO(body))
    text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if len(text) > MAX_PDF_CHARS:
        text = text[:MAX_PDF_CHARS] + "\n...(truncated)"
    return f"PDF {target} ({len(reader.pages)} page(s)):\n{text or '(no extractable text)'}"


def _dispatch(session: BrowserSession, name: str, args: dict) -> str:
    if name == "browser_goto":
        return session.goto(args["url"])
    if name == "browser_state":
        return session.get_state()
    if name == "browser_click":
        return session.click(args["id"])
    if name == "browser_type":
        return session.type(args["id"], args["text"])
    if name == "browser_select":
        return session.select(args["id"], args["value"])
    if name == "read_pdf":
        return read_pdf(session, args["url"])
    raise LookupError


TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS} - LOOP_TOOLS

# Transient-error retries performed during the most recent execute_tool call (for tracing).
last_call = {"retries": 0}


def _count_retry(attempt: int, err: Exception) -> None:
    last_call["retries"] = attempt


def execute_tool(session: BrowserSession, name: str, args: dict | None) -> str:
    """Run a tool and always return a string (errors become 'ERROR: ...')."""
    if name not in TOOL_NAMES:
        return f"ERROR: Unknown tool {name!r}. Available tools: {', '.join(sorted(TOOL_NAMES))}"
    args = args or {}
    last_call["retries"] = 0
    try:
        return with_retry(lambda: _dispatch(session, name, args), attempts=3, backoff=1.0,
                          deadline_s=TOOL_TIMEOUT_S, on_retry=_count_retry)
    except KeyError as e:
        return f"ERROR: Missing required argument {e} for {name}"
    except PlaywrightTimeout:
        msg = f"ERROR: {name} timed out after {TOOL_TIMEOUT_S}s (retried). The page may be slow or the element not actionable."
        try:
            msg += "\n\nCurrent page state:\n" + session.get_state()
        except Exception:
            pass
        return msg
    except Exception as e:
        # ValueErrors are our own readable messages; prefix others with their type.
        detail = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
        msg = f"ERROR: {detail.strip().splitlines()[0] if detail.strip() else type(e).__name__}"
        # Attach the current page so the agent can recover without an extra call.
        if name.startswith("browser_"):
            try:
                msg += "\n\nCurrent page state:\n" + session.get_state()
            except Exception:
                pass
        return msg
