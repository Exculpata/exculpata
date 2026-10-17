CREATE TABLE IF NOT EXISTS workbench_candidate_review (
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    signature TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'reviewed', 'dismissed')),
    reviewed_by TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    reviewed_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (matter_id, namespace, signature)
);
