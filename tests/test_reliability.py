import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from agent.reliability import LOOP_WARNING, LoopDetector, with_retry


def test_three_identical_actions_trigger_warning():
    d = LoopDetector()
    assert d.record("browser_click", {"id": 3}) is None
    assert d.record("browser_click", {"id": 3}) is None
    assert d.record("browser_click", {"id": 3}) == LOOP_WARNING


def test_arg_order_does_not_matter():
    d = LoopDetector()
    d.record("browser_type", {"id": 1, "text": "a"})
    d.record("browser_type", {"text": "a", "id": 1})
    assert d.record("browser_type", {"id": 1, "text": "a"}) == LOOP_WARNING


def test_alternating_between_two_actions_triggers_warning():
    d = LoopDetector(repeat_limit=99)  # isolate the alternation rule
    a, b = ("browser_goto", {"url": "/a"}), ("browser_goto", {"url": "/b"})
    results = [d.record(*x) for x in (a, b, a, b, a, b)]
    assert results[:5] == [None] * 5
    assert results[5] == LOOP_WARNING


def test_varied_actions_do_not_trigger():
    d = LoopDetector()
    for i in range(10):
        assert d.record("browser_click", {"id": i}) is None


def test_with_retry_retries_transient_errors():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise PlaywrightTimeout("Timeout 15000ms exceeded")
        return "ok"

    assert with_retry(flaky, attempts=3, backoff=0) == "ok"
    assert len(calls) == 3


def test_with_retry_does_not_retry_validation_errors():
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("Element 9 not found")

    with pytest.raises(ValueError):
        with_retry(bad, attempts=3, backoff=0)
    assert len(calls) == 1
