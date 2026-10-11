"""Deterministic, source-linked discovery briefing; no generation or saved state."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlencode, urlsplit

from .ask_router import classify
from .store_records import MatterRecord
from .workspace_store import AUTOMATIC_DISCOVERY_PRINCIPAL, WorkspaceProblem

# Bound reads explicitly: crossing a bound refuses a partial briefing.
MAX_ROWS = 100_000
# EntityService.date_draft recomputes its complete ordering on each 25-row page.
# Refuse an oversized draft before paging it repeatedly under the source guard.
MAX_DATE_MENTIONS = 5_000
PAGE_SIZE = 100
SOURCE_LINK_LIMIT = 3
MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 2_000


@dataclass(frozen=True)
class RecordedDocumentDate:
    """Semantic document-date metadata from an inventory, bound to its version.

    Arrival times, filesystem mtimes and dates mentioned in text are not this
    metadata. Current RecordBench inventories do not record semantic dates;
    callers must leave this empty unless their inventory explicitly provides it.
    """

    document_id: str
    version_id: str
    value: str
    basis: str = "document_date"


@dataclass(frozen=True)
class BriefingLink:
    label: str
    href: str


@dataclass(frozen=True)
class BriefingLine:
    kind: str
    value: str
    text: str
    links: tuple[BriefingLink, ...]
    count: int | None = None
    suggested: bool = False


@dataclass(frozen=True)
class BriefingSection:
    key: str
    title: str
    lines: tuple[BriefingLine, ...]
    total: int = 0
    omitted: int = 0


@dataclass(frozen=True)
class SuggestedQuestion:
    label: str
    text: str
    links: tuple[BriefingLink, ...]


@dataclass(frozen=True)
class Briefing:
    matter_id: str
    matter_slug: str
    sections: tuple[BriefingSection, ...]
    coverage: tuple[BriefingLine, ...]
    questions: tuple[SuggestedQuestion, ...] = ()

    def section(self, key: str) -> BriefingSection:
        return next(section for section in self.sections if section.key == key)


def _questions(matter_slug: str, sections: tuple[BriefingSection, ...]) -> tuple[SuggestedQuestion, ...]:
    """Round-robin current person/place/date inputs, without pairing subjects.

    Use only the displayed D1 ranks and their current source links. Original
    names and stated dates stay intact; unsuitable text is skipped, never
    shortened or rewritten. Canonical deduplication does not change the text.
    """
    root = "/matters/" + matter_slug
    buckets = {kind: [] for kind in ("person", "place", "mentioned_date")}
    for section in sections:
        for line in section.lines:
            if (line.kind not in buckets or section.key != ("dates" if line.kind == "mentioned_date" else "people_places")
                    or not line.suggested or not line.count or line.count < 0
                    or not isinstance(line.value, str) or not line.value.strip() or not line.value.isprintable()):
                continue
            # D1 emits passage links only for metadata-checked current support;
            # retained entity references and chronology links cannot qualify.
            supported = False
            for link in line.links:
                url = urlsplit(link.href)
                if (not url.scheme and not url.netloc and url.path == root and url.fragment == "support-pane"
                        and any(token.strip() for token in parse_qs(url.query).get("support", ()))):
                    supported = True
                    break
            if not supported:
                continue
            subject = "the mentioned date " + line.value if line.kind == "mentioned_date" else line.value
            text = f"What do the records say about {subject}?"
            if (len(text) > MAX_QUESTION_CHARS or classify(line.value).kind != "question"
                    or classify(text).kind != "question"):
                continue
            buckets[line.kind].append((line, text))
    ranked = [iter(sorted(rows, key=lambda item: (-item[0].count, item[0].value,
              tuple((link.href, link.label) for link in item[0].links)))) for rows in buckets.values()]
    questions, seen = [], set()
    while len(questions) < MAX_QUESTIONS:
        before = len(questions)
        for rows in ranked:
            for line, text in rows:
                # Canonicalize the structured subject, not the rendered text;
                # the fixed template's final punctuation must not retain spaces.
                key = (("the mentioned date " if line.kind == "mentioned_date" else "")
                       + " ".join(line.value.split())).casefold()
                if key not in seen:
                    questions.append(SuggestedQuestion("Suggested", text, line.links))
                    seen.add(key)
                    break
            if len(questions) == MAX_QUESTIONS:
                break
        if len(questions) == before:
            break
    return tuple(questions)


def _link(base, label, path="/setup", **query):
    return BriefingLink(label, base + path + ("?" + urlencode(query) if query else ""))


def _sample(links, link):
    if link not in links and len(links) < SOURCE_LINK_LIMIT:
        links.append(link)


def _bounded(count):
    if count > MAX_ROWS:
        raise WorkspaceProblem("This matter exceeds the complete briefing read limit; no partial briefing was returned.")


def _catalog(workspace, matter_id):
    # A single metadata projection avoids recalculating library-wide facets for
    # every page. The caller holds the authorized workspace/source locks.
    cursor = workspace.connection.execute(
        "SELECT document_id,version_id,action_token,display_name,relative_path,"
        "kind,tone,source_state,state_label FROM workbench_source_catalog "
        "WHERE matter_id=? ORDER BY display_name,document_id", (matter_id,))
    count = 0
    while rows := cursor.fetchmany(PAGE_SIZE):
        count += len(rows)
        _bounded(count)
        yield from rows


def _jobs(workspace, matter_id, sources):
    # Read each job inventory once, restricted to the current source catalog.
    # Preserve the store's decoding and media preflight display semantics.
    for table, field, decode, version in (
        ("workbench_ingest_job", "ingest", workspace._job, ""),
        ("workbench_media_job", "media", workspace._media_job, " AND j.source_version_id=c.version_id"),
    ):
        cursor = workspace.connection.execute(
            f"SELECT j.* FROM {table} j JOIN workbench_source_catalog c "
            "ON c.matter_id=j.matter_id AND c.document_id=j.document_id" + version +
            " WHERE c.matter_id=?", (matter_id,))
        count = 0
        while rows := cursor.fetchmany(PAGE_SIZE):
            count += len(rows)
            _bounded(count)
            for row in rows:
                sources[row["document_id"]][field] = decode(row)


def _arrived(base, sources, inventory_dates, coverage):
    all_sources = _link(base, "Review all sources", view="list")
    lines = [BriefingLine("source_count", "", f"{len(sources)} sources in the source inventory.", (all_sources,), len(sources))]
    for field, kind, title in (("kind", "file_type", "File type"), ("folder", "folder", "Top-level folder")):
        grouped = defaultdict(list)
        for source in sources.values():
            grouped[source[field]].append(source["link"])
        for value, links in sorted(grouped.items()):
            label = value or "Root (no folder)"
            browse = _link(base, "Review this file type", view="list", kind=value) if kind == "file_type" else (
                _link(base, "Review this folder", view="list", folder=value) if value else all_sources)
            lines.append(BriefingLine(kind, value, f"{title}: {label} — {len(links)} sources.",
                                      (*links[:SOURCE_LINK_LIMIT], browse), len(links)))
    accepted, rejected = defaultdict(list), 0
    for index, item in enumerate(inventory_dates, 1):
        _bounded(index)
        source = sources.get(item.document_id)
        try:
            day = date.fromisoformat(item.value)
            valid = item.value == day.isoformat()
        except (ValueError, TypeError):
            valid = False
        if not valid or item.basis != "document_date" or not source or source["version_id"] != item.version_id:
            rejected += 1
            continue
        accepted[item.document_id].append(day)
    # Conflicting dates for one version have no established single document date.
    dated = [(days[0], sources[key]["link"]) for key, days in accepted.items() if len(set(days)) == 1]
    rejected += sum(len(days) for days in accepted.values() if len(set(days)) > 1)
    if dated:
        for kind, label, day in (("earliest_document_date", "Earliest recorded document date", min(day for day, _ in dated)),
                                 ("latest_document_date", "Latest recorded document date", max(day for day, _ in dated))):
            links = tuple(link for value, link in sorted(dated, key=lambda item: (item[0], item[1].href)) if value == day)
            lines.append(BriefingLine(kind, day.isoformat(), f"{label}: {day.isoformat()}.", links[:SOURCE_LINK_LIMIT]))
    else:
        lines.append(BriefingLine("document_dates_unavailable", "", "Earliest and latest document dates are unavailable: no current semantic document-date metadata is recorded.", (all_sources,)))
    missing = len(sources) - len(dated)
    if missing or rejected:
        coverage.append(BriefingLine("document_date_coverage", "", f"Document dates cover {len(dated)} of {len(sources)} sources; {rejected} invalid, stale, conflicting or non-document date records were excluded. Arrival times, filesystem times and dates mentioned in text are not document dates.", (all_sources,), missing))
    return BriefingSection("arrived", "What arrived", tuple(lines), len(sources))


def _automatic_reference(repo, query, params, name, service):
    cursor = repo.connection.execute("SELECT m.* " + query +
        "AND e.display_name=? ORDER BY m.source_name,m.unit_number,m.start_offset,m.mention_id", (*params, name))
    first, count = None, 0
    while rows := cursor.fetchmany(PAGE_SIZE):
        count += len(rows)
        _bounded(count)
        mentions = [dict(row) for row in rows]
        first = first or mentions[0]
        available = service.current_sources(mentions)
        if available:
            return mentions[min(available)], True
    return first, False


def _suggestions(base, matter_id, actor_id, service, maximum, coverage):
    lines, total = [], 0
    # Inbox counts mix automatic, run-based and answer-triggered discovery. Keep
    # this summary on occurrences recorded by the automatic system principal.
    query = ("FROM workbench_entity_mention m JOIN workbench_entity e ON e.entity_id=m.entity_id "
             "AND e.matter_id=m.matter_id WHERE e.matter_id=? AND e.status='suggested' "
             "AND e.origin='extraction' AND e.created_by=? AND e.entity_type=? "
             "AND m.origin='extraction' AND m.created_by=? AND m.review_status<>'dismissed' ")
    for entity_type in ("person", "place"):
        params = (matter_id, AUTOMATIC_DISCOVERY_PRINCIPAL, entity_type, AUTOMATIC_DISCOVERY_PRINCIPAL)
        with service.repository.reading(matter_id, actor_id) as repo:
            groups = repo.connection.execute("SELECT COUNT(*) FROM (SELECT e.display_name " + query + "GROUP BY e.display_name)", params).fetchone()[0]
            _bounded(groups)
            rows = repo.connection.execute(
                "SELECT e.display_name,COUNT(*) AS mentions,COUNT(DISTINCT m.document_id) AS sources "
                + query + "GROUP BY e.display_name ORDER BY mentions DESC,e.display_name LIMIT ?", (*params, maximum)).fetchall()
            samples = [(dict(row), *_automatic_reference(repo, query, params, row["display_name"], service)) for row in rows]
        total += groups
        for row, mention, available in samples:
            link = _link(base, "Open original passage", "", support=mention["support_token"])
            if available:
                link = BriefingLink(link.label, link.href + "#support-pane")
            else:
                link = _link(base, "Review retained source references", "/entities/" + mention["entity_id"])
            lines.append(BriefingLine(entity_type, row["display_name"],
                f"Suggested {entity_type}: {row['display_name']} — {row['mentions']} recorded mentions in {row['sources']} sources.",
                (link, _link(base, "Review this name", "/entities", q=row["display_name"])), row["mentions"], True))
    lines.sort(key=lambda line: (-line.count, line.value, line.kind))
    if not lines:
        lines.append(BriefingLine("empty", "", "No automatically suggested people or places are recorded.", (_link(base, "Review people and things", "/entities"),)))
    omitted = max(total - sum(line.kind != "empty" for line in lines), 0)
    coverage.append(BriefingLine("suggestion_coverage", "", f"Suggested ranks count recorded automatic mentions, including retained historical support; human decisions remain separate. {omitted} additional people/place groups are not displayed.", (_link(base, "Review suggestions", "/entities"),), omitted))
    return BriefingSection("people_places", "Who and what appears most", tuple(lines), total, omitted)


def _dates(base, matter_id, actor_id, service, maximum, coverage):
    groups, offset, page, total = {}, 0, 1, None
    while total is None or offset < total:
        items, found, _ordered = service.date_draft(matter_id, actor_id, page=page)
        if found > MAX_DATE_MENTIONS:
            raise WorkspaceProblem("This matter exceeds the complete found-date briefing limit; no partial briefing was returned.")
        _bounded(found)
        if total is not None and total != found:
            raise WorkspaceProblem("Found dates changed while the briefing was read. Try again.")
        total = found
        if not items and offset < total:
            raise WorkspaceProblem("The found-date inventory is incomplete; no partial briefing was returned.")
        for item in items:
            # Calendar day as stated, without inventing a timezone or resolving
            # ambiguous slash dates. Different ambiguous spellings stay separate.
            value = item["stated"][:10] if item["ordered"] else item["stated"]
            key = (bool(item["ordered"]), value)
            group = groups.setdefault(key, dict(count=0, links=[]))
            group["count"] += 1
            if item["available"]:
                _sample(group["links"], BriefingLink(item["source_name"], base + "?" + urlencode({"support": item["support_token"]}) + "#support-pane"))
        offset += len(items)
        page += 1
    ranked = sorted(groups.items(), key=lambda pair: (-pair[1]["count"], not pair[0][0], pair[0][1]))
    lines = []
    timeline = _link(base, "Review all found dates", "/chronology")
    for (ordered, value), group in ranked[:maximum]:
        qualifier = "as stated" if ordered else "calendar order unresolved"
        lines.append(BriefingLine("mentioned_date", value, f"Suggested date: {value} — {group['count']} found mentions ({qualifier}).",
                                  (*group["links"], timeline), group["count"], True))
    if not lines:
        lines.append(BriefingLine("empty", "", "No automatically found dates are recorded.", (timeline,)))
    omitted = max(len(groups) - maximum, 0)
    coverage.append(BriefingLine("date_coverage", "", f"Date ranks count all {total} retained, non-dismissed found mentions; these are not document dates or established events. {omitted} additional date groups are not displayed.", (timeline,), omitted))
    return BriefingSection("dates", "Busiest dates", tuple(lines), len(groups), omitted)


def _unread(base, matter_id, actor_id, workspace, receipts, sources, readiness, maximum, coverage):
    lines, seen_sources = [], set()
    for document_id, source in sources.items():
        ingest, media = source.get("ingest"), source.get("media")
        active = next((job for job in (ingest, media) if job and job.state in ("queued", "running")), None)
        failed = next((job for job in (ingest, media) if job and job.state in ("failed", "cancelled")), None)
        if active:
            state, reason = "processing", active.message or active.stage
        elif failed:
            state, reason = getattr(failed, "display_state", failed.state), failed.message or failed.stage
        elif source["tone"] != "ready":
            state, reason = source["state"], source["reason"]
        elif source["reason"] and source["reason"] != "Searchable":
            state, reason = "coverage notice", source["reason"]
        else:
            continue
        seen_sources.add((document_id, source["version_id"]))
        lines.append(BriefingLine("source_incomplete", document_id, f"{source['name']} — {state}: {reason or 'No reason was recorded.'}", (source["link"],), 1))
    receipt_offset, item_count = 0, 0
    while True:
        batch = receipts.recent(matter_id, actor_id, limit=PAGE_SIZE, offset=receipt_offset)
        _bounded(receipt_offset + len(batch))
        for receipt in batch:
            receipt_id = receipt["receipt_id"]
            unrecorded = receipt["counts"]["unrecorded"]
            if unrecorded:
                lines.append(BriefingLine("intake_unrecorded", receipt_id, f"{receipt['collection_name']} — {unrecorded} selected files have no recorded intake metadata.", (_link(base, "Review selection receipt", "/intake/" + receipt_id),), unrecorded))
            for offset in range(0, receipt["recorded_count"], PAGE_SIZE):
                rows = receipts.items(matter_id, actor_id, receipt_id, limit=PAGE_SIZE, offset=offset)
                if len(rows) != min(PAGE_SIZE, receipt["recorded_count"] - offset):
                    raise WorkspaceProblem("The intake inventory changed; no partial briefing was returned.")
                item_count += len(rows)
                _bounded(item_count)
                for row in rows:
                    if row["availability"] == "searchable":
                        continue
                    key = (row["catalog_document_id"], row["version_id"])
                    if key in seen_sources:
                        continue
                    href = _link(base, "Review selection receipt", "/intake/" + receipt_id, page=row["ordinal"] // 100 + 1)
                    reasons = []
                    if row["upload_message"]:
                        reasons.append(row["upload_message"])
                    if row["selection_state"] == "skipped" and row["reviewed_reason"]:
                        reasons.append(row["reviewed_reason"])
                    if row["preflight_state"] != "valid" and row["reason"]:
                        reasons.append("Filename check: " + row["reason"])
                    lines.append(BriefingLine("intake_incomplete", f"{receipt_id}:{row['ordinal']}",
                        f"{row['relative_path'] or row['display_name']} — {row['availability']}: {' '.join(reasons) or 'No further reason was recorded.'}", (href,), 1))
        receipt_offset += len(batch)
        if len(batch) < PAGE_SIZE:
            break
    # Legacy uploads may lack selection receipts. Read them directly within the
    # already-authorized workspace lock; no upload-session/actor default limit.
    cursor = workspace.connection.execute(
        "SELECT u.upload_item_id,u.upload_session_id,u.document_id,u.relative_path,u.state,u.message FROM workbench_upload_item u "
        "JOIN workbench_upload_session s ON s.upload_session_id=u.upload_session_id AND s.matter_id=u.matter_id "
        "LEFT JOIN workbench_source_catalog c ON c.document_id=u.document_id AND c.matter_id=u.matter_id "
        "WHERE u.matter_id=? AND c.document_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM workbench_intake_transfer t WHERE t.matter_id=u.matter_id AND t.upload_item_id=u.upload_item_id) "
        "ORDER BY u.relative_path,u.upload_item_id", (matter_id,))
    for index, row in enumerate(cursor, 1):
        _bounded(index)
        lines.append(BriefingLine("upload_incomplete", row["upload_item_id"],
            f"{row['relative_path']} — {row['state']}: {row['message'] or 'No reason was recorded.'}",
            (_link(base, "Review this upload", "/upload-review/" + row["upload_session_id"] + "/" + row["upload_item_id"]),), 1))
    lines.sort(key=lambda line: (line.text, line.kind, line.value))
    total = sum(line.count or 0 for line in lines)
    omitted = sum(line.count or 0 for line in lines[maximum:])
    displayed = tuple(lines[:maximum]) or (BriefingLine("empty", "", "No incomplete intake or extraction items are recorded.", (_link(base, "Review all sources", view="list"),), 0),)
    coverage.append(BriefingLine("readiness", readiness.state,
        f"Matter readiness: {readiness.searchable_count} searchable, {readiness.processing_count} processing, {readiness.attention_count} needing attention; skipped selection items are additional. {omitted} incomplete items are not displayed.",
        (_link(base, "Review source readiness", view="list"),), omitted))
    return BriefingSection("unread", "What could not be read and why", displayed, total, omitted)


def build_briefing(
    matter: MatterRecord,
    actor_id: str,
    *,
    workspace,
    entity_service,
    intake_receipts,
    inventory: Iterable[RecordedDocumentDate] = (),
    source_metadata: Callable[[str], object] | None = None,
    max_ranked: int = 5,
    max_incomplete: int = 50,
) -> Briefing:
    """Assemble existing records under their source and membership boundaries.

    Collaborators must belong to this matter/workspace. No source bytes, model,
    generation, schema changes or persisted briefing are involved. Page reads
    are exhaustive up to MAX_ROWS; a larger input is refused explicitly.
    """
    if type(max_ranked) is not int or not 1 <= max_ranked <= 25 or type(max_incomplete) is not int or not 1 <= max_incomplete <= 100:
        raise ValueError("Use 1–25 ranked entries and 1–100 incomplete entries.")
    if entity_service.repository.connection is not workspace.connection or intake_receipts.workspace is not workspace:
        raise ValueError("Briefing collaborators must share the same workspace.")
    if entity_service.current_sources is None:
        raise ValueError("Briefings require a current-source metadata checker.")
    base = "/matters/" + matter.slug
    coverage = []
    with entity_service.source_guard(), workspace._lock:
        workspace.membership(matter.matter_id, actor_id)
        if workspace.get_matter_by_id(matter.matter_id).slug != matter.slug:
            raise KeyError(matter.slug)
        version = workspace.connection.execute("PRAGMA data_version").fetchone()[0]
        sources = {}
        for row in _catalog(workspace, matter.matter_id):
            reason = row["state_label"]
            if source_metadata is not None and row["tone"] != "ready":
                try:
                    document = source_metadata(row["document_id"])
                except (KeyError, OSError, RuntimeError):
                    document = None
                if (document is not None and document.document_id == row["document_id"]
                        and document.version_id == row["version_id"] and document.message):
                    reason = document.message
            sources[row["document_id"]] = dict(version_id=row["version_id"], kind=row["kind"],
                folder=row["relative_path"].split("/", 1)[0] if "/" in row["relative_path"] else "",
                link=_link(base, row["display_name"], "/sources/" + row["action_token"]),
                tone=row["tone"], state=row["source_state"], reason=reason, name=row["relative_path"] or row["display_name"])
        _jobs(workspace, matter.matter_id, sources)
        readiness = workspace.matter_readiness(matter.matter_id)
        sections = (
            _arrived(base, sources, inventory, coverage),
            _suggestions(base, matter.matter_id, actor_id, entity_service, max_ranked, coverage),
            _dates(base, matter.matter_id, actor_id, entity_service, max_ranked, coverage),
            _unread(base, matter.matter_id, actor_id, workspace, intake_receipts, sources, readiness, max_incomplete, coverage),
        )
        workspace.membership(matter.matter_id, actor_id)
        if workspace.connection.execute("PRAGMA data_version").fetchone()[0] != version:
            raise WorkspaceProblem("Matter records changed while the briefing was read. Try again.")
    coverage.append(BriefingLine("source_link_coverage", "", f"Each count covers its complete group; up to {SOURCE_LINK_LIMIT} example source links are shown alongside the existing review pages.", (_link(base, "Review all sources", view="list"),)))
    coverage.append(BriefingLine("extraction_coverage", "", "Readiness and recorded failures do not establish that every page, image, attachment or media segment was read. Review each source's extraction and coverage notices.", (_link(base, "Review source coverage", view="list"),)))
    return Briefing(matter.matter_id, matter.slug, sections, tuple(coverage), _questions(matter.slug, sections))
