.PHONY: db test lint

# Tests and the app expect PostgreSQL 16 on localhost:54329 (user postgres / password postgres).
# `make db USE_DOCKER=1` starts it with Docker; plain `make db` prints a native recipe.
db:
ifdef USE_DOCKER
	docker compose up -d db
else
	@echo "Start a local PostgreSQL 16 on port 54329 (or run: make db USE_DOCKER=1):"
	@echo "  initdb -D ~/.local/share/radar-pg16 -U postgres --pwprompt   # password: postgres"
	@echo "  pg_ctl -D ~/.local/share/radar-pg16 -o \"-p 54329\" -l /tmp/radar-pg16.log start"
endif

# --extra ml: training, eval and the simulator need scikit-learn and pyarrow (pyproject.toml)
test:
	uv run --extra ml pytest -q

lint:
	uv run ruff check .
