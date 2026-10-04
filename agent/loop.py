"""The agent loop: observe -> decide (LLM) -> guard -> act (tool) -> repeat -> verify."""
from __future__ import annotations

import json
import os
import re
from typing import Callable

from agent import llm, policy
from agent import verifier as verifier_mod
from agent.approval import ask_approval
from agent.browser import BrowserSession
from agent.memory import Memory
from agent.reliability import LoopDetector
from agent import tools
from agent.tools import TOOL_SCHEMAS, execute_tool
from agent.trace import Trace

FULL_RESULTS_KEPT = 6
TRIMMED_RESULT_CHARS = 300
MAX_FAILED_VERIFICATIONS = 2
APPROVAL_VALID_STEPS = 5  # a voluntary approval covers the next guarded action within N steps
MAX_CONSECUTIVE_LOOP_WARNINGS = 3  # then the run is stopped as stuck

DENIED_RESULT = ("ERROR: Action denied by the human operator. Do not retry this action; "
                 "choose another approach or call finish explaining that it was not approved.")
ALREADY_DENIED_RESULT = ("ERROR: The human operator has already denied an action in this task; not asking again. "
                         "Call finish with success=false explaining what was not approved.")

SYSTEM_PROMPT = """You are an autonomous worker operating a web browser to complete tasks for a user.

Your job is to achieve the user's end goal, not just to describe how. Work out the steps yourself.

How you work:
- Make exactly one tool call per turn. Action tools return the fresh page state, so read it before deciding the next step.
- Page state lists interactive elements as [id]. Ids change after every action, so always use ids from the most recent state.
- Never ask the user for information you can find yourself in the environment. Explore the available pages first.
- Read page contents carefully. When choosing between several items, compare the relevant fields explicitly (for example, "latest" means compare the dates and pick the most recent; a similar name is not the same entity).
- Distinguish similar fields (e.g. issue date vs due date, net vs total) and use exactly the one the task asks for.
- Save important facts with `remember` as soon as you discover them (values you extracted, ids of records you created).
- Convert data to the format a form requires (dates, numbers without thousands separators, etc.). Look for format hints in labels and placeholders.
- If an action fails or the page shows an error, read the error message and try a different approach instead of repeating the same action.
- If a page shows a temporary or server error (e.g. "temporarily unavailable", "try again"), retry the same action once (re-enter any lost form data first) before trying anything else. If an action fails twice, switch approach or ask the user.
- Do not create duplicate records. If something seems to already exist, check before creating it again.
- Call `request_approval` BEFORE any irreversible action (paying, deleting, sending, cancelling) or any high-value action, i.e. submitting or saving anything involving a monetary amount of {threshold} or more. Only proceed if approved. Do NOT request approval for ordinary reversible actions such as navigating, filling forms, or saving records below that amount; just do them. If an action is denied, do not attempt or re-request it in any form; call `finish` with success=false explaining it was not approved.
- Names: if a candidate's name is exactly the name in the task (ignoring case), use it without asking; a longer name that merely contains it is a different entity (task "Smith Ltd": use "Smith Ltd", not "Smith Ltd Holdings").
- Ambiguity: if NO candidate matches the name exactly but several partially match (e.g. task "Smith", list shows "Smith Ltd" and "Smith & Co"), do NOT pick one yourself, not even the closest. Call `ask_user` with the candidates.
- Otherwise call `ask_user` only if required information is missing and cannot be found, or if you are stuck.
- Call `finish` only when the goal is achieved and you have seen evidence of it on the page (e.g. the saved record with the correct values). The summary must be short and state what was done and the evidence. If the goal cannot be achieved, call `finish` with success=false and explain why. Your work will be independently verified against the real system state."""


def system_prompt() -> str:
    return SYSTEM_PROMPT.replace("{threshold}", f"{policy.approval_threshold():,.0f}")


def _has_error(result: str) -> bool:
    """Tool result reports a failure or the page shows an error/alert."""
    return bool(re.search(r"^ERROR:|^Errors/alerts: (?!none)", result, re.M))


def _short(value, limit: int = 60) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 3] + "..."


class Agent:
    def __init__(self, session: BrowserSession | None, memory: Memory, max_steps: int = 30,
                 base_url: str = "http://localhost:8000",
                 verifier: Callable[..., dict] | None = None,
                 snapshot: Callable[[], dict] | None = None,
                 approver: Callable[[str, str], str] = ask_approval,
                 trace: Trace | None = None):
        self.session = session
        self.memory = memory
        self.max_steps = max_steps
        self.base_url = base_url
        self.verifier = verifier or verifier_mod.verify
        self.snapshot = snapshot or (lambda: verifier_mod.gather_snapshot(base_url))
        self.approver = approver
        self.trace = trace
        self._flags: dict = {}  # per-step trace flags
        self._failed_actions: set[str] = set()
        self.loops = LoopDetector()
        self.history: list[dict] = []
        self.steps: list[dict] = []
        self.task = ""
        self.clarifications: list[str] = []
        self.failed_verifications = 0
        self.last_verdict: dict = {}
        self.approval_valid_until = 0  # step number until which a voluntary approval holds
        self.denials = 0
        self.loop_warnings = 0
        self.step = 0

    # ------------------------------------------------------------ prompting --
    def _task_message(self) -> dict:
        paths = [p.strip() for p in os.getenv("START_PATHS", "/portal,/finance").split(",") if p.strip()]
        starts = " and ".join([", ".join(paths[:-1]), paths[-1]] if len(paths) > 1 else paths)
        return {
            "role": "user",
            "content": (
                f"Task: {self.task}\n\n"
                f"Environment base URL: {self.base_url}\n"
                f"Starting points: {starts}"
            ),
        }

    def _trimmed_history(self) -> list[dict]:
        tool_idx = [i for i, m in enumerate(self.history) if m["role"] == "tool"]
        old = set(tool_idx[:-FULL_RESULTS_KEPT])
        out = []
        for i, m in enumerate(self.history):
            if i in old and len(m["content"]) > TRIMMED_RESULT_CHARS:
                m = {**m, "content": m["content"][:TRIMMED_RESULT_CHARS] + " ...(older result trimmed)"}
            out.append(m)
        return out

    def _build_messages(self) -> list[dict]:
        status = {
            "role": "user",
            "content": f"Memory (facts saved so far):\n{self.memory.as_text()}\n\n"
                       f"Step {self.step} of {self.max_steps}. Choose the next tool call.",
        }
        return [self._task_message(), *self._trimmed_history(), status]

    # ------------------------------------------------------------- finishing --
    def _actions(self) -> list[dict]:
        return [{"tool": s["tool"], "args": s["args"]} for s in self.steps]

    def on_finish(self, summary: str, success: bool) -> dict:
        """Decide what a `finish` call means. Returns {"end": bool, "status", "feedback"}."""
        if not success:
            return {"end": True, "status": "failed", "feedback": ""}
        task = self.task
        if self.clarifications:
            task += "\nUser clarifications: " + "; ".join(self.clarifications)
        print("    verifying against real system state...")
        verdict = self.verifier(task, summary, self.memory.as_dict(), self._actions(), self.snapshot())
        self.last_verdict = verdict
        self._flags["verification"] = {"verified": bool(verdict.get("verified")),
                                       "discrepancies": verdict.get("discrepancies", [])}
        if verdict.get("verified"):
            print(f"    VERIFIED: {verdict.get('evidence', '')}")
            return {"end": True, "status": "done", "feedback": ""}
        self.failed_verifications += 1
        disc = "; ".join(verdict.get("discrepancies") or ["(no details)"])
        print(f"    NOT VERIFIED ({self.failed_verifications}/{MAX_FAILED_VERIFICATIONS}): {disc}")
        if self.failed_verifications >= MAX_FAILED_VERIFICATIONS:
            return {"end": True, "status": "unverified", "feedback": disc}
        return {"end": False, "status": "", "feedback": f"VERIFICATION FAILED: {disc}. Fix this and call finish again."}

    # ------------------------------------------------------------ user I/O --
    def _ask_user(self, question: str) -> str | None:
        print(f"\n  AGENT ASKS: {question}")
        try:
            answer = input("  your answer> ").strip()
        except EOFError:
            return None
        print(f"  (user answered: {answer})")
        return answer or "(no answer given)"

    def _describe(self, name: str, args: dict) -> str:
        if name == "browser_click" and self.session is not None:
            try:
                target = int(args.get("id"))
            except (TypeError, ValueError):
                target = None
            el = next((e for e in self.session.get_state_elements() if e["id"] == target), None)
            if el:
                return f'click {el["tag"]} "{el["label"]}" on {self.session.page.url}'
        return f"{name}({_short(args, 100)})"

    def _shot(self, name: str) -> None:
        if self.trace is not None:
            self.trace.screenshot(self.session, name, self.step)

    def _approve(self, action: str, reason: str) -> bool:
        self._flags["approval_requested"] = True
        self._shot("approval")
        approved = self.approver(action, reason) == "approved"
        self._flags["approved"] = approved
        return approved

    def _guarded_execute(self, name: str, args: dict) -> str:
        """Run a browser tool, asking the human first if policy requires it."""
        reason = policy.check(name, args, self.session)
        if reason:
            if self.step <= self.approval_valid_until:
                self.approval_valid_until = 0  # one-shot: consumed by this action
                self._flags["approved"] = True
                print(f"    (policy: {reason} -> covered by earlier approval)")
            elif not self._approve(self._describe(name, args), reason):
                self.denials += 1
                return DENIED_RESULT
        on_form = self.session is not None and any(
            e["tag"] in {"input", "select", "textarea"} for e in self.session.get_state_elements())
        url_before = self.session.page.url if self.session is not None else None
        result = execute_tool(self.session, name, args)
        if tools.last_call["retries"]:
            self._flags["retry"] = tools.last_call["retries"]
        if name == "browser_click" and on_form and self.session.page.url != url_before:
            self._shot("left_form")
        return result

    # ------------------------------------------------------------------ run --
    def _record(self, name: str, args: dict, result: str) -> None:
        key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if key in self._failed_actions:
            self._flags["retry"] = self._flags.get("retry") or "repeat_after_error"
        if _has_error(result):
            self._failed_actions.add(key)
            self._shot("error")
        flags, self._flags = self._flags, {}
        self.steps.append({"step": self.step, "tool": name, "args": args, "result": result[:500], "flags": flags})
        if self.trace is not None:
            self.trace.log_step(self.step, name, args, result, flags)
        print(f"[{self.step:02d}] {name}({_short(args)}) -> {_short(result, 120)}")

    def run(self, task: str) -> dict:
        self.task = task
        for self.step in range(1, self.max_steps + 1):
            try:
                reply = llm.chat(system_prompt(), self._build_messages(), TOOL_SCHEMAS)
            except Exception as e:
                return self._result("failed", f"LLM error: {e}")

            call = reply["tool_call"]
            if call is None:
                if reply["text"]:
                    self.history.append({"role": "assistant", "content": reply["text"]})
                self.history.append({"role": "user", "content": "Respond with exactly one tool call."})
                self._record("(no tool call)", {}, reply["text"] or "")
                continue

            name, args = call["name"], call["args"]
            self.history.append({"role": "assistant", "content": "", "tool_name": name, "tool_args": args,
                                 "meta": call.get("meta") or {}})

            if name == "remember":
                self.memory.set(args.get("key", ""), args.get("value", ""))
                result = f"Saved {args.get('key')} = {args.get('value')}"
            elif name == "ask_user":
                answer = self._ask_user(args.get("question", ""))
                if answer is None:  # no interactive user available
                    self._record(name, args, "(no stdin; stopping)")
                    return self._result("asked_user", f"Agent needs input: {args.get('question', '')}")
                self.clarifications.append(f"Q: {args.get('question', '')} A: {answer}")
                result = f"User answered: {answer}"
            elif name == "request_approval":
                if self.denials:  # don't pester the operator after a "no"
                    result = ALREADY_DENIED_RESULT
                elif self._approve(args.get("action", ""), args.get("reason", "")):
                    self.approval_valid_until = self.step + APPROVAL_VALID_STEPS
                    result = "APPROVED by the human operator. You may perform this action now."
                else:
                    self.denials += 1
                    result = DENIED_RESULT
            elif name == "finish":
                summary, success = args.get("summary", ""), bool(args.get("success", False))
                self._shot("finish")
                outcome = self.on_finish(summary, success)
                if outcome["end"]:
                    self._record(name, args, outcome["status"])
                    return self._result(outcome["status"], summary)
                result = outcome["feedback"]
            else:
                result = self._guarded_execute(name, args)

            warning = self.loops.record(name, args)
            self.loop_warnings = self.loop_warnings + 1 if warning else 0
            if warning:
                result = f"{warning}\n\n{result}"
            self.history.append({"role": "tool", "tool_name": name, "content": result})
            self._record(name, args, result)
            if self.loop_warnings >= MAX_CONSECUTIVE_LOOP_WARNINGS:
                return self._result("failed", f"Stopped: agent kept repeating {name} after "
                                              f"{self.loop_warnings} loop warnings. Last result: {result[-300:]}")

        return self._result("max_steps", f"Stopped after {self.max_steps} steps without finishing.")

    def _result(self, status: str, summary: str) -> dict:
        v = self.last_verdict
        return {
            "status": status,
            "summary": summary,
            "evidence": v.get("evidence", ""),
            "discrepancies": v.get("discrepancies", []),
            "steps": self.steps,
            "memory": self.memory.as_dict(),
        }
