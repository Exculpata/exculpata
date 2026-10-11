ALTER TABLE workbench_source_organization ADD COLUMN source_role TEXT NOT NULL DEFAULT 'unclassified'
    CHECK(source_role IN ('unclassified', 'background'));
ALTER TABLE workbench_source_organization ADD COLUMN role_revision INTEGER NOT NULL DEFAULT 0 CHECK(role_revision >= 0);
ALTER TABLE workbench_source_organization ADD COLUMN role_updated_by TEXT REFERENCES workbench_principal(principal_id);
ALTER TABLE workbench_source_organization ADD COLUMN role_updated_at TEXT;
