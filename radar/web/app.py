"""The server-rendered app (design §9): session auth, security headers and the routers."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from radar.config import Settings
from radar.db import Database, assert_role_safety
from radar.web import auth, hooks, oversight, routes

MACHINE_PATHS = ("/webhooks/", "/internal/", "/healthz")  # these answer in JSON
TITLES = {400: "Bad request", 401: "Not allowed", 404: "Not found", 409: "Already changed"}
CSP = ("default-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none';"
       " base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "same-origin"}


def _viewer(request: Request) -> auth.SessionUser | None:
    """The logged-in user for an error page's nav; None when there is none or it cannot be
    read (the error page must render even when the database is the problem)."""
    try:
        return auth.current_user(request)
    except Exception:
        return None


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
        response.headers.update(SECURITY_HEADERS)
        return response

    # added last, so it runs first: the session is decoded before any route reads it.
    # SameSite=lax keeps the cookie off cross-site POSTs, which is the CSRF defence here.
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                       https_only=settings.production, same_site="lax")

    @app.exception_handler(auth.LoginRequired)
    async def to_login(request: Request, exc: auth.LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def error_page(request: Request, exc: HTTPException):
        if request.url.path.startswith(MACHINE_PATHS):
            return await http_exception_handler(request, exc)
        return auth.render(request, "error.html", exc.status_code, headers=exc.headers,
                           user=await run_in_threadpool(_viewer, request),
                           title=TITLES.get(exc.status_code, "Error"),
                           message=exc.detail if exc.status_code != 404 else None)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):  # the traceback goes to the log
        if request.url.path.startswith(MACHINE_PATHS):
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        # this handler runs outside the middleware stack, so it sets the headers itself
        return auth.render(request, "error.html", 500, headers=SECURITY_HEADERS,
                           user=await run_in_threadpool(_viewer, request),
                           title="Something went wrong")

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    for router in (auth.router, routes.router, oversight.router, hooks.router):
        app.include_router(router)
    return app
