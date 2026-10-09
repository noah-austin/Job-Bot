"""Playwright wrapper that exposes the page to the agent as a numbered list of form controls.

Every interactive element (across all frames, since Greenhouse and others embed
forms in iframes) is tagged with a stable `data-jobbot-id`, so the model can say
"fill e12" instead of writing selectors.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.sync_api import BrowserContext, Frame, Page, sync_playwright

TAG_AND_DESCRIBE_JS = r"""
(prefix) => {
  const counter = window.__jobbotCounter || (window.__jobbotCounter = {n: 0});
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const text = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return lb.split(' ').map(id => text(document.getElementById(id))).join(' ');
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return text(l);
    }
    const wrap = el.closest('label');
    if (wrap) return text(wrap);
    const fieldset = el.closest('fieldset');
    const legend = fieldset && fieldset.querySelector('legend');
    // Nearest preceding text in the same container usually is the question.
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      const t = text(p);
      if (t && t.length < 300) return (legend ? text(legend) + ' / ' : '') + t;
    }
    return legend ? text(legend) : '';
  };
  const sel = 'input, select, textarea, button, a[href], [role=button], [role=combobox], [role=checkbox], [role=radio], [role=option], [contenteditable=true]';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const isFile = type === 'file';
    if (!isFile && !visible(el)) continue;   // file inputs are often visually hidden
    if (!el.dataset.jobbotId) el.dataset.jobbotId = prefix + (++counter.n);
    const item = {
      id: el.dataset.jobbotId,
      tag: el.tagName.toLowerCase(),
      type: type || el.getAttribute('role') || '',
      label: (labelFor(el) || el.getAttribute('placeholder') || el.getAttribute('name') || text(el)).slice(0, 200),
      required: el.required || el.getAttribute('aria-required') === 'true',
    };
    if (el.tagName === 'A') { item.text = text(el).slice(0, 80); item.href = el.href; }
    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') item.text = text(el).slice(0, 80);
    if (['checkbox', 'radio'].includes(type)) item.checked = el.checked;
    else if ('value' in el && !isFile && type !== 'password') item.value = (el.value || '').slice(0, 200);
    if (type === 'password') item.value = el.value ? '(filled)' : '';
    if (el.tagName === 'SELECT') item.options = [...el.options].slice(0, 60).map(o => o.text.trim());
    out.push(item);
  }
  return out;
}
"""

PAGE_TEXT_JS = r"""
() => {
  const errs = [...document.querySelectorAll('[role=alert], .error, .errors, [class*="error"], [aria-invalid=true]')]
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 20);
  return {
    title: document.title,
    text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 6000),
    errors: errs,
  };
}
"""


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    errors: list[str]
    elements: list[dict]

    def render(self, include_text: bool = True) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if self.errors:
            lines.append("Errors on page: " + " | ".join(self.errors))
        if include_text:
            lines += ["", "--- visible text ---", self.text, ""]
        lines.append("--- interactive elements ---")
        for e in self.elements:
            parts = [e["id"], e["tag"]]
            if e.get("type"):
                parts.append(f"type={e['type']}")
            if e.get("required"):
                parts.append("REQUIRED")
            parts.append(f"label={e.get('label', '')!r}")
            for key in ("text", "value", "checked", "href"):
                if key in e and e[key] not in ("", None):
                    parts.append(f"{key}={e[key]!r}")
            if e.get("options"):
                parts.append(f"options={e['options']!r}")
            lines.append(" ".join(str(p) for p in parts))
        return "\n".join(lines)


class BrowserLike(Protocol):
    """What the agent needs from a browser. Lets tests swap in a fake."""

    @property
    def url(self) -> str: ...
    def goto(self, url: str) -> None: ...
    def snapshot(self) -> Snapshot: ...
    def screenshot(self, path: Path) -> bytes: ...
    def fill(self, element_id: str, value: str) -> None: ...
    def select(self, element_id: str, option: str) -> None: ...
    def set_checked(self, element_id: str, checked: bool) -> None: ...
    def upload(self, element_id: str, path: Path) -> None: ...
    def click(self, element_id: str) -> None: ...
    def press(self, key: str) -> None: ...


class PlaywrightBrowser:
    """A persistent Chromium profile, so cookies and logins survive between runs."""

    def __init__(self, state_dir: Path, headless: bool = True):
        state_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.context: BrowserContext = self._pw.chromium.launch_persistent_context(
            str(state_dir),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            # Optional: use a preinstalled Chromium instead of Playwright's bundled one.
            executable_path=os.environ.get("JOBBOT_CHROMIUM_PATH") or None,
        )
        self.page: Page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15000)
        # Some sites open the application in a new tab; follow it.
        self.context.on("page", self._on_new_page)

    def _on_new_page(self, page: Page) -> None:
        self.page = page
        page.set_default_timeout(15000)

    def close(self) -> None:
        self.context.close()
        self._pw.stop()

    @property
    def url(self) -> str:
        return self.page.url

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        self._settle()

    def _frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def snapshot(self) -> Snapshot:
        elements: list[dict] = []
        for i, frame in enumerate(self._frames()):
            try:
                elements += frame.evaluate(TAG_AND_DESCRIBE_JS, "e" if i == 0 else f"f{i}e")
            except Exception:
                continue  # cross-origin or mid-navigation frame
        info = self.page.evaluate(PAGE_TEXT_JS)
        return Snapshot(url=self.page.url, title=info["title"], text=info["text"], errors=info["errors"], elements=elements)

    def screenshot(self, path: Path) -> bytes:
        return self.page.screenshot(path=str(path), full_page=False)

    def _locate(self, element_id: str):
        for frame in self._frames():
            loc = frame.locator(f'[data-jobbot-id="{element_id}"]')
            try:
                if loc.count():
                    return loc.first
            except Exception:
                continue
        raise LookupError(f"element {element_id} not found; call observe to refresh the element list")

    def fill(self, element_id: str, value: str) -> None:
        loc = self._locate(element_id)
        role = (loc.get_attribute("role") or "").lower()
        if role == "combobox" or loc.get_attribute("aria-autocomplete"):
            # Typeahead widgets (React-Select etc.) need real keystrokes, then a pick.
            loc.click()
            loc.press_sequentially(value, delay=30)
            self.page.wait_for_timeout(800)
            return
        loc.fill(value)

    def select(self, element_id: str, option: str) -> None:
        loc = self._locate(element_id)
        try:
            loc.select_option(label=option)
        except Exception:
            loc.select_option(value=option)

    def set_checked(self, element_id: str, checked: bool) -> None:
        self._locate(element_id).set_checked(checked, force=True)

    def upload(self, element_id: str, path: Path) -> None:
        self._locate(element_id).set_input_files(str(path))

    def click(self, element_id: str) -> None:
        self._locate(element_id).click()
        self._settle()

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        self._settle()
