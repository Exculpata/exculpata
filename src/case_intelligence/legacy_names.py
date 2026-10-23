"""Previous product spelling, isolated so the rest of the tree can avoid it.

Fresh installs and staff-facing copy use Exculpata. Existing nodes, applied
database functions, and clients that already send the previous names keep
working through the constants in this module.
"""
from __future__ import annotations

import logging
import threading

from dataclasses import dataclass
from types import MappingProxyType
from string import Formatter


@dataclass(frozen=True)
class NameContract:
    """An alias, a saved generation coordinate, or a deliberately retained name."""

    legacy: str
    current: str
    behavior: str


# Templates are the authority, including names that must not be migrated.
# Adding a new spelling requires its old spelling and its resolution behavior.
NAME_CONTRACTS = MappingProxyType({
    "stem": NameContract("recordbench", "exculpata", "generation"),
    "product": NameContract("RecordBench", "Exculpata", "alias"),
    "environment": NameContract("RECORDBENCH_{suffix}", "EXCULPATA_{suffix}", "alias"),
    "header": NameContract("X-RecordBench-{suffix}", "X-Exculpata-{suffix}", "alias"),
    "header_prefix_lower": NameContract("x-recordbench-", "x-exculpata-", "alias"),
    "git_config": NameContract("recordbench.{suffix}", "exculpata.{suffix}", "alias"),
    "host_root": NameContract("/srv/recordbench", "/srv/exculpata", "generation"),
    "app_env": NameContract("recordbench.env", "exculpata.env", "generation"),
    "storage_marker": NameContract(".recordbench-managed-storage.json", ".exculpata-managed-storage.json", "generation"),
    "storage_id": NameContract("recordbench-storage-{suffix}", "exculpata-storage-{suffix}", "generation"),
    "storage_id_prefix": NameContract("recordbench-storage-", "exculpata-storage-", "generation"),
    "storage_marker_temporary": NameContract("..recordbench-managed-storage.json-{suffix}.tmp", "..exculpata-managed-storage.json-{suffix}.tmp", "generation"),
    "hostname": NameContract("recordbench.example.test", "exculpata.example.test", "generation"),
    "administrator": NameContract("recordbench.admin", "exculpata.admin", "generation"),
    "backup_unit": NameContract("recordbench-backup-{node_id}.{kind}", "exculpata-backup-{node_id}.{kind}", "generation"),
    "backup_template": NameContract("recordbench-backup.{kind}.in", "exculpata-backup.{kind}.in", "generation"),
    "backup_tool": NameContract("recordbench_backup.py", "exculpata_backup.py", "generation"),
    "database": NameContract("recordbench", "recordbench", "retained"),
    "container_data": NameContract("/var/lib/recordbench", "/var/lib/recordbench", "retained"),
    "container_secrets": NameContract("/run/recordbench-secrets", "/run/recordbench-secrets", "retained"),
    "container_accounts": NameContract("/var/lib/recordbench-accounts", "/var/lib/recordbench-accounts", "retained"),
    "container_auth": NameContract("/run/recordbench-auth", "/run/recordbench-auth", "retained"),
    "image_workdir": NameContract("/opt/recordbench", "/opt/recordbench", "retained"),
    "sql_function": NameContract("recordbench_{suffix}", "recordbench_{suffix}", "retained"),
    "whisperx_build": NameContract("3.8.6+recordbench.1", "3.8.6+recordbench.1", "retained"),
    "stage_receipt": NameContract(".recordbench-stage-receipt.json", ".recordbench-stage-receipt.json", "retained"),
    "exact_v1": NameContract("recordbench-exact-v1", "recordbench-exact-v1", "retained"),
    "exact_v2": NameContract("recordbench-exact-v2", "recordbench-exact-v2", "retained"),
    "words_v1": NameContract("recordbench-words-v1", "recordbench-words-v1", "retained"),
    "question_draft_set": NameContract("recordbench-question-draft-set-v1", "recordbench-question-draft-set-v1", "retained"),
    "review_acceptance": NameContract("recordbench-review-acceptance-v1", "recordbench-review-acceptance-v1", "retained"),
    "question_draft_v1": NameContract("recordbench-question-draft-evaluation-v1", "recordbench-question-draft-evaluation-v1", "retained"),
    "question_draft_v2": NameContract("recordbench-question-draft-evaluation-v2", "recordbench-question-draft-evaluation-v2", "retained"),
    "auth_mode_diagnostic": NameContract("recordbench/auth-mode-diagnostic/v1", "recordbench/auth-mode-diagnostic/v1", "retained"),
    "ocr_limit": NameContract("recordbench-ocr-limit", "recordbench-ocr-limit", "retained"),
    "legacy_generator_9b": NameContract("recordbench-qwen35-9b", "recordbench-qwen35-9b", "retained"),
    "gpu_lock": NameContract("/tmp/recordbench-transcription-gpu.lock", "/tmp/recordbench-transcription-gpu.lock", "retained"),
    "transcription_notice": NameContract("/usr/share/licenses/recordbench-transcription", "/usr/share/licenses/recordbench-transcription", "retained"),
    "proxy_entrypoint": NameContract("/usr/local/sbin/recordbench-auth-entrypoint", "/usr/local/sbin/recordbench-auth-entrypoint", "retained"),
    "proxy_site": NameContract("/etc/apache2/sites-available/recordbench-auth.conf", "/etc/apache2/sites-available/recordbench-auth.conf", "retained"),
    "proxy_server_config": NameContract("/etc/apache2/conf-available/recordbench-server-name.conf", "/etc/apache2/conf-available/recordbench-server-name.conf", "retained"),
    "proxy_template": NameContract("recordbench-auth.conf.template", "recordbench-auth.conf.template", "retained"),
    "proxy_site_id": NameContract("recordbench-auth", "recordbench-auth", "retained"),
    "proxy_server_id": NameContract("recordbench-server-name", "recordbench-server-name", "retained"),
    "worker_name": NameContract("recordbench-transcription-worker-1", "recordbench-transcription-worker-1", "retained"),
    "ignored_node_directory": NameContract("recordbench-node", "recordbench-node", "retained"),
    "keytab": NameContract("recordbench.keytab", "recordbench.keytab", "retained"),
    "kerberos_users_example": NameContract("recordbench_users@example.test", "recordbench_users@example.test", "retained"),
    "kerberos_admin_example": NameContract("recordbench_administrators@example.test", "recordbench_administrators@example.test", "retained"),
    "local_users_group": NameContract("recordbench_users", "recordbench_users", "retained"),
    "local_admin_group": NameContract("recordbench_administrators", "recordbench_administrators", "retained"),
    "oidc_users_group": NameContract("RecordBench_Users", "RecordBench_Users", "retained"),
    "oidc_admin_group": NameContract("RecordBench_Administrators", "RecordBench_Administrators", "retained"),
    "login_rate_zone": NameContract("recordbench_login", "recordbench_login", "retained"),
    "nemotron_schema": NameContract("recordbench-nemotron-v1", "recordbench-nemotron-v1", "retained"),
    "nemotron_temporary": NameContract("recordbench-nemotron-", "recordbench-nemotron-", "retained"),
    "worker_gpu_lock": NameContract("/run/lock/recordbench-transcription-gpu.lock", "/run/lock/recordbench-transcription-gpu.lock", "retained"),
    "browser_name": NameContract("recordbench:{suffix}", "exculpata:{suffix}", "alias"),
    "cli_error_label": NameContract("recordbench:", "recordbench:", "retained"),
    "ticket_uri": NameContract("recordbench://ticket/{token}", "recordbench://ticket/{token}", "retained"),
    # Exact malformed input retained by the synthetic ticket rejection evaluation.
    "ticket_uri_rejection_fixture": NameContract("recordbench://ticket/bad?query", "recordbench://ticket/bad?query", "retained"),
    "ticket_uri_prefix": NameContract("recordbench://ticket/", "recordbench://ticket/", "retained"),
    "backup_status_default": NameContract("/srv/recordbench/.local/state/recordbench-backup/status.json", "/srv/recordbench/.local/state/recordbench-backup/status.json", "retained"),
    "browser_source_note": NameContract("recordbenchSourceNote", "exculpataSourceNote", "alias"),
    "portable_generator": NameContract("recordbench-qwen35-4b", "exculpata-qwen35-4b", "alias"),
    "installer_shim": NameContract("recordbench_install.py", "exculpata_install.py", "alias"),
    "backup_shim": NameContract("recordbench-backup.sh", "exculpata-backup.sh", "alias"),
    "backup_status_shim": NameContract("recordbench-backup-status.sh", "exculpata-backup-status.sh", "alias"),
    "restore_shim": NameContract("recordbench-restore-drill.sh", "exculpata-restore-drill.sh", "alias"),
    "workbench_cli": NameContract("recordbench-workbench", "exculpata-workbench", "alias"),
    "retrieval_cli": NameContract("recordbench-retrieval-worker", "exculpata-retrieval-worker", "alias"),
    # Existing persisted formats and runtime labels assembled from the stem.
    "format_context_model_acceptance_v1": NameContract("recordbench-context-model-acceptance-v1", "recordbench-context-model-acceptance-v1", "retained"),
    "format_answer_context_v1": NameContract("recordbench-answer-context-v1", "recordbench-answer-context-v1", "retained"),
    "format_assertion_v1": NameContract("recordbench-assertion-v1", "recordbench-assertion-v1", "retained"),
    "format_chronology_v1": NameContract("recordbench-chronology-v1", "recordbench-chronology-v1", "retained"),
    "format_discovery_gold_v1": NameContract("recordbench-discovery-gold-v1", "recordbench-discovery-gold-v1", "retained"),
    "format_entity_v1": NameContract("recordbench-entity-v1", "recordbench-entity-v1", "retained"),
    "format_entity_discovery_v1": NameContract("recordbench-entity-discovery-v1", "recordbench-entity-discovery-v1", "retained"),
    "format_intake_receipt_v1": NameContract("recordbench-intake-receipt-v1", "recordbench-intake-receipt-v1", "retained"),
    "format_context_selections_v1": NameContract("recordbench-context-selections-v1", "recordbench-context-selections-v1", "retained"),
    "format_local_ai_v1": NameContract("recordbench-local-ai-v1", "recordbench-local-ai-v1", "retained"),
    "format_model_portfolio_gold_v1": NameContract("recordbench-model-portfolio-gold-v1", "recordbench-model-portfolio-gold-v1", "retained"),
    "format_question_replay_v1": NameContract("recordbench-question-replay-v1", "recordbench-question-replay-v1", "retained"),
    "format_question_replay_result_v1": NameContract("recordbench-question-replay-result-v1", "recordbench-question-replay-result-v1", "retained"),
    "format_dispatch_v1": NameContract("recordbench-dispatch-v1", "recordbench-dispatch-v1", "retained"),
    "format_source_check_v1": NameContract("recordbench-source-check-v1", "recordbench-source-check-v1", "retained"),
    "format_notebook_v1": NameContract("recordbench-notebook-v1", "recordbench-notebook-v1", "retained"),
    "format_media_work_product_v2": NameContract("recordbench-media-work-product-v2", "recordbench-media-work-product-v2", "retained"),
    "format_work_product_bundle_v1": NameContract("recordbench-work-product-bundle-v1", "recordbench-work-product-bundle-v1", "retained"),
    "format_conversations_v1": NameContract("recordbench-conversations-v1", "recordbench-conversations-v1", "retained"),
    "runtime_storage_probe": NameContract("recordbench-storage-probe", "recordbench-storage-probe", "retained"),
    "runtime_playback_1": NameContract("recordbench-playback-1", "recordbench-playback-1", "retained"),
    "runtime_export": NameContract("recordbench-export", "recordbench-export", "retained"),
    "runtime_maintenance": NameContract("recordbench-maintenance", "recordbench-maintenance", "retained"),
    "runtime_discovery": NameContract("recordbench-discovery", "recordbench-discovery", "retained"),
    "runtime_research_thread": NameContract("recordbench-research-{suffix}", "recordbench-research-{suffix}", "retained"),
    "runtime_review_thread": NameContract("recordbench-full-review-{suffix}", "recordbench-full-review-{suffix}", "retained"),
    "runtime_source_thread": NameContract("recordbench-source-{suffix}", "recordbench-source-{suffix}", "retained"),
    "whisperx_wheel": NameContract("3.8.6+recordbench.1-py3-none-any.whl", "3.8.6+recordbench.1-py3-none-any.whl", "retained"),
    # Exact existing temporary prefixes, not a wildcard for new identifiers.
    "temporary_context_model": NameContract("recordbench-context-model-", "recordbench-context-model-", "retained"),
    "temporary_assertion_recovery": NameContract("recordbench-assertion-recovery-", "recordbench-assertion-recovery-", "retained"),
    "temporary_automatic_discovery": NameContract("recordbench-automatic-discovery-", "recordbench-automatic-discovery-", "retained"),
    "temporary_timing": NameContract("recordbench-timing-", "recordbench-timing-", "retained"),
    "temporary_receipt": NameContract("recordbench-receipt-", "recordbench-receipt-", "retained"),
    "temporary_account_browser": NameContract("recordbench-account-browser-", "recordbench-account-browser-", "retained"),
    "temporary_activity": NameContract("recordbench-activity-", "recordbench-activity-", "retained"),
    "temporary_ask_heading": NameContract("recordbench-ask-heading-", "recordbench-ask-heading-", "retained"),
    "temporary_assertions_browser": NameContract("recordbench-assertions-browser-", "recordbench-assertions-browser-", "retained"),
    "temporary_dock": NameContract("recordbench-dock-", "recordbench-dock-", "retained"),
    "temporary_briefing": NameContract("recordbench-briefing-", "recordbench-briefing-", "retained"),
    "temporary_cited_context_browser": NameContract("recordbench-cited-context-browser-", "recordbench-cited-context-browser-", "retained"),
    "temporary_compiled_report": NameContract("recordbench-compiled-report-", "recordbench-compiled-report-", "retained"),
    "temporary_dusk": NameContract("recordbench-dusk-", "recordbench-dusk-", "retained"),
    "temporary_email_browser": NameContract("recordbench-email-browser-", "recordbench-email-browser-", "retained"),
    "temporary_entities_browser": NameContract("recordbench-entities-browser-", "recordbench-entities-browser-", "retained"),
    "temporary_connections_browser": NameContract("recordbench-connections-browser-", "recordbench-connections-browser-", "retained"),
    "temporary_exact_search": NameContract("recordbench-exact-search-", "recordbench-exact-search-", "retained"),
    "temporary_full_text_browser": NameContract("recordbench-full-text-browser-", "recordbench-full-text-browser-", "retained"),
    "temporary_full_text_synthesis_browser": NameContract("recordbench-full-text-synthesis-browser-", "recordbench-full-text-synthesis-browser-", "retained"),
    "temporary_intake_browser": NameContract("recordbench-intake-browser-", "recordbench-intake-browser-", "retained"),
    "temporary_slice1a_browser": NameContract("recordbench-slice1a-browser-", "recordbench-slice1a-browser-", "retained"),
    "temporary_context_browser": NameContract("recordbench-context-browser-", "recordbench-context-browser-", "retained"),
    "temporary_knowledge_browser": NameContract("recordbench-knowledge-browser-", "recordbench-knowledge-browser-", "retained"),
    "temporary_rename_browser": NameContract("recordbench-rename-browser-", "recordbench-rename-browser-", "retained"),
    "temporary_preflight_browser": NameContract("recordbench-preflight-browser-", "recordbench-preflight-browser-", "retained"),
    "temporary_note_conflicts_browser": NameContract("recordbench-note-conflicts-browser-", "recordbench-note-conflicts-browser-", "retained"),
    "temporary_one_box": NameContract("recordbench-one-box-", "recordbench-one-box-", "retained"),
    "temporary_report_conflicts_browser": NameContract("recordbench-report-conflicts-browser-", "recordbench-report-conflicts-browser-", "retained"),
    "temporary_reports_browser": NameContract("recordbench-reports-browser-", "recordbench-reports-browser-", "retained"),
    "temporary_decision_conflicts_browser": NameContract("recordbench-decision-conflicts-browser-", "recordbench-decision-conflicts-browser-", "retained"),
    "temporary_review_report": NameContract("recordbench-review-report-", "recordbench-review-report-", "retained"),
    "temporary_folder_browser": NameContract("recordbench-folder-browser-", "recordbench-folder-browser-", "retained"),
    "temporary_occurrence_browser": NameContract("recordbench-occurrence-browser-", "recordbench-occurrence-browser-", "retained"),
    "temporary_cedar_browser": NameContract("recordbench-cedar-browser-", "recordbench-cedar-browser-", "retained"),
    "temporary_layout": NameContract("recordbench-layout-", "recordbench-layout-", "retained"),
    "temporary_zoom": NameContract("recordbench-zoom-", "recordbench-zoom-", "retained"),
    "temporary_context_upgrade": NameContract("recordbench-context-upgrade-", "recordbench-context-upgrade-", "retained"),
    "temporary_demo": NameContract("recordbench-demo-", "recordbench-demo-", "retained"),
    "temporary_discovery_migration": NameContract("recordbench-discovery-migration-", "recordbench-discovery-migration-", "retained"),
    "temporary_entity_restore": NameContract("recordbench-entity-restore-", "recordbench-entity-restore-", "retained"),
    "temporary_full_text_model": NameContract("recordbench-full-text-model-", "recordbench-full-text-model-", "retained"),
    "temporary_full_text_restore": NameContract("recordbench-full-text-restore-", "recordbench-full-text-restore-", "retained"),
    "temporary_people_acceptance": NameContract("recordbench-people-acceptance-", "recordbench-people-acceptance-", "retained"),
    "temporary_groups_acceptance": NameContract("recordbench-groups-acceptance-", "recordbench-groups-acceptance-", "retained"),
    "temporary_report_rollback": NameContract("recordbench-report-rollback-", "recordbench-report-rollback-", "retained"),
    "temporary_browser_ci": NameContract("recordbench-browser-ci-", "recordbench-browser-ci-", "retained"),
    "temporary_byte_match_reader": NameContract("recordbench-byte-match-reader-", "recordbench-byte-match-reader-", "retained"),
    "temporary_email_reader": NameContract("recordbench-email-reader-", "recordbench-email-reader-", "retained"),
    "temporary_receipt_rollback": NameContract("recordbench-receipt-rollback-", "recordbench-receipt-rollback-", "retained"),
    "temporary_preflight_rollback": NameContract("recordbench-preflight-rollback-", "recordbench-preflight-rollback-", "retained"),
    "temporary_occurrence_reader": NameContract("recordbench-occurrence-reader-", "recordbench-occurrence-reader-", "retained"),
    "temporary_ocr": NameContract("recordbench-ocr-", "recordbench-ocr-", "retained"),
})


def contract_errors(contracts=NAME_CONTRACTS) -> list[str]:
    """Reject incomplete pairs before generated consumers can hide a lost fallback."""
    errors = []
    for key, pair in contracts.items():
        if not all(isinstance(value, str) and value for value in (pair.legacy, pair.current)):
            errors.append(f"{key}: both names are required")
            continue
        if pair.behavior not in {"alias", "generation", "retained"}:
            errors.append(f"{key}: unknown resolution behavior")
        if (pair.legacy == pair.current) != (pair.behavior == "retained"):
            errors.append(f"{key}: retained names must agree; renamed names need a fallback")
        fields = lambda value: {field for _, field, _, _ in Formatter().parse(value) if field}
        if fields(pair.legacy) != fields(pair.current):
            errors.append(f"{key}: template fields differ")
    return errors


LEGACY_STEM = NAME_CONTRACTS["stem"].legacy
CURRENT_STEM = NAME_CONTRACTS["stem"].current
LEGACY_PRODUCT = NAME_CONTRACTS["product"].legacy
CURRENT_PRODUCT = NAME_CONTRACTS["product"].current


def name_value(key: str, names: str = CURRENT_STEM, **parts: str) -> str:
    """Resolve a validated generation without translating existing node state."""
    if names not in (LEGACY_STEM, CURRENT_STEM):
        raise ValueError("Unknown naming generation")
    pair = NAME_CONTRACTS[key]
    return (pair.legacy if names == LEGACY_STEM else pair.current).format(**parts)


LEGACY_ENV_PREFIX = name_value("environment", LEGACY_STEM, suffix="")
CURRENT_ENV_PREFIX = name_value("environment", suffix="")
LEGACY_ENV_NAME = LEGACY_ENV_PREFIX[:-1]
LEGACY_HEADER_PREFIX = name_value("header", LEGACY_STEM, suffix="")
CURRENT_HEADER_PREFIX = name_value("header", suffix="")
LEGACY_GIT_PREFIX = name_value("git_config", LEGACY_STEM, suffix="")
CURRENT_GIT_PREFIX = name_value("git_config", suffix="")

_LOG = logging.getLogger(__name__)
_warned: set[str] = set()
_lock = threading.Lock()


def warn_legacy(old: str, new: str) -> None:
    """Log a content-free deprecation once per name per process."""
    with _lock:
        if old in _warned:
            return
        _warned.add(old)
    _LOG.warning("%s is deprecated; use %s", old, new)


def sql_function(suffix: str) -> str:
    """Applied SQLite function name. The stored name is unchanged."""
    return name_value("sql_function", suffix=suffix)


def accepts_product(value: object) -> bool:
    """Accept either product string. Malformed JSON values fail closed."""
    return isinstance(value, str) and value in (LEGACY_PRODUCT, CURRENT_PRODUCT)
