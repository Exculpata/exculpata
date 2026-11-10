CREATE TABLE IF NOT EXISTS workbench_discovery_view (
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('user', 'matter')),
    principal_id TEXT NOT NULL DEFAULT '',
    columns TEXT NOT NULL,
    item_fields TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (matter_id, scope, principal_id)
);
