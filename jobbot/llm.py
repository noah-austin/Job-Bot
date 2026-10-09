"""Thin wrapper around the Claude API for the bot's single-shot calls."""

import json
from typing import Any

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, model: str, client: anthropic.Anthropic | None = None):
        self.model = model
        self.client = client or anthropic.Anthropic()

    def json(self, system: str, prompt: str, schema: dict[str, Any], effort: str = "low", max_tokens: int = 16000) -> dict:
        """One request whose answer is guaranteed to match `schema`."""
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise LLMError(f"model declined: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise LLMError("response truncated at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text in response")
        return json.loads(text)
