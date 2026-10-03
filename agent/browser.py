"""Text-based browser for the agent, built on the Playwright sync API.

Every observation is a plain-text "page state": URL, title, visible errors,
a compact text summary, and a numbered list of interactive elements. The
numbers are written into the DOM as `data-agent-id` attributes so that
actions (click/type/select) can address elements by id. Ids are reassigned
on every `get_state()` call.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from agent.reliability import TOOL_TIMEOUT_S

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
MAX_ELEMENTS = 80
MAX_TEXT_CHARS = 1500

# Runs in the page. Returns {elements, errors, text, truncated}.
_SNAPSHOT_JS = r"""
({maxElements, maxText}) => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();

  document.querySelectorAll('[data-agent-id]').forEach(e => e.removeAttribute('data-agent-id'));

  const labelFor = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' || tag === 'button') {
      const t = clean(el.innerText) || clean(el.value);
      if (t) return t;
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    if (el.placeholder) return clean(el.placeholder);
    if (el.title) return clean(el.title);
    return clean(el.name || el.id || '');
  };

  const all = Array.from(document.querySelectorAll('a, button, input, select, textarea'))
    .filter(el => !(el.tagName === 'INPUT' && el.type === 'hidden'))
    .filter(isVisible);

  const elements = [];
  all.slice(0, maxElements).forEach((el, i) => {
    const id = i + 1;
    el.setAttribute('data-agent-id', String(id));
    const tag = el.tagName.toLowerCase();
    const item = { id, tag, label: labelFor(el), disabled: !!el.disabled };
    if (tag === 'input') { item.type = el.type; item.value = el.value; }
    if (tag === 'textarea') { item.value = el.value; }
    if (tag === 'select') {
      item.options = Array.from(el.options).map(o => clean(o.text));
      item.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
    }
    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      try {
        const u = new URL(href, location.href);
        item.href = u.origin === location.origin ? u.pathname + u.search : u.href;
      } catch (e) { item.href = href; }
    }
    elements.push(item);
  });

  const errors = Array.from(document.querySelectorAll('[role="alert"], [class*="error"]'))
    .filter(isVisible).map(e => clean(e.innerText)).filter(Boolean);

  // innerText keeps table rows on their own lines with tab-separated cells.
  const lines = (document.body ? document.body.innerText : '')
    .split('\n')
    .map(l => l.split('\t').map(clean).filter(Boolean).join(' | '))
    .filter(Boolean);
  let text = lines.join('\n');
  if (text.length > maxText) text = text.slice(0, maxText) + '\n...(text truncated)';

  return { elements, errors: [...new Set(errors)], text, truncated: all.length > maxElements, total: all.length };
}
"""


def _q(s: str, limit: int = 80) -> str:
    s = s if len(s) <= limit else s[: limit - 3] + "..."
    return '"' + s.replace('"', "'") + '"'


def _format_element(e: dict) -> str:
    tag = e["tag"]
    if tag == "a":
        line = f'[{e["id"]}] link {_q(e["label"])} -> {e.get("href", "")}'
    elif tag == "button":
        line = f'[{e["id"]}] button {_q(e["label"])}'
    elif tag == "input":
        line = f'[{e["id"]}] input({e.get("type", "text")}) {_q(e["label"])} value={_q(e.get("value", ""))}'
    elif tag == "select":
        opts = ",".join(_q(o, 40) for o in e.get("options", []))
        line = f'[{e["id"]}] select {_q(e["label"])} selected={_q(e.get("value", ""))} options=[{opts}]'
    else:
        line = f'[{e["id"]}] {tag} {_q(e["label"])} value={_q(e.get("value", ""))}'
    if e.get("disabled"):
        line += " (disabled)"
    return line


class BrowserSession:
    def __init__(self, headless: bool | None = None, base_url: str = BASE_URL):
        if headless is None:
            headless = os.getenv("HEADLESS", "1") != "0"
        self.base_url = base_url.rstrip("/") + "/"
        self._pw = sync_playwright().start()
        slow_mo = float(os.getenv("SLOW_MO_MS", "0") or 0)  # slow actions down for demo recordings
        self.browser = self._pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        self.context = self.browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.page.set_default_timeout(TOOL_TIMEOUT_S * 1000)
        self.page.set_default_navigation_timeout(TOOL_TIMEOUT_S * 1000)
        self.last_elements: list[dict] = []

    # ------------------------------------------------------------- helpers --
    def resolve_url(self, url: str) -> str:
        """Allow relative paths like '/portal' (resolved against base_url)."""
        return urljoin(self.base_url, url)

    def _settle(self) -> None:
        """Wait for navigation / network activity triggered by an action."""
        try:
            self.page.wait_for_load_state("load", timeout=8_000)
            self.page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightTimeout:
            pass  # page still busy; the next state will show whatever loaded

    def _locate(self, element_id):
        try:
            element_id = int(element_id)
        except (TypeError, ValueError):
            raise ValueError(f"Element id must be an integer, got {element_id!r}")
        loc = self.page.locator(f'[data-agent-id="{element_id}"]')
        if loc.count() == 0:
            raise ValueError(f"Element {element_id} not found; call get_state, ids change after navigation")
        return loc.first

    def get_state_elements(self) -> list[dict]:
        """Elements from the latest state the agent saw (ids match that state)."""
        return list(self.last_elements)

    # ---------------------------------------------------------- observation --
    def get_state(self) -> str:
        snap = self.page.evaluate(_SNAPSHOT_JS, {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT_CHARS})
        self.last_elements = snap["elements"]
        parts = [
            f"URL: {self.page.url}",
            f"Title: {self.page.title()}",
            "Errors/alerts: " + (" || ".join(snap["errors"]) if snap["errors"] else "none"),
            "",
            "Visible text:",
            snap["text"] or "(empty)",
            "",
            f"Interactive elements ({len(snap['elements'])}"
            + (f" of {snap['total']}, list truncated" if snap["truncated"] else "")
            + "):",
        ]
        parts += [_format_element(e) for e in snap["elements"]] or ["(none)"]
        return "\n".join(parts)

    # -------------------------------------------------------------- actions --
    def goto(self, url: str) -> str:
        target = self.resolve_url(url)
        try:
            self.page.goto(target, wait_until="load")
        except PlaywrightError as e:
            if "Download is starting" in str(e):
                raise ValueError(f"{target} is a file download, not a page; use read_pdf to read it") from None
            raise
        self._settle()
        return self.get_state()

    def click(self, element_id) -> str:
        self._locate(element_id).click()
        self._settle()
        return self.get_state()

    def type(self, element_id, text: str) -> str:
        loc = self._locate(element_id)
        loc.fill("")  # clear first
        loc.fill(str(text))
        return self.get_state()

    def select(self, element_id, value: str) -> str:
        loc = self._locate(element_id)
        options = loc.evaluate("el => Array.from(el.options || []).map(o => ({value: o.value, text: o.text.trim()}))")
        if not options:
            raise ValueError(f"Element {element_id} is not a select")
        want = str(value).strip()
        match = (
            next((o for o in options if o["value"] == want), None)
            or next((o for o in options if o["text"] == want), None)
            or next((o for o in options if o["text"].lower() == want.lower()), None)
        )
        if match is None:
            available = ", ".join(repr(o["text"]) for o in options)
            raise ValueError(f"No option {want!r} in select {element_id}; available: {available}")
        loc.select_option(value=match["value"])
        self._settle()
        return self.get_state()

    def screenshot(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=path, full_page=True)
        return path

    def close(self) -> None:
        for closer in (self.context.close, self.browser.close, self._pw.stop):
            try:
                closer()
            except Exception:
                pass
