"""scripts/soak.py on the small world: two simulated days, unattended."""

import importlib.util
from pathlib import Path

import pytest

from radar.bootstrap import HISTORY_END
from radar.db import owner_connect
from tests.conftest import WORKER_PASSWORD, world_database


def _soak():
    spec = importlib.util.spec_from_file_location(
        "soak", Path(__file__).resolve().parents[1] / "scripts" / "soak.py")
    soak = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(soak)
    return soak


@pytest.mark.slow
def test_soak_runs_one_sweep_per_tenant_per_local_day(small_world_dir, monkeypatch):
    soak = _soak()
    monkeypatch.setenv("RADAR_WORKER_PASSWORD", WORKER_PASSWORD)
    monkeypatch.setenv("RADAR_DATA_DIR", str(small_world_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with world_database(small_world_dir) as world:
        with owner_connect(world["owner_dsn"], autocommit=True) as conn:  # bootstrap's last job
            conn.execute("INSERT INTO jobs (kind, payload, status, finished_at)"
                         " VALUES ('feed_tick', '{}', 'done', %s)", (HISTORY_END,))
        out = soak.run(world["owner_dsn"], days=2, step_minutes=60)
    assert out["start"] == HISTORY_END.isoformat()
    # 2026-10-17 01:00 UTC, the first step, is Friday evening in New York and LA, past the
    # sweep hour of a day nothing swept: that day's sweep runs late, then one per local day
    days = {"2026-10-17": 1, "2026-10-18": 1}
    assert out["sweeps"] == {"hearthline": {"2026-10-16": 1} | days,
                             "kitewire": {"2026-10-16": 1} | days, "tallyworks": days}
    assert out["dead_jobs"] == 0 and out["score_runs_running"] == 0
    assert sum(out["delivered"]["hearthline"].values()) > 0
    assert len(out["db_bytes_per_day"]) == 3  # the start and the end of each day
