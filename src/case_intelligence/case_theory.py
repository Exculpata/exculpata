"""Matter-scoped storage for versioned claims and their citation receipts."""
from __future__ import annotations

import uuid

STATUSES = frozenset({"supported", "contested", "unsupported"})


class CaseTheoryError(ValueError):
    pass


class CaseTheoryRepository:
    def __init__(self, connection):
        self.connection = connection

    def create(self, matter_id, claims, actor_id, *, assertion_matter=None):
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM review_matter WHERE matter_id=%s FOR UPDATE", (matter_id,))
            if cursor.fetchone() is None:
                raise CaseTheoryError("Matter not found.")
            if not 1 <= len(claims) <= 100:
                raise CaseTheoryError("A version requires between 1 and 100 claims.")
            cursor.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM review_case_theory_version WHERE matter_id=%s",
                (matter_id,),
            )
            version = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO review_case_theory_version(matter_id,version,created_by) VALUES (%s,%s,%s)",
                (matter_id, version, actor_id),
            )
            for ordinal, claim in enumerate(claims, 1):
                if claim.get("status") not in STATUSES:
                    raise CaseTheoryError("Claim status must be supported, contested or unsupported.")
                if not isinstance(claim.get("text"), str) or not claim["text"].strip():
                    raise CaseTheoryError("Claim text is required.")
                if not 1 <= len(claim.get("links", [])) <= 20:
                    raise CaseTheoryError("Every claim needs between 1 and 20 citations.")
                claim_id = uuid.uuid4()
                cursor.execute(
                    "INSERT INTO review_case_theory_claim(matter_id,version,ordinal,claim_id,text,status) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (matter_id, version, ordinal, claim_id, claim["text"].strip(), claim["status"]),
                )
                for link_ordinal, link in enumerate(claim["links"], 1):
                    kind = link.get("kind")
                    assertion = document = chunk = source_version = digest = None
                    if kind == "source_passage":
                        document, chunk = link.get("document_id"), link.get("chunk_id")
                        cursor.execute(
                            "SELECT source_version_id,encode(sha256(convert_to(text,'UTF8')),'hex') "
                            "FROM review_chunk WHERE matter_id=%s AND document_id=%s AND chunk_id=%s FOR SHARE",
                            (matter_id, document, chunk),
                        )
                        passage = cursor.fetchone()
                        if passage is None or passage != (link.get("source_version_id"), link.get("sha256")):
                            raise CaseTheoryError("Source passage is unavailable or its version or digest has changed.")
                        source_version, digest = passage
                    elif kind == "assertion":
                        assertion = link.get("assertion_id")
                        if assertion_matter is None or assertion_matter(assertion) != matter_id:
                            raise CaseTheoryError("Assertion is unavailable or belongs to another matter.")
                        cursor.execute(
                            "INSERT INTO review_case_theory_assertion_owner(assertion_id,matter_id) "
                            "VALUES (%s,%s) ON CONFLICT (assertion_id) DO NOTHING", (assertion, matter_id),
                        )
                    else:
                        raise CaseTheoryError("Citations must identify an assertion or source passage.")
                    cursor.execute(
                        "INSERT INTO review_case_theory_link(matter_id,claim_id,ordinal,kind,assertion_id,"
                        "document_id,chunk_id,source_version_id,sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (matter_id, claim_id, link_ordinal, kind, assertion, document, chunk, source_version, digest),
                    )
            return self.read(matter_id, version)

    def _claims(self, cursor, matter_id, version):
        cursor.execute(
            "SELECT claim_id,text,status,needs_review FROM review_case_theory_claim "
            "WHERE matter_id=%s AND version=%s ORDER BY ordinal FOR SHARE", (matter_id, version),
        )
        claims = {claim_id: dict(claim_id=str(claim_id), text=text, status=status,
                                needs_review=needs_review, links=[], removed_citations=[])
                  for claim_id, text, status, needs_review in cursor.fetchall()}
        cursor.execute(
            "SELECT l.claim_id,l.ordinal,l.kind,l.assertion_id,l.document_id,l.chunk_id,"
            "l.source_version_id,l.sha256,d.protective_order,NULL::text,NULL::timestamptz "
            "FROM review_case_theory_link l JOIN review_case_theory_claim c "
            "ON (c.matter_id,c.claim_id)=(l.matter_id,l.claim_id) LEFT JOIN review_document d "
            "ON (d.matter_id,d.document_id)=(l.matter_id,l.document_id) WHERE c.matter_id=%s AND c.version=%s "
            "UNION ALL SELECT a.claim_id,a.link_ordinal,NULL,a.assertion_id,a.document_id,a.chunk_id,"
            "a.source_version_id,a.sha256,NULL,a.reason,a.removed_at FROM review_case_theory_link_audit a "
            "JOIN review_case_theory_claim c ON (c.matter_id,c.claim_id)=(a.matter_id,a.claim_id) "
            "WHERE c.matter_id=%s AND c.version=%s ORDER BY 11 NULLS FIRST,2",
            (matter_id, version, matter_id, version),
        )
        for claim_id, ordinal, kind, assertion, document, chunk, source_version, digest, restriction, reason, timestamp in cursor.fetchall():
            claim = claims[claim_id]
            if kind is not None:
                link = {"kind": kind}
                if kind == "assertion":
                    link["assertion_id"] = assertion
                else:
                    link.update(document_id=document, chunk_id=chunk, source_version_id=source_version, sha256=digest)
                    if restriction != "none":
                        link["protective_order"] = restriction
                claim["links"].append(link)
            else:
                claim["removed_citations"].append(dict(
                    link_ordinal=ordinal, assertion_id=assertion, document_id=document, chunk_id=chunk,
                    source_version_id=source_version, sha256=digest, reason=reason, removed_at=timestamp.isoformat(),
                ))
        return list(claims.values())

    def _snapshot(self, cursor, matter_id, header):
        return dict(version=header[0], created_by=header[1], created_at=header[2].isoformat(),
                    claims=self._claims(cursor, matter_id, header[0]))

    def read(self, matter_id, version=None):
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT v.version,v.created_by,v.created_at FROM review_case_theory_version v "
                "JOIN review_matter m USING(matter_id) WHERE v.matter_id=%s" +
                (" AND v.version=%s" if version is not None else "") + " ORDER BY v.version DESC LIMIT 1 FOR SHARE OF m",
                (matter_id, version) if version is not None else (matter_id,),
            )
            header = cursor.fetchone()
            if header is None:
                raise CaseTheoryError("Case theory version not found.")
            return self._snapshot(cursor, matter_id, header)

    def export(self, matter_id):
        """Storage export only; application export integration is a later slice."""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT v.version,v.created_by,v.created_at FROM review_case_theory_version v "
                "JOIN review_matter m USING(matter_id) WHERE v.matter_id=%s ORDER BY v.version FOR SHARE OF m",
                (matter_id,),
            )
            headers = cursor.fetchall()
            return dict(format="case-theory-v1", matter_id=matter_id,
                        versions=[self._snapshot(cursor, matter_id, header) for header in headers])


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
