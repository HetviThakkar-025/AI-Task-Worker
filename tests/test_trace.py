import json

from agent import loop as loop_mod
from agent.loop import Agent
from agent.memory import Memory
from agent.trace import Trace


def test_log_step_and_report(tmp_path):
    t = Trace(run_dir=tmp_path / "run")
    t.log_step(1, "browser_click", {"id": 3}, "x" * 900, {"approval_requested": True, "approved": False})
    rec = json.loads((t.run_dir / "trace.jsonl").read_text().splitlines()[0])
    assert rec["step"] == 1 and len(rec["result"]) == 500 and rec["flags"]["approved"] is False

    steps = [{"step": 1, "tool": "browser_click", "args": {"id": 3}, "result": "ERROR: a|b",
              "flags": {"approval_requested": True, "approved": False}}]
    report = t.write_report("do it", "failed", "denied", "", steps, {"/api/bills": []}, ["nothing saved"])
    text = report.read_text()
    assert "`failed`" in text and "- nothing saved" in text
    assert "[approval_requested, denied] ERROR: a\\|b" in text  # flags shown, pipe escaped
    assert "`GET /api/bills`" in text


def test_agent_writes_trace(tmp_path, monkeypatch):
    script = iter([
        {"text": None, "tool_call": {"name": "remember", "args": {"key": "k", "value": "v"}}},
        {"text": None, "tool_call": {"name": "finish", "args": {"summary": "ok", "success": True}}},
    ])
    monkeypatch.setattr(loop_mod.llm, "chat", lambda s, m, t: next(script))
    trace = Trace(run_dir=tmp_path / "run")
    agent = Agent(None, Memory(), trace=trace, snapshot=lambda: {},
                  verifier=lambda *a: {"verified": True, "evidence": "e", "discrepancies": []})
    assert agent.run("t")["status"] == "done"
    lines = (trace.run_dir / "trace.jsonl").read_text().splitlines()
    assert [json.loads(l)["tool"] for l in lines] == ["remember", "finish"]
    assert json.loads(lines[1])["flags"]["verification"]["verified"] is True
