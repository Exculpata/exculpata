BEGIN;

ALTER TABLE review_document
  ADD COLUMN IF NOT EXISTS protective_order text NOT NULL DEFAULT 'none';

CREATE TABLE review_case_theory_version (
  matter_id text NOT NULL REFERENCES review_matter(matter_id) ON DELETE CASCADE,
  version integer NOT NULL CHECK (version > 0),
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (matter_id, version)
);

CREATE TABLE review_case_theory_claim (
  matter_id text NOT NULL,
  version integer NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal > 0),
  claim_id uuid NOT NULL,
  text text NOT NULL CHECK (length(btrim(text)) > 0),
  status text NOT NULL CHECK (status IN ('supported', 'contested', 'unsupported')),
  PRIMARY KEY (matter_id, version, ordinal),
  UNIQUE (matter_id, claim_id),
  FOREIGN KEY (matter_id, version)
    REFERENCES review_case_theory_version(matter_id, version) ON DELETE CASCADE
);

CREATE TABLE review_case_theory_link (
  matter_id text NOT NULL,
  claim_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal > 0),
  kind text NOT NULL CHECK (kind IN ('assertion', 'source_passage')),
  assertion_id text,
  document_id text,
  chunk_id text,
  PRIMARY KEY (matter_id, claim_id, ordinal),
  FOREIGN KEY (matter_id, claim_id)
    REFERENCES review_case_theory_claim(matter_id, claim_id) ON DELETE CASCADE,
  FOREIGN KEY (matter_id, document_id, chunk_id)
    REFERENCES review_chunk(matter_id, document_id, chunk_id),
  CHECK (
    (kind='assertion' AND assertion_id IS NOT NULL AND document_id IS NULL AND chunk_id IS NULL)
    OR
    (kind='source_passage' AND assertion_id IS NULL AND document_id IS NOT NULL AND chunk_id IS NOT NULL)
  )
);

CREATE INDEX review_case_theory_link_assertion
  ON review_case_theory_link(matter_id, assertion_id) WHERE assertion_id IS NOT NULL;

INSERT INTO review_schema_migration(version) VALUES (6) ON CONFLICT DO NOTHING;
COMMIT;
