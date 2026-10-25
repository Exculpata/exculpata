BEGIN;

ALTER TABLE review_case_theory_claim
  DROP CONSTRAINT review_case_theory_claim_status_check,
  ADD CONSTRAINT review_case_theory_claim_status_check
    CHECK (status IN ('supported', 'contested', 'unsupported', 'needs review'));

ALTER TABLE review_case_theory_link
  DROP CONSTRAINT review_case_theory_link_matter_id_document_id_chunk_id_fkey,
  ADD CONSTRAINT review_case_theory_link_passage_fkey
    FOREIGN KEY (matter_id, document_id, chunk_id)
    REFERENCES review_chunk(matter_id, document_id, chunk_id) ON DELETE CASCADE;

CREATE TABLE review_case_theory_assertion_owner (
  assertion_id text PRIMARY KEY,
  matter_id text NOT NULL REFERENCES review_matter(matter_id) ON DELETE CASCADE,
  UNIQUE (matter_id, assertion_id)
);

INSERT INTO review_case_theory_assertion_owner(assertion_id, matter_id)
  SELECT DISTINCT assertion_id, matter_id FROM review_case_theory_link
  WHERE assertion_id IS NOT NULL;

ALTER TABLE review_case_theory_link
  ADD CONSTRAINT review_case_theory_link_assertion_fkey
    FOREIGN KEY (matter_id, assertion_id)
    REFERENCES review_case_theory_assertion_owner(matter_id, assertion_id) ON DELETE CASCADE;

CREATE TABLE review_case_theory_link_audit (
  matter_id text NOT NULL,
  claim_id uuid NOT NULL,
  link_ordinal integer NOT NULL,
  assertion_id text,
  document_id text,
  chunk_id text,
  removed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  reason text NOT NULL CHECK (reason IN (
    'matter_deleted', 'claim_deleted', 'passage_deleted', 'assertion_deleted', 'explicit_unlink'
  ))
);
CREATE INDEX review_case_theory_link_audit_matter ON review_case_theory_link_audit(matter_id);

CREATE FUNCTION audit_case_theory_unlink() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  removal_reason text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM review_matter WHERE matter_id=OLD.matter_id) THEN
    removal_reason := 'matter_deleted';
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
    (matter_id, claim_id, link_ordinal, assertion_id, document_id, chunk_id, reason)
    VALUES (OLD.matter_id, OLD.claim_id, OLD.ordinal, OLD.assertion_id,
            OLD.document_id, OLD.chunk_id, removal_reason);
  UPDATE review_case_theory_claim SET status='needs review'
    WHERE matter_id=OLD.matter_id AND claim_id=OLD.claim_id
      AND NOT EXISTS (
        SELECT 1 FROM review_case_theory_link
        WHERE matter_id=OLD.matter_id AND claim_id=OLD.claim_id
      );
  RETURN OLD;
END;
$$;

CREATE TRIGGER review_case_theory_unlink_audit
  AFTER DELETE ON review_case_theory_link
  FOR EACH ROW EXECUTE FUNCTION audit_case_theory_unlink();

INSERT INTO review_schema_migration(version) VALUES (7) ON CONFLICT DO NOTHING;
COMMIT;
