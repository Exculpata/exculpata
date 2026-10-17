BEGIN;
-- Portable review-state schema; the control SQLite store remains authoritative.
CREATE TABLE IF NOT EXISTS review_candidate_review (
    matter_id TEXT NOT NULL REFERENCES review_matter(matter_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    signature TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'reviewed', 'dismissed')),
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (matter_id, namespace, signature)
);
INSERT INTO review_schema_migration(version) VALUES (6) ON CONFLICT DO NOTHING;
COMMIT;
