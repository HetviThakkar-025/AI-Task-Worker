"""Approval policy: decides, before a tool runs, whether a human must approve it.

Rules are based only on what is visible on the page (element labels and
input values), so they apply to any site, not a specific workflow.
"""
from __future__ import annotations

import os
import re

RISKY_LABEL = re.compile(r"\b(pay|paid|payment|delete|remove|cancel|send)\b", re.I)
SUBMIT_LABEL = re.compile(r"\b(save|submit|create)\b", re.I)
CLICKABLE = {"a", "button"}


def approval_threshold() -> float:
    return float(os.getenv("APPROVAL_THRESHOLD", "100000"))


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _is_clickable(el: dict) -> bool:
    return el["tag"] in CLICKABLE or (el["tag"] == "input" and el.get("type") in {"submit", "button"})


def check(tool_name: str, args: dict, session) -> str | None:
    """Return a reason string if the action needs human approval, else None."""
    if tool_name != "browser_click":
        return None
    elements = session.get_state_elements()
    try:
        target = next((e for e in elements if e["id"] == int(args.get("id"))), None)
    except (TypeError, ValueError):
        return None
    if target is None or not _is_clickable(target):
        return None

    label = target.get("label", "")
    if RISKY_LABEL.search(label):
        return f'Clicking "{label}" looks irreversible (payment, deletion, cancellation or sending).'

    if SUBMIT_LABEL.search(label):
        threshold = approval_threshold()
        for e in elements:
            if e["tag"] == "input" and "amount" in e.get("label", "").lower():
                amount = _number(e.get("value", ""))
                if amount is not None and amount >= threshold:
                    return (f'Submitting "{label}" with {e["label"]} = {e["value"]}, '
                            f"which is at or above the approval threshold of {threshold:,.0f}.")
    return None
