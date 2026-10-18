"""Process settings from environment variables (see .env.example)."""

import os
from dataclasses import dataclass
from pathlib import Path

from psycopg.conninfo import make_conninfo


@dataclass(frozen=True)
class Settings:
    owner_dsn: str
    web_dsn: str
    worker_dsn: str
    web_password: str
    worker_password: str
    secret_key: str
    demo_passcode: str
    anthropic_api_key: str | None
    llm_model: str
    data_dir: Path
    feed_source: str  # 'files' (data_dir/feed) or 'db' (feed_buffer, for serverless)
    webhook_secrets: dict[str, str]  # key id -> secret
    tick_secret: str | None  # bearer for /internal/tick; None disables the endpoint
    tick_budget_s: float
    production: bool  # session cookie is https-only


def _parse_secrets(raw: str) -> dict[str, str]:
    """'kid1:secret1,kid2:secret2' -> {kid: secret}. Secrets may contain ':'."""
    pairs = (item.strip().split(":", 1) for item in raw.split(",") if item.strip())
    return {kid: secret for kid, secret in pairs}


def load_settings() -> Settings:
    env = os.environ.get
    # Local-demo defaults only; a deployment sets every secret explicitly.
    owner_dsn = env("RADAR_OWNER_DSN", "postgresql://postgres:postgres@localhost:54329/radar")
    web_password = env("RADAR_WEB_PASSWORD", "radar_web")
    worker_password = env("RADAR_WORKER_PASSWORD", "radar_worker")
    feed_source = env("FEED_SOURCE", "files")
    if feed_source not in ("files", "db"):
        raise ValueError(f"FEED_SOURCE must be 'files' or 'db', not {feed_source!r}")
    return Settings(
        owner_dsn=owner_dsn,
        web_dsn=env("RADAR_WEB_DSN")
        or make_conninfo(owner_dsn, user="radar_web", password=web_password),
        worker_dsn=env("RADAR_WORKER_DSN")
        or make_conninfo(owner_dsn, user="radar_worker", password=worker_password),
        web_password=web_password,
        worker_password=worker_password,
        secret_key=env("RADAR_SECRET_KEY", "dev-only-secret-key"),
        demo_passcode=env("RADAR_DEMO_PASSCODE", "radar-demo"),
        anthropic_api_key=env("ANTHROPIC_API_KEY") or None,
        llm_model=env("RADAR_LLM_MODEL", "claude-haiku-5-5"),
        data_dir=Path(env("RADAR_DATA_DIR", "data")),
        feed_source=feed_source,
        webhook_secrets=_parse_secrets(env("RADAR_WEBHOOK_SECRETS", "")),
        # Vercel cron sends CRON_SECRET; deployments set both names to the same value
        tick_secret=env("TICK_SECRET") or env("CRON_SECRET") or None,
        tick_budget_s=float(env("TICK_BUDGET_S", "240")),
        production=env("RADAR_ENV") == "production",
    )
