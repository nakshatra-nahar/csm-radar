"""Machine endpoints: signed webhooks, the scheduler tick (Ruling Y) and the health check."""

import hmac
import json
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from radar import clock
from radar.agent import jobs, worker
from radar.db import Database, UnsafeRole, assert_role_safety
from radar.ingest import feed, pipeline, webhooks

router = APIRouter()
_worker_lock = threading.Lock()


@router.get("/healthz")
def healthz():
    return {"ok": True}  # no database access, so no writes


def _ingest(db: Database, slug: str, source: str, body: dict, signature: str) -> dict | None:
    """Land one verified record in the key id's tenant and enqueue its rule checks in the same
    transaction (design §8). None if no tenant has that slug."""
    with db.system_tx() as conn:
        tenant = conn.execute("SELECT id FROM tenants WHERE slug = %s", (slug,)).fetchone()
    if tenant is None:
        return None
    tid = tenant["id"]
    record = {"source": source, "body": body, "channel": "webhook",
              "headers": {"Stripe-Signature": signature}}
    with db.tenant_tx(tid) as conn:
        stats = pipeline.ingest(conn, tid, [record], available_at=clock.now(), verified=True)
        for account in stats.triggers:
            jobs.enqueue(conn, "evaluate_rules", tid, {"account_id": str(account)},
                         dedupe_key=f"rules:{account}", lane=0)
    return {"accepted": stats.accepted, "duplicates": stats.duplicates,
            "quarantined": stats.quarantined}


@router.post("/webhooks/{source}")
async def webhook(source: str, request: Request):
    """Stripe-style HMAC over the raw body with the configured secrets only (never the demo
    fallback); the tenant is the key id's slug ('<slug>-<n>')."""
    if source not in feed.SOURCES:
        raise HTTPException(404)
    raw = await request.body()
    signature = request.headers.get("stripe-signature", "")  # Starlette: case-insensitive
    try:
        kid = webhooks.verify(signature, raw, request.app.state.settings.webhook_secrets,
                              int(clock.now().timestamp()))
    except webhooks.WebhookError:
        raise HTTPException(401) from None
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400) from None
    if not isinstance(body, dict):
        raise HTTPException(400)
    result = await run_in_threadpool(_ingest, request.app.state.db, kid.rpartition("-")[0],
                                     source, body, signature)
    if result is None:
        raise HTTPException(401)
    return JSONResponse(result, status_code=202)


def _worker_db(app) -> Database:
    with _worker_lock:  # two first ticks at once must not open two pools
        if app.state.db_worker is None:
            s = app.state.settings
            db = Database(s.worker_dsn, pgbouncer=s.pgbouncer)
            try:  # design §4.3: never run the jobs as a role RLS would not bind
                with db.system_tx() as conn:
                    assert_role_safety(conn)
            except Exception:
                db.close()
                raise
            app.state.db_worker = db
        return app.state.db_worker


@router.api_route("/internal/tick", methods=["GET", "POST"])
def tick(request: Request):
    """Vercel cron (GET) and GitHub Actions (POST) run the worker for a time budget."""
    settings = request.app.state.settings
    if not settings.tick_secret:
        raise HTTPException(503, "TICK_SECRET is not set")
    given = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(given, f"Bearer {settings.tick_secret}".encode()):
        raise HTTPException(401)
    try:
        db = _worker_db(request.app)
    except UnsafeRole as e:  # fail closed, and say why; any other failure is a 500
        return JSONResponse({"detail": str(e)}, status_code=503)
    return worker.run_for(db, budget_s=settings.tick_budget_s)
