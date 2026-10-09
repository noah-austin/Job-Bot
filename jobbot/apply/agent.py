"""The application agent: Claude drives a real browser through any ATS.

Claude sees the page as text plus a numbered list of controls (and a screenshot
when it asks to observe), and acts through the tools below. Two guarantees are
enforced in code, not just in the prompt:

* Passwords never reach the model. `fill_credential` looks them up (or creates
  them) in the vault and types them in directly.
* Nothing is submitted unless JOBBOT_AUTO_SUBMIT is on. With it off, `submit`
  stops the run, screenshots the finished form, and files it for your approval.
"""

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anthropic

from jobbot.apply.browser import BrowserLike
from jobbot.gmail.otp import extract_code, extract_verification_link
from jobbot.llm import FALLBACK_BETA
from jobbot.profile import Profile
from jobbot.vault import Vault

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are applying to a job on behalf of the candidate, operating a real web browser
through the tools provided. The goal is a complete, accurate, submitted application.

How to work:
- The page is given as visible text plus a list of interactive elements with ids like e12 or f1e4.
  Act on elements by id. After every action you get the updated element list. Call observe when you
  need a screenshot (custom widgets, layout questions, CAPTCHAs, anything confusing).
- Find the application form. If the posting page has an "Apply" button or link, click it.
- If the site requires an account, sign in or create one. Use fill_credential for email/username and
  password fields; never type a password yourself. When creating an account, use the candidate's
  email. If the site sends a verification code or link, call wait_for_email.
- Upload the resume to resume/CV file inputs with upload_file.
- Fill every required field, and optional fields when the candidate's facts cover them.
- Typeahead/combobox fields: fill the text, then click the matching option that appears.
- Multi-page forms: complete each page and click Next/Continue until you reach the final review.
- When everything is filled and the only remaining step is the final submit button, call submit with
  that button's id. Do not click the final submit button with click.
- After a successful submit, confirm the confirmation message and call finish with outcome "applied".

Truthfulness rules (strict):
- Use only facts from <candidate>. Never invent employers, dates, degrees, skills, numbers, salary
  figures, or yes/no answers about eligibility, clearance, licenses, criminal history, or references.
- For a free-text question (e.g. "Why do you want to work here?"), you may write a short, specific
  answer grounded only in the candidate's real resume and the job posting.
- If a required question can't be answered from the candidate's facts, call request_human with the
  exact question. Do not guess.
- Voluntary demographic questions: use the candidate's approved answers if given, otherwise choose the
  "decline to answer" option.

Stop and call request_human for: CAPTCHAs, identity verification, video/assessment steps, payments,
anything asking to agree to unusual legal terms, or being stuck after a few attempts.
Call finish with outcome "job_closed" if the posting is no longer accepting applications, or
"already_applied" if the site says the candidate already applied."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "observe",
        "description": "Get a screenshot plus the full page text and element list. Use when the page is confusing or after navigation you didn't expect.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "fill",
        "description": "Type a value into a text input, textarea, or combobox.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "string"}, "value": {"type": "string"}},
            "required": ["element_id", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a native <select> by its visible text.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "string"}, "option": {"type": "string"}},
            "required": ["element_id", "option"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_checked",
        "description": "Check or uncheck a checkbox or radio button.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "string"}, "checked": {"type": "boolean"}},
            "required": ["element_id", "checked"],
            "additionalProperties": False,
        },
    },
    {
        "name": "upload_file",
        "description": "Attach one of the candidate's documents to a file input.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "string"}, "document": {"type": "string", "enum": ["resume"]}},
            "required": ["element_id", "document"],
            "additionalProperties": False,
        },
    },
    {
        "name": "click",
        "description": "Click a button, link, radio, or dropdown option. Not for the final submit button; use submit for that.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "string"}},
            "required": ["element_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key such as Enter, Tab, ArrowDown, or Escape.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fill_credential",
        "description": "Fill the candidate's login for this site into an email/username or password field. Creates and stores a new password when signing up. Use for every login and sign-up field instead of fill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {"type": "string"},
                "field": {"type": "string", "enum": ["username", "password"]},
            },
            "required": ["element_id", "field"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_for_email",
        "description": "Wait for a verification email the site just sent. Returns the code, or opens the verification link in the browser.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expect": {"type": "string", "enum": ["code", "link"]},
                "sender_hint": {"type": "string", "description": "company or ATS name to search for, e.g. 'workday' or 'Acme'"},
            },
            "required": ["expect", "sender_hint"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit",
        "description": "Click the final submit button of a fully completed application.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": {"type": "string"}},
            "required": ["element_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_human",
        "description": "Stop and hand this application to the candidate, with the reason and any exact question that needs an answer.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish",
        "description": "End the run with its outcome.",
        "input_schema": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["applied", "already_applied", "job_closed", "failed"]},
                "notes": {"type": "string"},
            },
            "required": ["outcome", "notes"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class ApplyResult:
    outcome: str            # applied | ready_to_submit | needs_review | already_applied | job_closed | failed
    notes: str
    steps: int
    screenshot: Path | None = None
    actions: list[dict] = field(default_factory=list)


class _Stop(Exception):
    def __init__(self, result: ApplyResult):
        self.result = result


class ApplicationAgent:
    def __init__(
        self,
        browser: BrowserLike,
        profile: Profile,
        vault: Vault,
        model: str,
        artifacts_dir: Path,
        auto_submit: bool = False,
        max_steps: int = 60,
        wait_for_email: Callable[[str, datetime], str | None] | None = None,
        client: anthropic.Anthropic | None = None,
    ):
        self.browser = browser
        self.profile = profile
        self.vault = vault
        self.model = model
        self.artifacts_dir = artifacts_dir
        self.auto_submit = auto_submit
        self.max_steps = max_steps
        self._wait_for_email = wait_for_email  # (query, since) -> email body or None
        self.client = client or anthropic.Anthropic()
        self.actions: list[dict] = []
        self.submitted = False
        self._shot_n = 0
        self._run_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # ---------- tool implementations ----------

    def _screenshot(self) -> Path:
        self._shot_n += 1
        path = self.artifacts_dir / f"{self._run_tag}-{self._shot_n:02d}.png"
        self.browser.screenshot(path)
        return path

    def _state(self, include_text: bool = False) -> str:
        return self.browser.snapshot().render(include_text=include_text)

    def _observe(self) -> list[dict]:
        path = self._screenshot()
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(path.read_bytes()).decode()}},
            {"type": "text", "text": self._state(include_text=True)},
        ]

    def _looks_like_submit(self, element_id: str) -> bool:
        """Backstop so a final submit can't slip through `click` and bypass the approval gate."""
        for e in self.browser.snapshot().elements:
            if e["id"] == element_id:
                words = f"{e.get('text', '')} {e.get('label', '')}".lower()
                return "submit" in words or "send application" in words
        return False

    def _run_tool(self, name: str, args: dict) -> str | list[dict]:
        b = self.browser
        if name == "observe":
            return self._observe()
        if name == "fill":
            b.fill(args["element_id"], args["value"])
        elif name == "select_option":
            b.select(args["element_id"], args["option"])
        elif name == "set_checked":
            b.set_checked(args["element_id"], args["checked"])
        elif name == "upload_file":
            path = Path(self.profile.resume_path)
            if not path.exists():
                raise _Stop(ApplyResult("needs_review", f"resume file {path} is missing", len(self.actions)))
            b.upload(args["element_id"], path)
        elif name == "click":
            if self._looks_like_submit(args["element_id"]):
                return self._run_tool("submit", args)
            b.click(args["element_id"])
        elif name == "press_key":
            b.press(args["key"])
        elif name == "fill_credential":
            username = self.profile.personal.email
            if args["field"] == "username":
                b.fill(args["element_id"], username)
            else:
                _, password, created = self.vault.get_or_create(b.url, username)
                b.fill(args["element_id"], password)
                if created:
                    self.actions.append({"tool": "account_created", "site": b.url})
                    return "Filled a newly generated password (stored in the vault). If there is a confirm-password field, call fill_credential on it too.\n\n" + self._state()
                return "Filled the stored password.\n\n" + self._state()
        elif name == "wait_for_email":
            return self._handle_email(args["expect"], args["sender_hint"])
        elif name == "submit":
            shot = self._screenshot()
            if not self.auto_submit:
                raise _Stop(ApplyResult("ready_to_submit", "Form complete; waiting for approval to submit.", len(self.actions), shot))
            b.click(args["element_id"])
            self.submitted = True
            return "Clicked submit. Check the page for a confirmation or for validation errors.\n\n" + self._state(include_text=True)
        elif name == "request_human":
            raise _Stop(ApplyResult("needs_review", args["reason"], len(self.actions), self._screenshot()))
        elif name == "finish":
            outcome = args["outcome"]
            if outcome == "applied" and not self.submitted:
                return "Error: the application has not been submitted yet. Use submit on the final button first."
            raise _Stop(ApplyResult(outcome, args["notes"], len(self.actions), self._screenshot()))
        else:
            raise ValueError(f"unknown tool {name}")
        return self._state()

    def _handle_email(self, expect: str, hint: str) -> str:
        if self._wait_for_email is None:
            raise _Stop(ApplyResult("needs_review", "Site needs email verification but Gmail isn't connected.", len(self.actions), self._screenshot()))
        since = datetime.now(timezone.utc)
        body = self._wait_for_email(hint, since)
        if body is None:
            return f"No email from '{hint}' arrived within the wait window. Try a resend link if the page has one, or request_human."
        if expect == "code":
            code = extract_code(body)
            return f"Verification code: {code}" if code else "Email arrived but no code was found in it:\n" + body[:2000]
        link = extract_verification_link(body)
        if not link:
            return "Email arrived but no verification link was found in it:\n" + body[:2000]
        self.browser.goto(link)
        return "Opened the verification link.\n\n" + self._state(include_text=True)

    # ---------- the loop ----------

    def run(self, apply_url: str, job_summary: str) -> ApplyResult:
        self.browser.goto(apply_url)
        messages: list[dict] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"<candidate>\n{self.profile.facts_for_llm()}\n</candidate>"},
                    {"type": "text", "text": f"<job>\n{job_summary}\n</job>"},
                    *self._observe(),
                    {"type": "text", "text": "Apply to this job."},
                ],
            }
        ]
        for step in range(self.max_steps):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                return ApplyResult("failed", f"model declined: {response.stop_details}", step)
            # Append the full content (thinking blocks included) so the history stays valid.
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [blk for blk in response.content if blk.type == "tool_use"]
            if response.stop_reason == "max_tokens" or not tool_uses:
                messages.append({"role": "user", "content": "Continue by calling one of the tools. Call finish or request_human if you are done or stuck."})
                continue

            results = []
            for tu in tool_uses:
                self.actions.append({"tool": tu.name, "input": tu.input})
                log.info("step %d: %s %s", step, tu.name, json.dumps(tu.input)[:200])
                try:
                    output = self._run_tool(tu.name, tu.input)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output})
                except _Stop as stop:
                    stop.result.actions = self.actions
                    return stop.result
                except Exception as exc:  # element gone, timeout, etc. Let the model recover.
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True, "content": f"{type(exc).__name__}: {exc}"})
            messages.append({"role": "user", "content": results})

        return ApplyResult("needs_review", f"Gave up after {self.max_steps} steps.", self.max_steps, self._screenshot(), self.actions)

