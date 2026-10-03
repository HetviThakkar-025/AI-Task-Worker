"""Per-run trace: JSONL step log, screenshots and a Markdown report.

Each run gets its own directory runs/<YYYYmmdd_HHMMSS>/ containing
trace.jsonl, *.png and report.md.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

RESULT_CHARS = 500


def _cell(value, limit: int = 90) -> str:
    """Single-line, pipe-safe text for a Markdown table cell."""
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 3] + "..."


class Trace:
    def __init__(self, run_dir: str | Path | None = None, root: str | Path = "runs"):
        if run_dir is None:
            run_dir = Path(root) / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path(run_dir)
        suffix = 1
        while self.run_dir.exists() and any(self.run_dir.iterdir()):  # two runs in the same second
            self.run_dir = Path(f"{run_dir}_{suffix}")
            suffix += 1
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.run_dir / "trace.jsonl"
        self.screenshots: list[dict] = []  # {"step", "name", "file"}

    def log_step(self, step: int, tool: str, args: dict, result_preview: str, extra: dict | None = None) -> None:
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "step": step,
            "tool": tool,
            "args": args,
            "result": (result_preview or "")[:RESULT_CHARS],
            "flags": extra or {},
        }
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def screenshot(self, session, name: str, step: int = 0) -> str | None:
        """Save a PNG of the current page; never let a failed screenshot break the run."""
        if session is None:
            return None
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        file = f"{step:02d}_{safe}.png"
        try:
            session.screenshot(str(self.run_dir / file))
        except Exception:
            return None
        self.screenshots.append({"step": step, "name": name, "file": file})
        return file

    def write_report(self, task: str, status: str, summary: str, evidence: str, steps: list[dict],
                     final_state: dict, discrepancies: list[str] | None = None) -> Path:
        lines = [
            "# Run report",
            "",
            f"- **Task:** {task}",
            f"- **Status:** `{status}`",
            f"- **Steps:** {len(steps)}",
            f"- **Run directory:** `{self.run_dir}`",
            "",
            "## Summary",
            "",
            summary or "(none)",
            "",
            "## Verification",
            "",
            f"**Evidence:** {evidence or '(not verified)'}",
            "",
        ]
        if discrepancies:
            lines += ["**Discrepancies:**", ""] + [f"- {d}" for d in discrepancies] + [""]

        lines += ["## Steps", "", "| Step | Tool | Args | Outcome |", "|---:|---|---|---|"]
        for s in steps:
            outcome = s.get("result", "")
            flags = s.get("flags") or {}
            marks = [k for k in ("approval_requested", "retry") if flags.get(k)]
            if "approved" in flags:
                marks.append("approved" if flags["approved"] else "denied")
            if marks:
                outcome = f"[{', '.join(marks)}] {outcome}"
            lines.append(f"| {s['step']} | `{s['tool']}` | {_cell(s.get('args', {}), 60)} | {_cell(outcome)} |")
        lines.append("")

        lines += ["## Screenshots", ""]
        if self.screenshots:
            lines += [f"- Step {sh['step']}, {sh['name']}: [{sh['file']}]({sh['file']})" for sh in self.screenshots]
        else:
            lines.append("(none)")
        lines.append("")

        lines += ["## Final system state", ""]
        for path, data in final_state.items():
            lines += [f"`GET {path}`", "", "```json", json.dumps(data, indent=2, ensure_ascii=False), "```", ""]

        report = self.run_dir / "report.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        return report
