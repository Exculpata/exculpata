CREATE TABLE IF NOT EXISTS workbench_discovery_index_item (
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id),
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    token TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    projection_fingerprint TEXT NOT NULL DEFAULT '',
    source_payload TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    cache_payload TEXT NOT NULL DEFAULT '{}',
    path_key TEXT NOT NULL,
    name_key TEXT NOT NULL,
    stem_key TEXT NOT NULL,
    path_hash TEXT NOT NULL,
    name_hash TEXT NOT NULL,
    stem_hash TEXT NOT NULL,
    readiness TEXT NOT NULL DEFAULT 'pending',
    seen_generation TEXT NOT NULL,
    ready_generation TEXT NOT NULL DEFAULT '',
    is_loadfile INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (matter_id, document_id),
    UNIQUE (matter_id, token)
);
CREATE INDEX IF NOT EXISTS discovery_index_path ON workbench_discovery_index_item(matter_id,path_hash,document_id);
CREATE INDEX IF NOT EXISTS discovery_index_name ON workbench_discovery_index_item(matter_id,name_hash,document_id);
CREATE INDEX IF NOT EXISTS discovery_index_stem ON workbench_discovery_index_item(matter_id,stem_hash,document_id);
CREATE INDEX IF NOT EXISTS discovery_index_generation ON workbench_discovery_index_item(matter_id,seen_generation,document_id);
CREATE TABLE IF NOT EXISTS workbench_discovery_index_value (
    matter_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    field_key TEXT NOT NULL,
    direction INTEGER NOT NULL,
    leading_key BYTEA NOT NULL,
    sort_key BYTEA NOT NULL,
    value TEXT NOT NULL,
    present INTEGER NOT NULL,
    PRIMARY KEY (matter_id,document_id,field_key,direction),
    FOREIGN KEY (matter_id,document_id) REFERENCES workbench_discovery_index_item(matter_id,document_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS discovery_index_order ON workbench_discovery_index_value(matter_id,field_key,direction,leading_key,document_id);
CREATE TABLE IF NOT EXISTS workbench_discovery_index_load_row (
    matter_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    line INTEGER NOT NULL,
    source_order BYTEA NOT NULL,
    order_prefix BYTEA NOT NULL,
    path_value TEXT NOT NULL,
    bates_value TEXT NOT NULL,
    path_name_hash TEXT NOT NULL,
    bates_hash TEXT NOT NULL,
    values_json TEXT NOT NULL,
    attached_document TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (matter_id,source_id,line),
    FOREIGN KEY (matter_id,source_id) REFERENCES workbench_discovery_index_item(matter_id,document_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS discovery_index_attachment ON workbench_discovery_index_load_row(matter_id,attached_document,order_prefix,line);
CREATE INDEX IF NOT EXISTS discovery_index_row_name ON workbench_discovery_index_load_row(matter_id,path_name_hash,source_id,line);
CREATE INDEX IF NOT EXISTS discovery_index_row_bates ON workbench_discovery_index_load_row(matter_id,bates_hash,source_id,line);
CREATE TABLE IF NOT EXISTS workbench_discovery_index_state (
    matter_id TEXT PRIMARY KEY REFERENCES workbench_matter(matter_id),
    epoch TEXT NOT NULL,
    revision INTEGER NOT NULL,
    generation TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    key_version INTEGER NOT NULL,
    total INTEGER NOT NULL,
    indexed INTEGER NOT NULL,
    pending INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    phase TEXT NOT NULL,
    summaries TEXT NOT NULL,
    checkpoint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS discovery_code_change_read ON workbench_discovery_code_event(matter_id,event_id);
