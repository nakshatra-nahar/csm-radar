"""The worker's job handlers (design §8). Each takes (db, job) and opens its own
transactions; the worker completes or fails the job afterwards. Time comes from radar.clock."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from radar import clock, policy
from radar.agent import brief, jobs
from radar.agent.jobs import Job
from radar.agent.llm import AnthropicClient
from radar.asof import load_frames
from radar.config import load_settings
from radar.db import Database
from radar.features import rule_facts
from radar.ingest import feed
from radar.scoring import score_tenant

CADENCE = {"stripe": timedelta(hours=1), "zendesk": timedelta(hours=1),
           "gmail": timedelta(hours=1), "salesforce": timedelta(hours=26)}
PRODUCT_DUE_DAY, PRODUCT_GRACE_DAYS = 3, 3  # the monthly batch is due on the 3rd, + 3 days


def _lock(conn, tenant_id) -> None:
    """Serialise case opening and freezing per tenant until the transaction ends: two ticks
    at once (GitHub Actions and Vercel cron) cannot race the breaker count or freeze twice."""
    key = int.from_bytes(UUID(str(tenant_id)).bytes[:8], "big", signed=True)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def _tenant_ids(db: Database) -> list[UUID]:
    with db.system_tx() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM tenants ORDER BY slug")]


def _facts(db: Database, tenant_id, now: datetime, account_ids=None) -> tuple[dict, list]:
    """(tenant row, must-act hits at now). Facts are computed outside the transaction, which
    the role's idle_in_transaction_session_timeout would otherwise cut short."""
    with db.tenant_tx(tenant_id, actor="agent") as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
        frames = load_frames(conn, tenant_id)
    facts = rule_facts(frames, tenant, now, account_ids)
    return tenant, policy.rule_hits(facts, tenant["config"])


def _enqueue_briefs(conn, tenant_id, case_ids) -> None:
    for case in case_ids:
        jobs.enqueue(conn, "generate_brief", tenant_id, {"case_id": str(case)},
                     dedupe_key=f"brief:{case}", lane=1)


def feed_tick(db: Database, job: Job) -> None:
    """Deliver due demo feed records for every tenant; one coalesced rule check per account
    the delivery touched with a trigger-worthy event."""
    data_dir = load_settings().data_dir
    for tid in _tenant_ids(db):
        stats = feed.deliver_due(db, tid, clock.now(), data_dir)
        with db.tenant_tx(tid) as conn:
            for account in stats.triggers:
                jobs.enqueue(conn, "evaluate_rules", tid, {"account_id": str(account)},
                             dedupe_key=f"rules:{account}", lane=0)


def evaluate_rules(db: Database, job: Job) -> None:
    """The must-act rules for one account now; new cases get a brief."""
    # shortcut: loads the tenant's whole history for one account; scope load_frames to the
    # account when event volume makes rule checks slow
    now = clock.now()
    tenant, hits = _facts(db, job.tenant_id, now, [UUID(job.payload["account_id"])])
    with db.tenant_tx(job.tenant_id, actor="agent") as conn:
        _lock(conn, job.tenant_id)
        new = policy.open_cases(conn, job.tenant_id, hits, f"job:{job.id}", now,
                                tenant["config"])
        _enqueue_briefs(conn, job.tenant_id, new)


def generate_brief(db: Database, job: Job) -> None:
    settings = load_settings()
    client = (AnthropicClient(settings.anthropic_api_key, settings.llm_model)
              if settings.anthropic_api_key else None)
    brief.generate_brief(db, job.tenant_id, UUID(job.payload["case_id"]), client)


def sla_check(db: Database, job: Job) -> None:
    """Escalate each open case past its SLA once: escalated_at, an audit row, and the
    manager's next digest (which lists cases escalated since the last one)."""
    now = clock.now()
    for tid in _tenant_ids(db):
        with db.tenant_tx(tid, actor="agent") as conn:
            for c in conn.execute(
                    "UPDATE cases SET escalated_at = %s WHERE status = 'open'"
                    " AND sla_due_at < %s AND escalated_at IS NULL"
                    " RETURNING id, kind, sla_due_at", (now, now)).fetchall():
                conn.execute(
                    "INSERT INTO audit_log (at, actor, action, entity_type, entity_id, after)"
                    " VALUES (%s, 'agent', 'sla_escalated', 'cases', %s, %s)",
                    (now, str(c["id"]), Jsonb({"kind": c["kind"],
                                               "sla_due_at": c["sla_due_at"].isoformat()})))


def _product_window(now: datetime) -> tuple[datetime, datetime]:
    """(since, by): the latest monthly batch past its grace must have arrived on or after
    the 1st of its month. Before the 6th that is last month's batch."""
    grace = timedelta(days=PRODUCT_DUE_DAY - 1 + PRODUCT_GRACE_DAYS)
    since = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now < since + grace:
        since = (since - timedelta(days=1)).replace(day=1)
    return since, since + grace


def _expected_by(source: str, last: datetime | None, now: datetime) -> datetime | None:
    if source == "product":
        return _product_window(now)[1]
    return last and last + CADENCE[source]


def is_stale(source: str, last: datetime | None, now: datetime) -> bool:
    """Source freshness v1 (design §5.2), on the latest available_at delivered."""
    if last is None:
        return True
    if source == "product":
        return last < _product_window(now)[0]
    return now > last + CADENCE[source]


def check_freshness(conn, now: datetime) -> None:
    """One open source_stale incident per stale source (the My Week banner reads it); it
    closes when the source catches up."""
    for s in conn.execute("SELECT source, last_deliver_at FROM source_status"
                          " ORDER BY source").fetchall():
        source, last = s["source"], s["last_deliver_at"]
        if not is_stale(source, last, now):
            conn.execute("UPDATE incidents SET closed_at = %s WHERE kind = 'source_stale'"
                         " AND source = %s AND closed_at IS NULL", (now, source))
            continue
        expected = _expected_by(source, last, now)
        conn.execute(
            "INSERT INTO incidents (kind, source, details, opened_at)"
            " VALUES ('source_stale', %s, %s, %s) ON CONFLICT (tenant_id, kind, source)"
            " WHERE closed_at IS NULL DO NOTHING",
            (source, Jsonb({"last_available_at": last and last.isoformat(),
                            "expected_by": expected and expected.isoformat()}), now))


def _digest(conn, user_id, kind: str, subject: str, lines: list[str], now: datetime) -> None:
    # internal mail needs no approval: sent at once through the dry-run transport
    conn.execute("INSERT INTO outbox (kind, to_user_id, subject, body, status, sent_at,"
                 " created_at) VALUES (%s, %s, %s, %s, 'sent', %s, %s)",
                 (kind, user_id, subject, "\n".join(lines) + "\n", now, now))


def _write_digests(conn, week_start: date, now: datetime) -> None:
    """A plain-text digest to each CSM whose list froze now, and to each manager: the team's
    lists, then the cases escalated since that manager's previous digest."""
    lists = conn.execute(
        "SELECT w.id, w.user_id, u.name, u.manager_id FROM worklists w JOIN users u"
        " ON u.id = w.user_id WHERE w.week_start = %s AND w.frozen_at = %s ORDER BY u.name",
        (week_start, now)).fetchall()
    team: dict = {}
    for w in lists:
        items = [f"  {i['position']}. {i['name']}: {i['reason']}" for i in conn.execute(
            "SELECT i.position, i.reason, a.name FROM worklist_items i JOIN accounts a"
            " ON a.id = i.account_id WHERE i.worklist_id = %s ORDER BY i.position",
            (w["id"],))]
        _digest(conn, w["user_id"], "csm_digest", f"Your list for the week of {week_start}",
                [f"Week of {week_start}: {len(items)} accounts.", "", *items], now)
        team.setdefault(w["manager_id"], []).extend(
            ["", f"{w['name']} ({len(items)} accounts)", *items])
    for m in conn.execute("SELECT id FROM users WHERE role = 'manager' AND active"
                          " ORDER BY email").fetchall():
        late = [f"  - {c['name']}: {c['kind']}, due {c['sla_due_at']:%Y-%m-%d %H:%M} UTC"
                for c in conn.execute(
                    "SELECT a.name, c.kind, c.sla_due_at FROM cases c JOIN accounts a"
                    " ON a.id = c.account_id JOIN users u ON u.id = c.owner_user_id"
                    " WHERE u.manager_id = %(m)s AND c.escalated_at <= %(now)s"
                    " AND c.escalated_at > COALESCE((SELECT max(created_at) FROM outbox"
                    "  WHERE kind = 'manager_digest' AND to_user_id = %(m)s), '-infinity')"
                    " ORDER BY c.sla_due_at", {"m": m["id"], "now": now})]
        _digest(conn, m["id"], "manager_digest", f"Your team for the week of {week_start}",
                [f"Week of {week_start}.", *team.get(m["id"], []), "",
                 "Past due (escalated since your last digest):", *(late or ["  none"])], now)


def daily_sweep(db: Database, job: Job) -> None:
    """Score every account, recheck every rule, open cases, and on the tenant's Monday freeze
    the lists and send the digests; briefs for new cases and frozen model slots. Payload
    {'bootstrap': true} opens the first backlog past the breaker, audited (Ruling AB)."""
    tid, now = job.tenant_id, clock.now()
    bootstrap = bool(job.payload.get("bootstrap"))
    run_id = score_tenant(db, tid, now, trigger="sweep", actionable=True)
    tenant, hits = _facts(db, tid, now)
    cfg = tenant["config"]
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    with db.tenant_tx(tid, actor="agent") as conn:
        _lock(conn, tid)
        briefs = policy.open_cases(conn, tid, hits, f"run:{run_id}", now, cfg,
                                   breaker=not bootstrap)
        if bootstrap:
            conn.execute(
                "INSERT INTO audit_log (at, actor, action, entity_type, entity_id, after)"
                " VALUES (%s, 'agent', 'bootstrap_backlog', 'score_runs', %s, %s)",
                (now, str(run_id), Jsonb({"count": len(briefs)})))
        week = local.date()
        if local.weekday() == 0 and policy.freeze_worklists(conn, tid, run_id, week, now, cfg):
            _write_digests(conn, week, now)
            briefs += [r["case_id"] for r in conn.execute(
                "SELECT i.case_id FROM worklist_items i JOIN worklists w"
                " ON w.id = i.worklist_id WHERE w.week_start = %s AND w.frozen_at = %s"
                " AND i.kind = 'model'", (week, now))]
        _enqueue_briefs(conn, tid, briefs)
        check_freshness(conn, now)


HANDLERS: dict[str, Callable[[Database, Job], None]] = {
    "feed_tick": feed_tick, "evaluate_rules": evaluate_rules, "daily_sweep": daily_sweep,
    "generate_brief": generate_brief, "sla_check": sla_check}
