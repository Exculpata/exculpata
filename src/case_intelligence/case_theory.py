"""Matter-fenced storage and validation for versioned, cited case theories."""

from __future__ import annotations

import uuid


STATUSES = frozenset({"supported", "contested", "unsupported"})


class CaseTheoryError(ValueError):
    pass


class CaseTheoryRepository:
    def __init__(self, connection):
        self.connection = connection

    def passage_exists(self, matter_id, document_id, chunk_id):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM review_chunk WHERE matter_id=%s AND document_id=%s AND chunk_id=%s",
                (matter_id, document_id, chunk_id),
            )
            return cursor.fetchone() is not None

    def create(self, matter_id, claims, actor_id, *, assertion_matter=None):
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM review_matter WHERE matter_id=%s FOR UPDATE", (matter_id,)
                )
                if cursor.fetchone() is None:
                    raise CaseTheoryError("Matter not found.")
                cursor.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM review_case_theory_version WHERE matter_id=%s",
                    (matter_id,),
                )
                version = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO review_case_theory_version(matter_id,version,created_by) VALUES (%s,%s,%s)",
                    (matter_id, version, actor_id),
                )
                for claim_ordinal, claim in enumerate(claims, 1):
                    claim_id = uuid.uuid4()
                    cursor.execute(
                        "INSERT INTO review_case_theory_claim VALUES (%s,%s,%s,%s,%s,%s)",
                        (matter_id, version, claim_ordinal, claim_id, claim["text"].strip(), claim["status"]),
                    )
                    for link_ordinal, link in enumerate(claim["links"], 1):
                        if link["kind"] == "assertion" and assertion_matter is not None:
                            owner = assertion_matter(link["assertion_id"])
                            if owner != matter_id:
                                raise CaseTheoryError("A claim cannot cite an assertion from another matter.")
                            cursor.execute(
                                "INSERT INTO review_case_theory_assertion_owner(assertion_id,matter_id) "
                                "VALUES (%s,%s) ON CONFLICT (assertion_id) DO NOTHING",
                                (link["assertion_id"], owner),
                            )
                        cursor.execute(
                            "INSERT INTO review_case_theory_link VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (matter_id, claim_id, link_ordinal, link["kind"], link.get("assertion_id"),
                             link.get("document_id"), link.get("chunk_id")),
                        )
            self.connection.commit()
            return self.read(matter_id, version)
        except Exception:
            self.connection.rollback()
            raise

    def read(self, matter_id, version=None):
        with self.connection.cursor() as cursor:
            if version is None:
                cursor.execute("SELECT MAX(version) FROM review_case_theory_version WHERE matter_id=%s", (matter_id,))
                version = cursor.fetchone()[0]
            if version is None:
                raise CaseTheoryError("Case theory not found.")
            cursor.execute(
                "SELECT version,created_by,created_at FROM review_case_theory_version WHERE matter_id=%s AND version=%s",
                (matter_id, version),
            )
            header = cursor.fetchone()
            if header is None:
                raise CaseTheoryError("Case theory version not found.")
            cursor.execute(
                "SELECT claim_id,text,status FROM review_case_theory_claim WHERE matter_id=%s AND version=%s ORDER BY ordinal",
                (matter_id, version),
            )
            claims = []
            for claim_id, text, status in cursor.fetchall():
                cursor.execute(
                    "SELECT l.kind,l.assertion_id,l.document_id,l.chunk_id,d.protective_order "
                    "FROM review_case_theory_link l LEFT JOIN review_document d ON "
                    "d.matter_id=l.matter_id AND d.document_id=l.document_id "
                    "WHERE l.matter_id=%s AND l.claim_id=%s ORDER BY l.ordinal",
                    (matter_id, claim_id),
                )
                links = []
                for kind, assertion_id, document_id, chunk_id, protective_order in cursor.fetchall():
                    link = {"kind": kind}
                    if kind == "assertion":
                        link["assertion_id"] = assertion_id
                    else:
                        link.update(document_id=document_id, chunk_id=chunk_id)
                        if protective_order != "none":
                            link["protective_order"] = protective_order
                    links.append(link)
                claims.append({"claim_id": str(claim_id), "text": text, "status": status, "links": links})
            return {"version": header[0], "created_by": header[1], "created_at": header[2].isoformat(), "claims": claims}

    def export(self, matter_id):
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT version FROM review_case_theory_version WHERE matter_id=%s ORDER BY version", (matter_id,))
            versions = [row[0] for row in cursor.fetchall()]
        return {"format": "case-theory-v1", "matter_id": matter_id,
                "versions": [self.read(matter_id, version) for version in versions]}


class CaseTheoryService:
    def __init__(self, repository, *, authorize, assertion_matter):
        self.repository = repository
        self.authorize = authorize
        self.assertion_matter = assertion_matter

    def _authorized(self, matter_id):
        if not self.authorize(matter_id):
            raise CaseTheoryError("Matter access denied.")

    def create_version(self, matter_id, claims, actor_id="case-team"):
        self._authorized(matter_id)
        if not claims:
            raise CaseTheoryError("A case theory version needs at least one claim.")
        for claim in claims:
            if claim.get("status") not in STATUSES:
                raise CaseTheoryError("Claim status must be supported, contested or unsupported.")
            if not str(claim.get("text", "")).strip():
                raise CaseTheoryError("Claim text is required.")
            if not claim.get("links"):
                raise CaseTheoryError("Every claim needs at least one citation.")
            for link in claim["links"]:
                if link.get("kind") == "assertion":
                    linked_matter = self.assertion_matter(link.get("assertion_id"))
                    if linked_matter != matter_id:
                        raise CaseTheoryError("A claim cannot cite an assertion from another matter.")
                elif link.get("kind") == "source_passage":
                    if not self.repository.passage_exists(matter_id, link.get("document_id"), link.get("chunk_id")):
                        raise CaseTheoryError("A claim cannot cite a source passage from another matter or an unavailable passage.")
                else:
                    raise CaseTheoryError("Claim citations must identify an assertion or source passage.")
        return self.repository.create(matter_id, claims, actor_id, assertion_matter=self.assertion_matter)

    def current(self, matter_id):
        self._authorized(matter_id)
        return self.repository.read(matter_id)

    def version(self, matter_id, version):
        self._authorized(matter_id)
        return self.repository.read(matter_id, version)

    def list_claims(self, matter_id, version=None):
        return self.current(matter_id)["claims"] if version is None else self.version(matter_id, version)["claims"]

    def export(self, matter_id):
        self._authorized(matter_id)
        return self.repository.export(matter_id)

    def for_report(self, matter_id):
        return self.current(matter_id)
