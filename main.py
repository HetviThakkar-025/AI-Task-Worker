"""CLI entry point.

Usage: python main.py "task text" [--headed] [--no-reset] [--flaky] [--auto-approve] [--max-steps N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
BASE_URL = "http://localhost:8000"


def setup_env() -> None:
    load_dotenv(ROOT / ".env")
    lib = os.getenv("PW_LIB_PATH")
    if lib:  # extra shared libs for Chromium (must be set before Playwright starts)
        os.environ["LD_LIBRARY_PATH"] = lib + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")


def server_up() -> bool:
    try:
        httpx.get(BASE_URL, timeout=2)
        return True
    except httpx.HTTPError:
        return False


def ensure_server() -> subprocess.Popen | None:
    if server_up():
        return None
    python = ROOT / ".venv" / "bin" / "python"
    print("Mock server not running; starting it...")
    proc = subprocess.Popen(
        [str(python if python.exists() else sys.executable), "-m", "uvicorn", "mock_env.app:app", "--port", "8000"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        if server_up():
            return proc
        time.sleep(0.5)
    proc.terminate()
    raise SystemExit("Mock server did not start on port 8000")


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous AI task worker")
    parser.add_argument("task", help="Natural-language task")
    parser.add_argument("--no-reset", action="store_true", help="Do not reset the mock environment first")
    parser.add_argument("--flaky", action="store_true", help="Make the next bill save fail once with a 503")
    parser.add_argument("--auto-approve", action="store_true", help="Approve all approval requests (tests only)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (sets HEADLESS=0)")
    parser.add_argument("--max-steps", type=int, default=30)
    args = parser.parse_args()

    setup_env()
    if args.headed:
        os.environ["HEADLESS"] = "0"
    if args.auto_approve:
        os.environ["AUTO_APPROVE"] = "1"
        print("WARNING: --auto-approve is on. Every approval request will be granted automatically (tests only).")
    # Imported after setup_env so .env / LD_LIBRARY_PATH are in place.
    from agent.browser import BrowserSession
    from agent.loop import Agent
    from agent.memory import Memory
    from agent.trace import Trace
    from agent.verifier import gather_snapshot

    server = ensure_server()
    if not args.no_reset:
        httpx.post(f"{BASE_URL}/reset")
        print("Environment reset.")
    if args.flaky:
        httpx.post(f"{BASE_URL}/chaos", json={"flaky_save": True})
        print("Chaos: flaky_save enabled (next bill save returns 503 once).")

    print(f"TASK: {args.task}\n")
    trace = Trace(root=ROOT / "runs")
    session = BrowserSession()
    try:
        agent = Agent(session, Memory(), max_steps=args.max_steps, base_url=BASE_URL, trace=trace)
        result = agent.run(args.task)
    finally:
        session.close()

    print("\n" + "=" * 72)
    print(f"STATUS:   {result['status']}")
    print(f"SUMMARY:  {result['summary']}")
    print(f"EVIDENCE: {result['evidence'] or '(none)'}")
    if result["discrepancies"] and result["status"] != "done":
        print(f"DISCREPANCIES: {result['discrepancies']}")
    print(f"STEPS:    {len(result['steps'])}")
    print(f"MEMORY:   {json.dumps(result['memory'], ensure_ascii=False)}")
    final_state = gather_snapshot(BASE_URL)
    print(f"/api/bills: {json.dumps(final_state.get('/api/bills'), indent=2)}")
    report = trace.write_report(args.task, result["status"], result["summary"], result["evidence"],
                                result["steps"], final_state, result["discrepancies"])
    print(f"\nRUN DIR:  {trace.run_dir}  (report: {report.name}, trace.jsonl, {len(trace.screenshots)} screenshots)")

    if server:
        server.terminate()


if __name__ == "__main__":
    main()
