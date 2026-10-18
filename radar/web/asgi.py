"""The process-wide app for uvicorn and Vercel: `uvicorn radar.web.asgi:app`."""

from radar.config import load_settings
from radar.db import Database
from radar.web.app import create_app

settings = load_settings()
app = create_app(settings, Database(settings.web_dsn))
