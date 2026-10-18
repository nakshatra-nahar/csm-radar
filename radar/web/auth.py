"""Login with the shared demo passcode, then a demo user (design §9). The signed session holds
the user, tenant and role; no request ever takes them from the URL, query or form."""

import hmac
from pathlib import Path
from typing import Annotated, TypedDict
from uuid import UUID

import jinja2
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

# autoescape on everywhere; no template uses |safe on database or LLM text
templates = Jinja2Templates(env=jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True))
router = APIRouter()


class LoginRequired(Exception):
    """No user in the session; the app turns this into a redirect to /login."""


class SessionUser(TypedDict):
    """The plan's SessionUser; a dict keyed "id" (not user_id) because rbac.authorize (Ruling G)
    takes the same dict the agent passes."""
    id: UUID
    tenant_id: UUID
    role: str
    name: str


def current_user(request: Request) -> SessionUser:
    """The session's user as rbac.authorize expects it."""
    s = request.session
    if "user_id" not in s:
        raise LoginRequired
    return {"id": UUID(s["user_id"]), "tenant_id": UUID(s["tenant_id"]), "role": s["role"],
            "name": s["name"]}


User = Annotated[SessionUser, Depends(current_user)]


def tx(request: Request, user: SessionUser):
    """The user's tenant transaction; triggers audit writes as user:<id>."""
    return request.app.state.db.tenant_tx(user["tenant_id"], actor=f"user:{user['id']}")


def render(request: Request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _picker(request: Request) -> list[dict]:
    """Every tenant with its active users (users is under RLS: one tenant at a time)."""
    db = request.app.state.db
    with db.system_tx() as conn:
        tenants = conn.execute("SELECT id, name FROM tenants ORDER BY name").fetchall()
    for t in tenants:
        with db.tenant_tx(t["id"]) as conn:
            t["users"] = conn.execute("SELECT id, name, role FROM users WHERE active"
                                      " ORDER BY role, name").fetchall()
    return tenants


@router.get("/login")
def login_page(request: Request):
    if request.session.get("passcode_ok"):
        return render(request, "login.html", tenants=_picker(request))
    return render(request, "login.html")


@router.post("/login")
def check_passcode(request: Request, passcode: str = Form("")):
    expected = request.app.state.settings.demo_passcode
    if not hmac.compare_digest(passcode.encode(), expected.encode()):
        return render(request, "login.html", 401, error="That passcode is not right.")
    request.session["passcode_ok"] = True
    return render(request, "login.html", tenants=_picker(request))


@router.post("/login/user")
def choose_user(request: Request, user: str = Form("")):
    """user is '<tenant id>:<user id>'; the user must exist inside that tenant."""
    if not request.session.get("passcode_ok"):
        raise HTTPException(401)
    try:
        tid, uid = (UUID(part) for part in user.split(":"))
    except ValueError:
        raise HTTPException(400) from None
    with request.app.state.db.tenant_tx(tid) as conn:
        row = conn.execute("SELECT id, tenant_id, role, name FROM users WHERE id = %s"
                           " AND active", (uid,)).fetchone()
    if row is None:
        raise HTTPException(400)
    request.session.clear()
    request.session.update({"user_id": str(row["id"]), "tenant_id": str(row["tenant_id"]),
                            "role": row["role"], "name": row["name"]})
    return RedirectResponse("/week", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
