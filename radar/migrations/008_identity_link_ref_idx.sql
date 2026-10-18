-- The latest link of a ref at T, whatever its source (Ruling BB): brief evidence, the account
-- timeline. identity_links_lookup_idx leads with source, which those lookups do not know.
CREATE INDEX identity_links_ref_idx
    ON identity_links (tenant_id, account_ref, available_at DESC, id DESC);
