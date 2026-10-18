"""The `radar` console script. DSNs come from Settings; only migrate, seed, bootstrap, prune
and push use the owner DSN."""

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from radar import clock, evaluate, ops, registry
from radar.agent import worker
from radar.bootstrap import bootstrap, seed_tenants_and_users, train_and_promote
from radar.config import Settings, load_settings
from radar.db import Database, assert_role_safety
from radar.migrate import migrate
from radar.sim import generate
from radar.tenants import TENANTS, tenant_id


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def _worker_db(s: Settings) -> Database:
    db = Database(s.worker_dsn)
    with db.system_tx() as conn:  # design §4.3: never run the jobs as a role RLS would not bind
        assert_role_safety(conn)
    return db


def _seed(a, s: Settings) -> None:
    db = Database(s.worker_dsn)
    try:
        seed_tenants_and_users(s.owner_dsn, db, list(TENANTS))
    finally:
        db.close()


def _train(a, s: Settings) -> None:
    db = _worker_db(s)  # RLS keeps the retire and the version count to this tenant
    try:
        _print({"model_id": train_and_promote(db, tenant_id(a.tenant), clock.now())})
    finally:
        db.close()


def _eval(a, s: Settings) -> None:
    for violation in evaluate.write_report(a.tenant, Path(a.out)):
        print("acceptance:", violation)


def _dictionary(a, s: Settings) -> None:
    path = Path("docs", "data-dictionary.md")
    path.parent.mkdir(exist_ok=True)
    path.write_text(registry.render_dictionary_md())
    print(path)


def _web(a, s: Settings) -> None:
    import uvicorn
    uvicorn.run("radar.web.asgi:app", host=a.host, port=a.port)


def _worker(a, s: Settings) -> None:
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    db = _worker_db(s)
    try:
        worker.run_forever(db, f"{socket.gethostname()}:{os.getpid()}", stop)
    finally:
        db.close()


def _tick(a, s: Settings) -> None:
    db = _worker_db(s)
    try:
        _print(worker.run_for(db, "cli-tick", a.budget or s.tick_budget_s))
    finally:
        db.close()


def _push(a, s: Settings) -> None:
    try:
        _print(ops.push(a.to, a.source or s.owner_dsn, s.web_password, s.worker_password,
                        a.pg_bin, a.force))
    except ops.PushError as e:
        sys.exit(f"radar push: {e}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="radar", description="Radar: the CSM risk worklist.")
    sub = p.add_subparsers(required=True, metavar="command")

    def cmd(name, run, text):
        c = sub.add_parser(name, help=text, description=text)
        c.set_defaults(run=run)
        return c
    cmd("migrate", lambda a, s: _print(migrate(s.owner_dsn, s.web_password, s.worker_password)),
        "create the app roles and apply pending migrations")
    cmd("seed", _seed, "add the demo tenants and their staff (merges new config keys)")
    sim = sub.add_parser("sim", help="the synthetic world").add_subparsers(
        required=True, metavar="command")
    gen = sim.add_parser("generate", help="write the feed and truth files")
    gen.set_defaults(run=lambda a, s: _print(generate(a.out or s.data_dir, a.seed, a.scale)))
    gen.add_argument("--scale", type=float, default=1.0)
    gen.add_argument("--seed", type=int, default=7)
    gen.add_argument("--out", type=Path, help="default: RADAR_DATA_DIR")
    boot = cmd("bootstrap", lambda a, s: _print(bootstrap(s, a.scale)),
               "set up a demo database from scratch; safe to re-run")
    boot.add_argument("--scale", type=float, default=1.0,
                      help="world size if the feed must be generated")
    cmd("train", _train, "fit and promote a tenant's model").add_argument(
        "--tenant", required=True, choices=list(TENANTS))
    ev = cmd("eval", _eval, "write a tenant's model card and eval/<tenant>.json")
    ev.add_argument("--tenant", required=True, choices=list(TENANTS))
    ev.add_argument("--out", default="docs", help="model card folder")
    cmd("dictionary", _dictionary, "write docs/data-dictionary.md from the registry")
    web = cmd("web", _web, "serve the app with uvicorn")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    cmd("worker", _worker, "run the unattended worker until stopped")
    cmd("tick", _tick, "run due jobs once, within a time budget (local or cron)").add_argument(
        "--budget", type=float, help="seconds; default TICK_BUDGET_S")
    prune = cmd("prune", lambda a, s: _print(ops.prune(s.owner_dsn, a.older_than_days)),
                "delete old score runs nothing reads")
    prune.add_argument("--older-than-days", type=int, required=True)
    push = cmd("push", _push, "copy the local bootstrapped database to a remote one")
    push.add_argument("--to", required=True, help="target owner DSN")
    push.add_argument("--from", dest="source", help="source owner DSN; default RADAR_OWNER_DSN")
    push.add_argument("--pg-bin", help="folder holding pg_dump and pg_restore")
    push.add_argument("--force", action="store_true",
                      help="empty a target that already holds data first")
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    args.run(args, load_settings())
