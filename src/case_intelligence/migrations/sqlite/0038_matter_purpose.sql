-- Reviewer-authored orientation, independent of descriptors and source roles.
CREATE TABLE IF NOT EXISTS workbench_matter_purpose (
    matter_id TEXT PRIMARY KEY REFERENCES workbench_matter(matter_id) ON DELETE CASCADE,
    objective TEXT NOT NULL CHECK(length(objective)<=500 AND length(CAST(objective AS BLOB))<=1024),
    background TEXT NOT NULL CHECK(length(background)<=10000 AND length(CAST(background AS BLOB))<=24000),
    revision INTEGER NOT NULL CHECK(revision>0),
    updated_by TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    updated_at TEXT NOT NULL
);
