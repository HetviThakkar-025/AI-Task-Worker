"""Reliability helpers: loop detection, transient-error retries, tool timeouts."""
from __future__ import annotations

import json
import time
from collections import deque
from typing import Callable, TypeVar

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

T = TypeVar("T")

# Upper bound for a single browser operation (Playwright per-call timeout).
TOOL_TIMEOUT_S = 15

LOOP_WARNING = "You are repeating yourself. Try a different approach, or call ask_user if you are stuck."


class LoopDetector:
    """Flags when the agent repeats the same action or ping-pongs between two."""

    def __init__(self, repeat_limit: int = 3, window: int = 10):
        self.repeat_limit = repeat_limit
        self.recent: deque[str] = deque(maxlen=window)

    @staticmethod
    def _key(tool: str, args: dict | None) -> str:
        return f"{tool}:{json.dumps(args or {}, sort_keys=True, default=str)}"

    def record(self, tool: str, args: dict | None) -> str | None:
        """Record an action; return a warning string if a loop is detected."""
        key = self._key(tool, args)
        self.recent.append(key)
        if list(self.recent).count(key) >= self.repeat_limit:
            return LOOP_WARNING
        last6 = list(self.recent)[-6:]
        if len(last6) == 6 and len(set(last6)) == 2 and all(last6[i] != last6[i + 1] for i in range(5)):
            return LOOP_WARNING
        return None


def is_transient(err: Exception) -> bool:
    """Browser-level failures worth retrying. App validation errors are not."""
    if isinstance(err, PlaywrightTimeout):
        return True
    if isinstance(err, PlaywrightError):
        msg = str(err)
        return "net::ERR" in msg or "Navigation" in msg or "navigation" in msg
    return False


def with_retry(fn: Callable[[], T], attempts: int = 3, backoff: float = 1.0,
               deadline_s: float | None = None,
               on_retry: Callable[[int, Exception], None] | None = None) -> T:
    """Call fn, retrying transient browser errors with exponential backoff.

    Non-transient errors (e.g. ValueError for a bad element id) are raised at
    once. If deadline_s is given, no new attempt starts after it has elapsed.
    """
    start = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            out_of_time = deadline_s is not None and time.monotonic() - start >= deadline_s
            if not is_transient(e) or attempt == attempts or out_of_time:
                raise
            if on_retry:
                on_retry(attempt, e)
            time.sleep(backoff * 2 ** (attempt - 1))
    raise AssertionError("unreachable")
