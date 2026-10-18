# Deploying Radar

Two ways to run Radar for other people:

- **A. Vercel + Neon (free).** The web app runs as one Vercel Python function. A GitHub
  Actions schedule calls `POST /internal/tick` every 15 minutes, which runs the worker for up
  to 240 seconds. A daily Vercel cron calls the same endpoint as a backstop.
- **B. Docker Compose** (or Railway, or any host that keeps a process running). A long-lived
  `radar worker` replaces the tick.

Every command below runs from the repository root. Values in `<angle brackets>` are yours.

## A. Vercel + Neon

### A1. What you need

- A Vercel account (Hobby) and the CLI: `npm i -g vercel`, then `vercel login`.
- A GitHub account with this repository pushed to it.
- [uv](https://docs.astral.sh/uv/) and, on macOS, Homebrew.
- Optional: an Anthropic API key with credit. Without one, briefs use the built-in
  templates.

### A2. Create the Vercel project and the Neon database

1. Link the project. Either run `vercel link` in the repository root and answer the prompts,
   or in the Vercel dashboard choose **Add New → Project** and import the GitHub repository.
   Vercel finds the app through `[tool.vercel] entrypoint` in `pyproject.toml`.
2. In the project, open **Storage → Create Database**, pick **Neon** from the Marketplace,
   choose the **Free** plan and the region **AWS us-east-1** (next to Vercel's default
   function region, iad1). Connect it to the project. Pick Postgres 16 if the dialog offers a
   version; 17 also works.
3. Open the database in the Neon console (**Storage → your database → Open in Neon**) and
   click **Connect**. Copy two connection strings for the `neondb_owner` role and the
   `neondb` database:
   - with **Connection pooling off**: the direct owner URL, host `ep-<id>.us-east-1.aws.neon.tech`;
   - with **Connection pooling on**: note its host, `ep-<id>-pooler.us-east-1.aws.neon.tech`.
4. In the Neon console, open **Branches → production → Compute → Edit** and set autoscaling
   to a minimum of 0.25 CU and a maximum of 0.5 CU (see "Compute budget" below).

The integration also adds `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `PG*` and `POSTGRES_*`
variables to the Vercel project. They carry the owner's credentials, and Radar never reads
them: the web app and the worker connect only through `RADAR_WEB_DSN` and
`RADAR_WORKER_DSN`, as roles that row-level security binds. Each DSN names every connection
parameter itself, so libpq never fills one in from a `PG*` variable. The app refuses to start
if either DSN is for a role that is a superuser, bypasses RLS or owns the tables.

### A3. Generate the secrets

Run this in the shell you will use for the rest of part A. The generated values never
appear on screen. Type the passcode, the Neon owner URL and the Anthropic key at the `read -rs`
prompts (input is not echoed) rather than in a literal `export` line, which your shell would
save to its history. Leave the Anthropic key empty to use template briefs.

```sh
export RADAR_WEB_PASSWORD="$(openssl rand -base64 24)"
export RADAR_WORKER_PASSWORD="$(openssl rand -base64 24)"
export RADAR_SECRET_KEY="$(openssl rand -hex 32)"
export TICK_SECRET="$(openssl rand -hex 32)"
printf 'Demo passcode: '; read -rs RADAR_DEMO_PASSCODE; echo; export RADAR_DEMO_PASSCODE
printf 'Neon direct owner URL (A2.3): '; read -rs NEON_DIRECT_OWNER_URL; echo; export NEON_DIRECT_OWNER_URL
printf 'Anthropic API key (or Enter): '; read -rs VERCEL_ANTHROPIC_KEY; echo  # its own name, not exported: A4 stays on templates
export NEON_POOLED_HOST='<the -pooler host from A2.3>'
```

Neon requires high-entropy passwords for roles created with SQL, which is how `radar push`
creates `radar_web` and `radar_worker`; `openssl rand -base64 24` gives 192 bits. Keep these
values somewhere safe (a password manager): you need the two role passwords again if you ever
push again.

### A4. Bootstrap locally with PostgreSQL 16

The bootstrap replays months of synthetic history record by record. Over the ~250 ms round
trip from a laptop to Neon that would take days, so it runs against a local PostgreSQL 16,
and `radar push` then copies the finished database to Neon in bulk with `pg_dump` and
`pg_restore`, which takes minutes.

If you already run PostgreSQL 16 on port 54329 (the test database), skip to step 2.

1. Install and start PostgreSQL 16 (Homebrew):

   ```sh
   brew install postgresql@16
   PG16=/opt/homebrew/opt/postgresql@16/bin
   printf 'postgres\n' > /tmp/pgpass && "$PG16/initdb" -D ~/radar-pg16 -U postgres --pwfile=/tmp/pgpass && rm /tmp/pgpass
   "$PG16/pg_ctl" -D ~/radar-pg16 -o "-p 54329" -l ~/radar-pg16.log start
   ```

2. Create the database and point Radar at it as the owner:

   ```sh
   PG16=/opt/homebrew/opt/postgresql@16/bin
   PGPASSWORD=postgres "$PG16/createdb" -h localhost -p 54329 -U postgres radar
   export RADAR_OWNER_DSN=postgresql://postgres:postgres@localhost:54329/radar
   ```

3. Install Radar with the `ml` extra (bootstrap trains the models with scikit-learn) and run
   the bootstrap. It generates the synthetic world into `data/`, replays it, trains a model
   per tenant, scores, sweeps and writes the first briefs, and buffers the rest of the feed in
   the database. It is safe to re-run.

   ```sh
   uv sync --extra ml
   uv run radar bootstrap
   ```

   The two role passwords from A3 must be exported when you run this: `migrate` gives the
   local `radar_web` and `radar_worker` roles those passwords.

### A5. Push to Neon

```sh
uv run radar push --to "$NEON_DIRECT_OWNER_URL" --pg-bin /opt/homebrew/opt/postgresql@16/bin
```

- Use the **direct** owner URL, not the pooled one: the restore is one long transaction.
  `bootstrap`, `push` and `prune` all need a direct (non-pooled) owner connection: they
  connect as the owner without honouring `RADAR_PGBOUNCER`, so a PgBouncer host breaks them.
- `--pg-bin` must hold a `pg_dump` and `pg_restore` whose major version is at least the
  source server's (16) and at most the target's. Homebrew's default `postgresql` is 17, and
  `pg_restore` 17 writes `SET transaction_timeout`, which a PostgreSQL 16 server rejects.
- `push` runs the migrations on Neon, creates `radar_web` and `radar_worker` with the
  passwords from A3, copies every row and prints the row count per table.
- It refuses a target role that cannot bypass RLS (Neon's `neondb_owner` can) and a target
  that already holds data. `--force` empties the target first, then copies.

### A6. Set the Vercel environment variables

Both role DSNs use Neon's **pooled** host with the app roles' own credentials, in libpq's
keyword form (the base64 passwords may hold `/` or `+`, which a URL would need escaped).
Neon's pooler is PgBouncer in transaction mode, so `RADAR_PGBOUNCER=1` turns off prepared
statements and takes the session time zone from the role instead of a startup option.

```sh
add() { printf %s "$2" | vercel env add "$1" production; }
secret() { printf %s "$2" | vercel env add "$1" production --sensitive; }

secret RADAR_WEB_DSN "host=$NEON_POOLED_HOST port=5432 dbname=neondb user=radar_web password=$RADAR_WEB_PASSWORD sslmode=require channel_binding=require"
secret RADAR_WORKER_DSN "host=$NEON_POOLED_HOST port=5432 dbname=neondb user=radar_worker password=$RADAR_WORKER_PASSWORD sslmode=require channel_binding=require"
secret RADAR_SECRET_KEY "$RADAR_SECRET_KEY"
secret RADAR_DEMO_PASSCODE "$RADAR_DEMO_PASSCODE"
secret TICK_SECRET "$TICK_SECRET"
secret CRON_SECRET "$TICK_SECRET"   # Vercel cron sends this one; same value as TICK_SECRET
[ -n "$VERCEL_ANTHROPIC_KEY" ] && secret ANTHROPIC_API_KEY "$VERCEL_ANTHROPIC_KEY"  # from A3
add FEED_SOURCE db                  # the rest of the feed is in the feed_buffer table
add RADAR_PGBOUNCER 1
add RADAR_ENV production            # HTTPS-only session cookie; role DSNs required
```

| Variable | What goes in it |
| --- | --- |
| `RADAR_WEB_DSN` | pooled host, port 5432, `neondb`, `radar_web` and its password, `sslmode=require` |
| `RADAR_WORKER_DSN` | the same with `radar_worker` and its password |
| `RADAR_SECRET_KEY` | signs the session cookie |
| `RADAR_DEMO_PASSCODE` | the shared passcode on the login page |
| `TICK_SECRET`, `CRON_SECRET` | one value: the bearer for `/internal/tick` |
| `ANTHROPIC_API_KEY` | optional; LLM-drafted briefs |
| `FEED_SOURCE` | `db` |
| `RADAR_PGBOUNCER` | `1` |
| `RADAR_ENV` | `production` |
| `RADAR_WEBHOOK_SECRETS` | optional; `<slug>-<n>:<secret>,...` to accept signed webhooks |

In production the app refuses to start unless both role DSNs are set and each names host,
port, dbname, user, password and sslmode, with sslmode `require`, `verify-ca` or
`verify-full` (`disable`, `allow` and `prefer` are refused). Do not point either at
`DATABASE_URL`.

### A7. Deploy

```sh
vercel deploy --prod
```

`vercel.json` gives the function the Hobby maximum of 300 seconds, keeps `data/`, `eval/`,
`tests/`, `scripts/` and `docs/` out of the bundle except the model cards that `/model`
renders, and adds the daily backstop cron. `.vercelignore` keeps `.env` and `data/` from
being uploaded. The function installs from `pyproject.toml` and `uv.lock` without the `ml`
extra, so scikit-learn, SciPy and PyArrow stay out of the bundle (about 116 MB of
dependencies instead of 344 MB); serving and scoring do not need them.

### A8. Schedule the tick from GitHub Actions

In the GitHub repository, open **Settings → Secrets and variables → Actions** and add two
repository secrets (or use the `gh` CLI):

```sh
gh secret set RADAR_URL --body 'https://<your-project>.vercel.app'
printf %s "$TICK_SECRET" | gh secret set TICK_SECRET
```

`.github/workflows/tick.yml` then posts to `/internal/tick` every 15 minutes. Until
`RADAR_URL` is set, each scheduled run logs a notice and exits successfully instead of
failing.

### A9. Verify

1. `curl -fsS https://<your-project>.vercel.app/healthz` prints `{"ok":true}`.
2. Open the site, enter the demo passcode, and pick a user.
3. Run the tick once: **Actions → tick → Run workflow**, or `gh workflow run tick.yml`. The
   run's log shows the JSON summary of the jobs it ran.
4. Log in as an admin and open `/ops`: the agent's activity from that tick is listed.

### A10. Operations

**Neon storage.** The Free plan holds 1 GB. The bootstrap measured about 584 MB, and the database
grows about 5.4 MB a day (4.6 MB of it score history; docs/measurements.md). Check the size with
`psql "$NEON_DIRECT_OWNER_URL" -c "SELECT pg_size_pretty(pg_database_size('neondb'))"`.
When it passes about 800 MB, prune old score runs:

```sh
RADAR_OWNER_DSN="$NEON_DIRECT_OWNER_URL" uv run radar prune --older-than-days 35
```

`prune` measures age by each run's `as_of`. It always keeps the latest done run of each kind,
every run a worklist was frozen from, and the Monday runs that history reads. Delivered feed
lines are already deleted from `feed_buffer`, so the buffer holds only what is still to come.

**Compute budget.** The Free plan allows 100 CU-hours a month. Neon suspends the compute 5
minutes after the last query, so each 15-minute tick keeps it awake for about 5 to 6
minutes: about 65 to 70 CU-hours a month at 0.25 CU. Page views add to that. Keep autoscaling
capped at 0.25 to 0.5 CU. If usage nears the allowance (Neon console → **Usage**), change the
schedule in `.github/workflows/tick.yml` to `*/30 * * * *`.

**GitHub schedules.** GitHub disables scheduled workflows in a public repository after 60
days without repository activity, and a scheduled run can start several minutes late. Re-enable
the workflow under **Actions → tick**. The daily Vercel cron in `vercel.json` keeps the
worker running in the meantime (on Hobby it fires once a day, at some point in the scheduled
hour). That hour must be at or after every tenant's `sweep_hour_local` in local time, or the
cron never runs that tenant's daily sweep: `0 14 * * *` is 06:00 to 06:59 in Los Angeles in
winter (`tests/test_deploy.py` checks it).

**Rotating TICK_SECRET.** Generate a new value, replace it in all three places, then redeploy:

```sh
export TICK_SECRET="$(openssl rand -hex 32)"
for name in TICK_SECRET CRON_SECRET; do
  vercel env rm "$name" production --yes
  printf %s "$TICK_SECRET" | vercel env add "$name" production --sensitive
done
printf %s "$TICK_SECRET" | gh secret set TICK_SECRET
vercel deploy --prod
```

**LLM drafts.** With no Anthropic key, or no credit on it, every brief uses the template
fallback. Add credit (and `ANTHROPIC_API_KEY`, then redeploy) to get LLM-drafted briefs.

**Deploying changed feature code.** Each model stores a hash of the feature code it was
trained on (`registry.manifest()`, which covers `radar/features.py` and `radar/asof.py`).
After a deploy that changes that code, scoring fails closed with an incident until each
tenant has a model trained on the new code: run `RADAR_WORKER_DSN=<the Neon worker DSN> uv run
radar train --tenant <slug>` for each tenant with a model, or repeat A4 and A5.

## B. Docker Compose (or any always-on host)

Compose runs PostgreSQL 16, a one-shot `bootstrap`, the `web` app and a long-lived `worker`,
so there is no tick schedule. All services use the one image from `Dockerfile`
(`python:3.12-slim`, `uv sync --frozen --no-dev --extra ml`, a non-root user), and `ROLE`
picks the `radar` subcommand.

Write a `.env` next to `docker-compose.yml`, replacing each `<...>` with the output of the
command it names:

```sh
cat > .env <<'ENV'
RADAR_WEB_PASSWORD=<openssl rand -base64 24>
RADAR_WORKER_PASSWORD=<openssl rand -base64 24>
RADAR_SECRET_KEY=<openssl rand -hex 32>
RADAR_DEMO_PASSCODE=<a passcode>
ANTHROPIC_API_KEY=
ENV
docker compose up --build
```

`web` and `worker` start once `bootstrap` has finished (the first run generates and replays
the world, which takes a while). Then open http://localhost:8000; `/healthz` answers
`{"ok":true}` and the worker logs its jobs. Re-running `docker compose up` re-runs the
bootstrap, which adds nothing the second time.

On Railway or a similar host, run the same image as two services from one Postgres 16:
`ROLE="web --host 0.0.0.0 --port 8000"` (and the service's port set to 8000: `ROLE` is
expanded once, so it cannot refer to `$PORT`) and `ROLE=worker`, with `RADAR_OWNER_DSN` (or the
two role DSNs), the secrets above and `FEED_SOURCE=db`. Run `ROLE=bootstrap` once first, or
bootstrap locally and `radar push` as in A4 and A5. If the host's Postgres sits behind
PgBouncer in transaction mode, also set `RADAR_PGBOUNCER=1` for `web` and `worker`, and give
`bootstrap` (and any later `push` or `prune`) a direct owner connection instead (see A5).
