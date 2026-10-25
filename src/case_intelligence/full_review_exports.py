"""Every-source exports retain a conservative model label without revision attestation."""
from __future__ import annotations

import csv
import io
import json
from typing import Mapping, Sequence, TYPE_CHECKING

from .answer_presentation import review_rejection_notice
from .branding import PRODUCT_NAME
from .legacy_names import LEGACY_PRODUCT, LEGACY_STEM, name_value
from .store_records import (
    MatterRecord, ReviewCriterionRecord, ReviewCriterionVersionRecord,
    ReviewDecisionRecord, ReviewRunRecord, SourceCatalogRecord,
)

if TYPE_CHECKING:
    from .work_product_exports import ExportArtifact


def export_full_review(
    matter: MatterRecord,
    criterion: ReviewCriterionRecord,
    version: ReviewCriterionVersionRecord,
    run: ReviewRunRecord,
    decisions: Sequence[ReviewDecisionRecord],
    metrics: Mapping[str, object],
    format_name: str,
    *,
    exported_at: str | None = None,
    frozen_text_sources: Sequence[SourceCatalogRecord] | None = None,
) -> ExportArtifact:
    """Export a frozen every-source check without portable internal identifiers."""

    from .work_product_exports import (
        CSV_MEDIA_TYPE, ExportArtifact, ExportBlock, ExportProblem, JSON_MEDIA_TYPE,
        MAX_READABLE_REVIEW_DECISIONS, MAX_WORKFLOW_EXPORT_BYTES, _PORTABLE_REVIEW_METRICS,
        _artifact, _csv_safe, _now, _portable_review_citation, _portable_review_citations,
        _review_citation_text, _staff_label, _validate_review_export_scope, safe_file_stem,
    )
    from .model_qualification import UNVALIDATED_MODEL_LABEL

    model_notice = UNVALIDATED_MODEL_LABEL
    created = exported_at or _now()
    _validate_review_export_scope(matter, criterion, version, run, decisions,
        frozen_text_sources=frozen_text_sources)
    stem = safe_file_stem(f"{matter.display_name}-{criterion.title}-source-check")
    if format_name == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(
            (
                "Ordinal", "Source", "Type", f"{LEGACY_PRODUCT} label", "Rationale",
                "Cited support", "Needs attention", "Validation sample",
                "Staff decision", "Staff note", "Reviewed at",
            )
        )
        for item in decisions:
            citations = _portable_review_citations(item.citations)
            writer.writerow(
                (
                    item.ordinal,
                    _csv_safe(item.source_name),
                    _csv_safe(item.source_kind),
                    _staff_label(item.machine_decision),
                    _csv_safe(model_notice + ": " + review_rejection_notice(item.rationale)),
                    _csv_safe(
                        "; ".join(_review_citation_text(citation) for citation in citations)
                    ),
                    _csv_safe(review_rejection_notice(item.error_message)),
                    "yes" if item.validation_sample else "no",
                    _staff_label(item.human_decision),
                    _csv_safe(item.human_note),
                    item.reviewed_at or "",
                )
            )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        if len(body) > MAX_WORKFLOW_EXPORT_BYTES:
            raise ExportProblem("This review is too large to export as one CSV file.")
        return ExportArtifact(body, CSV_MEDIA_TYPE, stem + ".csv")
    if format_name == "json":
        body = json.dumps(
            {
                "schema": name_value("format_source_check_v1"),
                "product": f"{LEGACY_PRODUCT}",
                "exported_at": created,
                "model_notice": model_notice,
                "matter": {"name": matter.display_name},
                "criterion": {
                    "title": criterion.title,
                    "version": version.version_number,
                    "instructions": version.instructions,
                    "include_guidance": version.include_guidance,
                    "exclude_guidance": version.exclude_guidance,
                },
                "check": {
                    "scope": "Every source"
                    if run.run_kind == "full"
                    else "Representative sample",
                    "status": _staff_label(run.state),
                    "snapshot_count": run.snapshot_count,
                    "reviewed_count": run.reviewed_count,
                    "included_count": run.included_count,
                    "excluded_count": run.excluded_count,
                    "attention_count": run.attention_count,
                    "created_at": run.created_at,
                    "finished_at": run.finished_at,
                },
                "validation_metrics": {
                    key: metrics.get(key) for key in _PORTABLE_REVIEW_METRICS
                },
                "decisions": [
                    {
                        "ordinal": item.ordinal,
                        "source_name": item.source_name,
                        "source_kind": item.source_kind,
                        f"{LEGACY_STEM}_label": _staff_label(item.machine_decision),
                        "rationale": review_rejection_notice(item.rationale),
                        "citations": _portable_review_citations(item.citations),
                        "attention_note": review_rejection_notice(item.error_message),
                        "validation_sample": bool(item.validation_sample),
                        "staff_decision": _staff_label(item.human_decision),
                        "staff_note": item.human_note,
                        "reviewed_at": item.reviewed_at,
                    }
                    for item in decisions
                ],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        if len(body) > MAX_WORKFLOW_EXPORT_BYTES:
            raise ExportProblem("This review is too large to export as one JSON file.")
        return ExportArtifact(body, JSON_MEDIA_TYPE, stem + ".json")
    blocks: list[ExportBlock] = [
        ExportBlock(criterion.title, "title"),
        ExportBlock(model_notice, "note"),
        ExportBlock(matter.display_name, "subtitle"),
        ExportBlock(
            f"Every-source check v{version.version_number} exported from {PRODUCT_NAME} at {created}.",
            "metadata",
        ),
        ExportBlock("Saved criterion", "heading1"),
        ExportBlock(version.instructions),
        ExportBlock("Check totals", "heading1"),
        ExportBlock(f"Frozen searchable population: {run.snapshot_count:,}", "bullet"),
        ExportBlock(f"Included: {run.included_count:,}", "bullet"),
        ExportBlock(f"Not identified: {run.excluded_count:,}", "bullet"),
        ExportBlock(f"Needs attention: {run.attention_count:,}", "bullet"),
        ExportBlock("Human validation", "heading1"),
        ExportBlock(
            f"{metrics.get('reviewed_total', 0)} of {metrics.get('sample_total', 0)} sampled decisions reviewed.",
            "normal",
        ),
    ]
    for label, key in (
        ("Precision", "precision"), ("Recall", "recall"),
        ("Elusion", "elusion"), ("Richness", "richness"), ("Error rate", "error_rate"),
    ):
        value = metrics.get(key)
        blocks.append(
            ExportBlock(f"{label}: {'not yet defined' if value is None else f'{float(value):.1%}'}", "bullet")
        )
    blocks.append(ExportBlock("Source decisions", "heading1"))
    readable_decisions = decisions[:MAX_READABLE_REVIEW_DECISIONS]
    if len(decisions) > len(readable_decisions):
        blocks.append(
            ExportBlock(
                f"This readable summary includes the first {len(readable_decisions):,} of "
                f"{len(decisions):,} source decisions. Use the complete CSV or JSON export "
                "for the full every-source ledger.",
                "note",
            )
        )
    for item in readable_decisions:
        blocks.append(
            ExportBlock(
                f"{item.ordinal}. {item.source_name} — {item.machine_decision.replace('_', ' ').title()}",
                "heading1",
            )
        )
        if item.rationale:
            blocks.append(ExportBlock(review_rejection_notice(item.rationale)))
        if item.human_decision:
            blocks.append(
                ExportBlock(
                    f"Human validation: {item.human_decision.replace('_', ' ').title()}"
                    + (f" — {item.human_note}" if item.human_note else ""),
                    "note",
                )
            )
        for citation in item.citations:
            portable_citation = _portable_review_citation(citation)
            if portable_citation is None:
                continue
            blocks.append(
                ExportBlock(
                    f"Source: {_review_citation_text(portable_citation)}",
                    "citation",
                )
            )
    blocks.append(
        ExportBlock(
            "Exculpata labels do not replace attorney or reviewer judgment. Preserve the criterion version, validate a representative sample, and verify source support.",
            "footer",
        )
    )
    return _artifact(tuple(blocks), stem=stem, format_name=format_name, created_at=created)

