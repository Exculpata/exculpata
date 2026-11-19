CREATE TABLE IF NOT EXISTS workbench_matter_model_grant (
    grant_id TEXT NOT NULL UNIQUE,
    matter_id TEXT NOT NULL REFERENCES workbench_matter(matter_id) ON DELETE CASCADE,
    capability TEXT NOT NULL CHECK (capability IN ('generation','embedding','reranking')),
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK (approved IN (0,1)),
    configuration_id TEXT NOT NULL,
    PRIMARY KEY (matter_id,capability,endpoint,model)
);

CREATE TABLE IF NOT EXISTS workbench_model_destination (
    capability TEXT PRIMARY KEY CHECK (capability IN ('generation','embedding','reranking')),
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    configuration_id TEXT NOT NULL
);
