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
    s = load_settings()
    assert (s.tick_secret, s.tick_budget_s, s.production) == ("t1", 30.0, True)
