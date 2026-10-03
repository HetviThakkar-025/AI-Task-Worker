"""Human-in-the-loop approval prompt."""
from __future__ import annotations

import os
import sys
import textwrap

WIDTH = 72


def auto_approve_enabled() -> bool:
    return os.getenv("AUTO_APPROVE") == "1"


def _box(title: str, rows: list[str]) -> str:
    inner = WIDTH - 4
    lines = ["+" + "-" * (WIDTH - 2) + "+", f"| {title:<{inner}} |", "+" + "-" * (WIDTH - 2) + "+"]
    for row in rows:
        for chunk in textwrap.wrap(row, inner) or [""]:
            lines.append(f"| {chunk:<{inner}} |")
    lines.append("+" + "-" * (WIDTH - 2) + "+")
    return "\n".join(lines)


def ask_approval(action: str, reason: str) -> str:
    """Ask the human operator. Returns "approved" or "denied".

    No answer available on stdin (EOF) counts as denied.
    """
    print("\n" + _box("APPROVAL REQUIRED", [f"Action: {action}", f"Reason: {reason}"]))
    if auto_approve_enabled():
        print("  WARNING: AUTO_APPROVE=1 is set (tests only) -> approved automatically")
        return "approved"
    try:
        answer = input("  Approve? [y/N] ").strip().lower()
    except EOFError:
        print("  (no input available) -> denied")
        return "denied"
    if not sys.stdin.isatty():  # piped answer: echo it so logs show what was chosen
        print(answer)
    decision = "approved" if answer in {"y", "yes"} else "denied"
    print(f"  -> {decision}\n")
    return decision
