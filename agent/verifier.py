"""Independent verification of the worker's claimed outcome.

The environment snapshot is gathered by the harness, not by the agent, so the
verifier judges the real system state rather than the agent's own account.
"""
from __future__ import annotations

import json
import re
from typing import Callable

import httpx

from agent import llm

# Read-only endpoints that expose the environment's ground truth.
SNAPSHOT_ENDPOINTS = ["/api/bills", "/api/vendors", "/api/invoices"]

VERIFIER_SYSTEM = """You are an independent auditor checking the work of an autonomous software worker.

You receive: the original task (plus any clarifications the user gave), the worker's claimed summary, the actions it took, and a snapshot of the REAL system state fetched directly from the systems.

Decide whether the task was actually achieved, based on the real system state, not on the worker's claims.
- Identify which source records the task refers to and check the worker chose the right ones (e.g. "latest" means the most recent by date; similar names are different entities).
- Compare every concrete value (names, identifiers, amounts, dates) in the resulting records with the source data. Formatting differences are fine (e.g. 22/10/2026 equals 2026-10-22, 4,700.50 equals 4700.5); different values are not.
- Check that nothing was missing, duplicated or created that the task did not ask for.
- Be strict: if you cannot confirm the outcome from the snapshot, it is not verified.

Reply with ONLY a JSON object, no other text:
{"verified": true|false, "evidence": "<one or two sentences citing the records that prove or disprove the outcome>", "discrepancies": ["<each problem found>"]}"""


def gather_snapshot(base_url: str, endpoints: list[str] = SNAPSHOT_ENDPOINTS) -> dict:
    snap = {}
    for path in endpoints:
        try:
            r = httpx.get(base_url.rstrip("/") + path, timeout=10)
            r.raise_for_status()
            snap[path] = r.json()
        except Exception as e:  # report, don't crash: missing data -> not verifiable
            snap[path] = f"(unavailable: {e})"
    return snap


def parse_verdict(text: str | None) -> dict:
    """Parse the verifier's JSON reply. Anything unparseable counts as not verified."""
    fallback = {"verified": False, "evidence": "",
                "discrepancies": [f"Verifier output could not be parsed: {(text or '')[:200]!r}"]}
    if not text:
        return fallback
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return fallback
    try:
        data = json.loads(match.group(0), strict=False)  # tolerate raw newlines inside strings
    except json.JSONDecodeError:
        return fallback
    if not isinstance(data, dict) or not isinstance(data.get("verified"), bool):
        return fallback
    disc = data.get("discrepancies") or []
    return {
        "verified": data["verified"],
        "evidence": str(data.get("evidence", "")),
        "discrepancies": [str(d) for d in (disc if isinstance(disc, list) else [disc])],
    }


def verify(task: str, summary: str, memory: dict, actions: list[dict], environment_snapshot: dict,
           chat: Callable = llm.chat) -> dict:
    prompt = (
        f"ORIGINAL TASK:\n{task}\n\n"
        f"WORKER'S CLAIMED SUMMARY:\n{summary}\n\n"
        f"WORKER'S SAVED FACTS:\n{json.dumps(memory, ensure_ascii=False)}\n\n"
        f"WORKER'S ACTIONS:\n{json.dumps(actions, ensure_ascii=False)}\n\n"
        f"REAL SYSTEM STATE (fetched independently):\n{json.dumps(environment_snapshot, indent=1, ensure_ascii=False)}"
    )
    try:
        reply = chat(VERIFIER_SYSTEM, [{"role": "user", "content": prompt}], None)
    except Exception as e:
        return {"verified": False, "evidence": "", "discrepancies": [f"Verifier call failed: {e}"]}
    return parse_verdict(reply.get("text"))
