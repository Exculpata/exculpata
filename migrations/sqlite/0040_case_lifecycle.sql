-- Applied once inside the caller's BEGIN IMMEDIATE before worker startup.
-- Preserve every lifecycle row and legacy date; no migration performs a purge.
CREATE TABLE workbench_matter_lifecycle_v40 (
    matter_id TEXT PRIMARY KEY REFERENCES workbench_matter(matter_id),
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'closing', 'purging', 'purge_failed', 'deleted')),
    purge_id TEXT UNIQUE,
    requested_by TEXT REFERENCES workbench_principal(principal_id),
    requested_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    conversation_count INTEGER NOT NULL DEFAULT 0 CHECK (conversation_count >= 0),
    message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    updated_at TEXT NOT NULL
);
INSERT INTO workbench_matter_lifecycle_v40 SELECT * FROM workbench_matter_lifecycle;
DROP TABLE workbench_matter_lifecycle;
-- Existing admission triggers reference the final name during this atomic swap.
PRAGMA legacy_alter_table = ON;
ALTER TABLE workbench_matter_lifecycle_v40 RENAME TO workbench_matter_lifecycle;
PRAGMA legacy_alter_table = OFF;
CREATE INDEX workbench_matter_lifecycle_state_idx
    ON workbench_matter_lifecycle(state, updated_at DESC);

-- Rebuilding the lifecycle table removes its attached 0012 cleanup trigger.
-- Recreate it in this transaction, without waiting for another startup.
CREATE TRIGGER workbench_deleted_matter_media_cleanup
AFTER UPDATE OF state ON workbench_matter_lifecycle
WHEN NEW.state = 'deleted'
BEGIN
    DELETE FROM workbench_media_clip WHERE matter_id = NEW.matter_id;
    DELETE FROM workbench_media_job WHERE matter_id = NEW.matter_id;
END;

CREATE TABLE workbench_case_policy (
    matter_id TEXT PRIMARY KEY REFERENCES workbench_matter(matter_id),
    policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version=1),
    mode TEXT NOT NULL CHECK (mode IN ('ongoing','legacy_review','unscheduled')),
    next_check_in_at TEXT,
    last_checked_at TEXT,
    selected_by TEXT REFERENCES workbench_principal(principal_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO workbench_case_policy
    (matter_id,mode,next_check_in_at,created_at,updated_at)
SELECT m.matter_id,
    CASE WHEN r.matter_id IS NOT NULL THEN 'legacy_review' ELSE 'unscheduled' END,
    r.expires_at, m.created_at, m.updated_at
FROM workbench_matter m LEFT JOIN workbench_matter_retention r ON r.matter_id=m.matter_id;

CREATE TABLE workbench_case_exit (
    exit_id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id),
    state TEXT NOT NULL CHECK (state IN ('closing','cancelled','confirmed','deleted')),
    requested_by TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    requested_at TEXT NOT NULL,
    eligible_after TEXT NOT NULL CHECK (eligible_after > requested_at),
    export_id TEXT,
    archive_digest TEXT,
    snapshot_digest TEXT,
    exported_by TEXT REFERENCES workbench_principal(principal_id),
    exported_at TEXT,
    confirmed_by TEXT REFERENCES workbench_principal(principal_id),
    confirmed_at TEXT,
    purge_id TEXT UNIQUE,
    updated_at TEXT NOT NULL,
    CHECK ((export_id IS NULL AND archive_digest IS NULL AND snapshot_digest IS NULL)
        OR (export_id IS NOT NULL AND length(archive_digest)=64 AND length(snapshot_digest)=64))
);
CREATE UNIQUE INDEX workbench_case_exit_current_idx ON workbench_case_exit(matter_id)
    WHERE state IN ('closing','confirmed');
CREATE TABLE workbench_case_confirmation (
    token_digest TEXT PRIMARY KEY CHECK (length(token_digest)=64),
    exit_id TEXT NOT NULL REFERENCES workbench_case_exit(exit_id),
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    session_id TEXT NOT NULL REFERENCES workbench_session(session_id),
    name_digest TEXT NOT NULL CHECK (length(name_digest)=64),
    export_id TEXT NOT NULL,
    archive_digest TEXT NOT NULL CHECK (length(archive_digest)=64),
    snapshot_digest TEXT NOT NULL CHECK (length(snapshot_digest)=64),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL CHECK (expires_at > created_at)
);

-- A previously opened old connection, or an old binary reopening this database,
-- lacks this function and fails closed on lifecycle writes. Do not remove these
-- guards to downgrade; restore the pre-upgrade backup into an isolated system.
CREATE TRIGGER workbench_case_writer_insert BEFORE INSERT ON workbench_matter_lifecycle
BEGIN SELECT CASE WHEN recordbench_case_lifecycle_writer() <> 1
    THEN RAISE(ABORT, 'case lifecycle writer version required') END; END;
CREATE TRIGGER workbench_case_writer_update BEFORE UPDATE ON workbench_matter_lifecycle
BEGIN SELECT CASE WHEN recordbench_case_lifecycle_writer() <> 1
    THEN RAISE(ABORT, 'case lifecycle writer version required') END; END;
CREATE TRIGGER workbench_case_writer_delete BEFORE DELETE ON workbench_matter_lifecycle
BEGIN SELECT CASE WHEN recordbench_case_lifecycle_writer() <> 1
    THEN RAISE(ABORT, 'case lifecycle writer version required') END; END;
