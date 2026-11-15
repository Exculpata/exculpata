CREATE TABLE IF NOT EXISTS workbench_discovery_production (
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id),
    attempt_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    source_format TEXT NOT NULL,
    root_key TEXT NOT NULL,
    parser_revision TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    target TEXT NOT NULL,
    input_revision TEXT NOT NULL DEFAULT '',
    generation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building','complete','failed','stale')),
    phase TEXT NOT NULL,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    headers_json TEXT NOT NULL DEFAULT '[]',
    accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count>=0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count>=0),
    matched_count INTEGER NOT NULL DEFAULT 0 CHECK (matched_count>=0),
    unmatched_count INTEGER NOT NULL DEFAULT 0 CHECK (unmatched_count>=0),
    conflicted_count INTEGER NOT NULL DEFAULT 0 CHECK (conflicted_count>=0),
    invalidation_serial INTEGER NOT NULL DEFAULT 0 CHECK (invalidation_serial>=0),
    file_failure TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (matter_id,attempt_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS workbench_discovery_production_current
    ON workbench_discovery_production(matter_id,document_id) WHERE status<>'stale';
CREATE INDEX IF NOT EXISTS workbench_discovery_production_root
    ON workbench_discovery_production(matter_id,root_key,source_format,attempt_id);
CREATE TABLE IF NOT EXISTS workbench_discovery_dat_record (
    matter_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal>0),
    line INTEGER NOT NULL CHECK (line>0),
    kind TEXT NOT NULL CHECK (kind IN ('accepted','rejected')),
    headers_json TEXT NOT NULL,
    cells_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    optional_values_json TEXT NOT NULL,
    raw_fingerprint TEXT NOT NULL,
    projection_fingerprint TEXT NOT NULL DEFAULT '',
    association_diagnostics_json TEXT NOT NULL DEFAULT '[]',
    semantic_json TEXT NOT NULL DEFAULT '[]',
    path_key TEXT NOT NULL DEFAULT '',
    begin_key TEXT NOT NULL DEFAULT '',
    end_key TEXT NOT NULL DEFAULT '',
    control_key TEXT NOT NULL DEFAULT '',
    family_begin_key TEXT NOT NULL DEFAULT '',
    family_end_key TEXT NOT NULL DEFAULT '',
    family_prefix TEXT NOT NULL DEFAULT '',
    family_low TEXT NOT NULL DEFAULT '',
    family_high TEXT NOT NULL DEFAULT '',
    family_checked_generation TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '',
    association_status TEXT NOT NULL DEFAULT 'pending',
    generation TEXT NOT NULL DEFAULT '',
    family_root TEXT NOT NULL DEFAULT '',
    family_parent TEXT NOT NULL DEFAULT '',
    family_order TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (matter_id,attempt_id,ordinal),
    FOREIGN KEY (matter_id,attempt_id) REFERENCES workbench_discovery_production(matter_id,attempt_id)
);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_document
    ON workbench_discovery_dat_record(matter_id,document_id,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_path
    ON workbench_discovery_dat_record(matter_id,path_key,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_begin
    ON workbench_discovery_dat_record(matter_id,begin_key,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_control
    ON workbench_discovery_dat_record(matter_id,control_key,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_end
    ON workbench_discovery_dat_record(matter_id,end_key,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_family
    ON workbench_discovery_dat_record(matter_id,family_begin_key,family_end_key,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_interval
    ON workbench_discovery_dat_record(matter_id,family_prefix,family_low,family_high,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_dat_family_members
    ON workbench_discovery_dat_record(matter_id,family_root,family_order,attempt_id,ordinal);
CREATE TABLE IF NOT EXISTS workbench_discovery_opt_page (
    matter_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal>0),
    line INTEGER NOT NULL CHECK (line>0),
    kind TEXT NOT NULL CHECK (kind IN ('accepted','rejected')),
    headers_json TEXT NOT NULL,
    cells_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    optional_values_json TEXT NOT NULL,
    raw_fingerprint TEXT NOT NULL,
    projection_fingerprint TEXT NOT NULL DEFAULT '',
    association_diagnostics_json TEXT NOT NULL DEFAULT '[]',
    semantic_json TEXT NOT NULL DEFAULT '[]',
    page_label_key TEXT NOT NULL DEFAULT '',
    path_key TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '',
    association_status TEXT NOT NULL DEFAULT 'pending',
    generation TEXT NOT NULL DEFAULT '',
    group_ordinal INTEGER CHECK (group_ordinal>0),
    group_key TEXT NOT NULL DEFAULT '',
    supplied_count TEXT NOT NULL DEFAULT '',
    observed_count INTEGER NOT NULL DEFAULT 0 CHECK (observed_count>=0),
    family_root TEXT NOT NULL DEFAULT '',
    family_parent TEXT NOT NULL DEFAULT '',
    family_order TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (matter_id,attempt_id,ordinal),
    FOREIGN KEY (matter_id,attempt_id) REFERENCES workbench_discovery_production(matter_id,attempt_id)
);
CREATE INDEX IF NOT EXISTS workbench_discovery_opt_document
    ON workbench_discovery_opt_page(matter_id,attempt_id,document_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_opt_label
    ON workbench_discovery_opt_page(matter_id,page_label_key,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_opt_group
    ON workbench_discovery_opt_page(matter_id,attempt_id,group_ordinal,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_opt_group_key
    ON workbench_discovery_opt_page(matter_id,group_key,attempt_id,ordinal);
CREATE INDEX IF NOT EXISTS workbench_discovery_candidate_path
    ON workbench_source_catalog(matter_id,display_name_key,document_id);
CREATE INDEX IF NOT EXISTS workbench_discovery_candidate_upload
    ON workbench_upload_item(matter_id,document_id,upload_item_id);
CREATE INDEX IF NOT EXISTS workbench_discovery_candidate_transfer
    ON workbench_intake_transfer(matter_id,upload_item_id,source_version_id,receipt_id);
CREATE INDEX IF NOT EXISTS workbench_discovery_candidate_name
    ON workbench_source_catalog(matter_id,
        substr(display_name_key,length(rtrim(display_name_key,replace(display_name_key,'/','')))+1),document_id);
CREATE INDEX IF NOT EXISTS workbench_discovery_candidate_stem
    ON workbench_source_catalog(matter_id,
        CASE WHEN length(rtrim(substr(display_name_key,length(rtrim(display_name_key,replace(display_name_key,'/','')))+1),
            replace(substr(display_name_key,length(rtrim(display_name_key,replace(display_name_key,'/','')))+1),'.','')))=0
        THEN substr(display_name_key,length(rtrim(display_name_key,replace(display_name_key,'/','')))+1)
        ELSE substr(substr(display_name_key,length(rtrim(display_name_key,replace(display_name_key,'/','')))+1),1,
            length(rtrim(substr(display_name_key,length(rtrim(display_name_key,replace(display_name_key,'/','')))+1),
                replace(substr(display_name_key,length(rtrim(display_name_key,replace(display_name_key,'/','')))+1),'.','')))-1)
        END,document_id);
