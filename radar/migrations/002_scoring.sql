-- Models, score runs and per-account scores (append-only), plus the latest_scores view.

CREATE TABLE models (
    tenant_id     uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  REFERENCES tenants (id),
    id            uuid NOT NULL DEFAULT gen_random_uuid(),
    version       int NOT NULL,
    status        text NOT NULL DEFAULT 'candidate'
                  CHECK (status IN ('candidate', 'active', 'retired', 'rejected')),
    algorithm     text NOT NULL DEFAULT 'logreg',
    artifact      jsonb NOT NULL,
    manifest      jsonb NOT NULL,
    label_version text,
    train_window  jsonb,
    metrics       jsonb NOT NULL DEFAULT '{}',
    model_card_md text,
    trained_at    timestamptz NOT NULL DEFAULT now(),
    promoted_at   timestamptz,
    promoted_by   text,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, version)
);
CREATE UNIQUE INDEX models_one_active_idx ON models (tenant_id) WHERE status = 'active';

CREATE TABLE score_runs (
    tenant_id           uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                        REFERENCES tenants (id),
    id                  uuid NOT NULL DEFAULT gen_random_uuid(),
    model_id            uuid,
    trigger             text NOT NULL CHECK (trigger IN ('sweep', 'backfill', 'event', 'manual')),
    actionable          boolean NOT NULL,
    as_of               timestamptz NOT NULL,
    watermarks          jsonb NOT NULL DEFAULT '{}',
    crosswalk_watermark timestamptz,
    config_version      int NOT NULL,
    manifest            jsonb NOT NULL DEFAULT '{}',
    code_version        text,
    status              text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'done', 'failed')),
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    n_scored            int,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, model_id) REFERENCES models (tenant_id, id)
);

CREATE TABLE account_scores (
    tenant_id                  uuid NOT NULL
                               DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                               REFERENCES tenants (id),
    run_id                     uuid NOT NULL,
    account_id                 uuid NOT NULL,
    as_of                      timestamptz NOT NULL,
    eligible                   boolean NOT NULL,
    p                          double precision,
    logit                      double precision,
    lift                       double precision,
    rules_score                double precision,
    band                       text,
    rank_in_book               int,
    owner_user_id              uuid,
    arr_minor                  bigint,
    expected_arr_at_risk_minor bigint,
    features                   jsonb,
    contributions              jsonb,
    context                    jsonb,
    coverage                   jsonb,
    PRIMARY KEY (tenant_id, run_id, account_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES score_runs (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id),
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id)
);
CREATE INDEX account_scores_account_idx ON account_scores (tenant_id, account_id, as_of DESC);

-- security_invoker: the view runs with the caller's rights, so RLS on the base tables applies.
CREATE VIEW latest_scores WITH (security_invoker = true) AS
SELECT s.*
FROM account_scores s
JOIN (
    SELECT DISTINCT ON (tenant_id) tenant_id, id
    FROM score_runs
    WHERE status = 'done' AND actionable
    ORDER BY tenant_id, as_of DESC, finished_at DESC, id
) r ON r.tenant_id = s.tenant_id AND r.id = s.run_id;
