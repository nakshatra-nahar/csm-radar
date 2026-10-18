"""Screenshots of the app's pages for a CSM, their manager and the admin of one tenant.

Logs in through the real login form (TestClient), saves each page's HTML with the stylesheet
inlined, and renders it with headless Chrome at laptop and phone size:

    uv run python scripts/screenshots.py --dsn <radar_web DSN> --out <dir> [--tenant hearthline]
        [--chrome <path>]

Chrome is --chrome, else $RADAR_CHROME, else the macOS install path.

--docs writes the README and deck images instead: docs/img/{team,week,account,trace,ops}.png at
desktop width, each window --height high and cut to its --crop box (needs Pillow). The
committed images come from:

    uv run --with pillow python scripts/screenshots.py --docs --out docs/img \
        --dsn postgresql://radar_web:<password>@localhost:54329/radar_full \
        --csm susan.anderson@hearthline.io --account "Flores & Gonzalez Homes" \
        --height team=1030 --height week=1110 --height account=1740 --height ops=1420 \
        --height trace=3560 --crop trace=0,2790,1440,3560

Opening /week as the CSM sets that list's viewed_at (the team page then shows it opened), so
--docs shoots team before week; reset it afterwards as the owner:
UPDATE worklists SET viewed_at = NULL WHERE user_id = <the CSM> AND week_start = <the week>.

The database must be bootstrapped (tenants, users, at least one sweep). The demo passcode is
read from the environment like the app's (RADAR_DEMO_PASSCODE).
"""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from radar.config import load_settings
from radar.db import Database
from radar.web.app import create_app

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SIZES = {"desktop": (1440, 900), "phone": (390, 844)}
MIN_CHROME_WIDTH = 500
DOCS_WIDTH = 1440
CSS = Path(__file__).resolve().parents[1] / "radar" / "web" / "static" / "style.css"
STYLESHEET = '<link rel="stylesheet" href="/static/style.css">'


def people(db: Database, slug: str, csm_email: str | None = None,
           account_name: str | None = None) -> dict:
    """The tenant, its CSM (csm_email, else the one with the longest list, else the first CSM),
    that CSM's manager (else the first manager), the admin, and an account to show
    (account_name, else first on the list, else owned)."""
    with db.system_tx() as conn:
        tid = conn.execute("SELECT id FROM tenants WHERE slug = %s", (slug,)).fetchone()["id"]
    with db.tenant_tx(tid) as conn:
        csm = conn.execute(
            "SELECT u.id, u.manager_id, count(i.id) AS n FROM users u LEFT JOIN worklists w"
            " ON w.user_id = u.id LEFT JOIN worklist_items i ON i.worklist_id = w.id"
            " WHERE u.role = 'csm' AND u.active AND (%(e)s::text IS NULL OR u.email = %(e)s)"
            " GROUP BY u.tenant_id, u.id ORDER BY n DESC, u.email LIMIT 1",
            {"e": csm_email}).fetchone()
        if csm is None:
            raise SystemExit(f"no active CSM {csm_email} in {slug}")
        manager = csm["manager_id"] or conn.execute(
            "SELECT id FROM users WHERE role = 'manager' AND active ORDER BY email LIMIT 1"
        ).fetchone()["id"]
        admin = conn.execute("SELECT id FROM users WHERE role = 'admin' AND active"
                             " ORDER BY email LIMIT 1").fetchone()["id"]
        account = conn.execute(
            "SELECT id FROM accounts WHERE name = %s", (account_name,)).fetchone() if \
            account_name else conn.execute(
            "SELECT coalesce((SELECT i.account_id FROM worklist_items i JOIN worklists w"
            " ON w.id = i.worklist_id WHERE w.user_id = %(u)s ORDER BY w.week_start DESC,"
            " i.position LIMIT 1), (SELECT id FROM accounts WHERE owner_user_id = %(u)s"
            " ORDER BY name LIMIT 1)) AS id", {"u": csm["id"]}).fetchone()
        if account is None:
            raise SystemExit(f"no account named {account_name} in {slug}")
        account = account["id"]
    return {"tenant": tid, "csm": csm["id"], "manager": manager, "admin": admin,
            "account": account}


def login(app, passcode: str, tenant, user) -> TestClient:
    c = TestClient(app)
    c.post("/login", data={"passcode": passcode})
    r = c.post("/login/user", data={"user": f"{tenant}:{user}"})
    if r.status_code != 200:
        raise SystemExit(f"login as {user} failed: {r.status_code}")
    return c


def shoot(html: str, out: Path, name: str, chrome: str = CHROME) -> list[Path]:
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
        subprocess.run([chrome, "--headless=new", "--hide-scrollbars", f"--screenshot={png}",
                        f"--window-size={w},{h}", target.as_uri()], check=True,
                       capture_output=True, timeout=60)
        pngs.append(png)
    return pngs


def crop(png: Path, box: tuple[int, int, int, int]) -> None:
    from PIL import Image  # only --crop needs Pillow; it is not a project dependency

    Image.open(png).crop(box).save(png, optimize=True)


def docs_set(app, settings, who: dict, args) -> list[Path]:
    """docs/img: team before week, and the CSM logs in only after it (login lands on /week),
    so the CSM's list is not yet marked opened by this run; the trace is the account page as
    the real recompute POST returns it."""
    clients = {}
    account = f"/accounts/{who['account']}"
    pngs = []
    for name, role, path in (("team", "manager", "/team"), ("week", "csm", "/week"),
                             ("account", "csm", account), ("trace", "csm", account),
                             ("ops", "admin", "/ops")):
        if role not in clients:
            clients[role] = login(app, settings.demo_passcode, who["tenant"], who[role])
        c = clients[role]
        r = c.post(f"{account}/recompute") if name == "trace" else c.get(path)
        if r.status_code != 200:
            raise SystemExit(f"{role} {path}: {r.status_code}")
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / f"{name}.html"
            page.write_text(r.text.replace(STYLESHEET, f"<style>{CSS.read_text()}</style>"))
            png = args.out / f"{name}.png"
            subprocess.run([args.chrome, "--headless=new", "--hide-scrollbars",
                            f"--screenshot={png}",
                            f"--window-size={DOCS_WIDTH},{args.height.get(name, 1400)}",
                            page.as_uri()], check=True, capture_output=True, timeout=60)
        if name in args.crop:
            crop(png, args.crop[name])
        pngs.append(png)
    return pngs


def _pairs(kind):
    """NAME=VALUE options collected into a dict; kind parses VALUE."""
    def parse(text: str) -> tuple[str, object]:
        name, _, value = text.partition("=")
        return name, kind(value)
    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", required=True, help="radar_web DSN of a bootstrapped database")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--tenant", default="hearthline", help="tenant slug")
    parser.add_argument("--chrome", default=os.environ.get("RADAR_CHROME", CHROME),
                        help="Chrome or Chromium binary (default: $RADAR_CHROME, else macOS)")
    parser.add_argument("--csm", help="the CSM's email (default: the longest list)")
    parser.add_argument("--account", help="account name (default: first on the CSM's list)")
    parser.add_argument("--docs", action="store_true",
                        help="write the docs/img set (team, week, account, trace, ops)")
    parser.add_argument("--height", action="append", default=[], metavar="PAGE=PX",
                        type=_pairs(int), help="--docs window height per page (default 1400)")
    parser.add_argument("--crop", action="append", default=[], metavar="PAGE=L,T,R,B",
                        type=_pairs(lambda v: tuple(int(n) for n in v.split(","))),
                        help="--docs crop box per page, in pixels")
    args = parser.parse_args(argv)
    args.height, args.crop = dict(args.height), dict(args.crop)
    return args


def main(argv: list[str] | None = None) -> list[Path]:
    args = parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    settings, db = load_settings(), Database(args.dsn)
    try:
        app, who = create_app(settings, db), people(db, args.tenant, args.csm, args.account)
        if args.docs:
            return docs_set(app, settings, who, args)
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
            pngs += shoot(r.text, args.out, f"{args.tenant}-{name}", args.chrome)
    finally:
        db.close()
    return pngs


if __name__ == "__main__":
    for png in main():
        print(png)
