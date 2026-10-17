"""Docs and drivers stay aligned with the retained compatibility names."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess

from case_intelligence.legacy_names import (
    CURRENT_ENV_PREFIX,
    CURRENT_HEADER_PREFIX,
    CURRENT_PRODUCT,
    CURRENT_STEM,
    LEGACY_ENV_PREFIX,
    LEGACY_HEADER_PREFIX,
    LEGACY_PRODUCT,
    LEGACY_STEM,
    accepts_product,
)
from tests.test_oss_installer import ROOT, installer


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_accepts_product_rejects_non_strings_without_raising():
    assert accepts_product(LEGACY_PRODUCT) is True
    assert accepts_product(CURRENT_PRODUCT) is True

    class Unhashable:
        __hash__ = None

    for value in (None, 1, [], {}, Unhashable()):
        assert accepts_product(value) is False


def test_storage_marker_with_array_product_fails_closed(tmp_path):
    for marker_name, storage_id in (
        (".exculpata-managed-storage.json", f"{CURRENT_STEM}-storage-synthetic"),
        (f".{LEGACY_STEM}-managed-storage.json", f"{LEGACY_STEM}-storage-synthetic"),
    ):
        storage = tmp_path / marker_name
        storage.mkdir()
        (storage / marker_name).write_text(json.dumps({
            "format_version": 1,
            "product": [],
            "storage_id": storage_id,
        }))
        assert installer._matter_storage_layout_valid(storage) is False


def test_authentication_docs_keep_legacy_headers_and_secrets_mount():
    text = _read("docs/AUTHENTICATION.md")
    secrets = f"/run/{LEGACY_STEM}-secrets/local-accounts.json"
    assert secrets in text
    assert f"/run/{CURRENT_STEM}-secrets/local-accounts.json" not in text
    for suffix in ("Authenticated-User", "Proxy-Secret", "Auth-Diagnostic"):
        assert f"{CURRENT_HEADER_PREFIX}{suffix}" in text
        assert f"{LEGACY_HEADER_PREFIX}{suffix}" in text
    assert f"{LEGACY_HEADER_PREFIX}*" in text
    assert f"{CURRENT_HEADER_PREFIX}*" in text


def test_local_account_docs_keep_mounted_container_paths():
    text = _read("docs/LOCAL_ACCOUNT_BROWSER.md")
    mounted = f"/var/lib/{LEGACY_STEM}-accounts"
    assert mounted in text
    assert f"{mounted}/local-accounts.json" in text
    assert f"/var/lib/{CURRENT_STEM}-accounts" not in text


def test_backup_docs_keep_legacy_coordinates_and_database():
    text = _read("docs/STORAGE_AND_BACKUP.md")
    assert f"`{LEGACY_STEM}`" in text
    assert f"`{LEGACY_STEM}-backup-<hash>`" in text
    assert f"`{CURRENT_STEM}-backup-<hash>`" in text
    assert f"the `{LEGACY_STEM}` user and database" in text


def test_configuration_docs_show_the_deployed_fallback():
    text = _read("docs/CONFIGURATION.md")
    expression = "${" + CURRENT_ENV_PREFIX + "X:-${" + LEGACY_ENV_PREFIX + "X:-default}}"
    swapped = "${" + CURRENT_ENV_PREFIX + "X:-${" + CURRENT_ENV_PREFIX + "X:-default}}"
    assert f"`{LEGACY_ENV_PREFIX}*`" in text
    assert expression in text
    assert swapped not in text
    install = _read("docs/INSTALL.md")
    assert f"unset image namespace to `{CURRENT_STEM}`" in install
    assert f"an unset project name to\n`{LEGACY_STEM}`" in install


def test_qa_launchers_and_drivers_share_environment_names():
    helper = (
        "const qa = (name) => process.env[`" + CURRENT_ENV_PREFIX
        + "QA_${name}`] || process.env[`" + LEGACY_ENV_PREFIX + "QA_${name}`];"
    )
    direct = "process.env." + LEGACY_ENV_PREFIX + "QA_"
    for name in ("qa-people-browser.cjs", "qa-team-groups-browser.cjs"):
        text = _read("scripts/" + name)
        assert helper in text
        assert direct not in text
        assert "qa('ORIGIN')" in text
        assert "qa('ARTIFACTS')" in text
        assert "qa('BROWSER_CHANNEL')" in text
    for name in ("qa-people-browser.py", "qa-team-groups-browser.py"):
        text = _read("scripts/" + name)
        assert CURRENT_ENV_PREFIX + "QA_ORIGIN" in text
        assert CURRENT_ENV_PREFIX + "QA_ARTIFACTS" in text
    node = shutil.which("node")
    if node is None:
        return
    script = "\n".join((
        helper,
        "const current = " + repr(CURRENT_ENV_PREFIX) + ";",
        "const legacy = " + repr(LEGACY_ENV_PREFIX) + ";",
        "process.env[current + 'QA_ORIGIN'] = 'https://current.example';",
        "process.env[legacy + 'QA_ORIGIN'] = 'https://legacy.example';",
        "if (qa('ORIGIN') !== 'https://current.example') process.exit(2);",
        "delete process.env[current + 'QA_ORIGIN'];",
        "if (qa('ORIGIN') !== 'https://legacy.example') process.exit(3);",
        "delete process.env[legacy + 'QA_ORIGIN'];",
        "if (qa('ORIGIN')) process.exit(4);",
    ))
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _module(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_sample_selects_the_current_app_env_file():
    sample = _read(".env.example")
    assert f"{CURRENT_ENV_PREFIX}APP_ENV_FILE={CURRENT_STEM}.env" in sample
    assert f"{CURRENT_ENV_PREFIX}STORAGE_NAMES={CURRENT_STEM}" in sample
    compose = _read("compose.yaml")
    default = (
        "${" + CURRENT_ENV_PREFIX + "APP_ENV_FILE:-${"
        + LEGACY_ENV_PREFIX + "APP_ENV_FILE:-" + LEGACY_STEM + ".env}}"
    )
    assert default in compose


def test_recovery_database_role_stays_on_the_legacy_name():
    text = _read("docs/ISOLATED_NODE_RECOVERY.md")
    assert f"-U {LEGACY_STEM} -d {LEGACY_STEM}" in text
    assert f"-U {CURRENT_STEM} -d {CURRENT_STEM}" not in text


def test_upgraded_nodes_keep_their_root_account_and_admin():
    text = _read("docs/FIRST_RUN.md")
    assert f"/srv/{LEGACY_STEM}" in text
    assert f"`{LEGACY_STEM}`" in text
    assert f"`{LEGACY_STEM}.admin`" in text


def test_authentication_names_the_legacy_container_bind_setting():
    assert f"{LEGACY_ENV_PREFIX}ALLOW_CONTAINER_BIND" in _read("docs/AUTHENTICATION.md")


def test_storage_docs_name_both_marker_files():
    text = _read("docs/STORAGE_AND_BACKUP.md")
    assert f".{LEGACY_STEM}-managed-storage.json" in text
    assert f".{CURRENT_STEM}-managed-storage.json" in text


def test_exact_search_docs_keep_the_parser_ids():
    text = _read("docs/EXACT_SEARCH.md")
    assert f"{LEGACY_STEM}-exact-v1" in text
    assert f"{LEGACY_STEM}-exact-v2" in text
    assert f"{CURRENT_STEM}-exact-v1" not in text
    assert f"{CURRENT_STEM}-exact-v2" not in text


def test_whisperx_pin_keeps_the_built_local_version():
    pin = "3.8.6+" + LEGACY_STEM + ".1"
    assert pin in _read("services/transcription/compatibility/README.md")
    assert pin in _read("services/transcription/src/transcription_v2/resources.py")
    assert "3.8.6+" + CURRENT_STEM + ".1" not in _read("docs/BRANDING.md")


def test_container_probes_read_either_environment_prefix(monkeypatch):
    suffix = "GENERATOR_CONTAINER_TEST"
    current = CURRENT_ENV_PREFIX + suffix
    legacy = LEGACY_ENV_PREFIX + suffix
    for relative, name in (
        ("tests/test_generator_cache_container.py", "generator_cache_container"),
        ("tests/test_model_stager_container.py", "model_stager_container"),
    ):
        module = _module(relative, name)
        monkeypatch.delenv(current, raising=False)
        monkeypatch.delenv(legacy, raising=False)
        assert module._either(suffix) is None
        monkeypatch.setenv(legacy, "legacy-probe")
        assert module._either(suffix) == "legacy-probe"
        monkeypatch.setenv(current, "current-probe")
        assert module._either(suffix) == "current-probe"


def test_documented_identifiers_match_code_or_the_legacy_list():
    checker = _module("scripts/doc_identifier_check.py", "doc_identifier_check")
    assert checker.findings(ROOT) == []
    corpus, legacy = checker.load(ROOT)
    renamed = f"Use `{CURRENT_STEM}-exact-v1` for this query.\n"
    assert checker.findings_in_text("synthetic.md", renamed, corpus, legacy)
    retained = f"Use `{LEGACY_STEM}-exact-v1` for this query.\n"
    assert checker.findings_in_text("synthetic.md", retained, corpus, legacy) == []
    role = "```\npg_restore -U " + CURRENT_STEM + " -d " + CURRENT_STEM + "\n```\n"
    found = checker.findings_in_text("synthetic.md", role, corpus, legacy)
    assert any(item.token == f"-U {CURRENT_STEM}" for item in found)
    example = f"Restore into `/srv/{CURRENT_STEM}-restore-test`.\n"
    assert checker.findings_in_text("synthetic.md", example, corpus, legacy) == []
    mount = f"Cache at `/var/lib/{CURRENT_STEM}/model-cache`.\n"
    assert checker.findings_in_text("synthetic.md", mount, corpus, legacy)


def test_browser_readiness_harnesses_use_the_dispatched_event():
    producer = _read("src/case_intelligence/static/case-intelligence.js")
    assert f'CustomEvent("{CURRENT_STEM}:readiness"' in producer
    for driver in ("matter-knowledge", "email-coverage"):
        source = _read(f"scripts/browser-accept-{driver}.py")
        assert "addEventListener('{CURRENT_STEM}:readiness'" in source
        assert "{LEGACY_STEM}:readiness" not in source


def test_activity_readiness_events_initialize_and_preserve_both_names():
    """Execute the activity closure so a helper outside its scope cannot pass."""
    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the browser event regression")
    source = _read("src/case_intelligence/static/case-intelligence.js")
    activity = source.split(
        "/* A workspace-wide activity drawer keeps background work visible across pages. */", 1
    )[1].split("/* Compact matter navigation, persisted appearance, and the cross-workspace assistant. */", 1)[0]
    script = r'''
const assert = require('node:assert/strict');
const delays = [];
const element = {querySelector: () => ({}), addEventListener: () => {}};
globalThis.document = {
  body: {}, querySelector: () => element, addEventListener: () => {}
};
globalThis.window = new EventTarget();
window.clearTimeout = () => {};
window.setTimeout = (callback, delay) => { delays.push(delay); return delays.length; };
''' + activity + r'''
assert.deepEqual(delays, [1500], 'initialization must reach the first poll');
for (const name of EVENT_NAMES) {
  const before = delays.length;
  window.dispatchEvent(new Event(name));
  assert.equal(delays.length, before + 1, name + ' must schedule an activity refresh');
  assert.equal(delays.at(-1), 1500);
}
window.dispatchEvent(new Event('pagehide'));
const paused = delays.length;
for (const name of EVENT_NAMES) window.dispatchEvent(new Event(name));
assert.equal(delays.length, paused, 'hidden pages must stay paused');
window.dispatchEvent(new Event('pageshow'));
assert.equal(delays.length, paused + 1, 'returning pages must resume polling');
'''.replace("EVENT_NAMES", json.dumps([f"{CURRENT_STEM}:readiness", f"{LEGACY_STEM}:readiness"]))
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
