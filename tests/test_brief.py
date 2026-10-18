import dataclasses
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg.rows import dict_row

from radar import clock, registry, scoring
from radar.agent import brief, evidence, template, validate
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


def test_relinked_ref_is_not_customer_safe(owner_conn, worker_db, brief_world):
    """Ruling AC (2): a ref re-linked to another account is never safe for this one's email."""
    w = brief_world
    other = owner_conn.execute(
        "INSERT INTO accounts (tenant_id, sf_account_id, name) VALUES (%s, %s, 'Elsewhere')"
        " RETURNING id", (w["tid"], uuid.uuid4().hex[:18])).fetchone()["id"]
    owner_conn.execute(
        "INSERT INTO identity_links (tenant_id, source, account_ref, account_id, method,"
        " available_at) VALUES (%s, 'test', 'zd_org:1', %s, 'zendesk_external_id', %s)",
        (w["tid"], other, clock.now() - timedelta(days=1)))
    ticket = next(i for i in pack_of(worker_db, w).items if i.event_id == w["ticket"])
    assert ticket.match_method == "zendesk_external_id" and not ticket.customer_safe


@pytest.mark.parametrize(("text", "phone"), [
    ("555-1234", True), ("555 1234", True), ("call 5555-1234 now", True),
    ("+1 555 123 4567", True), ("2026-10-17", False), ("3.14", False),
    ("1,200.00", False), ("2026-2027", False)])
def test_phone_regex_catches_local_numbers_not_dates(text, phone):
    assert bool(validate.PHONE_RE.search(text)) is phone


AS_OF = datetime(2026, 10, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def topk_packs(trained_world_db):
    """model_topk cases for hearthline's 20 highest-p accounts in a run at AS_OF: (run as_of,
    {case: (account, pack)})."""
    tid, db = tenant_id("hearthline"), trained_world_db["db"]
    run = scoring.score_tenant(db, tid, AS_OF, "manual", True)
    with owner_connect(trained_world_db["owner_dsn"], autocommit=True,
                       row_factory=dict_row) as conn:
        top = [r["account_id"] for r in conn.execute(
            "SELECT account_id FROM account_scores WHERE run_id = %s AND p IS NOT NULL"
            " ORDER BY p DESC, account_id LIMIT 20", (run,))]
        cases = {add_case(conn, tid, a, "model_topk", None, []): a for a in top}
    with db.tenant_tx(tid) as conn:
        return {c: (a, build_pack(conn, tid, c)) for c, a in cases.items()}


def test_topk_pack_cites_the_events_behind_its_contributions(trained_world_db, topk_packs):
    """Ruling AC (1): model-ranked briefs can cite real tickets, usage months and emails,
    each the account's own and visible at the run's as_of."""
    tid = tenant_id("hearthline")
    kinds = set()
    with owner_connect(trained_world_db["owner_dsn"], row_factory=dict_row) as conn:
        for account, pack in topk_packs.values():
            evidence = [i for i in pack.items if i.event_id is not None]
            assert len(evidence) <= 10
            kinds |= {i.kind for i in evidence}
            for it in evidence:
                e = conn.execute("SELECT * FROM events WHERE tenant_id = %s AND id = %s",
                                 (tid, it.event_id)).fetchone()
                assert e["available_at"] <= AS_OF and e["occurred_at"] <= AS_OF
                link = conn.execute(
                    "SELECT account_id FROM identity_links WHERE tenant_id = %s AND"
                    " account_ref = %s AND available_at <= %s ORDER BY available_at DESC,"
                    " id DESC LIMIT 1", (tid, e["account_ref"], AS_OF)).fetchone()
                assert link["account_id"] == account
                if it.kind == "message":
                    assert (e["attrs"].get("subject") or "\0") not in it.display
                    assert it.display.startswith("Last customer email on")
                if it.kind == "usage":
                    assert re.fullmatch(r"Active users: \d+ in \w{3} \d{4} vs \d+ a month in"
                                        r" \w{3}-\w{3}", it.display), it.display
    assert {"ticket", "usage", "message"} <= kinds, kinds


def test_topk_pack_hash_is_deterministic(trained_world_db, topk_packs):
    tid, db = tenant_id("hearthline"), trained_world_db["db"]
    with db.tenant_tx(tid) as conn:
        for case, (_, pack) in topk_packs.items():
            assert build_pack(conn, tid, case).hash() == pack.hash()


PLAYS = ["value_review", "support_escalation", "exec_check_in", "save_call",
         "billing_outreach", "renewal_check_in"]


@pytest.mark.parametrize("play", PLAYS)
def test_template_email_quotes_no_system_evidence(play, pack, topk_packs):
    """Ruling AF (2): the template email is plain words to the customer, with no evidence
    display, number, contact title or evidence id; the CSM summary still cites the evidence."""
    for p in [pack, *(pk for _, pk in topk_packs.values())]:
        p = dataclasses.replace(p, play=play)
        out = template.render(p)
        assert validate.check(json.dumps(out), "end_turn", p)[1] == []
        email = out["email"]
        words = f"{email['subject']}\n{email['body']}"
        assert email["evidence_ids"] == [] and not re.search(r"\d", words), words
        assert "{first_name}" in email["body"] and "{sender_name}" in email["body"]
        titles = [i.display.split("from their ", 1)[1] for i in p.items
                  if "from their " in i.display]
        assert not [i.display for i in p.items if i.display in words]
        assert not [t for t in titles if t.lower() in words.lower()]
        assert out["summary"] and all(pt["evidence_ids"] for pt in out["summary"])
        assert [pt["text"] for pt in out["summary"]] == [
            i.display for i in p.items if i.trust == "system"][:5]


def test_find_new_champion_has_no_template_email(pack):
    assert template.render(dataclasses.replace(pack, play="find_new_champion"))["email"] is None


def test_feature_event_relinked_after_the_run_is_not_customer_safe(trained_world_db,
                                                                   topk_packs):
    """Ruling AF (1): a feature event's safety reads the link usable now, not at the run's
    as_of, so a ref re-linked to another account after the run is never customer_safe."""
    tid, db = tenant_id("hearthline"), trained_world_db["db"]
    case, (account, pack) = next(
        (c, ap) for c, ap in topk_packs.items()
        if any(i.customer_safe and i.trust == "system" for i in ap[1].items))
    it = next(i for i in pack.items if i.customer_safe and i.trust == "system")
    assert clock.now() > AS_OF
    with db.tenant_tx(tid) as conn, conn.transaction(force_rollback=True):
        ref = conn.execute("SELECT account_ref FROM events WHERE id = %s",
                           (it.event_id,)).fetchone()["account_ref"]
        other = conn.execute("SELECT id FROM accounts WHERE id <> %s ORDER BY id LIMIT 1",
                             (account,)).fetchone()["id"]
        conn.execute("INSERT INTO identity_links (source, account_ref, account_id, method,"
                     " available_at) VALUES ('test', %s, %s, 'zendesk_external_id', %s)",
                     (ref, other, clock.now()))
        after = next(i for i in build_pack(conn, tid, case).items if i.event_id == it.event_id)
    assert not after.customer_safe


# --- Ruling AQ: plain labels, display rounding, material changes only ----------------------

def _event(type_, status=None, amount_minor=None, quantity=None, **attrs):
    return {"type": type_, "status": status, "priority": None, "amount_minor": amount_minor,
            "quantity": quantity, "attrs": attrs,
            "occurred_at": datetime(2026, 10, 11, 9, tzinfo=UTC)}


def test_event_descriptions_use_plain_labels():
    def text(e, dated=False):
        return evidence.describe(e, "USD", cap=None, dated=dated)[0]
    assert text(_event("crm.opportunity_history", "Closed Lost", opp_id="006X")) \
        == "Renewal opportunity: Closed Lost"
    assert text(_event("email.message", direction="inbound")) == "Email from customer"
    assert text(_event("email.message", direction="outbound")) == "Email to customer"
    assert text(_event("billing.invoice", "paid", 124500, attempt_count=1)) \
        == "Invoice paid: USD 1,245.00"
    assert text(_event("billing.subscription", "active", quantity=12,
                       cancel_at_period_end=True)) == "Subscription set to cancel at period end"
    # the pack keeps the date (the timeline shows it in its own column)
    assert text(_event("email.message", direction="inbound"), dated=True) \
        == "Email from customer on 2026-10-11"
    for type_ in ("crm.account", "crm.contact", "product.event", "billing.customer"):
        assert "no status" not in text(_event(type_)) and "event (" not in text(_event(type_))


SCORE = {"band": "red", "p": 0.4, "lift": 2.0, "rank_in_book": 1, "rules_score": 2.0,
         "arr_minor": 1_000_00, "context": {"term_end": "2026-12-01"},
         "contributions": {"days_since_customer_email": 0.2612, "tenure_years": 0.0012,
                           "log_arr": 0.31, "seat_utilization": -0.4},
         "features": {"days_since_customer_email": 120.0, "tenure_years": 1.36,
                      "log_arr": 9.21034, "seat_utilization": 0.5}}


def test_template_brief_uses_plain_labels_and_display_rounding():
    assert list(registry.LABELS) == registry.FEATURE_NAMES  # every feature has a plain label
    drift = {"changed_features": [
        {"name": "tenure_years", "before": 1.3601, "after": 1.3605, "delta_contribution": -3e-4},
        {"name": "seat_utilization", "before": 0.5, "after": 0.5, "delta_contribution": 0.2}]}
    shown = [d for _, kind, d in evidence._score_items("Acme", SCORE, drift, "USD")
             if kind in ("change", "contribution")]
    assert not [d for d in shown if "moved from" in d]  # no material change
    # tenure's +0.0012 is below what the Why panel shows, so the brief leaves it out too
    assert shown == ["ARR is USD 10,000 and adds 0.31 to the log-odds",
                     "days since customer email is 120 and adds 0.26 to the log-odds"]
    real = {"changed_features": [{"name": "days_since_customer_email", "before": 30.0,
                                  "after": 120.0, "delta_contribution": 0.2}]}
    [change] = [d for _, kind, d in evidence._score_items("Acme", SCORE, real, "USD")
                if kind == "change"]
    assert change == "days since customer email moved from 30 to 120 since the previous run"
