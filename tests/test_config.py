import pytest

from radar.config import load_settings


def test_load_settings_parses_webhook_secrets_and_derives_role_dsns(monkeypatch):
    monkeypatch.setenv("RADAR_OWNER_DSN", "postgresql://owner:pw@db:5432/radar")
    monkeypatch.setenv("RADAR_WEB_PASSWORD", "webpw")
    monkeypatch.setenv("RADAR_WEBHOOK_SECRETS", "k1:s1, k2:s:2")
    monkeypatch.delenv("RADAR_WEB_DSN", raising=False)
    s = load_settings()
    assert s.webhook_secrets == {"k1": "s1", "k2": "s:2"}
    assert "user=radar_web" in s.web_dsn and "password=webpw" in s.web_dsn
    assert "dbname=radar" in s.web_dsn and "host=db" in s.web_dsn
    assert s.llm_model == "claude-haiku-5-5"


def test_tick_secret_reads_either_env_name_and_budget_defaults(monkeypatch):
    for name in ("TICK_SECRET", "CRON_SECRET", "TICK_BUDGET_S", "RADAR_ENV"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert (s.tick_secret, s.tick_budget_s, s.production) == (None, 240.0, False)
    monkeypatch.setenv("CRON_SECRET", "c1")
    assert load_settings().tick_secret == "c1"
    monkeypatch.setenv("TICK_SECRET", "t1")
    monkeypatch.setenv("TICK_BUDGET_S", "30")
    monkeypatch.setenv("RADAR_ENV", "production")
    monkeypatch.setenv("RADAR_WEB_DSN", POOLED)
    monkeypatch.setenv("RADAR_WORKER_DSN", POOLED)
    s = load_settings()
    assert (s.tick_secret, s.tick_budget_s, s.production) == ("t1", 30.0, True)


def test_feed_source_is_files_or_db(monkeypatch):
    monkeypatch.delenv("FEED_SOURCE", raising=False)
    assert load_settings().feed_source == "files"
    monkeypatch.setenv("FEED_SOURCE", "db")
    assert load_settings().feed_source == "db"
    monkeypatch.setenv("FEED_SOURCE", "s3")
    with pytest.raises(ValueError, match="FEED_SOURCE"):
        load_settings()


NEON_PG_VARS = {"DATABASE_URL": "postgresql://neondb_owner:ownerpw@owner-host/neondb",
                "PGHOST": "owner-host", "PGUSER": "neondb_owner", "PGPASSWORD": "ownerpw",
                "PGDATABASE": "neondb"}
POOLED = "postgresql://radar_web:w@ep-x-pooler.example:5432/neondb?sslmode=require"


def test_role_dsns_must_name_every_parameter_so_libpq_never_reads_pg_vars(monkeypatch):
    """Ruling AU: Vercel's Neon integration injects owner credentials as DATABASE_URL and PG*.
    libpq fills any parameter a DSN leaves out from PG*, so a role DSN must name them all."""
    for name, value in NEON_PG_VARS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("RADAR_WEB_DSN", POOLED.replace(":5432", ""))  # no port
    with pytest.raises(ValueError, match="RADAR_WEB_DSN must set port"):
        load_settings()
    monkeypatch.setenv("RADAR_WEB_DSN", POOLED)
    monkeypatch.setenv("RADAR_WORKER_DSN", POOLED.replace("radar_web:w", "radar_worker:k"))
    s = load_settings()
    assert "neondb_owner" not in s.web_dsn + s.worker_dsn and "ownerpw" not in s.web_dsn


def test_production_takes_role_dsns_only_from_their_own_variables(monkeypatch):
    monkeypatch.setenv("RADAR_ENV", "production")
    monkeypatch.delenv("RADAR_WORKER_DSN", raising=False)
    monkeypatch.setenv("RADAR_WEB_DSN", POOLED)
    with pytest.raises(ValueError, match="RADAR_WORKER_DSN"):
        load_settings()  # never derived from RADAR_OWNER_DSN in production
    monkeypatch.setenv("RADAR_WORKER_DSN", POOLED.replace("?sslmode=require", ""))
    with pytest.raises(ValueError, match="RADAR_WORKER_DSN must set sslmode"):
        load_settings()  # PGSSLMODE must not be able to turn TLS off


def test_pgbouncer_setting_defaults_off(monkeypatch):
    monkeypatch.delenv("RADAR_PGBOUNCER", raising=False)
    assert load_settings().pgbouncer is False
    monkeypatch.setenv("RADAR_PGBOUNCER", "1")
    assert load_settings().pgbouncer is True


def test_production_role_dsns_refuse_sslmodes_that_allow_plaintext(monkeypatch):
    """disable/allow/prefer can all end up unencrypted; production takes only require or
    stronger."""
    monkeypatch.setenv("RADAR_ENV", "production")
    monkeypatch.setenv("RADAR_WEB_DSN", POOLED)
    for weak in ("disable", "allow", "prefer"):
        monkeypatch.setenv("RADAR_WORKER_DSN", POOLED.replace("require", weak))
        with pytest.raises(ValueError, match="RADAR_WORKER_DSN sslmode must be one of"):
            load_settings()
    for strong in ("require", "verify-ca", "verify-full"):
        monkeypatch.setenv("RADAR_WORKER_DSN", POOLED.replace("require", strong))
        assert f"sslmode={strong}" in load_settings().worker_dsn
