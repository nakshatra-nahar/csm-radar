-- The unattended agent: job queue, schedules, cases, worklists, briefs, outbox, incidents,
-- and the audit ledger with its triggers.

-- jobs and schedule_runs are system tables holding ids only; tenant_id is NULL for
-- system-wide work. RLS: radar_web sees its tenant's rows, radar_worker sees all (004).
CREATE TABLE jobs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id        uuid DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                     REFERENCES tenants (id),
    kind             text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}',
    dedupe_key       text,
    lane             smallint NOT NULL DEFAULT 0,
    run_at           timestamptz NOT NULL DEFAULT now(),
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'dead')),
    attempts         int NOT NULL DEFAULT 0,
    max_attempts     int NOT NULL DEFAULT 5,
    lease_expires_at timestamptz,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
-- NULLS NOT DISTINCT so system jobs (tenant_id NULL) dedupe too; jobs without a key never collide.
CREATE UNIQUE INDEX jobs_dedupe_idx ON jobs (tenant_id, dedupe_key) NULLS NOT DISTINCT
    WHERE status = 'queued' AND dedupe_key IS NOT NULL;
CREATE INDEX jobs_queue_idx ON jobs (lane, run_at, id) WHERE status = 'queued';

CREATE TABLE schedule_runs (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  uuid REFERENCES tenants (id),
    kind       text NOT NULL,
    period_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (tenant_id, kind, period_key)
);

CREATE TABLE cases (
    tenant_id          uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                       REFERENCES tenants (id),
    id                 uuid NOT NULL DEFAULT gen_random_uuid(),
    account_id         uuid NOT NULL,
    kind               text NOT NULL,
    status             text NOT NULL
                       CHECK (status IN ('open', 'held', 'done', 'dismissed', 'snoozed', 'auto_closed')),
    play               text,
    opened_at          timestamptz NOT NULL DEFAULT now(),
    opened_by          text NOT NULL,
    owner_user_id      uuid,
    sla_due_at         timestamptz,
    escalated_at       timestamptz,
    evidence_event_ids bigint[] NOT NULL DEFAULT '{}',
    outcome            text,
    outcome_reason     text,
    closed_at          timestamptz,
    closed_by          uuid,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id),
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id)
);
CREATE UNIQUE INDEX cases_one_live_idx ON cases (tenant_id, account_id, kind)
    WHERE status IN ('open', 'held');

CREATE TABLE suppressions (
    tenant_id     uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  REFERENCES tenants (id),
    id            uuid NOT NULL DEFAULT gen_random_uuid(),
    account_id    uuid NOT NULL,
    created_by    uuid,
    reason        text NOT NULL,
    p_at_dismiss  double precision,
    created_at    timestamptz NOT NULL DEFAULT now(),
    lifted_at     timestamptz,
    lifted_reason text,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id)
);

CREATE TABLE worklists (
    tenant_id  uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
               REFERENCES tenants (id),
    id         uuid NOT NULL DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL,
    week_start date NOT NULL,
    run_id     uuid,
    frozen_at  timestamptz NOT NULL DEFAULT now(),
    viewed_at  timestamptz,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, user_id, week_start),
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES score_runs (tenant_id, id)
);

CREATE TABLE worklist_items (
    tenant_id                  uuid NOT NULL
                               DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                               REFERENCES tenants (id),
    id                         uuid NOT NULL DEFAULT gen_random_uuid(),
    worklist_id                uuid NOT NULL,
    account_id                 uuid NOT NULL,
    position                   int NOT NULL,
    kind                       text NOT NULL CHECK (kind IN ('must_act', 'check_in', 'model')),
    reason                     text,
    p                          double precision,
    expected_arr_at_risk_minor bigint,
    case_id                    uuid,
    added_at                   timestamptz NOT NULL DEFAULT now(),
    new_since_monday           boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, worklist_id, account_id),
    FOREIGN KEY (tenant_id, worklist_id) REFERENCES worklists (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id),
    FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id)
);

CREATE TABLE briefs (
    tenant_id      uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                   REFERENCES tenants (id),
    id             uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id        uuid NOT NULL,
    evidence_hash  text NOT NULL,
    prompt_version text NOT NULL,
    status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'failed')),
    generator      text,
    evidence       jsonb NOT NULL DEFAULT '{}',
    output         jsonb,
    validation     jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, case_id, evidence_hash, prompt_version),
    FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id)
);

CREATE TABLE llm_calls (
    tenant_id   uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                REFERENCES tenants (id),
    id          bigint GENERATED ALWAYS AS IDENTITY,
    brief_id    uuid NOT NULL,
    attempt     int NOT NULL,
    model       text NOT NULL,
    request_id  text,
    response_id text,
    stop_reason text,
    usage       jsonb,
    cost_usd    numeric(12, 6),
    latency_ms  int,
    ok          boolean NOT NULL,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, brief_id) REFERENCES briefs (tenant_id, id)
);

-- Customer email is only ever a draft until a human approves; transport is dry-run.
CREATE TABLE outbox (
    tenant_id     uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  REFERENCES tenants (id),
    id            uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id       uuid,
    brief_id      uuid,
    kind          text NOT NULL CHECK (kind IN ('customer_email', 'csm_digest', 'manager_digest')),
    to_contact_id uuid,
    to_user_id    uuid,
    subject       text NOT NULL,
    body          text NOT NULL,
    status        text NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'approved', 'sent', 'cancelled')),
    approved_by   uuid,
    approved_at   timestamptz,
    sent_at       timestamptz,
    transport     text NOT NULL DEFAULT 'dry_run',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
    FOREIGN KEY (tenant_id, brief_id) REFERENCES briefs (tenant_id, id),
    FOREIGN KEY (tenant_id, to_contact_id) REFERENCES contacts (tenant_id, id),
    FOREIGN KEY (tenant_id, to_user_id) REFERENCES users (tenant_id, id)
);

CREATE TABLE incidents (
    tenant_id       uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                    REFERENCES tenants (id),
    id              uuid NOT NULL DEFAULT gen_random_uuid(),
    kind            text NOT NULL
                    CHECK (kind IN ('circuit_breaker', 'source_stale', 'manifest_mismatch')),
    source          text,
    details         jsonb NOT NULL DEFAULT '{}',
    opened_at       timestamptz NOT NULL DEFAULT now(),
    acknowledged_by uuid,
    acknowledged_at timestamptz,
    closed_at       timestamptz,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE audit_log (
    tenant_id   uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                REFERENCES tenants (id),
    id          bigint GENERATED ALWAYS AS IDENTITY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL,  -- 'agent' | 'system' | 'user:<uuid>'
    action      text NOT NULL,
    entity_type text NOT NULL,
    entity_id   text,
    before      jsonb,
    after       jsonb,
    reason      text,
    PRIMARY KEY (tenant_id, id)
);

-- Runs as the caller (not SECURITY DEFINER), so its insert is subject to audit_log's RLS
-- and grants like any other write.
CREATE FUNCTION audit_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit_log (tenant_id, actor, action, entity_type, entity_id, before, after)
    VALUES (NEW.tenant_id,
            COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system'),
            lower(TG_OP), TG_TABLE_NAME, NEW.id::text,
            CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) END,
            to_jsonb(NEW));
    RETURN NULL;
END
$$;

CREATE TRIGGER cases_audit AFTER INSERT OR UPDATE ON cases
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER outbox_audit AFTER INSERT OR UPDATE ON outbox
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER suppressions_audit AFTER INSERT OR UPDATE ON suppressions
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER models_audit AFTER INSERT OR UPDATE ON models
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER incidents_audit AFTER INSERT OR UPDATE ON incidents
    FOR EACH ROW EXECUTE FUNCTION audit_row();
