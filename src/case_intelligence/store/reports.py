"""Report operations on the shared workspace connection and lock."""
from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from . import (
    MAX_REPORT_CITATION_EXCERPT_CHARS,
    MAX_REPORT_SECTION_CITATIONS,
    _MEDIA_CLIP,
    _REPORT,
    _REPORT_SECTION,
    _SOURCE_DOCUMENT,
)
from ..store_records import (
    ReportCitationRecord,
    ReportEditConflict,
    ReportRecord,
    ReportSectionRecord,
    WorkspaceProblem,
)


class ReportsStoreMixin:
    """State-free Report behavior supplied by WorkspaceStore."""

    @staticmethod
    def _report(row: sqlite3.Row) -> ReportRecord:
        values = dict(row)
        values["section_count"] = int(values.get("section_count", 0))
        return ReportRecord(**values)

    @staticmethod
    def _report_section(row: sqlite3.Row) -> ReportSectionRecord:
        values = dict(row)
        values["ordinal"] = int(values["ordinal"])
        return ReportSectionRecord(**values)

    @staticmethod
    def _report_citation(row: sqlite3.Row) -> ReportCitationRecord:
        values = dict(row)
        for name in ("ordinal", "start_ms", "end_ms"):
            values[name] = int(values[name])
        return ReportCitationRecord(**values)

    def create_report(
        self, matter_id: str, actor_id: str, title: str, purpose: str = ""
    ) -> ReportRecord:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        heading = self._derived_text(title, label="Report title", maximum=200)
        description = self._derived_text(
            purpose,
            label="Report purpose",
            maximum=2_000,
            required=False,
            multiline=True,
        )
        report_id = f"report-{uuid.uuid4().hex}"
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            self.connection.execute(
                "INSERT INTO workbench_report("
                "report_id,matter_id,title,purpose,status,created_by,created_at,"
                "updated_by,updated_at) VALUES (?,?,?,?,'draft',?,?,?,?)",
                (report_id, matter_id, heading, description, actor, now, actor, now),
            )
        return self.report(matter_id, report_id)

    def create_report_from_sections(
        self,
        matter_id: str,
        actor_id: str,
        title: str,
        purpose: str,
        *,
        origin_id: str,
        sections: Sequence[Mapping[str, object]],
        transaction_owned: bool = False,
    ) -> ReportRecord:
        """Save a complete converted review atomically, or leave no new Report."""

        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        heading = self._derived_text(title, label="Report title", maximum=200)
        description = self._derived_text(purpose, label="Report purpose", maximum=2_000,
                                      required=False, multiline=True)
        source_id = self._safe_text(origin_id, label="Section origin", maximum=120)
        if not 1 <= len(sections) <= 500:
            raise WorkspaceProblem("A converted Report needs between 1 and 500 sections.")
        prepared = []
        for section in sections:
            if not isinstance(section, Mapping):
                raise WorkspaceProblem("A converted Report section is invalid.")
            if not isinstance(section.get("heading"), str) or not isinstance(section.get("body"), str):
                raise WorkspaceProblem("A converted Report section needs text for its heading and body.")
            section_heading = self._derived_text(section.get("heading"), label="Section heading", maximum=200)
            body = self._derived_text(section.get("body"), label="Section text", maximum=50_000,
                                   required=False, multiline=True)
            basis = section.get("compilation_basis", "")
            if not isinstance(basis, str):
                raise WorkspaceProblem("The compilation basis must be text.")
            basis = self._derived_text(basis, label="Compilation basis", maximum=40_000,
                                    required=False, multiline=True)
            if basis and not body.endswith("\n\nReview basis:\n" + basis):
                raise WorkspaceProblem("The compilation basis does not match its saved section.")
            raw_citations = section.get("citations", ())
            if not isinstance(raw_citations, (list, tuple)) or len(raw_citations) > MAX_REPORT_SECTION_CITATIONS:
                raise WorkspaceProblem(f"A report section can cite up to {MAX_REPORT_SECTION_CITATIONS} passages.")
            try:
                citations = tuple(self._prepare_report_citation(value) for value in raw_citations)
            except (TypeError, ValueError, AttributeError) as exc:
                raise WorkspaceProblem("The converted Report has invalid source support. Open the original run.") from exc
            prepared.append((section_heading, body, basis, citations))

        report_id = f"report-{uuid.uuid4().hex}"
        now = self._now()
        with self._lock, (nullcontext() if transaction_owned else self.connection):
            if transaction_owned:
                if not self.connection.in_transaction:
                    raise RuntimeError("Report creation requires the caller's active transaction")
            else:
                self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor)
            self.connection.execute(
                "INSERT INTO workbench_report("
                "report_id,matter_id,title,purpose,status,created_by,created_at,updated_by,updated_at) "
                "VALUES (?,?,?,?,'draft',?,?,?,?)",
                (report_id, matter_id, heading, description, actor, now, actor, now),
            )
            for ordinal, (section_heading, body, basis, citations) in enumerate(prepared, 1):
                section_id = f"report-section-{uuid.uuid4().hex}"
                self.connection.execute(
                    "INSERT INTO workbench_report_section("
                    "section_id,report_id,matter_id,ordinal,heading,body,origin,origin_id,"
                    "created_by,created_at,updated_by,updated_at,compilation_basis) VALUES (?,?,?,?,?,?,'finding',?,?,?,?,?,?)",
                    (section_id, report_id, matter_id, ordinal, section_heading, body, source_id,
                     actor, now, actor, now, basis),
                )
                self.connection.executemany(
                    "INSERT INTO workbench_report_citation("
                    "citation_id,section_id,report_id,matter_id,ordinal,kind,document_id,"
                    "source_version_id,source_name,location,support_token,excerpt,media_clip_id,"
                    "start_ms,end_ms,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(f"report-citation-{uuid.uuid4().hex}", section_id, report_id, matter_id,
                      citation_ordinal, *citation, now)
                     for citation_ordinal, citation in enumerate(citations, 1)],
                )
            row = self.connection.execute(
                "SELECT r.*,? AS section_count FROM workbench_report r WHERE r.report_id=?",
                (len(prepared), report_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Converted Report was not saved")
            report = self._report(row)
        return report

    def reports(
        self,
        matter_id: str,
        actor_id: str,
        *,
        administrator_override: bool = False,
    ) -> tuple[ReportRecord, ...]:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        with self._lock:
            rows = self.connection.execute(
                "SELECT report.*,COUNT(section.section_id) AS section_count "
                "FROM workbench_report report LEFT JOIN workbench_report_section section "
                "ON section.matter_id=report.matter_id AND section.report_id=report.report_id "
                "WHERE report.matter_id=? GROUP BY report.report_id "
                "ORDER BY report.updated_at DESC,report.report_id DESC",
                (matter_id,),
            ).fetchall()
        return tuple(self._report(row) for row in rows)

    def reports_for_final_bundle(
        self,
        matter_id: str,
        actor_id: str,
        *,
        maximum: int,
        maximum_rows: int,
        maximum_bytes: int,
        administrator_override: bool = False,
    ) -> tuple[
        tuple[
            ReportRecord,
            tuple[tuple[ReportSectionRecord, tuple[ReportCitationRecord, ...]], ...],
        ],
        ...,
    ]:
        """Take one bounded SQLite read snapshot of all saved Report work.

        The savepoint pins the read version across separate SELECTs, including
        writers using another connection. The lock protects this connection.
        Rendering and source resolution happen after releasing both.
        """
        with self._lock:
            self.connection.execute("SAVEPOINT reports_final_bundle")
            try:
                self._authorize_export_read(
                    matter_id,
                    actor_id,
                    administrator_override=administrator_override,
                )
                counts: list[int] = []
                total_bytes = 0
                for table, columns in (
                    ("workbench_report", ("title", "purpose")),
                    ("workbench_report_section", ("heading", "body")),
                    ("workbench_report_citation", ("source_name", "location", "excerpt")),
                ):
                    # Table/column names are fixed above, never request input.
                    lengths = "+".join(
                        f"length(CAST({column} AS BLOB))" for column in columns
                    )
                    row = self.connection.execute(
                        f"SELECT COUNT(*),COALESCE(SUM({lengths}),0) "
                        f"FROM {table} WHERE matter_id=?",
                        (matter_id,),
                    ).fetchone()
                    counts.append(row[0])
                    total_bytes += row[1]
                if (
                    counts[0] > maximum
                    or sum(counts) > maximum_rows
                    or total_bytes > maximum_bytes
                ):
                    raise WorkspaceProblem(
                        "No complete bundle was created because saved Reports exceed "
                        "the export limit. Download Reports individually before closing this matter."
                    )
                reports = self.reports(
                    matter_id, actor_id, administrator_override=administrator_override
                )
                snapshot = tuple(
                    (
                        report,
                        tuple(
                            (
                                section,
                                self.report_citations(
                                    matter_id, report.report_id, section.section_id
                                ),
                            )
                            for section in self.report_sections(matter_id, report.report_id)
                        ),
                    )
                    for report in reports
                )
                # Orphaned/mis-scoped rows must not silently disappear from a
                # bundle advertised as complete, even after damaged imports.
                section_count = sum(len(sections) for _, sections in snapshot)
                citation_count = sum(
                    len(citations)
                    for _, sections in snapshot
                    for _, citations in sections
                )
                if section_count != counts[1] or citation_count != counts[2]:
                    raise WorkspaceProblem(
                        "No complete bundle was created because saved Report sections "
                        "are inconsistent. Contact an administrator before closing this matter."
                    )
                return snapshot
            finally:
                self.connection.execute("RELEASE SAVEPOINT reports_final_bundle")

    def report(self, matter_id: str, report_id: str) -> ReportRecord:
        if not _REPORT.fullmatch(report_id or ""):
            raise KeyError(report_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT report.*,COUNT(section.section_id) AS section_count "
                "FROM workbench_report report LEFT JOIN workbench_report_section section "
                "ON section.matter_id=report.matter_id AND section.report_id=report.report_id "
                "WHERE report.matter_id=? AND report.report_id=? GROUP BY report.report_id",
                (matter_id, report_id),
            ).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self._report(row)

    def _report_for_edit_locked(
        self, matter_id: str, report_id: str, actor_id: str, *,
        expected_updated_at: str | None = None, expected_status: str | None = None,
    ) -> ReportRecord:
        self.membership(matter_id, actor_id)
        current = self.report(matter_id, report_id)
        if expected_updated_at is None and expected_status is None:
            raise ValueError("a displayed Report version or status is required")
        if (expected_updated_at is not None and expected_updated_at != current.updated_at) or (
            expected_status is not None and expected_status != current.status
        ):
            raise ReportEditConflict(
                "This Report changed since the page was opened. Review the saved Report before trying again."
            )
        return current

    def _report_section_for_edit_locked(
        self, matter_id: str, report_id: str, section_id: str, expected_updated_at: str,
    ) -> ReportSectionRecord:
        row = self.connection.execute(
            "SELECT * FROM workbench_report_section WHERE matter_id=? AND report_id=? AND section_id=?",
            (matter_id, report_id, section_id),
        ).fetchone()
        if row is None:
            raise KeyError(section_id)
        current = self._report_section(row)
        if not expected_updated_at or expected_updated_at != current.updated_at:
            raise ReportEditConflict(
                "This section changed since the page was opened. Review the saved section before trying again."
            )
        return current

    def _report_edit_time(self, report: ReportRecord, section: ReportSectionRecord | None = None) -> str:
        values = [report.updated_at] + ([section.updated_at] if section else [])
        previous = max(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values)
        return self._timestamp(max(self.current_time(), previous + timedelta(microseconds=1)))

    def update_report(
        self, matter_id: str, report_id: str, actor_id: str, *,
        title: str, purpose: str, status: str, expected_updated_at: str,
    ) -> ReportRecord:
        if status not in {"draft", "final"}:
            raise WorkspaceProblem("Choose draft or final report status.")
        heading = self._derived_text(title, label="Report title", maximum=200)
        description = self._derived_text(purpose, label="Report purpose", maximum=2_000,
                                      required=False, multiline=True)
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self._report_for_edit_locked(matter_id, report_id, actor_id,
                                                   expected_updated_at=expected_updated_at)
            now = self._report_edit_time(current)
            self.connection.execute(
                "UPDATE workbench_report SET title=?,purpose=?,status=?,updated_by=?,"
                "updated_at=? WHERE matter_id=? AND report_id=?",
                (heading, description, status, actor_id, now, matter_id, report_id),
            )
            return self.report(matter_id, report_id)

    def _prepare_report_citation(
        self, value: Mapping[str, object]
    ) -> tuple[object, ...]:
        kind = str(value.get("kind", "source"))
        if kind not in {"source", "transcript", "media_clip"}:
            raise ValueError("invalid report citation kind")
        document_id = str(value.get("document_id", ""))
        source_version_id = str(value.get("source_version_id", ""))
        if not _SOURCE_DOCUMENT.fullmatch(document_id) or not re.fullmatch(
            r"[0-9a-f]{32}", source_version_id
        ):
            raise ValueError("invalid report citation source")
        support_token = str(value.get("support_token", ""))
        media_clip_id = str(value.get("media_clip_id", ""))
        start_ms = value.get("start_ms", 0)
        end_ms = value.get("end_ms", 0)
        if kind == "media_clip":
            if (
                not _MEDIA_CLIP.fullmatch(media_clip_id)
                or support_token
                or isinstance(start_ms, bool)
                or isinstance(end_ms, bool)
                or not isinstance(start_ms, int)
                or not isinstance(end_ms, int)
                or start_ms < 0
                or end_ms <= start_ms
            ):
                raise ValueError("invalid report media clip citation")
        elif (
            not re.fullmatch(r"[0-9a-f]{40}", support_token)
            or media_clip_id
            or start_ms != 0
            or end_ms != 0
        ):
            raise ValueError("invalid report source citation")
        return (
            kind,
            document_id,
            source_version_id,
            self._safe_text(
                str(value.get("source_name", "")),
                label="Report citation source",
                maximum=2_048,
            ),
            self._safe_text(
                str(value.get("location", "")),
                label="Report citation location",
                maximum=200,
            ),
            support_token,
            self._source_text(
                str(value.get("excerpt", "")),
                label="Report citation excerpt",
                maximum=MAX_REPORT_CITATION_EXCERPT_CHARS,
                required=False,
                multiline=True,
            ),
            media_clip_id,
            start_ms,
            end_ms,
        )

    def add_report_section(
        self,
        matter_id: str,
        report_id: str,
        actor_id: str,
        *,
        expected_status: str,
        heading: str,
        body: str,
        origin: str = "manual",
        origin_id: str = "",
        citations: Sequence[Mapping[str, object]] = (),
    ) -> ReportSectionRecord:
        if origin not in {"manual", "notebook", "answer", "finding", "media_clip"}:
            raise ValueError("invalid report section origin")
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        title = self._derived_text(heading, label="Section heading", maximum=200)
        content = self._derived_text(
            body,
            label="Section text",
            maximum=50_000,
            required=False,
            multiline=True,
        )
        source_id = self._safe_text(
            origin_id,
            label="Section origin",
            maximum=120,
            required=False,
        )
        prepared = tuple(self._prepare_report_citation(item) for item in citations)
        if len(prepared) > 100:
            raise WorkspaceProblem("A report section can cite up to 100 passages.")
        section_id = f"report-section-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current_report = self._report_for_edit_locked(matter_id, report_id, actor,
                                                          expected_status=expected_status)
            now = self._report_edit_time(current_report)
            ordinal = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_report_section "
                    "WHERE matter_id=? AND report_id=?",
                    (matter_id, report_id),
                ).fetchone()[0]
            )
            if ordinal > 500:
                raise WorkspaceProblem("A report can contain up to 500 sections.")
            self.connection.execute(
                "INSERT INTO workbench_report_section("
                "section_id,report_id,matter_id,ordinal,heading,body,origin,origin_id,"
                "created_by,created_at,updated_by,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    section_id,
                    report_id,
                    matter_id,
                    ordinal,
                    title,
                    content,
                    origin,
                    source_id,
                    actor,
                    now,
                    actor,
                    now,
                ),
            )
            self.connection.executemany(
                "INSERT INTO workbench_report_citation("
                "citation_id,section_id,report_id,matter_id,ordinal,kind,document_id,"
                "source_version_id,source_name,location,support_token,excerpt,media_clip_id,"
                "start_ms,end_ms,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"report-citation-{uuid.uuid4().hex}",
                        section_id,
                        report_id,
                        matter_id,
                        citation_ordinal,
                        *citation,
                        now,
                    )
                    for citation_ordinal, citation in enumerate(prepared, 1)
                ],
            )
            self.connection.execute(
                "UPDATE workbench_report SET updated_by=?,updated_at=? "
                "WHERE matter_id=? AND report_id=?",
                (actor, now, matter_id, report_id),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_report_section WHERE section_id=?",
                (section_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("report section creation did not persist")
        return self._report_section(row)

    def report_sections(
        self, matter_id: str, report_id: str
    ) -> tuple[ReportSectionRecord, ...]:
        self.report(matter_id, report_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_report_section WHERE matter_id=? AND report_id=? "
                "ORDER BY ordinal",
                (matter_id, report_id),
            ).fetchall()
        return tuple(self._report_section(row) for row in rows)

    def report_citations(
        self, matter_id: str, report_id: str, section_id: str
    ) -> tuple[ReportCitationRecord, ...]:
        if not _REPORT_SECTION.fullmatch(section_id or ""):
            raise KeyError(section_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_report_citation WHERE matter_id=? "
                "AND report_id=? AND section_id=? ORDER BY ordinal",
                (matter_id, report_id, section_id),
            ).fetchall()
        return tuple(self._report_citation(row) for row in rows)

    def update_report_section(
        self,
        matter_id: str,
        report_id: str,
        section_id: str,
        actor_id: str,
        *,
        expected_updated_at: str,
        expected_status: str,
        heading: str,
        body: str,
    ) -> ReportSectionRecord:
        if not _REPORT_SECTION.fullmatch(section_id or ""):
            raise KeyError(section_id)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        title = self._derived_text(heading, label="Section heading", maximum=200)
        content = self._derived_text(
            body,
            label="Section text",
            maximum=50_000,
            required=False,
            multiline=True,
        )
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current_report = self._report_for_edit_locked(matter_id, report_id, actor,
                                                          expected_status=expected_status)
            current_section = self._report_section_for_edit_locked(matter_id, report_id, section_id,
                                                                   expected_updated_at)
            if current_section.compilation_basis:
                suffix = "\n\nReview basis:\n" + current_section.compilation_basis
                content = self._derived_text(
                    content + suffix, label="Section text", maximum=50_000,
                    required=False, multiline=True,
                )
            now = self._report_edit_time(current_report, current_section)
            changed = self.connection.execute(
                "UPDATE workbench_report_section SET heading=?,body=?,updated_by=?,updated_at=? "
                "WHERE matter_id=? AND report_id=? AND section_id=?",
                (title, content, actor, now, matter_id, report_id, section_id),
            ).rowcount
            if not changed:
                raise KeyError(section_id)
            self.connection.execute(
                "UPDATE workbench_report SET updated_by=?,updated_at=? "
                "WHERE matter_id=? AND report_id=?",
                (actor, now, matter_id, report_id),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_report_section WHERE section_id=?",
                (section_id,),
            ).fetchone()
        if row is None:
            raise KeyError(section_id)
        return self._report_section(row)

    def move_report_section(
        self,
        matter_id: str,
        report_id: str,
        section_id: str,
        actor_id: str,
        direction: str,
        *, expected_updated_at: str,
    ) -> ReportSectionRecord:
        if direction not in {"up", "down"}:
            raise WorkspaceProblem("Choose a valid section movement.")
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current_report = self._report_for_edit_locked(matter_id, report_id, actor,
                                                          expected_updated_at=expected_updated_at)
            now = self._report_edit_time(current_report)
            current = self.connection.execute(
                "SELECT ordinal FROM workbench_report_section WHERE matter_id=? "
                "AND report_id=? AND section_id=?",
                (matter_id, report_id, section_id),
            ).fetchone()
            if current is None:
                raise KeyError(section_id)
            operator = "<" if direction == "up" else ">"
            ordering = "DESC" if direction == "up" else "ASC"
            adjacent = self.connection.execute(
                "SELECT section_id,ordinal FROM workbench_report_section WHERE matter_id=? "
                f"AND report_id=? AND ordinal{operator}? ORDER BY ordinal {ordering} LIMIT 1",
                (matter_id, report_id, int(current["ordinal"])),
            ).fetchone()
            if adjacent is not None:
                temporary = 1_000_000
                self.connection.execute(
                    "UPDATE workbench_report_section SET ordinal=? WHERE section_id=?",
                    (temporary, section_id),
                )
                self.connection.execute(
                    "UPDATE workbench_report_section SET ordinal=? WHERE section_id=?",
                    (int(current["ordinal"]), adjacent["section_id"]),
                )
                self.connection.execute(
                    "UPDATE workbench_report_section SET ordinal=? WHERE section_id=?",
                    (int(adjacent["ordinal"]), section_id),
                )
                self.connection.execute(
                    "UPDATE workbench_report SET updated_by=?,updated_at=? "
                    "WHERE matter_id=? AND report_id=?",
                    (actor, now, matter_id, report_id),
                )
            row = self.connection.execute(
                "SELECT * FROM workbench_report_section WHERE section_id=?",
                (section_id,),
            ).fetchone()
        if row is None:
            raise KeyError(section_id)
        return self._report_section(row)

    def delete_report_section(
        self, matter_id: str, report_id: str, section_id: str, actor_id: str,
        *, expected_updated_at: str, expected_status: str
    ) -> None:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current_report = self._report_for_edit_locked(matter_id, report_id, actor,
                                                          expected_status=expected_status)
            self._report_section_for_edit_locked(matter_id, report_id, section_id, expected_updated_at)
            now = self._report_edit_time(current_report)
            current = self.connection.execute(
                "SELECT ordinal FROM workbench_report_section WHERE matter_id=? "
                "AND report_id=? AND section_id=?",
                (matter_id, report_id, section_id),
            ).fetchone()
            if current is None:
                raise KeyError(section_id)
            self.connection.execute(
                "DELETE FROM workbench_report_section WHERE matter_id=? "
                "AND report_id=? AND section_id=?",
                (matter_id, report_id, section_id),
            )
            later = self.connection.execute(
                "SELECT section_id,ordinal FROM workbench_report_section "
                "WHERE matter_id=? AND report_id=? AND ordinal>? ORDER BY ordinal",
                (matter_id, report_id, int(current["ordinal"])),
            ).fetchall()
            for row in later:
                self.connection.execute(
                    "UPDATE workbench_report_section SET ordinal=? WHERE section_id=?",
                    (int(row["ordinal"]) - 1, row["section_id"]),
                )
            self.connection.execute(
                "UPDATE workbench_report SET updated_by=?,updated_at=? "
                "WHERE matter_id=? AND report_id=?",
                (actor, now, matter_id, report_id),
            )

    def delete_report(
        self, matter_id: str, report_id: str, actor_id: str, *, expected_updated_at: str
    ) -> ReportRecord:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self._report_for_edit_locked(matter_id, report_id, actor,
                                                   expected_updated_at=expected_updated_at)
            self.connection.execute(
                "DELETE FROM workbench_report WHERE matter_id=? AND report_id=?",
                (matter_id, report_id),
            )
            self.connection.execute(
                "DELETE FROM workbench_matter_activity WHERE matter_id=? "
                "AND activity_kind='report' AND object_id=?",
                (matter_id, report_id),
            )
        return current

    def iter_review_decisions_for_report(self, matter_id: str, actor_id: str, run_id: str):
        """Stream one SQLite read snapshot without an export-page population cap.

        A dedicated read-only connection preserves a snapshot without holding
        the shared workspace lock while callers consume and rank decisions.
        Callers must exhaust or close the iterator before starting Report writes.
        """
        self.review_run(matter_id, actor_id, run_id)
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            cursor = connection.execute(
                "SELECT * FROM workbench_review_decision WHERE matter_id=? AND run_id=? ORDER BY ordinal",
                (matter_id, run_id),
            )
            try:
                while rows := cursor.fetchmany(1_000):
                    yield from (self._review_decision(row) for row in rows)
            finally:
                cursor.close()
        finally:
            connection.close()
