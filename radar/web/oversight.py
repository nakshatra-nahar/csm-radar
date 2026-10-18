"""The manager's and admin's pages (design §9): the team dashboard, ops with incident
acknowledgement and model promotion, and the model card and data dictionary."""

from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import markdown
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from radar import clock, rbac, registry
from radar.web import queries
from radar.web.auth import User, render, tx

router = APIRouter()
DOCS = Path(__file__).resolve().parents[2] / "docs"
RENEWAL_DAYS = 90
TEAM_BANDS = ("red", "yellow", "green", "unknown", "not_in_window")


def _admin(user: dict, action: str) -> None:
    if not rbac.authorize(user, None, set(), action):
        raise HTTPException(404)


def _markdown(text: str) -> str:
    """Our own committed markdown as HTML, with raw HTML escaped rather than passed through."""
    md = markdown.Markdown(extensions=["tables"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    # wide tables scroll inside their own box, like every other table
    return md.convert(text).replace("<table>", '<div class="scroll"><table>').replace(
        "</table>", "</table></div>")


def _sum(rows, key) -> int | None:
    values = [r[key] for r in rows if r[key] is not None]
    return sum(values) if values else None


@router.get("/team")
def team(request: Request, user: User):
    """The CSMs the user may see (a manager's team, an admin's tenant) and their books."""
    if user["role"] == "csm":
        raise HTTPException(404)
    with tx(request, user) as conn:
        tenant = queries.tenant(conn, user["tenant_id"])
        team_ids = rbac.team_ids(conn, user["id"])
        # a CSM's row shows when the user may see that CSM's book
        csms = [c for c in queries.csm_activity(conn, queries.week_start(tenant["timezone"]),
                                                tenant["timezone"])
                if rbac.authorize(user, c["id"], team_ids, "view")]
        run = queries.latest_run(conn)
        current = queries.run_scores(conn, run["id"]) if run else []
        previous = queries.run_scores(conn, run["prev"]["id"]) if run and run["prev"] else []
        scores, before = ([s for s in rows
                           if rbac.authorize(user, s["owner_user_id"], team_ids, "view")]
                          for rows in (current, previous))
        # a manager still answers for what their departed CSMs left behind
        former = queries.former_reports(conn, user["id"])
        orphans = [s for s in current if s["band"] in ("red", "unknown")
                   and not s["owner_active"]
                   and rbac.authorize(user, s["owner_user_id"], former, "view")]
        model = queries.active_model(conn)
    by_csm = defaultdict(lambda: defaultdict(lambda: {"n": 0, "arr": 0}))
    for s in scores:
        cell = by_csm[s["owner_user_id"]][s["band"]]
        cell["n"], cell["arr"] = cell["n"] + 1, cell["arr"] + (s["arr_minor"] or 0)
    today = clock.now().astimezone(ZoneInfo(tenant["timezone"])).date()
    renewals = defaultdict(lambda: {"n": 0, "arr": 0})
    for s in scores:
        if s["term_end"] and today <= s["term_end"] <= today + timedelta(days=RENEWAL_DAYS):
            renewals[s["band"]]["n"] += 1
            renewals[s["band"]]["arr"] += s["arr_minor"] or 0
    at_risk = {"now": _sum(scores, "at_risk"), "before": _sum(before, "at_risk"),
               "red_now": sum(s["arr_minor"] or 0 for s in scores if s["band"] == "red"),
               "red_before": sum(s["arr_minor"] or 0 for s in before if s["band"] == "red")}
    return render(request, "team.html", user=user, tenant=tenant, csms=csms, run=run,
                  by_csm=by_csm, renewals=renewals, at_risk=at_risk, orphans=orphans,
                  bands=TEAM_BANDS, model=model, renewal_days=RENEWAL_DAYS)


@router.get("/ops")
def ops(request: Request, user: User):
    _admin(user, "view_ops")
    with tx(request, user) as conn:
        tenant = queries.tenant(conn, user["tenant_id"])
        ctx = queries.ops(conn, tenant["timezone"])
        stale = {s["source"] for s in queries.freshness(conn) if s["stale"]}
        active = next((m["artifact"] for m in ctx["models"] if m["status"] == "active"), None)
        for m in ctx["models"]:
            if m["status"] == "candidate":
                m["band_changes"] = queries.band_changes(conn, m["artifact"], active,
                                                         tenant["config"])
    return render(request, "ops.html", user=user, tenant=tenant, stale=stale, **ctx)


@router.post("/admin/incidents/{incident_id}/ack")
def acknowledge(request: Request, user: User, incident_id: UUID):
    _admin(user, "ack_incident")
    with tx(request, user) as conn:
        done = conn.execute("UPDATE incidents SET acknowledged_by = %s, acknowledged_at = %s"
                            " WHERE id = %s AND acknowledged_at IS NULL",
                            (user["id"], clock.now(), incident_id)).rowcount
        if not done:
            exists = conn.execute("SELECT 1 FROM incidents WHERE id = %s",
                                  (incident_id,)).fetchone()
            raise HTTPException(409 if exists else 404, "Already acknowledged.")
    return RedirectResponse("/ops", status_code=303)


@router.post("/admin/models/{model_id}/promote")
def promote(request: Request, user: User, model_id: UUID):
    """Candidate to active; the active one retires in the same transaction (one active model
    per tenant). The next score run uses it."""
    _admin(user, "promote_model")
    with tx(request, user) as conn:
        conn.execute("UPDATE models SET status = 'retired' WHERE status = 'active'")
        promoted = conn.execute(
            "UPDATE models SET status = 'active', promoted_at = %s, promoted_by = %s"
            " WHERE id = %s AND status = 'candidate'",
            (clock.now(), f"user:{user['id']}", model_id)).rowcount
        if not promoted:  # raising rolls the retirement back too
            raise HTTPException(404)
    return RedirectResponse("/ops", status_code=303)


@router.get("/model")
def model_card(request: Request, user: User):
    """The committed model card of the session's tenant; a tenant without a model is ranked
    by the rules heuristic, described from the registry."""
    with tx(request, user) as conn:
        tenant = queries.tenant(conn, user["tenant_id"])
    card = DOCS / f"model-card-{tenant['slug']}.md"
    rules = next(d for d in registry.METRICS if d.name == "rules_score")
    return render(request, "doc.html", user=user, tenant=tenant, title="Model card", kind="card",
                  html=_markdown(card.read_text()) if card.exists() else None, rules=rules)


@router.get("/dictionary")
def dictionary(request: Request, user: User):
    with tx(request, user) as conn:
        tenant = queries.tenant(conn, user["tenant_id"])
    return render(request, "doc.html", user=user, tenant=tenant, title="Data dictionary",
                  kind="dictionary",
                  html=_markdown(registry.render_dictionary_md()))
