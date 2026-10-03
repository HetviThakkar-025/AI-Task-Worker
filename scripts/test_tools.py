"""Drive the browser tool layer end-to-end against the mock server (localhost:8000).

Run: .venv/bin/python scripts/test_tools.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import setup_env  # noqa: E402

setup_env()  # .env and PW_LIB_PATH, before Playwright starts

from agent.browser import BrowserSession  # noqa: E402
from agent.tools import execute_tool  # noqa: E402

BASE = "http://localhost:8000"


def section(title: str) -> None:
    print(f"\n{'=' * 20} {title} {'=' * 20}")


def find_id(session: BrowserSession, label_contains: str, tag: str | None = None) -> int:
    for e in session.last_elements:
        if label_contains.lower() in e["label"].lower() and (tag is None or e["tag"] == tag):
            return e["id"]
    raise RuntimeError(f"No element with label containing {label_contains!r}")


def main() -> None:
    print("POST /reset ->", httpx.post(f"{BASE}/reset").json())
    s = BrowserSession()
    try:
        section("Open Company X invoices")
        state = execute_tool(s, "browser_goto", {"url": "/portal/invoices?company=Company X"})
        print(state)

        # Pick the newest invoice by issue date (rows: ID | issue | amount | due | status).
        rows = re.findall(r"^(\S+) \| (\d{2}/\d{2}/\d{4}) \|", state, re.M)
        newest = max(rows, key=lambda r: datetime.strptime(r[1], "%d/%m/%Y"))[0]
        print(f"\n-> rows found: {rows}\n-> newest invoice: {newest}")

        section(f"Click invoice {newest}")
        state = execute_tool(s, "browser_click", {"id": find_id(s, f"View invoice {newest}")})
        print(state)

        section("read_pdf")
        pdf_href = next(e["href"] for e in s.last_elements if e["label"] == "Download PDF")
        print(execute_tool(s, "read_pdf", {"url": pdf_href}))

        section("Finance: new bill form")
        print(execute_tool(s, "browser_goto", {"url": "/finance/bills/new"}))
        execute_tool(s, "browser_select", {"id": find_id(s, "Vendor", "select"), "value": "Company X"})
        execute_tool(s, "browser_type", {"id": find_id(s, "Invoice number", "input"), "text": newest})
        execute_tool(s, "browser_type", {"id": find_id(s, "Amount", "input"), "text": "4700.50"})
        print(execute_tool(s, "browser_type", {"id": find_id(s, "Due date", "input"), "text": "22/09/2026"}))

        section("Save with wrong date format")
        state = execute_tool(s, "browser_click", {"id": find_id(s, "Save", "button")})
        print(state)
        print("\n-> visible error:", re.search(r"^Errors/alerts: (.*)$", state, re.M).group(1))

        section("Retype date and Save again")
        execute_tool(s, "browser_type", {"id": find_id(s, "Due date", "input"), "text": "2026-09-22"})
        s.screenshot(str(ROOT / "runs" / "test.png"))
        print("screenshot (filled form) -> runs/test.png")
        print(execute_tool(s, "browser_click", {"id": find_id(s, "Save", "button")}))

        section("GET /api/bills")
        print(httpx.get(f"{BASE}/api/bills").json())

        section("Error handling")
        print("click nonexistent id ->", execute_tool(s, "browser_click", {"id": 999}).splitlines()[0])
        print("unknown tool ->", execute_tool(s, "fly_to_moon", {}))
        print("missing arg ->", execute_tool(s, "browser_type", {"id": 1}))
        print("goto a PDF ->", execute_tool(s, "browser_goto", {"url": pdf_href}).splitlines()[0])
        print("select bad option ->", execute_tool(s, "browser_goto", {"url": "/finance/bills/new"}) and
              execute_tool(s, "browser_select", {"id": find_id(s, "Vendor", "select"), "value": "Initech"}).splitlines()[0])
        print("read_pdf on HTML ->", execute_tool(s, "read_pdf", {"url": "/portal"}))
    finally:
        s.close()


if __name__ == "__main__":
    main()
