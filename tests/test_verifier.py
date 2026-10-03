from agent.verifier import parse_verdict, verify


def fake_chat(text):
    calls = []

    def chat(system, messages, tools):
        calls.append((system, messages, tools))
        return {"text": text, "tool_call": None}

    chat.calls = calls
    return chat


def run(text):
    return verify("task", "summary", {}, [], {"/api/bills": []}, chat=fake_chat(text))


def test_verified_true():
    v = run('{"verified": true, "evidence": "bill 1 matches", "discrepancies": []}')
    assert v == {"verified": True, "evidence": "bill 1 matches", "discrepancies": []}


def test_verified_false_in_code_fence():
    v = run('```json\n{"verified": false, "evidence": "x", "discrepancies": ["wrong due date"]}\n```')
    assert v["verified"] is False
    assert v["discrepancies"] == ["wrong due date"]


def test_malformed_output_is_not_verified():
    for text in ["looks good to me!", '{"verified": "yes"}', '{"verified": true', None, ""]:
        v = run(text)
        assert v["verified"] is False, text
        assert v["discrepancies"]


def test_verifier_uses_no_tools_and_sees_snapshot():
    chat = fake_chat('{"verified": true, "evidence": "", "discrepancies": []}')
    verify("T", "S", {"k": "v"}, [{"tool": "x", "args": {}}], {"/api/bills": [{"id": 7}]}, chat=chat)
    system, messages, tools = chat.calls[0]
    assert not tools
    assert '"id": 7' in messages[0]["content"]


def test_parse_verdict_handles_surrounding_text():
    assert parse_verdict('Here: {"verified": true, "evidence": "e", "discrepancies": []} done')["verified"]


def test_raw_newline_inside_string_is_accepted():
    # Seen live: model emitted a literal newline inside the evidence string.
    v = run('```json\n{\n  "verified": true,\n  "evidence": "Both\n invoices entered",\n  "discrepancies": []\n}\n```')
    assert v["verified"] is True
    assert "invoices entered" in v["evidence"]
