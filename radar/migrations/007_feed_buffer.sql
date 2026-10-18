-- The demo feed's not-yet-delivered lines, for a deployment with no persistent disk (Ruling Y).
-- line_no is the byte offset just past the line in its feed file, so file and buffer delivery
-- share one cursor (source_status.feed_offset). Delivered rows are deleted.
CREATE TABLE feed_buffer (
    tenant_id  uuid NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid
               REFERENCES tenants (id),
    source     text NOT NULL,
    line_no    bigint NOT NULL,
    deliver_at timestamptz NOT NULL,
    line       text NOT NULL,
    PRIMARY KEY (tenant_id, source, line_no)
);
ALTER TABLE feed_buffer ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON feed_buffer FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, DELETE ON feed_buffer TO radar_worker;
