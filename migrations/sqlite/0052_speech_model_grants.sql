ALTER TABLE workbench_matter_model_grant RENAME TO prior_matter_model_grant;
ALTER TABLE workbench_model_destination RENAME TO prior_model_destination;

CREATE TABLE workbench_matter_model_grant (
    grant_id TEXT NOT NULL UNIQUE,
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id) ON DELETE CASCADE,
    capability TEXT NOT NULL CHECK (capability IN ('generation','embedding','reranking','transcription','alignment','translation','speaker_labeling')),
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK (approved IN (0,1)),
    configuration_id TEXT NOT NULL,
    PRIMARY KEY (matter_id,capability,endpoint,model)
);

CREATE TABLE workbench_model_destination (
    capability TEXT PRIMARY KEY CHECK (capability IN ('generation','embedding','reranking','transcription','alignment','translation','speaker_labeling')),
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    configuration_id TEXT NOT NULL
);

INSERT INTO workbench_matter_model_grant
    (grant_id,matter_id,capability,endpoint,model,approved,configuration_id)
    SELECT grant_id,matter_id,capability,endpoint,model,approved,configuration_id
    FROM prior_matter_model_grant;
INSERT INTO workbench_model_destination (capability,endpoint,model,configuration_id)
    SELECT capability,endpoint,model,configuration_id FROM prior_model_destination;
DROP TABLE prior_matter_model_grant;
DROP TABLE prior_model_destination;
