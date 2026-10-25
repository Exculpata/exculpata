CREATE TABLE IF NOT EXISTS workbench_witness_inconsistency_review (
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reviewed', 'dismissed')),
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (matter_id, candidate_id)
);
