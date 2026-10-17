"""Shared, bounded source accounting for briefing and production summary."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode
from .workspace_store import WorkspaceProblem

PAGE_SIZE = 100
SOURCE_LINK_LIMIT = 3

@dataclass(frozen=True)
class RecordedDocumentDate:
    """Semantic document-date metadata from an inventory, bound to its version.

    Arrival times, filesystem mtimes and dates mentioned in text are not this
    metadata. Current inventories do not record semantic dates;
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


def _link(base, label, path="/setup", **query):
    return BriefingLink(label, base + path + ("?" + urlencode(query) if query else ""))


def _sample(links, link):
    if link not in links and len(links) < SOURCE_LINK_LIMIT:
        links.append(link)


def _catalog(workspace, matter_id, *, bounded):
    # A single metadata projection avoids recalculating library-wide facets for
    # every page. The caller holds the authorized workspace/source locks.
    cursor = workspace.connection.execute(
        "SELECT document_id,version_id,action_token,display_name,relative_path,"
        "kind,tone,source_state,state_label FROM workbench_source_catalog "
        "WHERE matter_id=? ORDER BY display_name,document_id", (matter_id,))
    count = 0
    while rows := cursor.fetchmany(PAGE_SIZE):
        count += len(rows)
        bounded(count)
        yield from rows


def _jobs(workspace, matter_id, sources, *, bounded):
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
            bounded(count)
            for row in rows:
                if row["document_id"] in sources:
                    sources[row["document_id"]][field] = decode(row)


def _arrived(base, sources, inventory_dates, coverage, *, bounded):
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
    document_dates, rejected = document_dates_for(sources, inventory_dates, bounded=bounded)
    dated = [(day, sources[key]["link"]) for key, day in document_dates]
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


def _unread(base, matter_id, actor_id, workspace, receipts, sources, readiness, maximum, coverage, *, bounded, receipt_id=None):
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
        batch = ([receipts.get(matter_id, actor_id, receipt_id)] if receipt_offset == 0 else []) if receipt_id else receipts.recent(matter_id, actor_id, limit=PAGE_SIZE, offset=receipt_offset)
        bounded(receipt_offset + len(batch))
        for receipt in batch:
            current_receipt_id = receipt["receipt_id"]
            unrecorded = receipt["counts"]["unrecorded"]
            if unrecorded:
                lines.append(BriefingLine("intake_unrecorded", current_receipt_id, f"{receipt['collection_name']} — {unrecorded} selected files have no recorded intake metadata.", (_link(base, "Review selection receipt", "/intake/" + current_receipt_id),), unrecorded))
            for offset in range(0, receipt["recorded_count"], PAGE_SIZE):
                rows = receipts.items(matter_id, actor_id, current_receipt_id, limit=PAGE_SIZE, offset=offset)
                if len(rows) != min(PAGE_SIZE, receipt["recorded_count"] - offset):
                    raise WorkspaceProblem("The intake inventory changed; no partial briefing was returned.")
                item_count += len(rows)
                bounded(item_count)
                for row in rows:
                    if row["availability"] == "searchable":
                        continue
                    key = (row["catalog_document_id"], row["version_id"])
                    if key in seen_sources:
                        continue
                    href = _link(base, "Review selection receipt", "/intake/" + current_receipt_id, page=row["ordinal"] // 100 + 1)
                    reasons = []
                    if row["upload_message"]:
                        reasons.append(row["upload_message"])
                    if row["selection_state"] == "skipped" and row["reviewed_reason"]:
                        reasons.append(row["reviewed_reason"])
                    if row["preflight_state"] != "valid" and row["reason"]:
                        reasons.append("Filename check: " + row["reason"])
                    lines.append(BriefingLine("intake_incomplete", f"{current_receipt_id}:{row['ordinal']}",
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
        "WHERE u.matter_id=? AND ? IS NULL AND c.document_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM workbench_intake_transfer t WHERE t.matter_id=u.matter_id AND t.upload_item_id=u.upload_item_id) "
        "ORDER BY u.relative_path,u.upload_item_id", (matter_id, receipt_id))
    for index, row in enumerate(cursor, 1):
        bounded(index)
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



def document_dates_for(sources, inventory_dates, *, bounded):
    accepted, rejected = defaultdict(list), 0
    for index, item in enumerate(inventory_dates, 1):
        bounded(index)
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
    dated = [(key, days[0]) for key, days in accepted.items() if len(set(days)) == 1]
    rejected += sum(len(days) for days in accepted.values() if len(set(days)) > 1)
    return dated, rejected
