BEGIN;

ALTER TABLE review_document
  DROP CONSTRAINT IF EXISTS review_document_page_count_check;
ALTER TABLE review_document
  ADD CONSTRAINT review_document_page_count_check
  CHECK (page_count >= 0);

INSERT INTO review_schema_migration(version) VALUES (7) ON CONFLICT DO NOTHING;
COMMIT;
