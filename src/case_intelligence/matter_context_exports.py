"""Complete context work product, called inside the existing export lease."""
import json

from .work_product_exports import ExportArtifact
from .workspace_store import WorkspaceProblem
from .matter_purpose_repository import MatterPurposeRepository


def _qualified(document, model_notice):
    if not model_notice:
        return document
    qualified = dict(document)
    qualified["model_notice"] = model_notice
    return qualified


def add_context_exports(workspace, matter_id, actor_id, administrator_override, add_work_product, model_notice=""):
    from .source_roles import add_source_role_export
    add_source_role_export(workspace, matter_id, actor_id, administrator_override, add_work_product)
    repository = workspace.assertion_repository(export_read=True, administrator_override=administrator_override)
    from .matter_context_repository import MatterContextRepository
    with repository.transaction(matter_id, actor_id):
        manifest = _qualified(MatterContextRepository(repository).export(matter_id), model_notice)
        add_work_product(kind="context_selection", path="context/selections.json",
            artifact=ExportArtifact(body=json.dumps(manifest, indent=2).encode("utf-8"),
                media_type="application/json", filename="selections.json"))
    from .answer_context import AnswerContextRepository
    with repository.transaction(matter_id, actor_id):
        context_jobs = workspace.connection.execute(
            'SELECT job_id FROM workbench_answer_context WHERE matter_id=? ORDER BY job_id LIMIT 1001',
            (matter_id,)).fetchall()
        if len(context_jobs) > 1000:
            raise WorkspaceProblem('Too many context receipts for a complete export.')
        context_bytes = workspace.connection.execute(
            'SELECT coalesce(sum(length(CAST(c.snapshot_json AS BLOB)) + '
            '(SELECT coalesce(sum(length(CAST(a.manifest_json AS BLOB))),0) FROM workbench_answer_context_attempt a WHERE a.job_id=c.job_id)),0) '
            'FROM workbench_answer_context c WHERE c.matter_id=?', (matter_id,)).fetchone()[0]
        if context_bytes > 8 * 1024 * 1024:
            raise WorkspaceProblem('Context receipts exceed the complete export bound. Export individual receipts.')
        for row in context_jobs:
            receipt = _qualified(AnswerContextRepository(workspace).receipt(matter_id, row[0]), model_notice)
            add_work_product(kind='answer_context', path=f'context/{row[0]}.json',
                artifact=ExportArtifact(body=json.dumps(receipt, indent=2).encode('utf-8'),
                    media_type='application/json', filename=row[0] + '.json'))
    with repository.transaction(matter_id, actor_id):
        purpose = MatterPurposeRepository(repository.entities, None).get(matter_id)
        events = [dict(row) for row in workspace.connection.execute(
            "SELECT * FROM workbench_audit_event WHERE matter_id=? AND action='matter.purpose_update' "
            "ORDER BY occurred_at,event_id LIMIT 10001", (matter_id,))]
        if len(events) > 10000:
            raise WorkspaceProblem('Orientation history exceeds the complete export limit.')
        manifest = _qualified(dict(format='exculpata-matter-purpose-v1', consumption='reviewer orientation only',
                        **purpose, audit_events=events), model_notice)
        add_work_product(kind='matter_purpose', path='context/matter-purpose.json',
            artifact=ExportArtifact(body=json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'),
                                    media_type='application/json', filename='matter-purpose.json'))
