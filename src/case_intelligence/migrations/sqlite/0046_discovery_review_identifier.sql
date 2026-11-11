CREATE TABLE IF NOT EXISTS workbench_discovery_identifier (
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    identifier TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (matter_id, document_id),
    UNIQUE (matter_id, identifier)
);
