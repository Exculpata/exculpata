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
  needs_review boolean NOT NULL DEFAULT false,
  PRIMARY KEY (matter_id, version, ordinal),
  UNIQUE (matter_id, claim_id),
  FOREIGN KEY (matter_id, version)
    REFERENCES review_case_theory_version(matter_id, version) ON DELETE CASCADE
);

CREATE TABLE review_case_theory_assertion_owner (
  assertion_id text PRIMARY KEY,
  matter_id text NOT NULL REFERENCES review_matter(matter_id) ON DELETE CASCADE,
  UNIQUE (matter_id, assertion_id)
);

CREATE TABLE review_case_theory_link (
  matter_id text NOT NULL,
  claim_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal > 0),
  kind text NOT NULL CHECK (kind IN ('assertion', 'source_passage')),
  assertion_id text,
  document_id text,
  chunk_id text,
  source_version_id text,
  sha256 text,
  PRIMARY KEY (matter_id, claim_id, ordinal),
  FOREIGN KEY (matter_id, claim_id)
    REFERENCES review_case_theory_claim(matter_id, claim_id) ON DELETE CASCADE,
  FOREIGN KEY (matter_id, document_id, chunk_id)
    REFERENCES review_chunk(matter_id, document_id, chunk_id) ON DELETE CASCADE,
  FOREIGN KEY (matter_id, assertion_id)
    REFERENCES review_case_theory_assertion_owner(matter_id, assertion_id) ON DELETE CASCADE,
  CHECK (
    (kind='assertion' AND assertion_id IS NOT NULL AND document_id IS NULL AND chunk_id IS NULL)
    OR
    (kind='source_passage' AND assertion_id IS NULL AND document_id IS NOT NULL AND chunk_id IS NOT NULL
      AND source_version_id IS NOT NULL AND sha256 IS NOT NULL)
  )
);
CREATE INDEX review_case_theory_link_assertion
  ON review_case_theory_link(matter_id, assertion_id) WHERE assertion_id IS NOT NULL;

CREATE TABLE review_case_theory_link_audit (
  matter_id text NOT NULL REFERENCES review_matter(matter_id) ON DELETE CASCADE,
  claim_id uuid NOT NULL,
  link_ordinal integer NOT NULL,
  assertion_id text,
  document_id text,
  chunk_id text,
  source_version_id text,
  sha256 text,
  removed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  reason text NOT NULL CHECK (reason IN (
    'matter_deleted', 'claim_deleted', 'passage_deleted', 'assertion_deleted', 'explicit_unlink', 'version_rebound'
  ))
);
CREATE INDEX review_case_theory_link_audit_matter ON review_case_theory_link_audit(matter_id);

CREATE FUNCTION audit_case_theory_unlink() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  removal_reason text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM review_matter WHERE matter_id=OLD.matter_id) THEN
    RETURN OLD;
  ELSIF OLD.document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM review_chunk WHERE matter_id=OLD.matter_id
      AND document_id=OLD.document_id AND chunk_id=OLD.chunk_id
  ) THEN
    removal_reason := 'passage_deleted';
  ELSIF NOT EXISTS (
    SELECT 1 FROM review_case_theory_claim WHERE matter_id=OLD.matter_id AND claim_id=OLD.claim_id
  ) THEN
    removal_reason := 'claim_deleted';
  ELSIF OLD.assertion_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM review_case_theory_assertion_owner
      WHERE matter_id=OLD.matter_id AND assertion_id=OLD.assertion_id
  ) THEN
    removal_reason := 'assertion_deleted';
  ELSE
    removal_reason := 'explicit_unlink';
  END IF;
  INSERT INTO review_case_theory_link_audit
    (matter_id, claim_id, link_ordinal, assertion_id, document_id, chunk_id, source_version_id, sha256, reason)
    VALUES (OLD.matter_id, OLD.claim_id, OLD.ordinal, OLD.assertion_id,
            OLD.document_id, OLD.chunk_id, OLD.source_version_id, OLD.sha256, removal_reason);
  UPDATE review_case_theory_claim SET needs_review=true
    WHERE matter_id=OLD.matter_id AND claim_id=OLD.claim_id;
  RETURN OLD;
END;
$$;

CREATE TRIGGER review_case_theory_unlink_audit
  AFTER DELETE ON review_case_theory_link
  FOR EACH ROW EXECUTE FUNCTION audit_case_theory_unlink();

INSERT INTO review_schema_migration(version) VALUES (6) ON CONFLICT DO NOTHING;
COMMIT;
