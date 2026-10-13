"""Database fences for frozen content, including writers with stale UI reads."""
import re

# These records describe authority or the exit itself; permission revocation and
# content-free auditing must remain possible while matter content is frozen.
_UNFENCED = frozenset({
    'workbench_matter_lifecycle', 'workbench_case_policy', 'workbench_case_exit',
    'workbench_case_confirmation', 'workbench_audit_event',
    'workbench_case_download_attempt', 'workbench_case_download_coverage',
    'workbench_case_purge_authority', 'workbench_case_session_binding',
    'workbench_case_oidc_request', 'workbench_case_oidc_authorization',
    'workbench_matter_membership', 'workbench_matter_group_grant',
    'workbench_matter_activity', 'workbench_job_matter_schedule',
})
_CHILDREN = {
    'workbench_message': ('conversation_id', 'workbench_conversation', 'conversation_id'),
    'workbench_ingest_plan_item': ('plan_id', 'workbench_ingest_plan', 'plan_id'),
    'workbench_answer_event': ('job_id', 'workbench_answer_job', 'job_id'),
    'workbench_answer_context_attempt': ('job_id', 'workbench_answer_context', 'job_id'),
    'workbench_research_event': ('job_id', 'workbench_research_job', 'job_id'),
    'workbench_review_event': ('run_id', 'workbench_review_run', 'run_id'),
    **{table: ('run_id', 'workbench_review_run', 'run_id') for table in (
        'workbench_text_review', 'workbench_text_review_budget', 'workbench_text_review_chunk',
        'workbench_text_review_counter', 'workbench_text_review_source', 'workbench_text_review_unit')},
}


def install_content_guards(connection):
    """Run in the migration transaction, before any content recovery/backfill.

    Direct matter tables are discovered so newly added direct content cannot
    silently escape the fence. Child-only tables use explicit ownership joins.
    The archive's separate exhaustive schema coverage check blocks unknown
    unclassified child tables from receiving a full-export receipt.
    """
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'workbench_%'")]
    for table in tables:
        if not re.fullmatch('workbench_[a-z0-9_]+', table):
            raise RuntimeError('Unexpected content table identity.')
        if table in _UNFENCED:
            continue
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if 'matter_id' in columns:
            def scope(prefix):
                return f'{prefix}.matter_id'
        elif table in _CHILDREN:
            key, parent, parent_key = _CHILDREN[table]
            def scope(prefix, key=key, parent=parent, parent_key=parent_key):
                return f'(SELECT matter_id FROM "{parent}" WHERE "{parent_key}"={prefix}."{key}")'
        else:
            continue
        for operation, prefixes in [('INSERT', ('NEW',)), ('DELETE', ('OLD',)), ('UPDATE', ('OLD', 'NEW'))]:
            conditions = [
                'EXISTS (SELECT 1 FROM workbench_matter_lifecycle WHERE matter_id=' + scope(prefix)
                + " AND state<>'active' AND recordbench_case_content_cleanup(matter_id)<>1)"
                for prefix in prefixes]
            name = f'case_fence_{table}_{operation.lower()}'
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS "{name}" BEFORE {operation} ON "{table}" '
                f'WHEN {" OR ".join(conditions)} BEGIN '
                "SELECT RAISE(ABORT, 'matter content is frozen for explicit exit'); END")
