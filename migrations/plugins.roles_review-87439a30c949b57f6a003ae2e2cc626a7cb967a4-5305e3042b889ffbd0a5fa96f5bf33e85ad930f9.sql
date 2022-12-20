ALTER TABLE roles_review.applications ADD COLUMN voting_id BIGINT;
ALTER TABLE roles_review.applications ADD COLUMN decision BOOLEAN;
UPDATE roles_review.applications SET decision = 'false' WHERE resolved;
ALTER TABLE roles_review.applications DROP COLUMN resolved;
