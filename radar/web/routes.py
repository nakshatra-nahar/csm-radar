"""The CSM pages and case actions (design §9). Every account read and every write passes
rbac.authorize inside the user's tenant_tx; anything not allowed is a 404."""

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from radar import clock, rbac, registry, scoring
from radar.agent import brief
from radar.web import queries
from radar.web.auth import User, render, tx

router = APIRouter()
OUTCOMES = ("called", "emailed", "met", "no_action")
DISMISSALS = ("not_a_risk", "already_handled")
BANDS = ("red", "yellow", "green", "unknown", "not_in_window")


def _authorized(conn, user: dict, account_id: UUID, action: str) -> dict:
    a = queries.account(conn, account_id)
    if a is None or not rbac.authorize(user, a["owner_user_id"],
                                       rbac.team_ids(conn, user["id"]), action):
        raise HTTPException(404)
    return a


def _back(account_id: UUID) -> RedirectResponse:
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@router.get("/")
def home():
    return RedirectResponse("/week", status_code=303)


@router.get("/week")
def week(request: Request, user: User):
    """The user's list frozen this ISO week: must-acts, check-ins, then model slots."""
    with tx(request, user) as conn:
        tenant = queries.tenant(conn, user["tenant_id"])
        start = queries.week_start(tenant["timezone"])
        wl = queries.worklist(conn, user["id"], start)
        team = rbac.team_ids(conn, user["id"])
        rows = [r for r in (queries.worklist_rows(conn, wl) if wl else [])
                if rbac.authorize(user, r["owner_user_id"], team, "view")]
        frozen_any = wl is not None or queries.any_worklist(conn, start)
        ctx = {"tenant": tenant, "week": start, "worklist": wl, "rows": rows,
               "frozen_any": frozen_any, "freshness": queries.freshness(conn),
               "model": queries.active_model(conn)}
    return render(request, "week.html", user=user, **ctx)


@router.get("/book")
def book(request: Request, user: User, band: str | None = None):
    band = band if band in BANDS else None
    with tx(request, user) as conn:
        team = rbac.team_ids(conn, user["id"])
        rows = [r for r in queries.book(conn, band)
                if rbac.authorize(user, r["owner_user_id"], team, "view")]
        ctx = {"tenant": queries.tenant(conn, user["tenant_id"]), "rows": rows, "band": band,
               "bands": BANDS, "model": queries.active_model(conn)}
    return render(request, "book.html", user=user, **ctx)


def _account_page(request: Request, user: dict, account_id: UUID, recompute=None):
    with tx(request, user) as conn:
        account = _authorized(conn, user, account_id, "view")
        tenant = queries.tenant(conn, user["tenant_id"])
        score = queries.score(conn, account_id)
        currency = tenant["config"]["currency"]
        ctx = {"tenant": tenant, "account": account, "score": score, "currency": currency,
               "model": queries.active_model(conn),
               "changes": score and scoring.diff_runs(conn, account_id, score["run_id"]),
               "why": queries.why(score and score["contributions"]),
               "timeline": queries.timeline(conn, account_id, score and score["as_of"],
                                            currency),
               "cases": queries.cases(conn, account_id, user["id"]),
               "features": registry.FEATURE_NAMES, "outcomes": OUTCOMES,
               "dismissals": DISMISSALS, "recompute": recompute}
    return render(request, "account.html", user=user, **ctx)


@router.get("/accounts/{account_id}")
def account_page(request: Request, user: User, account_id: UUID):
    return _account_page(request, user, account_id)


@router.post("/accounts/{account_id}/recompute")
def recompute(request: Request, user: User, account_id: UUID):
    """Recompute the latest score from raw events with the run's model and compare."""
    with tx(request, user) as conn:
        _authorized(conn, user, account_id, "view")
        score = queries.score(conn, account_id)
    if score is None:
        raise HTTPException(404)
    result = scoring.recompute_check(request.app.state.db, user["tenant_id"], score["run_id"],
                                     account_id)
    return _account_page(request, user, account_id, recompute=result)


def _open_case(conn, user: dict, account_id: UUID, case_id: UUID, action: str) -> dict:
    _authorized(conn, user, account_id, action)
    case = queries.case_of(conn, account_id, case_id)
    if case is None:
        raise HTTPException(404)
    if case["status"] != "open":
        raise HTTPException(409, "This case is no longer open.")
    return case


@router.post("/accounts/{account_id}/cases/{case_id}/outcome")
def log_outcome(request: Request, user: User, account_id: UUID, case_id: UUID,
                outcome: str = Form(""), reason: str = Form("")):
    """Closes the case with what the CSM did; the cases trigger writes the audit row."""
    reason = reason.strip()
    if outcome not in OUTCOMES or (outcome == "no_action" and not reason):
        raise HTTPException(400, "Pick an outcome; 'no action' needs a reason.")
    with tx(request, user) as conn:
        _open_case(conn, user, account_id, case_id, "log_outcome")
        conn.execute("UPDATE cases SET status = 'done', outcome = %s, outcome_reason = %s,"
                     " closed_at = %s, closed_by = %s WHERE id = %s",
                     (outcome, reason or None, clock.now(), user["id"], case_id))
    return _back(account_id)


@router.post("/accounts/{account_id}/cases/{case_id}/dismiss")
def dismiss(request: Request, user: User, account_id: UUID, case_id: UUID, reason: str = Form("")):
    """Not a risk, or already handled: the case closes and the account is suppressed from
    model slots until a must-act or a rise in p lifts it (policy._suppressed)."""
    if reason not in DISMISSALS:
        raise HTTPException(400, "Pick a reason to dismiss.")
    with tx(request, user) as conn:
        _open_case(conn, user, account_id, case_id, "dismiss")
        now = clock.now()
        conn.execute("UPDATE cases SET status = 'dismissed', outcome_reason = %s,"
                     " closed_at = %s, closed_by = %s WHERE id = %s",
                     (reason, now, user["id"], case_id))
        conn.execute("INSERT INTO suppressions (account_id, created_by, reason, p_at_dismiss,"
                     " created_at) VALUES (%s, %s, %s, (SELECT p FROM latest_scores"
                     " WHERE account_id = %s), %s)",
                     (account_id, user["id"], reason, account_id, now))
    return _back(account_id)


@router.post("/accounts/{account_id}/drafts/{outbox_id}/edit")
def edit_draft(request: Request, user: User, account_id: UUID, outbox_id: UUID,
               subject: str = Form(""), body: str = Form("")):
    if not subject.strip() or not body.strip():
        raise HTTPException(400, "A draft needs a subject and a body.")
    with tx(request, user) as conn:
        _authorized(conn, user, account_id, "edit_draft")
        draft = queries.draft_of(conn, account_id, outbox_id)
        if draft is None:
            raise HTTPException(404)
        if draft["status"] != "draft":
            raise HTTPException(409, "This email is no longer a draft.")
        conn.execute("UPDATE outbox SET subject = %s, body = %s WHERE id = %s",
                     (subject.strip(), body, outbox_id))
    return _back(account_id)


@router.post("/accounts/{account_id}/drafts/{outbox_id}/approve")
def approve(request: Request, user: User, account_id: UUID, outbox_id: UUID):
    """Approve and send through the dry-run transport, placeholders filled (brief.filled)."""
    with tx(request, user) as conn:
        _authorized(conn, user, account_id, "approve_send")
        if queries.draft_of(conn, account_id, outbox_id) is None:
            raise HTTPException(404)
        try:
            brief.approve_and_send(conn, outbox_id, user["id"])
        except PermissionError:
            raise HTTPException(404) from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
    return _back(account_id)
