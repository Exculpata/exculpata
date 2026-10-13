"""Versioned, reviewed coverage contract for preservation archives.

Every table and column is accounted for. A new database schema blocks export
until this map is deliberately updated; discovery never silently exports data.
"""
from __future__ import annotations

# This is a fixed contract, not a runtime schema-derived export allowlist.
_SCHEMA = """
workbench_analysis_run:analysis_id,matter_id,requested_by,state,source_count,unit_count,entity_count,finding_count,capped,message,created_at,finished_at
workbench_answer_context:job_id,matter_id,snapshot_json
workbench_answer_context_attempt:job_id,ordinal,worker_attempt,state,manifest_json,response_json,prepared_at,attempted_at,completed_at
workbench_answer_event:job_id,ordinal,state,stage,message,created_at
workbench_answer_job:job_id,matter_id,conversation_id,actor_id,idempotency_key,question,question_message_id,state,stage,attempts,worker_id,cancellation_requested,result_message_id,message,created_at,started_at,last_claimed_at,finished_at,updated_at
workbench_answer_notebook_scope:job_id,matter_id,mode,item_count,created_at
workbench_answer_notebook_scope_item:job_id,matter_id,ordinal,notebook_item_id,item_type,status,title,body,date_label,content_digest
workbench_answer_source_scope:job_id,matter_id,source_set_id
workbench_assertion:assertion_id,matter_id,record_type,title,statement,raw_date,date_uncertainty,sort_date,status,origin,revision,created_by,created_at,updated_by,updated_at
workbench_assertion_account:account_id,matter_id,assertion_id,stance,attributed_to,document_id,source_version_id,source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,support_token,created_by,created_at
workbench_assertion_history:assertion_id,matter_id,revision,action,snapshot_json,actor_id,created_at
workbench_assertion_role:role_id,matter_id,assertion_id,role,entity_id,entity_revision,display_name,entity_type,created_by,created_at
workbench_audit_event:event_id,occurred_at,actor_principal_id,session_id,matter_id,request_id,action,outcome,object_type,object_id,details_json
workbench_context_entry:matter_id,owner_id,kind,object_id,ordinal,approval_json,selected_by,selected_at
workbench_context_selection:matter_id,owner_id,revision,created_at,updated_at,updated_by
workbench_conversation:conversation_id,matter_id,title,created_at,updated_at
workbench_conversation_organization:conversation_id,matter_id,state,is_pinned,archived_at,archived_by,updated_by,updated_at
workbench_entity:entity_id,matter_id,entity_type,display_name,status,aliases_json,origin,notebook_item_id,notebook_snapshot_json,revision,created_by,created_at,updated_by,updated_at,extractor_version
workbench_entity_auto_discovery_unit:matter_id,document_id,source_version_id,content_basis_digest,unit_ordinal,unit_digest,extractor_version,state,note,wait_round
workbench_entity_discovery_seen:matter_id,occurrence_key
workbench_entity_discovery_unit:matter_id,run_id,document_id,source_version_id,unit_ordinal,unit_digest,extractor_version,state,note
workbench_entity_history:entity_id,matter_id,revision,action,snapshot_json,actor_id,created_at
workbench_entity_mention:mention_id,matter_id,entity_id,document_id,source_version_id,source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,support_token,origin,created_by,created_at,occurrence_key,extractor_version,surface_text,start_offset,end_offset,date_json,review_status
workbench_entity_reconciliation:operation_id,matter_id,action,before_json,after_json,actor_id,created_at,undone
workbench_ingest_job:job_id,matter_id,document_id,plan_id,source_location_id,relative_path,state,stage,completed_units,total_units,attempts,worker_id,message,created_at,started_at,finished_at,updated_at
workbench_ingest_plan:plan_id,matter_id,source_location_id,source_label,relative_folder,state,supported_count,unsupported_count,total_bytes,created_at,confirmed_at
workbench_ingest_plan_item:plan_id,ordinal,relative_path,display_name,media_type,byte_size,stable_device,stable_inode,stable_mtime_ns,document_id
workbench_intake_item:receipt_id,matter_id,ordinal,descriptor_digest,relative_path,display_name,expected_size,expected_type,supplied_type,preflight_state,selected_for_upload,reason,reviewed_state,reviewed_reason
workbench_intake_receipt:receipt_id,matter_id,actor_id,selection_key,selection_fingerprint,collection_name,selected_count,eligible_indexes_json,state,created_at,updated_at,metadata_bytes
workbench_intake_transfer:receipt_id,matter_id,ordinal,upload_item_id,source_version_id
workbench_job_matter_schedule:queue_kind,matter_id,claim_sequence
workbench_matter:matter_id,slug,display_name,descriptor,owner_id,created_at,updated_at
workbench_matter_activity:activity_id,matter_id,principal_id,activity_kind,object_id,locator,created_at,updated_at
workbench_matter_group_grant:matter_id,group_id,granted_by,created_at
workbench_matter_lifecycle:matter_id,state,purge_id,requested_by,requested_at,finished_at,error_code,source_count,conversation_count,message_count,updated_at
workbench_matter_membership:matter_id,principal_id,role,state,granted_by,created_at,updated_at,revoked_at
workbench_matter_purpose:matter_id,objective,background,revision,updated_by,updated_at
workbench_matter_retention:matter_id,expires_at,purge_after,scheduled_by,created_at,updated_at
workbench_media_clip:clip_id,matter_id,document_id,source_version_id,title,start_ms,end_ms,created_by,created_at
workbench_media_job:media_job_id,matter_id,document_id,source_version_id,requested_by,state,stage,progress,external_job_id,source_sha256,byte_size,media_type,duration_ms,attempts,worker_id,degraded,message,warnings_json,quality_json,provenance_json,created_at,started_at,finished_at,updated_at
workbench_media_summary:transcript_id,matter_id,document_id,source_version_id,state,attempts,payload_json,basis_digest,covered_segment_count,total_segment_count,message,created_at,started_at,finished_at,updated_at
workbench_media_transcript:transcript_id,matter_id,document_id,source_version_id,media_job_id,review_state,duration_ms,segment_count,transcript_digest,warnings_json,quality_json,provenance_json,imported_by,imported_at,updated_at
workbench_message:message_id,conversation_id,ordinal,role,content,payload_json,created_at
workbench_notebook_item:item_id,matter_id,item_type,status,title,body,date_label,is_pinned,origin,source_conversation_id,source_message_id,dedupe_key,created_by,created_at,updated_by,updated_at
workbench_notebook_reference:reference_id,item_id,matter_id,ordinal,document_id,source_version_id,source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,support_token,created_at
workbench_oidc_transaction:state_digest,next_path,created_at,expires_at,consumed_at
workbench_principal:principal_id,provider,provider_subject,display_name,login_name,active,created_at,last_seen_at
workbench_principal_preference:principal_id,theme,updated_at
workbench_report:report_id,matter_id,title,purpose,status,created_by,created_at,updated_by,updated_at
workbench_report_citation:citation_id,section_id,report_id,matter_id,ordinal,kind,document_id,source_version_id,source_name,location,support_token,excerpt,media_clip_id,start_ms,end_ms,created_at
workbench_report_compilation_job:job_id,matter_id,actor_id,request_key,kind,topic,selections_json,state,attempts,worker_id,lease_token,lease_expires_at,cancellation_requested,input_fingerprint,report_id,message,created_at,updated_at,finished_at
workbench_report_section:section_id,report_id,matter_id,ordinal,heading,body,origin,origin_id,created_by,created_at,updated_by,updated_at,compilation_basis
workbench_research_event:job_id,ordinal,state,stage,completed_steps,total_steps,message,created_at
workbench_research_job:job_id,matter_id,actor_id,idempotency_key,question,title,source_set_id,state,stage,attempts,worker_id,cancellation_requested,plan_json,result_json,total_steps,completed_steps,candidate_count,evidence_count,message,created_at,started_at,last_claimed_at,finished_at,updated_at,conversation_id,result_message_id
workbench_review_criterion:criterion_id,matter_id,title,created_by,created_at,updated_by,updated_at
workbench_review_criterion_version:criterion_version_id,criterion_id,matter_id,version_number,instructions,include_guidance,exclude_guidance,created_by,created_at
workbench_review_decision:run_id,matter_id,ordinal,document_id,source_version_id,action_token,source_name,source_kind,machine_decision,rationale,citations_json,error_message,validation_sample,human_decision,human_note,reviewed_by,reviewed_at,created_at,updated_at,source_basis_digest
workbench_review_event:run_id,ordinal,state,stage,reviewed_count,snapshot_count,message,created_at
workbench_review_finding:finding_id,matter_id,kind,signature,title,summary,status,active,created_by,created_at,updated_by,updated_at
workbench_review_finding_reference:reference_id,finding_id,matter_id,ordinal,document_id,source_version_id,source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,support_token,created_at
workbench_review_run:run_id,matter_id,actor_id,criterion_id,criterion_version_id,source_set_id,run_kind,state,stage,attempts,worker_id,cancellation_requested,snapshot_count,reviewed_count,included_count,excluded_count,attention_count,message,created_at,started_at,last_claimed_at,finished_at,updated_at
workbench_session:session_id,token_digest,principal_id,auth_method,application_roles,created_at,last_seen_at,idle_expires_at,absolute_expires_at,revoked_at
workbench_source_byte_match:matter_id,document_id,version_id,source_sha256,byte_size
workbench_source_catalog:matter_id,document_id,version_id,action_token,display_name,display_name_key,relative_path,search_key,media_type,kind,source_state,tone,state_label,count_label,processing_stage,completed_units,total_units,page_count,duration_ms,byte_size,origin,retryable,removable,has_video,cataloged_at,updated_at,content_basis_digest
workbench_source_collection:collection_id,matter_id,name,name_key,kind,created_by,created_at,updated_at
workbench_source_organization:matter_id,document_id,collection_id,relative_path,review_state,added_by,added_at,updated_by,updated_at,source_role,role_revision,role_updated_by,role_updated_at
workbench_source_set:source_set_id,matter_id,name,name_key,created_by,created_at,updated_by,updated_at
workbench_source_set_item:source_set_id,matter_id,document_id,added_by,added_at
workbench_speaker_mapping_revision:mapping_revision_id,transcript_id,matter_id,speaker_cluster,revision,display_name,identity_state,edited_by,edited_at
workbench_team_group:group_id,name,created_by,created_at
workbench_team_group_member:group_id,principal_id,granted_by,created_at
workbench_text_review:run_id,policy_json
workbench_text_review_budget:run_id,max_characters,max_calls,max_ledger_bytes,characters_used,calls_used,ledger_bytes,legacy,limit_reason
workbench_text_review_chunk:run_id,document_id,unit_ordinal,chunk_ordinal,cursor,coverage_start,coverage_end,packet_start,packet_end,state,decision,rationale,finding_key,attempt
workbench_text_review_counter:run_id,kind,state,count,characters
workbench_text_review_source:run_id,document_id,source_version_id,source_basis_digest,source_name,source_kind,source_state,extraction_note,reported_pages,inventory_sealed,unit_count,text_chars,chunk_count,empty_units,state
workbench_text_review_unit:run_id,document_id,unit_ordinal,unit_number,location,unit_digest,text_chars,chunk_count,citation_json,state
workbench_transcript_segment:segment_id,transcript_id,matter_id,ordinal,external_segment_id,start_ms,end_ms,speaker_cluster,model_text,translated_text,confidence,low_confidence,overlap,created_at
workbench_transcript_segment_revision:revision_id,segment_id,matter_id,revision,text,edited_by,edited_at
workbench_upload_item:upload_item_id,upload_session_id,matter_id,ordinal,display_name,relative_path,media_type,expected_size,received_size,state,document_id,message,updated_at
workbench_upload_session:upload_session_id,matter_id,collection_id,actor_id,state,item_count,total_bytes,created_at,updated_at
workbench_case_policy:matter_id,policy_version,mode,next_check_in_at,last_checked_at,selected_by,created_at,updated_at
workbench_case_exit:exit_id,matter_id,state,requested_by,requested_at,eligible_after,export_id,archive_digest,snapshot_digest,exported_by,exported_at,confirmed_by,confirmed_at,purge_id,updated_at,mode
workbench_case_confirmation:token_digest,exit_id,actor_id,session_id,name_digest,export_id,archive_digest,snapshot_digest,created_at,expires_at,mode
workbench_case_download_attempt:attempt_id,exit_id,export_id,archive_digest,total_size,actor_id,session_id,administrator,range_start,range_end,created_at
workbench_case_download_coverage:exit_id,export_id,archive_digest,total_size,actor_id,intervals_json,completed_at
workbench_case_purge_authority:purge_id,exit_id,mode,export_id,archive_digest,snapshot_digest,proof_actor_id,delivery_completed_at,acknowledged_at,actor_id,session_id,administrator
workbench_case_session_binding:session_id,binding_digest
"""
SCHEMA = {name: tuple(columns.split(',')) for name, columns in
          (line.split(':') for line in _SCHEMA.strip().splitlines())}

CASE_AUTHORIZATION_TABLES = frozenset({
    'workbench_case_download_attempt',
    'workbench_case_download_coverage',
    'workbench_case_purge_authority',
    'workbench_case_session_binding',
})

EXCLUDED_TABLES = {
    'workbench_principal': 'Global identities and login information',
    'workbench_principal_preference': 'Global UI preferences',
    'workbench_session': 'Authentication sessions and credentials',
    'workbench_oidc_transaction': 'Authentication transactions',
    'workbench_team_group': 'Global team identities',
    'workbench_team_group_member': 'Global team memberships',
    'workbench_matter_group_grant': 'Global authorization grants',
    'workbench_case_confirmation': 'One-use purge credentials and session bindings',
    'workbench_case_download_attempt': 'Operational download attempts, actor/session bindings and transfer generations; excluded from archive and content snapshot',
    'workbench_case_download_coverage': 'Operational byte-delivery receipts and actor bindings; excluded from archive and content snapshot',
    'workbench_case_purge_authority': 'Live purge authority, actor/session bindings and export or no-export acknowledgments; excluded from archive and content snapshot',
    'workbench_case_session_binding': 'Private authentication-session revision bindings; excluded from archive and content snapshot',
    'workbench_job_matter_schedule': 'Operational scheduler position',
    'workbench_matter_activity': 'Ephemeral UI activity locators',
}
# Child tables without matter_id have exactly one explicitly scoped parent.
CHILD_SCOPES = {
    'workbench_message': ('conversation_id', 'workbench_conversation', 'conversation_id'),
    'workbench_ingest_plan_item': ('plan_id', 'workbench_ingest_plan', 'plan_id'),
    'workbench_answer_event': ('job_id', 'workbench_answer_job', 'job_id'),
    'workbench_answer_context_attempt': ('job_id', 'workbench_answer_context', 'job_id'),
    'workbench_research_event': ('job_id', 'workbench_research_job', 'job_id'),
    'workbench_review_event': ('run_id', 'workbench_review_run', 'run_id'),
    **{name: ('run_id', 'workbench_review_run', 'run_id') for name in (
        'workbench_text_review', 'workbench_text_review_budget', 'workbench_text_review_chunk',
        'workbench_text_review_counter', 'workbench_text_review_source', 'workbench_text_review_unit')},
}
# Omitted only by field identity: authored text is never pattern-redacted.
OPERATIONAL_COLUMNS = frozenset("""
    session_id request_id token_digest idempotency_key request_key selection_key
    selection_fingerprint dedupe_key worker_id lease_token lease_expires_at
    external_job_id external_segment_id stable_device stable_inode stable_mtime_ns
    source_location_id action_token slug display_name_key search_key
    name_key last_claimed_at
""".split())
ACTOR_COLUMNS = frozenset("""
    actor_id actor_principal_id principal_id owner_id created_by updated_by
    requested_by granted_by revoked_by imported_by edited_by reviewed_by archived_by
    selected_by exported_by confirmed_by scheduled_by added_by role_updated_by
""".split())
# References are preserved as matter-local, non-capability identifiers.
REFERENCE_COLUMNS = frozenset("""
    job_id media_job_id run_id plan_id upload_item_id upload_session_id receipt_id
    event_id operation_id purge_id exit_id export_id support_token recorded_context_job
""".split())
OMITTED_COLUMNS = {
    'workbench_audit_event': {'details_json', 'object_id'},
    'workbench_case_exit': {'archive_digest', 'snapshot_digest'},
}
# Job messages and model/service diagnostics can embed host paths. The portable
# archive keeps durable state, stage, attempts, events and coverage counters;
# unstructured operational diagnostics are explicitly outside its contract.
for _table in SCHEMA:
    if (_table.endswith(('_job', '_event', '_run')) or
        _table in {'workbench_media_summary', 'workbench_text_review_source', 'workbench_review_decision'}):
        OMITTED_COLUMNS.setdefault(_table, set()).update(
            {'message', 'error_message', 'extraction_note', 'warnings_json'})
OMITTED_COLUMNS.setdefault('workbench_media_transcript', set()).add('warnings_json')
VOLATILE_TABLES = frozenset({
    'workbench_audit_event', 'workbench_case_exit', 'workbench_case_policy',
    'workbench_matter_lifecycle', 'workbench_matter_retention',
})


def exported_columns(table):
    return tuple(c for c in SCHEMA[table]
                 if c not in OPERATIONAL_COLUMNS and c not in OMITTED_COLUMNS.get(table, ()))


def validate_schema(connection):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables != set(SCHEMA):
        raise ValueError('Preservation archive coverage does not match this database schema.')
    for table, expected in SCHEMA.items():
        actual = tuple(r[1] for r in connection.execute(f'PRAGMA table_info("{table}")'))
        if actual != expected:
            raise ValueError('Preservation archive column coverage requires review.')


def scope_query(table):
    columns = SCHEMA[table]
    if 'matter_id' in columns:
        where = 'matter_id=?'
    else:
        key, parent, parent_key = CHILD_SCOPES[table]
        where = f'"{key}" IN (SELECT "{parent_key}" FROM "{parent}" WHERE matter_id=?)'
    # Full-row ordering is stable even for tables without an integer rowid.
    order = ','.join(f'"{column}"' for column in columns if not column.endswith('_json') and column not in {'body', 'content', 'text', 'model_text', 'translated_text'})
    selected = ','.join(f'"{column}"' for column in exported_columns(table))
    return f'SELECT {selected} FROM "{table}" WHERE {where} ORDER BY {order}'


AUDIT_EXPORT_NOTICE = (
    'Matter-scoped audit chronology only, not complete audit contents. All details_json '
    'payloads and object_id targets are omitted, together with session_id and request_id. '
    'Event and actor identifiers use matter-local pseudonyms; global and unscoped events '
    'and events after the archive snapshot are not included.'
)
AUDIT_FIELD_EXCLUSIONS = {
    'details_json': 'excluded: all audit event details, including otherwise safe contextual fields; complete event contents cannot be reconstructed',
    'object_id': 'excluded: raw audit target identifier; the exact event target cannot be reconstructed from this record',
    'session_id': 'excluded: authentication session identifier; session correlation is unavailable',
    'request_id': 'excluded: operational request identifier; request correlation is unavailable',
}


def coverage_manifest():
    coverage = {table: {'disposition': EXCLUDED_TABLES.get(table, 'included'),
        'columns': {c: ('excluded: operational metadata' if c in OPERATIONAL_COLUMNS or
                       c in OMITTED_COLUMNS.get(table, ()) else
                       'matter-local attribution' if c in ACTOR_COLUMNS else
                       'matter-local reference' if c in REFERENCE_COLUMNS else 'preserved')
                    for c in columns}}
        for table, columns in SCHEMA.items()}
    for table in CASE_AUTHORIZATION_TABLES.intersection(coverage):
        coverage[table]['snapshot_disposition'] = 'excluded: operational table is not exported'
        coverage[table]['columns'] = {column: 'excluded: operational authorization state' for column in SCHEMA[table]}
    audit = coverage['workbench_audit_event']
    audit.update(content_scope='matter_scoped_chronology', complete_audit_contents=False,
                 notice=AUDIT_EXPORT_NOTICE)
    audit['columns'].update(AUDIT_FIELD_EXCLUSIONS)
    return coverage

SOURCE_FIELDS = tuple('''document_id display_name stored_name media_type size state message units digest
version_id name_key page_count units_file origin source_location_id relative_path stable_device
stable_inode stable_mtime_ns processing_stage completed_units total_units duration_ms has_video
playback_name playback_media_type playback_size playback_state playback_message transcript_index_basis'''.split())
SOURCE_EXCLUSIONS = {'stored_name', 'name_key', 'source_location_id', 'stable_device',
                     'stable_inode', 'stable_mtime_ns', 'message', 'playback_message'}


def source_coverage_manifest():
    return {name: ('excluded: operational storage metadata' if name in SOURCE_EXCLUSIONS else
        'separate extracted unit member' if name in {'units', 'units_file'} else
        'separate verified rendition member' if name == 'playback_name' else 'preserved')
        for name in SOURCE_FIELDS}
