from agent import loop as loop_mod
from agent.loop import Agent
from agent.memory import Memory


def make_agent(verdicts, **kw):
    verdicts = list(verdicts)
    calls = []

    def fake_verifier(task, summary, memory, actions, snapshot):
        calls.append(summary)
        return verdicts.pop(0)

    agent = Agent(None, Memory(), verifier=fake_verifier, snapshot=lambda: {}, **kw)
    agent.task = "do the thing"
    return agent, calls


OK = {"verified": True, "evidence": "record matches", "discrepancies": []}
BAD = {"verified": False, "evidence": "", "discrepancies": ["amount differs"]}


def test_unsuccessful_finish_ends_failed_without_verifying():
    agent, calls = make_agent([])
    assert agent.on_finish("could not do it", False) == {"end": True, "status": "failed", "feedback": ""}
    assert calls == []


def test_verified_finish_ends_done():
    agent, _ = make_agent([OK])
    assert agent.on_finish("done", True)["status"] == "done"


def test_first_failed_verification_continues_with_feedback():
    agent, _ = make_agent([BAD])
    out = agent.on_finish("done", True)
    assert out["end"] is False
    assert out["feedback"].startswith("VERIFICATION FAILED: amount differs")


def test_second_failed_verification_ends_unverified():
    agent, _ = make_agent([BAD, BAD])
    agent.on_finish("done", True)
    out = agent.on_finish("done again", True)
    assert out == {"end": True, "status": "unverified", "feedback": "amount differs"}


def test_run_retries_after_failed_verification(monkeypatch):
    """Full loop with a scripted LLM: finish -> rejected -> finish -> verified."""
    script = iter([
        {"text": None, "tool_call": {"name": "remember", "args": {"key": "x", "value": "1"}}},
        {"text": None, "tool_call": {"name": "finish", "args": {"summary": "first try", "success": True}}},
        {"text": None, "tool_call": {"name": "finish", "args": {"summary": "fixed", "success": True}}},
    ])
    seen = []

    def fake_chat(system, messages, tools):
        seen.append(messages)
        return next(script)

    monkeypatch.setattr(loop_mod.llm, "chat", fake_chat)
    agent, calls = make_agent([BAD, OK])
    result = agent.run("do the thing")
    assert result["status"] == "done"
    assert result["evidence"] == "record matches"
    assert calls == ["first try", "fixed"]
    # the verification failure was fed back to the model as a tool result
    assert any("VERIFICATION FAILED" in m.get("content", "") for m in seen[2])
    # memory is re-injected in the latest user message
    assert "- x: 1" in seen[2][-1]["content"]


def test_denied_voluntary_approval_returns_denial(monkeypatch):
    script = iter([
        {"text": None, "tool_call": {"name": "request_approval", "args": {"action": "pay", "reason": "irreversible"}}},
        {"text": None, "tool_call": {"name": "finish", "args": {"summary": "not approved", "success": False}}},
    ])
    seen = []
    monkeypatch.setattr(loop_mod.llm, "chat", lambda s, m, t: (seen.append(m), next(script))[1])
    agent, _ = make_agent([], approver=lambda action, reason: "denied")
    result = agent.run("pay it")
    assert result["status"] == "failed"
    assert any(m.get("content") == loop_mod.DENIED_RESULT for m in seen[1])


def test_no_second_approval_prompt_after_denial(monkeypatch):
    script = iter([
        {"text": None, "tool_call": {"name": "request_approval", "args": {"action": "save", "reason": "high value"}}},
        {"text": None, "tool_call": {"name": "request_approval", "args": {"action": "save it", "reason": "again"}}},
        {"text": None, "tool_call": {"name": "finish", "args": {"summary": "not approved", "success": False}}},
    ])
    monkeypatch.setattr(loop_mod.llm, "chat", lambda s, m, t: next(script))
    prompts = []
    agent, _ = make_agent([], approver=lambda action, reason: (prompts.append(action), "denied")[1])
    result = agent.run("save it")
    assert prompts == ["save"]  # human asked exactly once
    assert agent.steps[1]["result"].startswith("ERROR: The human operator has already denied")
    assert result["status"] == "failed"


def test_system_prompt_includes_threshold(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD", "5000")
    assert "5,000 or more" in loop_mod.system_prompt()


def test_run_stops_when_stuck_in_a_loop(monkeypatch):
    same = {"text": None, "tool_call": {"name": "remember", "args": {"key": "k", "value": "v"}}}
    monkeypatch.setattr(loop_mod.llm, "chat", lambda s, m, t: same)
    agent, _ = make_agent([])
    result = agent.run("anything")
    # warnings start at the 3rd repeat; 3 consecutive warnings -> stop at step 5
    assert result["status"] == "failed"
    assert "kept repeating" in result["summary"]
    assert len(result["steps"]) == 5
