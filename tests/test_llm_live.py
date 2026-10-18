"""Opt-in smoke test against the real API: uv run pytest -m live_llm tests/test_llm_live.py"""

import os
from pathlib import Path

import pytest

from radar.agent import brief, validate
from radar.agent.evidence import TEXT_CAP, EvidenceItem, EvidencePack, redact
from radar.agent.llm import AnthropicClient
from radar.sim.names import INJECTION_TEXT


def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = Path(__file__).parents[1] / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    values = dict(line.split("=", 1) for line in lines if "=" in line and line[0] != "#")
    return values.get("ANTHROPIC_API_KEY", "").strip() or None


@pytest.mark.live_llm
def test_live_brief_on_the_injection_pack():
    key = _api_key()
    if key is None:
        pytest.skip("no ANTHROPIC_API_KEY in the environment or .env")
    display = ("Ticket 1 (open, urgent) on 2026-09-08: " + redact(INJECTION_TEXT))[:TEXT_CAP]
    pack = EvidencePack(
        account={"name": "Maple Street Realty", "segment": "smb"},
        score={"band": "yellow", "p": 0.21, "lift": 1.8, "rank_in_book": 4}, changes=[],
        play="support_escalation", ledger={"open_cases": [], "last_outcomes": [],
                                           "suppressions": []},
        items=[EvidenceItem("E1", "radar", "score", "Risk band yellow; p 0.21; lift 1.8x;"
                            " rank 4 in book", "system", None, False, None),
               EvidenceItem("E2", "zendesk", "ticket", display, "untrusted_customer_text",
                            "zendesk_external_id", True, 1)])
    result = AnthropicClient(key, "claude-haiku-5-5").complete(
        brief.SYSTEM_PROMPT, pack.to_json(), brief.instruction([]), validate.SCHEMA)
    if result.error and "credit balance is too low" in result.error:
        pytest.skip("the Anthropic account has no credit")
    assert result.error is None and result.request_id
    assert result.stop_reason == "end_turn"
    validate.Brief.model_validate_json(result.output_text)
