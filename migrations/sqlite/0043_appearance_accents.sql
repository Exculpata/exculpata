-- Independent appearance choices. Preserve existing Light and retire legacy themes.
CREATE TABLE workbench_principal_preference_new (
    principal_id TEXT PRIMARY KEY REFERENCES workbench_principal(principal_id) ON DELETE CASCADE,
    theme TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('dark', 'light')),
    accent TEXT NOT NULL DEFAULT 'cyan' CHECK (accent IN ('cyan', 'green', 'amber')),
    updated_at TEXT NOT NULL
);
INSERT INTO workbench_principal_preference_new (principal_id, theme, accent, updated_at)
SELECT principal_id, CASE WHEN theme = 'light' THEN 'light' ELSE 'dark' END, 'cyan', updated_at
FROM workbench_principal_preference;
DROP TABLE workbench_principal_preference;
ALTER TABLE workbench_principal_preference_new RENAME TO workbench_principal_preference;
