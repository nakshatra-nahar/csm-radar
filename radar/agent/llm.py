"""The only code that talks to an LLM. The model reads the evidence pack through a synthetic,
read-only tool call and can call nothing (tool_choice none); it only writes JSON."""

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import anthropic

TOOL_ID = "toolu_evidence_pack"
EVIDENCE_TOOL = {
    "name": "get_evidence_pack",
    "description": "Read-only. Returns the case's evidence pack as JSON.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
# USD per million tokens for prompts up to 100k tokens, checked 2026-10-17:
# (input, 5-minute cache write, 1-hour cache write, cache read, output).
# shortcut: packs are a few thousand tokens, so the >100k tier is not priced; add it if
# prompts ever grow that large.
PRICES = {
    "claude-haiku-5-5": ("0.10", "0.125", "0.20", "0.01", "0.50"),
    "claude-sonnet-5-5": ("2", "2.50", "4", "0.10", "10"),
}


@dataclass(frozen=True)
class LLMResult:
    output_text: str | None
    stop_reason: str
    usage: dict
    model: str
    request_id: str | None
    latency_ms: int
    error: str | None


class LLMClient(Protocol):
    def complete(self, system: str, pack_json: str, instruction: str,
                 schema: dict) -> LLMResult: ...


def cost_usd(model: str, usage: dict) -> Decimal:
    """Decimal cost of one call from the dated table; an unknown model raises KeyError."""
    p_in, p_5m, p_1h, p_read, p_out = (Decimal(x) for x in PRICES[model])
    split = usage.get("cache_creation") or {}
    write_1h = split.get("ephemeral_1h_input_tokens") or 0
    write_5m = (usage.get("cache_creation_input_tokens") or 0) - write_1h
    tokens = ((usage.get("input_tokens") or 0) * p_in + write_5m * p_5m + write_1h * p_1h
              + (usage.get("cache_read_input_tokens") or 0) * p_read
              + (usage.get("output_tokens") or 0) * p_out)
    return tokens / Decimal(1_000_000)


class AnthropicClient:
    def __init__(self, api_key: str, model: str, timeout: float = 60) -> None:
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def complete(self, system: str, pack_json: str, instruction: str,
                 schema: dict) -> LLMResult:
        """One attempt. Any API error (bad request, no credit, timeout) comes back as a
        result with `error` set, so the caller logs it and falls back; it never raises."""
        start = time.monotonic()
        try:
            r = self.client.messages.create(
                model=self.model, max_tokens=4096,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=[EVIDENCE_TOOL], tool_choice={"type": "none"},
                output_config={"effort": "medium",
                               "format": {"type": "json_schema", "schema": schema}},
                messages=[
                    {"role": "user", "content": "Load the evidence pack for this case."},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": TOOL_ID,
                                                       "name": EVIDENCE_TOOL["name"],
                                                       "input": {}}]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": TOOL_ID, "content": pack_json},
                        {"type": "text", "text": instruction}]},
                ])
        except anthropic.APIError as e:
            return LLMResult(None, "", {}, self.model, getattr(e, "request_id", None),
                             _ms(start), f"{type(e).__name__}: {e}")
        text = "".join(b.text for b in r.content if b.type == "text") or None
        return LLMResult(text, r.stop_reason, r.usage.model_dump(mode="json"), r.model,
                         r._request_id, _ms(start), None)


def _ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


class FakeClient:
    """Replays canned results in order and records each call (tests)."""

    def __init__(self, responses: list[LLMResult]) -> None:
        self.responses, self.calls = list(responses), []

    def complete(self, system: str, pack_json: str, instruction: str,
                 schema: dict) -> LLMResult:
        self.calls.append({"system": system, "pack_json": pack_json, "instruction": instruction})
        return self.responses.pop(0)
