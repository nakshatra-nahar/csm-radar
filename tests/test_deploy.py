"""What the Vercel deployment relies on: the runtime imports nothing from the `ml` extra,
prepared statements are off behind PgBouncer, and vercel.json bundles what the app reads."""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from radar.db import Database
from radar.tenants import tenant_id
from tests.conftest import WEB_PASSWORD, WORKER_PASSWORD, _role_dsn

ROOT = Path(__file__).resolve().parents[1]
ML_EXTRA = ("sklearn", "scipy", "pyarrow")  # installed locally, in CI and Docker; not on Vercel

# Runs in a fresh interpreter, so no earlier test has imported the blocked modules already.
SERVE_AND_SCORE = """
import sys
for name in {blocked!r}:
    sys.modules[name] = None  # any import of it now raises ImportError
from datetime import datetime
from uuid import UUID
import radar.web.asgi as asgi
from radar.db import Database
from radar.scoring import score_tenant
with asgi.app.state.db.pool.connection() as conn:
    assert asgi.app.state.db.pool.kwargs["prepare_threshold"] is None
    assert conn.prepare_threshold is None
worker = Database(asgi.settings.worker_dsn, pgbouncer=asgi.settings.pgbouncer)
print(score_tenant(worker, UUID({tenant!r}), datetime.fromisoformat({as_of!r}), "manual", False))
worker.close()
"""


def test_web_app_and_scoring_run_without_the_ml_extra(trained_world_db):
    """The Vercel bundle has no sklearn/scipy/pyarrow: import the ASGI app (behind PgBouncer
    settings) and score a tenant from its JSON model artifact with those modules blocked."""
    owner = trained_world_db["owner_dsn"]
    env = os.environ | {
        "RADAR_WEB_DSN": _role_dsn(owner, "radar_web", WEB_PASSWORD),
        "RADAR_WORKER_DSN": _role_dsn(owner, "radar_worker", WORKER_PASSWORD),
        "RADAR_PGBOUNCER": "1"}
    code = SERVE_AND_SCORE.format(blocked=ML_EXTRA, tenant=str(tenant_id("hearthline")),
                                  as_of=datetime(2026, 10, 12, tzinfo=UTC).isoformat())
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    assert re.fullmatch(r"[0-9a-f-]{36}\n", done.stdout)


def test_pgbouncer_pool_sends_no_startup_options_and_still_reads_utc(owner_dsn):
    """PgBouncer rejects startup options it does not track, so a pooled connection gets UTC
    from the role's own setting (migrate) instead of `-c timezone=UTC`."""
    db = Database(_role_dsn(owner_dsn, "radar_web", WEB_PASSWORD), pgbouncer=True)
    try:
        assert "options" not in db.pool.kwargs
        with db.system_tx() as conn:
            assert conn.prepare_threshold is None
            assert conn.execute("SHOW timezone").fetchone()["TimeZone"] == "UTC"
    finally:
        db.close()


def _glob_regex(pattern: str) -> re.Pattern:
    """The subset of Vercel's (minimatch) globs vercel.json uses: one top-level {a,b} list,
    **, * (not across /) and [!x]."""
    alts = pattern[1:-1].split(",") if pattern.startswith("{") else [pattern]
    def one(p: str) -> str:
        out = re.escape(p).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.sub(r"\\\[!(.+?)\\\]", r"[^\1]", out)  # re.escape escaped the brackets
    return re.compile("|".join(f"(?:{one(p)})" for p in alts))


def _excluded(path: str) -> bool:
    function = json.loads((ROOT / "vercel.json").read_text())["functions"]["radar/web/asgi.py"]
    return bool(_glob_regex(function["excludeFiles"]).fullmatch(path))


def test_vercel_bundle_keeps_every_file_the_app_reads_at_runtime():
    """Ruling AS: /model reads docs/model-card-<slug>.md, so docs/ is excluded except those."""
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    runtime = [p for p in tracked if p.startswith("radar/")
               or re.fullmatch(r"docs/model-card-[a-z]+\.md", p)]
    runtime += ["pyproject.toml", "uv.lock"]
    assert "radar/web/templates/doc.html" in runtime and "docs/model-card-hearthline.md" in runtime
    assert [p for p in runtime if _excluded(p)] == []
    for path in ("data/feed/hearthline/stripe.jsonl", "eval/hearthline.json",
                 "tests/test_web.py", "scripts/screenshots.py", "docs/design.md",
                 "docs/deploy.md", "docs/data-dictionary.md"):
        assert _excluded(path), path


def test_vercel_function_duration_and_daily_cron_backstop():
    config = json.loads((ROOT / "vercel.json").read_text())
    assert config["functions"]["radar/web/asgi.py"]["maxDuration"] == 300  # Hobby + Fluid max
    [cron] = config["crons"]
    minute, hour, *rest = cron["schedule"].split()
    assert cron["path"] == "/internal/tick" and minute.isdigit() and hour.isdigit()
    assert rest == ["*", "*", "*"]  # Hobby: at most once a day
