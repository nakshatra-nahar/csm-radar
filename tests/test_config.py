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
