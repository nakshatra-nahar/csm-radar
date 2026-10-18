import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg.rows import dict_row

from radar import clock, scoring
from radar.agent import brief, template, validate
from radar.agent.evidence import build_pack
from radar.agent.llm import FakeClient, LLMResult, cost_usd
from radar.asof import load_frames
from radar.bootstrap import HISTORY_END
from radar.db import owner_connect
from radar.sim import INJECTION_ACCOUNT_NAME
from radar.tenants import tenant_id
from tests.conftest import add_case, add_event

USAGE = {"input_tokens": 150, "output_tokens": 60, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}


def pack_of(worker_db, w, case=None):
    with worker_db.tenant_tx(w["tid"]) as conn:
        return build_pack(conn, w["tid"], case or w["case"])


def result(output, stop_reason="end_turn", error=None):
    text = output if isinstance(output, str) or output is None else json.dumps(output)
    return LLMResult(text, stop_reason, USAGE, "claude-haiku-5-5", "req_1", 120, error)


@pytest.fixture
def pack(worker_db, brief_world):
    return pack_of(worker_db, brief_world)


def item(pack, kind):
    return next(i for i in pack.items if i.kind == kind)


FIXTURES = Path(__file__).parent / "fixtures" / "llm"


def canned(name, pack):
    """A canned output from tests/fixtures/llm, its evidence_ids written as item kinds
    ("ticket") and mapped here to that kind's id in the pack."""
    out = json.loads((FIXTURES / f"{name}.json").read_text())
    for cites in out["summary"] + [out["email"]]:
        cites["evidence_ids"] = [item(pack, k).id for k in cites["evidence_ids"]]
    return out


@pytest.fixture
def valid_output(pack):
    return canned("valid", pack)


def mutate(name, out):
    """(output text, stop_reason) after one mutation of a valid output."""
    body = out["email"]["body"]
    extra = {"banned_phrase": " We noticed a churn signal.", "url": " See https://x.io/a",
             "bare_url": " Pay at evil.example/pay or bit.ly/x",
             "email_address": " Write to help@vendor.io", "phone": " Call +1 555 123 4567",
             "html": " <b>Thanks</b>", "eid_marker_in_email": " [E2]"}
    if name in extra:
        out["email"]["body"] = body + extra[name]
    elif name == "unknown_id":
        out["summary"][0]["evidence_ids"] = ["E99"]
    elif name == "number_not_in_evidence":
        out["summary"][0]["text"] += " ARR is 987,654."
    elif name == "uppercase_enum_ok":  # evidence ids are the schema's enum: e1 normalises to E1
        for point in out["summary"]:
            point["evidence_ids"] = [i.lower() for i in point["evidence_ids"]]
    text = json.dumps(out)
    if name == "truncated_json":
        text = text[: len(text) // 2]
    return text, {"refusal": "refusal", "max_tokens": "max_tokens"}.get(name, "end_turn")


FAILS = {"unknown_id": "ids_exist", "number_not_in_evidence": "numbers_cited",
         "banned_phrase": "banned_phrase", "url": "email_clean", "bare_url": "email_clean",
         "email_address": "email_clean",
         "phone": "email_clean", "html": "email_clean", "eid_marker_in_email": "email_clean",
         "refusal": "stop_reason", "max_tokens": "stop_reason", "truncated_json": "parses"}


def test_valid_output_passes(valid_output, pack):
    parsed, errors = validate.check(json.dumps(valid_output), "end_turn", pack)
    assert errors == []
    assert parsed["email"]["evidence_ids"] == valid_output["email"]["evidence_ids"]


@pytest.mark.parametrize("mutation", [
    "unknown_id", "number_not_in_evidence", "banned_phrase", "url", "bare_url", "email_address",
    "phone",
    "html", "eid_marker_in_email", "refusal", "max_tokens", "truncated_json",
    "uppercase_enum_ok"])
def test_validator_catches_mutations(mutation, valid_output, pack):
    text, stop_reason = mutate(mutation, valid_output)
    parsed, errors = validate.check(text, stop_reason, pack)
    if mutation == "uppercase_enum_ok":
        assert errors == []
        assert parsed["summary"][0]["evidence_ids"] == [item(pack, "score").id]
    else:
        assert FAILS[mutation] in {e.split(":")[0] for e in errors}, errors
        assert validate.rule_results(errors)[FAILS[mutation]] is False


def test_email_may_cite_only_customer_safe_items(valid_output, pack):
    invoice = item(pack, "invoice")  # matched by domain: true for the account, not provable
    assert not invoice.customer_safe and item(pack, "ticket").customer_safe
    valid_output["email"]["evidence_ids"] = [invoice.id]
    _, errors = validate.check(json.dumps(valid_output), "end_turn", pack)
    assert [e.split(":")[0] for e in errors] == ["email_customer_safe"]


def test_template_passes_the_validator(pack):
    out = template.render(pack)
    assert out == template.render(pack)
    assert validate.check(json.dumps(out), "end_turn", pack)[1] == []


def rows(owner_conn, sql, *args):
    return owner_conn.execute(sql, args).fetchall()


def brief_row(owner_conn, brief_id):
    return owner_conn.execute("SELECT * FROM briefs WHERE id = %s", (brief_id,)).fetchone()


def audit_reason(owner_conn, brief_id):
    return owner_conn.execute(
        "SELECT reason FROM audit_log WHERE entity_type = 'briefs' AND entity_id = %s",
        (str(brief_id),)).fetchone()["reason"]


def test_valid_llm_output_becomes_the_draft(owner_conn, worker_db, brief_world, valid_output):
    w, client = brief_world, FakeClient([result(valid_output)])
    brief_id = brief.generate_brief(worker_db, w["tid"], w["case"], client)
    row = brief_row(owner_conn, brief_id)
    assert (row["status"], row["generator"], row["prompt_version"]) == ("ready", "llm", "brief-v1")
    assert row["validation"]["attempts"][0]["rules"]["numbers_cited"] is True
    draft = rows(owner_conn, "SELECT * FROM outbox WHERE brief_id = %s", brief_id)
    assert [(d["kind"], d["status"], d["body"]) for d in draft] == [
        ("customer_email", "draft", valid_output["email"]["body"])]
    [call] = rows(owner_conn, "SELECT * FROM llm_calls WHERE brief_id = %s", brief_id)
    assert (call["ok"], call["attempt"], call["request_id"]) == (True, 1, "req_1")
    assert call["usage"] == USAGE and call["cost_usd"] == cost_usd("claude-haiku-5-5", USAGE)


def test_retry_then_template_fallback_logged(owner_conn, worker_db, brief_world, valid_output):
    w = brief_world
    bad = mutate("banned_phrase", valid_output)[0]
    client = FakeClient([result(bad), result(bad)])
    brief_id = brief.generate_brief(worker_db, w["tid"], w["case"], client)
    assert brief_row(owner_conn, brief_id)["generator"] == "template"
    calls = rows(owner_conn, "SELECT * FROM llm_calls WHERE brief_id = %s ORDER BY attempt",
                 brief_id)
    assert [(c["attempt"], c["ok"]) for c in calls] == [(1, False), (2, False)]
    assert "banned_phrase" in client.calls[1]["instruction"]  # the retry carries the errors
    assert audit_reason(owner_conn, brief_id).startswith("validation_failed")
    [draft] = rows(owner_conn, "SELECT body FROM outbox WHERE brief_id = %s", brief_id)
    assert draft["body"] == template.render(pack_of(worker_db, w))["email"]["body"]


def test_api_error_is_a_failed_attempt_then_template(owner_conn, worker_db, brief_world):
    w = brief_world
    err = "Error code: 400 - Your credit balance is too low to access the Anthropic API."
    client = FakeClient([LLMResult(None, "", {}, "claude-haiku-5-5", None, 30, err)])
    brief_id = brief.generate_brief(worker_db, w["tid"], w["case"], client)
    [call] = rows(owner_conn, "SELECT * FROM llm_calls WHERE brief_id = %s", brief_id)
    assert (call["ok"], call["error"], call["stop_reason"]) == (False, err, None)
    assert brief_row(owner_conn, brief_id)["generator"] == "template"
    assert audit_reason(owner_conn, brief_id) == f"api_error: {err}"


def test_no_api_key_uses_template(owner_conn, worker_db, brief_world):
    w = brief_world
    brief_id = brief.generate_brief(worker_db, w["tid"], w["case"], None)
    assert brief_row(owner_conn, brief_id)["generator"] == "template"
    assert rows(owner_conn, "SELECT 1 FROM llm_calls WHERE brief_id = %s", brief_id) == []
    assert audit_reason(owner_conn, brief_id) == "no_api_key"


def test_llm_disabled_uses_template(owner_conn, worker_db, brief_world, valid_output):
    w, client = brief_world, FakeClient([result(valid_output)])
    owner_conn.execute("UPDATE tenants SET config = jsonb_set(config, '{llm,enabled}', 'false')"
                       " WHERE id = %s", (w["tid"],))
    brief_id = brief.generate_brief(worker_db, w["tid"], w["case"], client)
    assert (client.calls, audit_reason(owner_conn, brief_id)) == ([], "llm_disabled")


def test_token_budget_exhausted_uses_template(owner_conn, worker_db, brief_world, valid_output):
    w = brief_world
    owner_conn.execute("UPDATE tenants SET config = jsonb_set(config,"
                       " '{llm,daily_token_budget}', '200') WHERE id = %s", (w["tid"],))
    brief.generate_brief(worker_db, w["tid"], w["case"], FakeClient([result(valid_output)]))
    other = add_case(owner_conn, w["tid"], w["account"], "dunning_exhausted",
                     "billing_outreach", [w["invoice"]])
    client = FakeClient([result(valid_output)])
    brief_id = brief.generate_brief(worker_db, w["tid"], other, client)  # 210 tokens used today
    assert client.calls == []
    assert brief_row(owner_conn, brief_id)["generator"] == "template"
    assert audit_reason(owner_conn, brief_id) == "token_budget_spent"


def test_brief_generated_once_under_retry(owner_conn, worker_db, brief_world, valid_output):
    w = brief_world
    client = FakeClient([result(valid_output), result(valid_output)])
    first = brief.generate_brief(worker_db, w["tid"], w["case"], client)
    second = brief.generate_brief(worker_db, w["tid"], w["case"], client)
    assert first == second and len(client.calls) == 1
    assert len(rows(owner_conn, "SELECT 1 FROM llm_calls WHERE brief_id = %s", first)) == 1
    assert len(rows(owner_conn, "SELECT 1 FROM outbox WHERE brief_id = %s", first)) == 1


def test_evidence_truncates_and_tags_untrusted(owner_conn, worker_db, brief_world):
    w = brief_world
    text = ("Ünïcödé 日本語 <script>alert(1)</script> mail evil@attacker.co or call"
            " +44 20 7946 0958, see https://evil.example/x?y=1 or bit.ly/promo ") * 110  # ~10k
    ticket = add_event(owner_conn, w["tid"], w["account"], "zd_org:9", "zendesk_external_id",
                       "support.ticket", {"ticket_id": 77, "subject": "Help",
                                          "description_excerpt": text}, status="open")
    case = add_case(owner_conn, w["tid"], w["account"], "cancel_announced", "save_call",
                    [ticket])
    [it] = [i for i in pack_of(worker_db, w, case).items if i.event_id == ticket]
    assert len(it.display) <= 280 and it.trust == "untrusted_customer_text"
    assert "[redacted]" in it.display
    assert not any(s in it.display for s in ("evil@", "https://", "7946", "bit.ly"))


def test_pack_is_deterministic_and_marks_only_exact_keys_safe(worker_db, brief_world, pack):
    again = pack_of(worker_db, brief_world)
    assert again.hash() == pack.hash() and len(pack.hash()) == 64
    by_kind = {i.kind: i for i in pack.items}
    assert (by_kind["ticket"].match_method, by_kind["ticket"].customer_safe) == (
        "zendesk_external_id", True)
    assert (by_kind["invoice"].match_method, by_kind["invoice"].customer_safe) == ("domain", False)
    assert by_kind["score"].trust == "system" and not by_kind["score"].customer_safe
    assert [i.id for i in pack.items] == [f"E{n}" for n in range(1, len(pack.items) + 1)]
    assert pack.play == "support_escalation"
    assert "0.42" in by_kind["score"].display and "3.1" in by_kind["score"].display


def test_cost_usd_uses_dated_table():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
             "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 2_000_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000,
                                "ephemeral_1h_input_tokens": 1_000_000}}
    assert cost_usd("claude-haiku-5-5", usage) == Decimal("0.935")  # .10+.50+.01+.125+.20
    assert cost_usd("claude-sonnet-5-5", usage) == Decimal("18.6")  # 2+10+.10+2.50+4
    # no 5m/1h split reported: every cache write is priced at the 5-minute rate
    assert cost_usd("claude-haiku-5-5", {"cache_creation_input_tokens": 1_000_000}) == Decimal(
        "0.125")
    with pytest.raises(KeyError):
        cost_usd("claude-unknown", usage)


INJECTED = ("discount", "40%", "maple-street-realty.co", "bit.ly", "onerror")


def test_injection_fixture_does_not_change_score_or_draft(trained_world_db):
    """Design §3.4: the planted ticket moves no score, and no injected entity reaches the
    draft; here the model obeys the injection and the validator sends it to the template."""
    tid, db = tenant_id("hearthline"), trained_world_db["db"]
    with owner_connect(trained_world_db["owner_dsn"], row_factory=dict_row) as conn:
        account = conn.execute("SELECT id FROM accounts WHERE tenant_id = %s AND name = %s",
                               (tid, INJECTION_ACCOUNT_NAME)).fetchone()["id"]
        tickets = [r["id"] for r in conn.execute(
            "SELECT id FROM events WHERE tenant_id = %s AND type = 'support.ticket'"
            " AND attrs->>'description_excerpt' LIKE '%%IGNORE ALL PREVIOUS%%'", (tid,))]
        tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tid,)).fetchone()
        artifact = conn.execute("SELECT artifact FROM models WHERE tenant_id = %s"
                                " AND status = 'active'", (tid,)).fetchone()["artifact"]

        def score():
            df = scoring.score_frame(load_frames(conn, tid), tenant, HISTORY_END, artifact)
            return df.loc[account, ["p", "rules_score", "band"]].tolist()
        with_body = score()
        conn.execute("UPDATE events SET attrs = attrs - 'subject' - 'description_excerpt'"
                     " WHERE id = ANY(%s)", (tickets,))
        assert tickets and score() == with_body
        conn.rollback()
        conn.autocommit = True
        case = add_case(conn, tid, account, "urgent_ticket_stalled", "support_escalation",
                        tickets)
    with db.tenant_tx(tid) as conn:
        pack = build_pack(conn, tid, case)
    ticket = next(i for i in pack.items if i.kind == "ticket")
    assert ticket.trust == "untrusted_customer_text"
    assert not any(s in ticket.display for s in ("maple-street-realty.co", "bit.ly"))
    obeyed = canned("injection_obeyed", pack)
    brief_id = brief.generate_brief(db, tid, case, FakeClient([result(obeyed), result(obeyed)]))
    with owner_connect(trained_world_db["owner_dsn"], row_factory=dict_row) as conn:
        out = brief_row(conn, brief_id)["output"]
        drafts = rows(conn, "SELECT subject, body FROM outbox WHERE brief_id = %s", brief_id)
        reason = audit_reason(conn, brief_id)
    shown = json.dumps([out, drafts]).lower()
    assert not [s for s in INJECTED if s in shown]
    assert reason.startswith("validation_failed") and "banned_phrase" in reason


class Boom:
    def complete(self, *args):
        raise RuntimeError("worker crashed mid-call")


def test_crashed_brief_is_failed_then_regenerated(owner_conn, worker_db, brief_world,
                                                  valid_output):
    w = brief_world
    with pytest.raises(RuntimeError):
        brief.generate_brief(worker_db, w["tid"], w["case"], Boom())
    [failed] = rows(owner_conn, "SELECT * FROM briefs WHERE case_id = %s", w["case"])
    assert failed["status"] == "failed" and "worker crashed" in failed["validation"]["error"]
    brief_id = brief.generate_brief(worker_db, w["tid"], w["case"],
                                    FakeClient([result(valid_output)]))
    row = brief_row(owner_conn, brief_id)
    assert (brief_id, row["status"], row["generator"]) == (failed["id"], "ready", "llm")
    assert len(rows(owner_conn, "SELECT 1 FROM outbox WHERE brief_id = %s", brief_id)) == 1


def test_stale_pending_brief_is_taken_over(owner_conn, worker_db, brief_world):
    w = brief_world
    brief_id = brief.generate_brief(worker_db, w["tid"], w["case"], None)
    owner_conn.execute("UPDATE briefs SET status = 'pending', updated_at = %s WHERE id = %s",
                       (clock.now() - timedelta(seconds=301), brief_id))
    assert brief.generate_brief(worker_db, w["tid"], w["case"], None) == brief_id
    assert brief_row(owner_conn, brief_id)["status"] == "ready"
