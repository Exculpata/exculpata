-- No migration authorizes deletion or invents delivery evidence.
ALTER TABLE workbench_case_exit ADD COLUMN mode TEXT NOT NULL DEFAULT 'export_exit'
    CHECK (mode IN ('export_exit','delete_without_export'));
DROP TABLE workbench_case_confirmation;
CREATE TABLE workbench_case_confirmation (
    token_digest TEXT PRIMARY KEY CHECK (length(token_digest)=64),
    exit_id TEXT NOT NULL REFERENCES workbench_case_exit(exit_id),
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    session_id TEXT NOT NULL REFERENCES workbench_session(session_id),
    name_digest TEXT NOT NULL CHECK (length(name_digest)=64),
    export_id TEXT, archive_digest TEXT, snapshot_digest TEXT,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL CHECK (expires_at > created_at),
    mode TEXT NOT NULL CHECK (mode IN ('export_exit','delete_without_export'))
);
CREATE TABLE workbench_case_download_attempt (
    attempt_id TEXT PRIMARY KEY,
    exit_id TEXT NOT NULL REFERENCES workbench_case_exit(exit_id),
    export_id TEXT NOT NULL, archive_digest TEXT NOT NULL,
    total_size INTEGER NOT NULL CHECK (total_size>0),
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    session_id TEXT NOT NULL REFERENCES workbench_session(session_id),
    administrator INTEGER NOT NULL CHECK (administrator IN (0,1)),
    range_start INTEGER NOT NULL CHECK (range_start>=0),
    range_end INTEGER NOT NULL CHECK (range_end>range_start AND range_end<=total_size),
    created_at TEXT NOT NULL
);
CREATE TABLE workbench_case_download_coverage (
    exit_id TEXT NOT NULL REFERENCES workbench_case_exit(exit_id),
    export_id TEXT NOT NULL, archive_digest TEXT NOT NULL,
    total_size INTEGER NOT NULL CHECK (total_size>0),
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    intervals_json TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (exit_id,export_id,archive_digest,actor_id)
);
CREATE TABLE workbench_case_purge_authority (
    purge_id TEXT PRIMARY KEY,
    exit_id TEXT NOT NULL UNIQUE REFERENCES workbench_case_exit(exit_id),
    mode TEXT NOT NULL CHECK (mode IN ('export_exit','delete_without_export')),
    export_id TEXT, archive_digest TEXT, snapshot_digest TEXT,
    proof_actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    delivery_completed_at TEXT,
    acknowledged_at TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    session_id TEXT NOT NULL REFERENCES workbench_session(session_id),
    administrator INTEGER NOT NULL CHECK (administrator IN (0,1)),
    CHECK (mode='delete_without_export' OR
        (export_id IS NOT NULL AND archive_digest IS NOT NULL AND snapshot_digest IS NOT NULL
         AND delivery_completed_at IS NOT NULL))
);
-- Old binaries reject this policy version before recovering or starting work.
CREATE TABLE workbench_case_policy_v41 (
    matter_id TEXT PRIMARY KEY REFERENCES workbench_matter(matter_id),
    policy_version INTEGER NOT NULL DEFAULT 2 CHECK (policy_version=2),
    mode TEXT NOT NULL CHECK (mode IN ('ongoing','legacy_review','unscheduled')),
    next_check_in_at TEXT, last_checked_at TEXT,
    selected_by TEXT REFERENCES workbench_principal(principal_id),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO workbench_case_policy_v41
    SELECT matter_id,2,mode,next_check_in_at,last_checked_at,selected_by,created_at,updated_at
    FROM workbench_case_policy;
DROP TABLE workbench_case_policy;
ALTER TABLE workbench_case_policy_v41 RENAME TO workbench_case_policy;
DROP TRIGGER workbench_case_writer_insert;
DROP TRIGGER workbench_case_writer_update;
DROP TRIGGER workbench_case_writer_delete;
CREATE TRIGGER workbench_case_writer_insert BEFORE INSERT ON workbench_matter_lifecycle
BEGIN SELECT CASE WHEN recordbench_case_lifecycle_writer() <> 2
    THEN RAISE(ABORT, 'case lifecycle writer version required') END; END;
CREATE TRIGGER workbench_case_writer_update BEFORE UPDATE ON workbench_matter_lifecycle
BEGIN SELECT CASE WHEN recordbench_case_lifecycle_writer() <> 2
    THEN RAISE(ABORT, 'case lifecycle writer version required') END; END;
CREATE TRIGGER workbench_case_writer_delete BEFORE DELETE ON workbench_matter_lifecycle
BEGIN SELECT CASE WHEN recordbench_case_lifecycle_writer() <> 2
    THEN RAISE(ABORT, 'case lifecycle writer version required') END; END;

-- Bind local lifecycle authority to the account revision at fresh sign-in.
CREATE TABLE workbench_case_session_binding (
    session_id TEXT PRIMARY KEY REFERENCES workbench_session(session_id),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest)=64)
);
