"""LLM adapter (Gemini via google-genai).

The rest of the agent uses a provider-neutral message format:
    {"role": "user" | "assistant" | "tool", "content": str,
     "tool_name": str?, "tool_args": dict?, "meta": dict?}
`meta` is opaque provider data returned with a tool call (here: Gemini's
thought signature) that must be sent back unchanged with that call.
All Gemini-specific conversion lives in this file, so swapping providers
means rewriting only this module.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MAX_TRIES = 5
RETRYABLE = {429, 500, 503}
MAX_WAIT_S = 90  # longer server-suggested waits (e.g. daily quota) are not worth sleeping through

_client: genai.Client | None = None
_exhausted: set[str] = set()  # models out of quota / unavailable during this process


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (add it to .env)")
        _client = genai.Client(api_key=key)
    return _client


def _models() -> list[str]:
    """Primary model first, then optional fallbacks (GEMINI_FALLBACK_MODELS, comma-separated)."""
    chain = [os.getenv("GEMINI_MODEL", "gemini-3.5-flash")]
    chain += [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
    return [m for i, m in enumerate(chain) if m not in chain[:i]]


def current_model() -> str | None:
    return next((m for m in _models() if m not in _exhausted), None)


def _to_declarations(tools: list[dict]) -> list[types.Tool]:
    decls = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters_json_schema=t.get("input_schema") or {"type": "object", "properties": {}},
        )
        for t in tools
    ]
    return [types.Tool(function_declarations=decls)]


def _to_contents(messages: list[dict]) -> list[types.Content]:
    """Convert neutral messages to Gemini contents, merging consecutive same-role turns."""
    contents: list[types.Content] = []
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_name"):
            g_role = "model"
            part = types.Part(
                function_call=types.FunctionCall(name=m["tool_name"], args=m.get("tool_args") or {}),
                thought_signature=(m.get("meta") or {}).get("thought_signature"),
            )
        elif role == "assistant":
            g_role, part = "model", types.Part(text=m["content"] or "")
        elif role == "tool":
            g_role = "user"
            part = types.Part(function_response=types.FunctionResponse(
                name=m["tool_name"], response={"result": m["content"]}))
        else:
            g_role, part = "user", types.Part(text=m["content"])
        if contents and contents[-1].role == g_role:
            contents[-1].parts.append(part)
        else:
            contents.append(types.Content(role=g_role, parts=[part]))
    return contents


def _retry_delay(err: errors.APIError, attempt: int) -> float:
    """Server-provided retry delay when present, else exponential backoff."""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(err.details))
    if m:
        return float(m.group(1)) + 1
    return min(2 ** attempt * 2, 60)


def _is_quota_exhausted(err: errors.APIError, delay: float) -> bool:
    return err.code == 429 and ("PerDay" in str(err.details) or delay > MAX_WAIT_S)


def _generate(contents, config) -> types.GenerateContentResponse:
    """Call the model with backoff on transient errors and fallback on exhausted quota."""
    attempt = 0
    while True:
        model = current_model()
        if model is None:
            raise RuntimeError(f"All configured models are out of quota: {', '.join(_models())}. "
                               "Set GEMINI_FALLBACK_MODELS or try again later.")
        attempt += 1
        try:
            return _get_client().models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as e:
            if e.code not in RETRYABLE:
                raise
            wait = _retry_delay(e, attempt)
            if _is_quota_exhausted(e, wait):
                _exhausted.add(model)
                nxt = current_model()
                print(f"    (quota exhausted for {model}" + (f", switching to {nxt})" if nxt else ")"))
                attempt = 0
                continue
            if attempt >= MAX_TRIES:
                _exhausted.add(model)  # treat as unavailable; fall back if another model is configured
                nxt = current_model()
                if nxt is None:
                    raise RuntimeError(f"LLM still failing after {MAX_TRIES} tries: {e.code} {e.status}") from e
                print(f"    ({model} unavailable after {MAX_TRIES} tries, switching to {nxt})")
                attempt = 0
                continue
            label = "rate limited" if e.code == 429 else f"server error {e.code}"
            print(f"    ({label}, waiting {wait:.0f}s)")
            time.sleep(wait)


def chat(system: str, messages: list[dict], tools: list[dict] | None) -> dict:
    """One model turn. Returns {"text", "tool_call": {"name", "args", "meta"} | None}.

    With tools, exactly one tool call is forced. With tools=None/[] it is a
    plain text completion.
    """
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if tools:
        config.tools = _to_declarations(tools)
        config.tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.ANY)
        )
    resp = _generate(_to_contents(messages), config)

    text_parts: list[str] = []
    tool_call = None
    candidate = resp.candidates[0] if resp.candidates else None
    for part in (candidate.content.parts if candidate and candidate.content and candidate.content.parts else []):
        if part.function_call and tool_call is None:
            meta = {"thought_signature": part.thought_signature} if part.thought_signature else {}
            tool_call = {"name": part.function_call.name, "args": dict(part.function_call.args or {}), "meta": meta}
        elif part.text and not part.thought:
            text_parts.append(part.text)
    return {"text": "\n".join(text_parts) or None, "tool_call": tool_call}
