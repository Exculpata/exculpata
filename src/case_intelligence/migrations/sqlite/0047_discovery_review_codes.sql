CREATE TABLE IF NOT EXISTS workbench_discovery_code (
    code_id TEXT NOT NULL,
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id),
    label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 80 AND label=trim(label)),
    position INTEGER NOT NULL CHECK (position>0),
    active INTEGER NOT NULL CHECK (active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (matter_id,code_id),
    UNIQUE (matter_id,label),
    UNIQUE (matter_id,position)
);
CREATE TABLE IF NOT EXISTS workbench_discovery_code_event (
    event_id INTEGER PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id),
    document_id TEXT NOT NULL,
    code_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('apply','remove')),
    created_by TEXT NOT NULL REFERENCES workbench_principal(principal_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY (matter_id,code_id) REFERENCES workbench_discovery_code(matter_id,code_id)
);
CREATE INDEX IF NOT EXISTS workbench_discovery_code_event_latest
    ON workbench_discovery_code_event(matter_id,document_id,code_id,event_id DESC);
CREATE TRIGGER IF NOT EXISTS workbench_discovery_code_event_immutable
BEFORE UPDATE ON workbench_discovery_code_event BEGIN
    SELECT RAISE(ABORT, 'review code events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS workbench_discovery_code_event_purge_only
BEFORE DELETE ON workbench_discovery_code_event
WHEN NOT EXISTS (SELECT 1 FROM workbench_matter_lifecycle WHERE matter_id=OLD.matter_id AND state='purging')
BEGIN
    SELECT RAISE(ABORT, 'review code events are deleted only with the matter');
END;
CREATE TRIGGER IF NOT EXISTS workbench_discovery_code_purge_only
BEFORE DELETE ON workbench_discovery_code
WHEN NOT EXISTS (SELECT 1 FROM workbench_matter_lifecycle WHERE matter_id=OLD.matter_id AND state='purging')
BEGIN
    SELECT RAISE(ABORT, 'retire review codes instead of deleting them');
END;
