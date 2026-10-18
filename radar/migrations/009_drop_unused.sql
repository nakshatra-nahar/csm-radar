-- Schema that no code reads or writes: the source-led identity index (008's index serves
-- every lookup), two models columns, llm_calls.response_id (request_id is the one stored)
-- and the 'snoozed' case status nothing sets.
DROP INDEX identity_links_lookup_idx;
ALTER TABLE models DROP COLUMN algorithm, DROP COLUMN model_card_md;
ALTER TABLE llm_calls DROP COLUMN response_id;
ALTER TABLE cases DROP CONSTRAINT cases_status_check,
    ADD CONSTRAINT cases_status_check
    CHECK (status IN ('open', 'held', 'done', 'dismissed', 'auto_closed'));
