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
    "git_config": NameContract("recordbench.{suffix}", "exculpata.{suffix}", "alias"),
    "host_root": NameContract("/srv/recordbench", "/srv/exculpata", "generation"),
    "app_env": NameContract("recordbench.env", "exculpata.env", "generation"),
    "storage_marker": NameContract(".recordbench-managed-storage.json", ".exculpata-managed-storage.json", "generation"),
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
    "local_users_group": NameContract("recordbench_users", "recordbench_users", "retained"),
    "local_admin_group": NameContract("recordbench_administrators", "recordbench_administrators", "retained"),
    "oidc_users_group": NameContract("RecordBench_Users", "RecordBench_Users", "retained"),
    "oidc_admin_group": NameContract("RecordBench_Administrators", "RecordBench_Administrators", "retained"),
    "login_rate_zone": NameContract("recordbench_login", "recordbench_login", "retained"),
    "nemotron_schema": NameContract("recordbench-nemotron-v1", "recordbench-nemotron-v1", "retained"),
    "nemotron_temporary": NameContract("recordbench-nemotron-", "recordbench-nemotron-", "retained"),
    "worker_gpu_lock": NameContract("/run/lock/recordbench-transcription-gpu.lock", "/run/lock/recordbench-transcription-gpu.lock", "retained"),
    "browser_name": NameContract("recordbench:{suffix}", "exculpata:{suffix}", "alias"),
    "browser_source_note": NameContract("recordbenchSourceNote", "exculpataSourceNote", "alias"),
    "portable_generator": NameContract("recordbench-qwen35-4b", "exculpata-qwen35-4b", "alias"),
    "installer_shim": NameContract("recordbench_install.py", "exculpata_install.py", "alias"),
    "backup_shim": NameContract("recordbench-backup.sh", "exculpata-backup.sh", "alias"),
    "backup_status_shim": NameContract("recordbench-backup-status.sh", "exculpata-backup-status.sh", "alias"),
    "restore_shim": NameContract("recordbench-restore-drill.sh", "exculpata-restore-drill.sh", "alias"),
    "workbench_cli": NameContract("recordbench-workbench", "exculpata-workbench", "alias"),
    "retrieval_cli": NameContract("recordbench-retrieval-worker", "exculpata-retrieval-worker", "alias"),
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
