"""The server-rendered app (design §9): session auth, security headers and the routers."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from radar.config import Settings
from radar.db import Database, assert_role_safety
from radar.web import auth, hooks, routes

CSP = ("default-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none';"
       " base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def create_app(settings: Settings, db_web: Database, db_worker: Database | None = None) -> FastAPI:
    """db_web serves every page as radar_web; db_worker (radar_worker) runs /internal/tick and
    is created from settings.worker_dsn on first use when not given."""
    for db in (db_web, db_worker):  # design §4.3: refuse a role RLS would not bind
        if db is not None:
            with db.system_tx() as conn:
                assert_role_safety(conn)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.db_worker = settings, db_web, db_worker

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    # added last, so it runs first: the session is decoded before any route reads it.
    # SameSite=lax keeps the cookie off cross-site POSTs, which is the CSRF defence here.
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                       https_only=settings.production, same_site="lax")

    @app.exception_handler(auth.LoginRequired)
    async def to_login(request: Request, exc: auth.LoginRequired):
        return RedirectResponse("/login", status_code=303)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    for router in (auth.router, routes.router, hooks.router):  # Part B adds its routers here
        app.include_router(router)
    return app
