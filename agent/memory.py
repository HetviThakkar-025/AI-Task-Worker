"""Working memory: facts the agent discovered and chose to keep.

It is re-injected into every prompt, so facts survive history trimming.
"""
from __future__ import annotations


class Memory:
    def __init__(self) -> None:
        self._facts: dict[str, str] = {}

    def set(self, key: str, value) -> None:
        self._facts[str(key).strip()] = str(value).strip()

    def get(self, key: str) -> str | None:
        return self._facts.get(key)

    def as_dict(self) -> dict[str, str]:
        return dict(self._facts)

    def as_text(self) -> str:
        if not self._facts:
            return "(empty)"
        return "\n".join(f"- {k}: {v}" for k, v in self._facts.items())
