CREATE TABLE IF NOT EXISTS workbench_intake_dat_profile (
    receipt_id TEXT NOT NULL,
    matter_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    producer TEXT NOT NULL,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    fields_json TEXT NOT NULL,
    PRIMARY KEY (receipt_id, ordinal),
    FOREIGN KEY (receipt_id, ordinal, matter_id)
        REFERENCES workbench_intake_item(receipt_id, ordinal, matter_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS workbench_intake_dat_producer
    ON workbench_intake_dat_profile(matter_id, producer);

ALTER TABLE workbench_discovery_production ADD COLUMN profile_positions_json TEXT NOT NULL DEFAULT '[]';
