# One image for every role: `docker run -e ROLE=web ...` (see docker-compose.yml).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH=/app/.venv/bin:$PATH \
    RADAR_DATA_DIR=/app/data
WORKDIR /app

# Dependencies first, so a code change does not reinstall them. The ml extra trains the
# models during bootstrap; only the Vercel bundle goes without it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra ml --no-install-project
COPY radar ./radar
# /model renders the committed model cards
COPY docs/model-card-*.md ./docs/
RUN uv sync --frozen --no-dev --extra ml \
    && useradd --create-home --uid 10001 radar \
    && mkdir -p /app/data && chown radar /app/data
USER radar

# ROLE is a radar subcommand with its arguments, e.g. "web --host 0.0.0.0" or "worker".
CMD ["sh", "-c", "exec radar $ROLE"]
