DROP TRIGGER IF EXISTS discovery_identifier_domain_insert;
CREATE TRIGGER discovery_identifier_domain_insert
BEFORE INSERT ON workbench_discovery_identifier
WHEN length(NEW.identifier) NOT BETWEEN 2 AND 32
 OR NEW.identifier GLOB '*[^A-Za-z0-9_-]*' OR instr(NEW.identifier, char(0)) > 0
 OR substr(NEW.identifier,1,1) NOT GLOB '[A-Za-z]'
 OR substr(NEW.identifier,-1) NOT GLOB '[0-9]'
 OR (length(NEW.identifier)>21 AND substr(NEW.identifier,21) GLOB '*[^0-9]*')
BEGIN SELECT RAISE(ABORT, 'Unsupported Team ID; resolve stored values before retrying upgrade'); END;
DROP TRIGGER IF EXISTS discovery_identifier_domain_update;
CREATE TRIGGER discovery_identifier_domain_update
BEFORE UPDATE ON workbench_discovery_identifier
WHEN length(NEW.identifier) NOT BETWEEN 2 AND 32
 OR NEW.identifier GLOB '*[^A-Za-z0-9_-]*' OR instr(NEW.identifier, char(0)) > 0
 OR substr(NEW.identifier,1,1) NOT GLOB '[A-Za-z]'
 OR substr(NEW.identifier,-1) NOT GLOB '[0-9]'
 OR (length(NEW.identifier)>21 AND substr(NEW.identifier,21) GLOB '*[^0-9]*')

BEGIN SELECT RAISE(ABORT, 'Unsupported Team ID'); END;
CREATE TEMP TABLE IF NOT EXISTS discovery_identifier_validation (identifier TEXT);
CREATE TEMP TRIGGER IF NOT EXISTS discovery_identifier_validate
BEFORE INSERT ON discovery_identifier_validation
BEGIN SELECT RAISE(ROLLBACK, 'Conflicting or unsupported Team ID; resolve stored values before retrying upgrade'); END;
INSERT INTO discovery_identifier_validation
SELECT identifier FROM workbench_discovery_identifier candidate
WHERE length(identifier) NOT BETWEEN 2 AND 32
 OR identifier GLOB '*[^A-Za-z0-9_-]*' OR instr(identifier, char(0)) > 0
 OR substr(identifier,1,1) NOT GLOB '[A-Za-z]'
 OR substr(identifier,-1) NOT GLOB '[0-9]'
 OR (length(identifier)>21 AND substr(identifier,21) GLOB '*[^0-9]*')
 OR EXISTS (SELECT 1 FROM workbench_discovery_identifier other
            WHERE other.matter_id=candidate.matter_id AND other.document_id!=candidate.document_id
              AND other.identifier=candidate.identifier COLLATE NOCASE);
DROP TABLE discovery_identifier_validation;
CREATE UNIQUE INDEX IF NOT EXISTS discovery_identifier_nocase
 ON workbench_discovery_identifier(matter_id, identifier COLLATE NOCASE);
