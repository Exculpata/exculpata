"""Field-aware portable projection; never redact patterns inside authored text."""
from __future__ import annotations

import hashlib
import json
import re

from .matter_archive_schema import (ACTOR_COLUMNS, OPERATIONAL_COLUMNS,
    REFERENCE_COLUMNS, SCHEMA, EXCLUDED_TABLES, exported_columns)

# Known generated payload structure, reviewed alongside the table coverage map.
# Unexpected nested structures fail closed instead of leaking future metadata.
_SAFE_KEYS = frozenset('''
candidate_source_count cited_passage_count cited_source_count collection_wide_request
broad_summary_request omitted_chars original_chars passages searchable_count searchable_source_count
current_display_name current_entity_type current_origin current_revision current_status support_state
history omissions retrieval routes selected_records preflight model_artifacts media
language_confidence diarization_requested speaker_count low_confidence_segments overlap_segments
review_recommended pipeline
answerable generator_answerable missing_information model_called elapsed_ms evidence_notice source_matches
modality_coverage review_scope recorded_context_job recorded_context_attempt notebook_context mode items
source_fingerprint retrieval_source_fingerprint _retrieval_source_fingerprint boundary changed_since_start
full_text_synthesis_input full_text_synthesis_version synthesis_version planner_version hierarchical_synthesis
issue matter basis requests_spent started_at stop_reason search_stop_reason omitted_groups partial
rejected_findings retained_findings uncited_evidence truncated_characters uncited_sources finding_retention_notice
full_text_cursor source_ledger_state unit_state run_state run_updated_at range_count counters has_gaps
locator_kind decisions inventoried criterion_digest decision_revision_digest human_decisions table_digests tables receipt run locator
input_bytes receipt_bytes outcomes oversized_originals finding_limit oversized_original unsealed_or_invalidated candidate_findings admitted
no_finding processed invalidated empty hashes limits_digest criteria_digest frozen_sources
pending_searches seed_search_pending lifetime_candidate_count lifetime_candidate_document_ids
lifetime_analyzed_units no_new_count discarded_passes search_elapsed_seconds candidate_document_ids gaps
search_pass_count candidate_passage_count evidence_passage_count evidence_source_count scope
requested effective search_seconds primary_candidates supplemental_candidates_per_kind selected_per_pass
unique_evidence synthesis_inputs evidence_item_chars evidence_chars output_tokens completed_passes
candidate_occurrences truncated_chars candidate_sources analyzed_unit_occurrences unavailable_sources
max_sources evidence_windows windows visible_window original_characters omitted_characters
source_preserved stage_order translation_is_separate_artifact delivery_only retention_managed_by_job_runner
evidentiary_output_allowed human_review_required mock_output mock requested_language translation_target
diarize_speakers min_speakers max_speakers hotwords alignment_model local_files_only
format version history_format record records entry entries header selection selection_revision
submitted_at source_ids record_type aliases notebook_snapshot references roles accounts
source target source_revision target_revision entity mentions mention_ids added_mentions removed_mentions updated_mentions deleted_mentions
added_roles removed_roles added_accounts removed_accounts before after delta changes
kind date start end granularity precision uncertainty raw normalized ambiguous ambiguity timezone candidates
text title summary introduction conclusion answer claims citations evidence evidence_id evidence_kind
source source_name location excerpt document_id source_version_id unit_number chunk_id excerpt_digest
line_start line_end number start_ms end_ms duration_ms speaker_cluster location_label
confidence low_confidence overlap source_limitation limitation limitations verification_notice
support_tokens parents id level input_digest groups nodes root policy ledger passes steps
question query queries reason reasons method coverage counts sources units ranges included excluded
selected omitted accepted rejected failed cancelled complete completed pending running queued skipped
state status stage note notes warnings content body date_label purpose objective background
available current approval fingerprint digest revision selected_at selected_by content_digest
record_digest reference_digest references_digest role_digest roles_digest accounts_digest
item_id object_id entity_id mention_id assertion_id account_id role_id origin notebook_item_id
snapshot snapshot_digest source_set_digest source_basis_digest content_basis_digest scope_digest
criterion criterion_id criterion_version_id instructions include_guidance exclude_guidance
source_set_id source_set_name source_set_count source_count source_snapshot_count
source_coverage source_scope requested_source_coverage requested_source_scope
requested_count reviewed_count ready_count unavailable_count failed_count pending_count
eligible_count visited_count omitted_count total_count selected_count excluded_count included_count
unit_count chunk_count text_chars empty_units inventory_sealed reported_pages source_state
source_kind page_count completed_units total_units candidate_count evidence_count
policy_digest source_digest frozen_source_digest decision_digest decisions_digest ledger_digest
source_counts unit_counts range_counts counts_digest sources_digest units_digest
findings finding_id finding_key finding_count finding_digest findings_digest omitted_findings
selected_findings selected_originals input_sources input_findings original_sources input_basis
omitted_parents omitted_claims rejected_claims unsupported_claims invalid_claims capped
limits max_groups sources_per_call characters_per_call characters_per_source generation_requests
wall_seconds output_tokens_per_call max_units max_characters max_calls max_ledger_bytes
max_record_chars chunk_chars overlap_chars packet_chars output_tokens max_output_tokens
characters_used calls_used ledger_bytes legacy limit_reason bytes characters count calls
sources_complete sources_attention unsealed_sources coverage_notice processing_notices
research synthesis full_text full_text_basis full_text_synthesis synthesis_receipt synthesis_input
synthesis_policy synthesis_checkpoint synthesis_result synthesis_status checkpoint result plan
requested_population coverage_start coverage_end packet_start packet_end cursor attempt attempts
in_scope out_of_scope valid invalid unknown eligible visited unavailable unreviewed reviewed
machine_decision rationale citations_json human_decision human_note validation_sample
machine_text translated_text model_text review_state reviewed_at reference source_version
source_roles source_role role_revision role_updated_at source_role_snapshot source_role_notice
context_supplied context_selection working_context supplied_context context_records notice
consumption provenance quality language detected_language language_probability diarization_status
model_revision revision artifact_revision runtime_version per_response_attestation profile
model_name model_version engine engine_version extractor_version pipeline_version timestamps
asr_model asr_revision aligner_model aligner_revision diarization_model diarization_revision
normalization repair admission runtime budget serialized_utf8_sha256 serialized_utf8_bytes
purpose input_tokens runtime_window reserved_output_tokens safety_margin_tokens tokenizer
token_ids_sha256 max_model_len usage prompt_tokens completion_tokens total_tokens
model system_fingerprint quality_metrics coverage_ratio silent_ratio repetition_ratio
transport_failed dispatch_attempted prepared response_metadata prepared_at attempted_at completed_at
assertion entity_mention source_reference notebook_reference report_citation review_decision
attributed_to stance display_name entity_type surface_text start_offset end_offset review_status
changes changed_fields old new values value key label name type comparison sources_total
confidence_score low_confidence_segment_count overlap_segment_count segment_count
support reference_count original_count original_characters characters_covered
human machine automatic manually_added exported_at created_at updated_at
'''.split()) | frozenset(column for table in SCHEMA if table not in EXCLUDED_TABLES for column in exported_columns(table))
_DROP_KEYS = OPERATIONAL_COLUMNS | frozenset('''
endpoint url base_url href workspace_url path absolute_path local_path root host hostname
request request_json serialized_request_utf8 headers authorization credentials password secret
api_key access_token refresh_token cookie cookies certificate token model_path configured_model
session session_token request_token lease response_headers service_job_id external_id
message error error_message warnings warning traceback details stderr stdout created_by_name updated_by_name login_name provider_subject provider application_roles auth_method next_path
'''.split())
# Portable support references remain useful but cannot serve as live capabilities.
_REF_KEYS = REFERENCE_COLUMNS | {'support_token', 'action_token'}
_DYNAMIC_REFERENCE = re.compile(r'(?:[a-z][a-z_-]*-)?[0-9a-f]{32,64}|[SPLFR][0-9]+(?:C[0-9]+)?')


_MODEL_KEYS = frozenset({'model', 'model_name', 'model_id', 'model_revision', 'artifact_revision',
    'engine_version', 'asr_model', 'alignment_model', 'aligner_model', 'diarization_model'})
_STRUCTURED = {
    'preflight': frozenset('revision outcome complete checked_ms first_speech_ms last_speech_ms quiet_ms leading_quiet_ms trailing_quiet_ms language quality inspection_id continued'.split()),
    'media': frozenset('duration_seconds audio_streams channels sample_rate_hz format_name has_video bit_rate'.split()),
    'model_artifacts': frozenset('status ready required code message artifact_role file_index schema_version manifest_sha256 artifact_count verified_file_count verified_bytes artifacts asr_bindings'.split()),
    'artifact': frozenset('role model_id revision license file_count verified_bytes'.split()),
    'binding': frozenset('model_id revision adapter supports_translation'.split()),
}

_SAFE_KEYS |= frozenset().union(*_STRUCTURED.values())


def origin_reference(matter_id, value):
    # Mixed report origins also point to ordinary message/note/finding/clip IDs,
    # which remain stable archive identities. Only operational identity families
    # already pseudonymized in their owning tables are mapped here and as keys.
    if isinstance(value, str) and re.fullmatch(
            r'(?:answer-job|research-job|report-compilation|review-run|analysis-run|media-job|ingest-job)-[0-9a-f]{32}', value):
        return reference(matter_id, value)
    return value


def structured(kind, value, matter_id, *, depth):
    if not isinstance(value, dict) or set(value) - _STRUCTURED[kind]:
        raise ValueError('Preservation metadata contains an unreviewed field.')
    out = {}
    for key, child in value.items():
        if key in _DROP_KEYS or key == 'inspection_id':
            continue
        if key == 'artifacts':
            if not isinstance(child, list):
                raise ValueError('Preservation artifacts have an unsupported value.')
            out[key] = [structured('artifact', item, matter_id, depth=depth + 1) for item in child]
        elif key == 'asr_bindings':
            if not isinstance(child, dict) or set(child) - {'primary', 'fast'}:
                raise ValueError('Preservation model bindings require review.')
            out[key] = {slot: structured('binding', item, matter_id, depth=depth + 1) for slot, item in child.items()}
        else:
            if key == 'revision' and kind in {'artifact', 'binding'} and isinstance(child, str):
                if child.startswith(('/', '\\', '~')) or re.match(r'^[A-Za-z]:', child) or '://' in child:
                    raise ValueError('Preservation provenance contains a nonportable runtime location.')
            # Only field names in this exact metadata family may enter portable.
            out[key] = portable({key: child}, matter_id, depth=depth + 1)[key]
    return out


def reference(matter_id, value, *, actor=False):
    if value is None or value == '':
        return value
    domain = 'actor' if actor else 'reference'
    return domain + '-' + hashlib.sha256((domain + '\0' + matter_id + '\0' + str(value)).encode()).hexdigest()[:24]


def portable(value, matter_id, *, depth=0):
    if depth > 48:
        raise ValueError('Preservation payload nesting exceeds the reviewed format.')
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if key in _DROP_KEYS:
                continue
            if key not in _SAFE_KEYS and not _DYNAMIC_REFERENCE.fullmatch(key):
                raise ValueError('Preservation payload contains an unreviewed field.')
            if key == 'matter_id' and child not in (None, matter_id):
                raise ValueError('Preservation payload crosses a matter boundary.')
            if key == 'review_recommended' and type(child) is not bool:
                raise ValueError('Preservation review recommendation must be a boolean.')
            if key == 'pipeline' and (not isinstance(child, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', child)):
                raise ValueError('Preservation pipeline must be a portable identifier.')
            if key in ACTOR_COLUMNS:
                child = reference(matter_id, child, actor=True)
            elif key in _REF_KEYS:
                child = reference(matter_id, child)
            elif key == 'origin_id':
                child = origin_reference(matter_id, child)
            elif key in {'support_tokens', 'uncited_evidence'}:
                child = [reference(matter_id, item) for item in child]
            elif key in _STRUCTURED:
                child = structured(key, child, matter_id, depth=depth + 1)
            else:
                if key in _MODEL_KEYS and isinstance(child, str):
                    if child.startswith(('/', '\\', '~')) or re.match(r'^[A-Za-z]:', child) or '://' in child:
                        raise ValueError('Preservation provenance contains a nonportable runtime location.')
                child = portable(child, matter_id, depth=depth + 1)
            out[reference(matter_id, key) if re.fullmatch(r'[0-9a-f]{40}', key) else origin_reference(matter_id, key)] = child
        return out
    if isinstance(value, list):
        return [portable(item, matter_id, depth=depth + 1) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError('Preservation payload has an unsupported value.')


def project_row(table, row, matter_id):
    out = {}
    for key in exported_columns(table):
        value = row[key]
        if key in ACTOR_COLUMNS:
            value = reference(matter_id, value, actor=True)
        elif key in REFERENCE_COLUMNS:
            value = reference(matter_id, value)
        elif key == 'selections_json' and value is not None:
            value = [selection_reference(matter_id, item) for item in json.loads(value)]
        elif key.endswith('_json') and value is not None:
            value = portable(json.loads(value), matter_id)
        elif key == 'origin_id':
            value = origin_reference(matter_id, value)
        out[key] = value
    return out


def selection_reference(matter_id, value):
    if not isinstance(value, str):
        raise ValueError('Preservation selection has an unsupported value.')
    kind, separator, identifier = value.partition(':')
    if not separator or kind not in {'conversation', 'research', 'review', 'notes', 'note'}:
        raise ValueError('Preservation selection has an unreviewed kind.')
    return kind + ':' + origin_reference(matter_id, identifier)


def payload_coverage_manifest():
    return {
        'unknown_fields': 'Creation blocked pending explicit format review',
        'omitted_nested_fields': sorted(_DROP_KEYS | {'inspection_id'}),
        'typed_metadata': {'review_recommended': 'boolean',
                           'pipeline': 'Unqualified identifier: 1-120 ASCII letters, digits, dot, underscore or hyphen'},
        'actor_fields': sorted(ACTOR_COLUMNS),
        'pseudonymized_reference_fields': sorted(_REF_KEYS),
        'structured_metadata_fields': {key: sorted(value) for key, value in _STRUCTURED.items()},
        'mixed_origins': 'Operational job/run identities use the same matter-local mapping as their owning table; ordinary content identities remain stable.',
        'authored_text': 'Preserved exactly; no pattern-based redaction.',
    }
