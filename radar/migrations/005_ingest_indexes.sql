-- Lookups ingestion makes per record (tenant_id leads, as in design §4.4).
-- Identity: a Gmail counterparty by contact email, a corporate domain to its account.
CREATE INDEX contacts_email_idx ON contacts (tenant_id, email);
CREATE INDEX accounts_domain_idx ON accounts (tenant_id, domain);
-- A new link closes the ref's open quarantine rows; retries scan only open rows.
CREATE INDEX quarantine_open_ref_idx ON quarantine (tenant_id, account_ref)
    WHERE resolved_at IS NULL;
