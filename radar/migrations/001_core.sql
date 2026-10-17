-- Core tables: tenants, people, the raw ledger, canonical events, identity, projections.
-- Every tenant table: tenant_id defaults to the transaction's app.tenant_id (NULLIF because a
-- pooled connection reads '' after its first transaction), PK (tenant_id, id), and composite
-- FKs so a row can only reference rows of its own tenant. RLS is switched on in 004.

CREATE TABLE tenants (
    id              uuid PRIMARY KEY,
    slug            text NOT NULL UNIQUE,
    name            text NOT NULL,
    timezone        text NOT NULL,
    currency        text NOT NULL,
    internal_domain text NOT NULL,
    config          jsonb NOT NULL DEFAULT '{}',
    config_version  int NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    tenant_id  uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
               REFERENCES tenants (id),
    id         uuid NOT NULL DEFAULT gen_random_uuid(),
    email      text NOT NULL,
    name       text NOT NULL,
    role       text NOT NULL CHECK (role IN ('csm', 'manager', 'admin')),
    manager_id uuid,
    sf_user_id text,
    active     boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, email),
    FOREIGN KEY (tenant_id, manager_id) REFERENCES users (tenant_id, id)
);

CREATE TABLE raw_records (
    tenant_id        uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                     REFERENCES tenants (id),
    id               bigint GENERATED ALWAYS AS IDENTITY,
    source           text NOT NULL,
    source_record_id text NOT NULL,
    payload          jsonb NOT NULL,
    payload_hash     text NOT NULL,
    channel          text,
    headers          jsonb,
    meta             jsonb,
    occurred_at      timestamptz,
    available_at     timestamptz NOT NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, source, source_record_id)
);

CREATE TABLE events (
    tenant_id     uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  REFERENCES tenants (id),
    id            bigint GENERATED ALWAYS AS IDENTITY,
    raw_record_id bigint NOT NULL,
    source        text NOT NULL,
    type          text NOT NULL,
    entity_id     text NOT NULL,
    account_ref   text,
    occurred_at   timestamptz NOT NULL,
    available_at  timestamptz NOT NULL,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    version_key   text NOT NULL,
    -- Hot filter attributes are typed columns: jsonb operators are not leakproof under RLS.
    status        text,
    priority      text,
    amount_minor  bigint,
    quantity      int,
    attrs         jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, raw_record_id) REFERENCES raw_records (tenant_id, id)
);
CREATE INDEX events_account_ref_idx ON events (tenant_id, account_ref, occurred_at)
    INCLUDE (available_at, type);
CREATE INDEX events_entity_idx ON events (tenant_id, entity_id, occurred_at);
CREATE INDEX events_available_idx ON events (tenant_id, available_at);

CREATE TABLE accounts (
    tenant_id        uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                     REFERENCES tenants (id),
    id               uuid NOT NULL DEFAULT gen_random_uuid(),
    sf_account_id    text,
    name             text NOT NULL,
    domain           text,
    owner_user_id    uuid,
    owner_sf_user_id text,
    segment          text,
    is_test          boolean NOT NULL DEFAULT false,
    merged_into      uuid,
    source_modstamp  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, sf_account_id),
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id),
    FOREIGN KEY (tenant_id, merged_into) REFERENCES accounts (tenant_id, id)
);

CREATE TABLE contacts (
    tenant_id       uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                    REFERENCES tenants (id),
    id              uuid NOT NULL DEFAULT gen_random_uuid(),
    account_id      uuid,
    sf_contact_id   text,
    email           text,
    first_name      text,
    last_name       text,
    title           text,
    is_champion     boolean NOT NULL DEFAULT false,
    opted_out       boolean NOT NULL DEFAULT false,
    departed        boolean NOT NULL DEFAULT false,
    hard_bounced_at timestamptz,
    source_modstamp timestamptz,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, sf_contact_id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id)
);

-- Append-only crosswalk. Resolution at T: latest row per (source, account_ref) with
-- available_at <= T. Events never store account_id, so re-attribution never rewrites history.
CREATE TABLE identity_links (
    tenant_id    uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                 REFERENCES tenants (id),
    id           bigint GENERATED ALWAYS AS IDENTITY,
    source       text NOT NULL,
    account_ref  text NOT NULL,
    account_id   uuid NOT NULL,
    method       text NOT NULL,
    available_at timestamptz NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, account_id) REFERENCES accounts (tenant_id, id)
);
CREATE INDEX identity_links_lookup_idx
    ON identity_links (tenant_id, source, account_ref, available_at DESC);

CREATE TABLE quarantine (
    tenant_id        uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                     REFERENCES tenants (id),
    id               bigint GENERATED ALWAYS AS IDENTITY,
    raw_record_id    bigint,
    source           text NOT NULL,
    account_ref      text,
    reason           text NOT NULL,
    details          jsonb NOT NULL DEFAULT '{}',
    available_at     timestamptz NOT NULL,
    resolved_at      timestamptz,
    resolved_link_id bigint,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, raw_record_id) REFERENCES raw_records (tenant_id, id),
    FOREIGN KEY (tenant_id, resolved_link_id) REFERENCES identity_links (tenant_id, id)
);

CREATE TABLE source_status (
    tenant_id      uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
                   REFERENCES tenants (id),
    source         text NOT NULL,
    feed_offset    bigint NOT NULL DEFAULT 0,
    last_deliver_at timestamptz,
    last_ingest_at timestamptz,
    counters       jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, source)
);
