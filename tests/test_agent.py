"""Drives the real Playwright browser through a local application form, with a
scripted stand-in for Claude, to check the browser wrapper, the tool loop, the
credential vault hand-off and the submit gate together."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from jobbot.apply.agent import ApplicationAgent
from jobbot.apply.browser import PlaywrightBrowser
from jobbot.db import Credential
from jobbot.vault import Vault

SITE = Path(__file__).parent / "site"


def _last_state(messages) -> str:
    """Concatenate the text of the most recent user turn (tool results or initial observe)."""
    content = messages[-1]["content"]
    texts = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, dict):
            inner = block.get("content", block.get("text", ""))
            if isinstance(inner, list):
                texts += [b.get("text", "") for b in inner if isinstance(b, dict)]
            else:
                texts.append(str(inner))
    return "\n".join(texts)


def _find(state: str, pattern: str) -> str:
    for line in state.splitlines():
        if re.search(pattern, line, re.I):
            return line.split()[0]
    raise AssertionError(f"no element matching {pattern!r} in:\n{state}")


class ScriptedClaude:
    """Plays the role of the model: each step looks at the page and picks an action."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        # Snapshot the request as sent; the agent keeps appending to the same list afterwards.
        self.calls.append({**kwargs, "messages_text": str(kwargs["messages"])})
        state = _last_state(kwargs["messages"])
        name, make_input = self.script.pop(0)
        block = SimpleNamespace(type="tool_use", id=f"tu{len(self.calls)}", name=name, input=make_input(state))
        return SimpleNamespace(stop_reason="tool_use", stop_details=None, content=[block])


SCRIPT = [
    ("click", lambda s: {"element_id": _find(s, r"text='Apply for this job'")}),
    ("fill", lambda s: {"element_id": _find(s, r"label='First name'"), "value": "Jane"}),
    ("fill", lambda s: {"element_id": _find(s, r"label='Last name'"), "value": "Doe"}),
    ("fill_credential", lambda s: {"element_id": _find(s, r"label='Email'"), "field": "username"}),
    ("fill_credential", lambda s: {"element_id": _find(s, r"type=password"), "field": "password"}),
    ("select_option", lambda s: {"element_id": _find(s, r"authorized"), "option": "Yes"}),
    ("upload_file", lambda s: {"element_id": _find(s, r"type=file"), "document": "resume"}),
    ("set_checked", lambda s: {"element_id": _find(s, r"type=checkbox"), "checked": True}),
    # Tries to click the submit button directly; the gate must catch it.
    ("click", lambda s: {"element_id": _find(s, r"text='Submit application'")}),
    ("finish", lambda s: {"outcome": "applied", "notes": "done"}),
]


@pytest.fixture
def browser(tmp_path):
    b = PlaywrightBrowser(tmp_path / "profile", headless=True)
    yield b
    b.close()


def make_agent(browser, profile, session, tmp_path, script, auto_submit):
    claude = ScriptedClaude(script)
    agent = ApplicationAgent(
        browser=browser,
        profile=profile,
        vault=Vault(session, Fernet.generate_key().decode()),
        model="test",
        artifacts_dir=tmp_path,
        auto_submit=auto_submit,
        client=claude,
    )
    return agent, claude


def test_stops_before_submit_without_approval(browser, profile, session, tmp_path):
    agent, claude = make_agent(browser, profile, session, tmp_path, SCRIPT, auto_submit=False)
    result = agent.run((SITE / "posting.html").as_uri(), "Acme - Backend Engineer")

    assert result.outcome == "ready_to_submit"
    assert result.screenshot and result.screenshot.exists()
    page = browser.page
    assert page.input_value("#fn") == "Jane"
    assert page.input_value("#em") == "jane@example.com"
    assert page.input_value("#auth") == "Yes"
    assert page.is_checked("#terms")
    assert page.eval_on_selector("#cv", "e => e.files[0].name") == "resume.pdf"
    # The generated password went into the page and the vault, never into the model's context.
    password = page.input_value("#pw")
    assert len(password) == 20 and session.query(Credential).count() == 1
    assert all(password not in call["messages_text"] for call in claude.calls)
    assert "Thank you" not in page.inner_text("body")


def test_submits_when_approved(browser, profile, session, tmp_path):
    agent, _ = make_agent(browser, profile, session, tmp_path, SCRIPT, auto_submit=True)
    result = agent.run((SITE / "posting.html").as_uri(), "Acme - Backend Engineer")

    assert result.outcome == "applied"
    assert "Thank you for applying" in browser.page.inner_text("body")


def test_finish_applied_rejected_before_submit(browser, profile, session, tmp_path):
    script = [
        ("finish", lambda s: {"outcome": "applied", "notes": "lying"}),
        ("request_human", lambda s: {"reason": "stuck"}),
    ]
    agent, claude = make_agent(browser, profile, session, tmp_path, script, auto_submit=True)
    result = agent.run((SITE / "posting.html").as_uri(), "x")
    assert result.outcome == "needs_review" and result.notes == "stuck"
    assert "not been submitted" in claude.calls[1]["messages_text"]
