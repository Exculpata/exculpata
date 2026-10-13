-- Ephemeral, operation-bound SSO evidence. Never enrollment or deletion consent.
CREATE TABLE IF NOT EXISTS workbench_case_oidc_request (
    state_digest TEXT PRIMARY KEY REFERENCES workbench_oidc_transaction(state_digest) ON DELETE CASCADE,
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    session_id TEXT NOT NULL REFERENCES workbench_session(session_id),
    subject_digest TEXT NOT NULL, configuration_digest TEXT NOT NULL,
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id),
    exit_id TEXT NOT NULL, mode TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('begin','purge')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_case_oidc_authorization (
    authorization_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    session_id TEXT NOT NULL REFERENCES workbench_session(session_id),
    subject_digest TEXT NOT NULL, configuration_digest TEXT NOT NULL,
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id),
    exit_id TEXT NOT NULL, mode TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('begin','purge')),
    authenticated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready','claimed')),
    purge_id TEXT,
    UNIQUE (session_id,matter_id,exit_id,action),
    CHECK (expires_at > authenticated_at)
);
