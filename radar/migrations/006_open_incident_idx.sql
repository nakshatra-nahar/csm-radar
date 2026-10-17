-- At most one open incident per (kind, source): the breaker, a stale source or a manifest
-- mismatch raises one incident, not one per sweep. Writers use ON CONFLICT DO NOTHING.
CREATE UNIQUE INDEX incidents_one_open_idx ON incidents (tenant_id, kind, source)
    NULLS NOT DISTINCT WHERE closed_at IS NULL;
