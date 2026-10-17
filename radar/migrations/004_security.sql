-- Row-level security on every tenant table, app-role grants and role settings.
-- Roles radar_web and radar_worker are created by radar/migrate.py (they are cluster-global).

-- One FOR ALL policy per table that has a tenant_id column. FORCE makes it bind the owner too
-- unless the owner is a superuser. schedule_runs is worker-only and gets no tenant policy.
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
        WHERE n.nspname = 'public' AND c.relkind = 'r'
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY', t);
        IF t <> 'schedule_runs' THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I FOR ALL'
                ' USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)'
                ' WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
                t);
        END IF;
    END LOOP;
END
$$;

-- The worker claims every tenant's jobs and runs system schedules.
CREATE POLICY jobs_worker_all ON jobs FOR ALL TO radar_worker USING (true) WITH CHECK (true);
CREATE POLICY schedule_runs_worker_all ON schedule_runs FOR ALL TO radar_worker
    USING (true) WITH CHECK (true);

GRANT USAGE ON SCHEMA public TO radar_web, radar_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO radar_web, radar_worker;

-- Append-only: no UPDATE or DELETE.
GRANT SELECT, INSERT ON
    raw_records, events, identity_links, score_runs, account_scores, llm_calls, audit_log
    TO radar_web, radar_worker;

-- A run is inserted 'running' (account_scores rows need it to exist) and then finished;
-- only its lifecycle columns may change.
GRANT UPDATE (status, finished_at, n_scored) ON score_runs TO radar_web, radar_worker;

-- tenants has no tenant_id, so the loop above skips it. Anyone may list tenants (login page);
-- a tenant_tx may change only its own config; only the owner (bootstrap) inserts tenants.
-- Not FORCEd, so the owner is unaffected.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenants_read ON tenants FOR SELECT USING (true);
CREATE POLICY tenants_update_own ON tenants FOR UPDATE
    USING (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, UPDATE (config, config_version) ON tenants TO radar_web, radar_worker;

GRANT SELECT, INSERT, UPDATE ON
    users, quarantine, accounts, contacts, source_status, models, jobs, cases,
    suppressions, worklists, worklist_items, briefs, outbox, incidents
    TO radar_web, radar_worker;
GRANT SELECT, INSERT, UPDATE ON schedule_runs TO radar_worker;
GRANT SELECT ON latest_scores TO radar_web, radar_worker;

-- jit off: short OLTP queries pay JIT compile cost for nothing. Timeouts stop a stuck
-- transaction from holding locks or a pooled connection.
ALTER ROLE radar_web SET jit = off;
ALTER ROLE radar_web SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE radar_web SET statement_timeout = '60s';
ALTER ROLE radar_worker SET jit = off;
ALTER ROLE radar_worker SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE radar_worker SET statement_timeout = '60s';
