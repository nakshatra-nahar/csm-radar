"""Screenshots of the app's pages for a CSM, their manager and the admin of one tenant.

Logs in through the real login form (TestClient), saves each page's HTML with the stylesheet
inlined, and renders it with headless Chrome at laptop and phone size:

    uv run python scripts/screenshots.py --dsn <radar_web DSN> --out <dir> [--tenant hearthline]

The database must be bootstrapped (tenants, users, at least one sweep). The demo passcode is
read from the environment like the app's (RADAR_DEMO_PASSCODE).
"""

import argparse
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from radar.config import load_settings
from radar.db import Database
from radar.web.app import create_app

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SIZES = {"desktop": (1440, 900), "phone": (390, 844)}
MIN_CHROME_WIDTH = 500
CSS = Path(__file__).resolve().parents[1] / "radar" / "web" / "static" / "style.css"
STYLESHEET = '<link rel="stylesheet" href="/static/style.css">'


def people(db: Database, slug: str) -> dict:
    """The tenant, its CSM with the longest list (else the first CSM), that CSM's manager (else
    the first manager), the admin, and an account to show (first on the list, else owned)."""
    with db.system_tx() as conn:
        tid = conn.execute("SELECT id FROM tenants WHERE slug = %s", (slug,)).fetchone()["id"]
    with db.tenant_tx(tid) as conn:
        csm = conn.execute(
            "SELECT u.id, u.manager_id, count(i.id) AS n FROM users u LEFT JOIN worklists w"
            " ON w.user_id = u.id LEFT JOIN worklist_items i ON i.worklist_id = w.id"
            " WHERE u.role = 'csm' AND u.active GROUP BY u.tenant_id, u.id ORDER BY n DESC, u.email"
            " LIMIT 1").fetchone()
        manager = csm["manager_id"] or conn.execute(
            "SELECT id FROM users WHERE role = 'manager' AND active ORDER BY email LIMIT 1"
        ).fetchone()["id"]
        admin = conn.execute("SELECT id FROM users WHERE role = 'admin' AND active"
                             " ORDER BY email LIMIT 1").fetchone()["id"]
        account = conn.execute(
            "SELECT coalesce((SELECT i.account_id FROM worklist_items i JOIN worklists w"
            " ON w.id = i.worklist_id WHERE w.user_id = %(u)s ORDER BY w.week_start DESC,"
            " i.position LIMIT 1), (SELECT id FROM accounts WHERE owner_user_id = %(u)s"
            " ORDER BY name LIMIT 1)) AS id", {"u": csm["id"]}).fetchone()["id"]
    return {"tenant": tid, "csm": csm["id"], "manager": manager, "admin": admin,
            "account": account}


def login(app, passcode: str, tenant, user) -> TestClient:
    c = TestClient(app)
    c.post("/login", data={"passcode": passcode})
    r = c.post("/login/user", data={"user": f"{tenant}:{user}"})
    if r.status_code != 200:
        raise SystemExit(f"login as {user} failed: {r.status_code}")
    return c


def shoot(html: str, out: Path, name: str) -> list[Path]:
    """Write the page with its CSS inlined, then one PNG per size. Headless Chrome lays out
    at least 500px wide, so a narrower size renders the page in an iframe of that width."""
    page = out / f"{name}.html"
    page.write_text(html.replace(STYLESHEET, f"<style>{CSS.read_text()}</style>"))
    pngs = []
    for size, (w, h) in SIZES.items():
        target = page
        if w < MIN_CHROME_WIDTH:
            target = out / f"{name}-{size}.html"
            target.write_text(f'<body style="margin:0"><iframe src="{page.name}" width="{w}"'
                              f' height="{h}" style="border:0"></iframe>')
        png = out / f"{name}-{size}.png"
        subprocess.run([CHROME, "--headless=new", "--hide-scrollbars", f"--screenshot={png}",
                        f"--window-size={w},{h}", target.as_uri()], check=True,
                       capture_output=True, timeout=60)
        pngs.append(png)
    return pngs


def main(argv: list[str] | None = None) -> list[Path]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", required=True, help="radar_web DSN of a bootstrapped database")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--tenant", default="hearthline", help="tenant slug")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    settings, db = load_settings(), Database(args.dsn)
    try:
        app, who = create_app(settings, db), people(db, args.tenant)
        shots = {"login": (None, "/login")}
        for role, paths in (("csm", ["/week", "/book", "/model", "/dictionary"]),
                            ("manager", ["/team"]), ("admin", ["/ops", "/team"])):
            for path in paths:
                shots[f"{role}-{path.strip('/')}"] = (role, path)
        if who["account"]:
            shots["csm-account"] = ("csm", f"/accounts/{who['account']}")
        clients = {role: login(app, settings.demo_passcode, who["tenant"], who[role])
                   for role in ("csm", "manager", "admin")}
        pngs = []
        for name, (role, path) in shots.items():
            r = (clients[role] if role else TestClient(app)).get(path)
            if r.status_code != 200:
                raise SystemExit(f"{role} {path}: {r.status_code}")
            pngs += shoot(r.text, args.out, f"{args.tenant}-{name}")
    finally:
        db.close()
    return pngs


if __name__ == "__main__":
    for png in main():
        print(png)
