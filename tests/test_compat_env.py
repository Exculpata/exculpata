"""Synthetic alias contracts for process settings and saved node settings."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import logging

import pytest
from starlette.datastructures import Headers

from case_intelligence import compat_env


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(compat_env, "_warned", set())
    monkeypatch.setattr(compat_env, "_conflicts", set())
    monkeypatch.delenv("EXCULPATA_SYNTHETIC", raising=False)
    monkeypatch.delenv("RECORDBENCH_SYNTHETIC", raising=False)


@pytest.mark.parametrize("new,old,expected", [
    (None, None, "default"), ("new-value", None, "new-value"),
    (None, "old-value", "old-value"), ("same", "same", "same"),
    ("new-value", "old-value", "new-value"), ("", "old-value", "old-value"),
    (None, "", "default"), ("", None, "default"), ("", "", "default"),
    ("new-value", "", "new-value"),
])
@pytest.mark.parametrize("name", ["SYNTHETIC", "EXCULPATA_SYNTHETIC", "RECORDBENCH_SYNTHETIC"])
def test_env_aliases_precedence_and_content_free_warnings(monkeypatch, caplog, name, new, old, expected):
    for prefix, value in (("EXCULPATA", new), ("RECORDBENCH", old)):
        if value is not None:
            monkeypatch.setenv(f"{prefix}_SYNTHETIC", value)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert compat_env.env(name, "default") == expected
    assert sum("deprecated" in record.message for record in caplog.records) == bool(old)
    assert sum("differ" in record.message for record in caplog.records) == bool(new and old and new != old)
    assert "new-value" not in caplog.text and "old-value" not in caplog.text


def test_warning_is_once_per_name_under_concurrency_and_later_conflict(caplog):
    source = {"RECORDBENCH_SYNTHETIC": "old-value"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(lambda _: compat_env.env("SYNTHETIC", environ=source), range(100))) == {"old-value"}
    source["EXCULPATA_SYNTHETIC"] = "new-value"
    for _ in range(3):
        assert compat_env.env("SYNTHETIC", environ=source) == "new-value"
        assert compat_env.env("OTHER", environ={"RECORDBENCH_OTHER": "other"}) == "other"
    assert sum("deprecated" in record.message for record in caplog.records) == 2
    assert sum("differ" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("suffix", ["Authenticated-User", "Proxy-Secret", "Auth-Diagnostic", "Export", "Playback", "Theme"])
@pytest.mark.parametrize("mode", ["new-only", "old-only", "both-equal", "both-different"])
def test_header_alias_selection_is_case_insensitive_and_rejects_conflicts(suffix, mode):
    values = {}
    if mode != "old-only":
        values[f"x-exculpata-{suffix.lower()}"] = "synthetic"
    if mode != "new-only":
        values[f"x-recordbench-{suffix.lower()}"] = "different" if mode == "both-different" else "synthetic"
    headers = Headers(values)
    legacy = f"X-RecordBench-{suffix}"
    assert compat_env.header_conflicts(headers, legacy) is (mode == "both-different")
    assert compat_env.header(headers, legacy) == (None if mode == "both-different" else "synthetic")


@pytest.mark.parametrize("mode", ["new-only", "old-only", "both-equal", "both-different"])
def test_installer_and_backup_read_saved_aliases_and_update_only_existing_keys(tmp_path, mode):
    from tests.test_oss_installer import installer
    from tests.test_backup_contract import backup

    settings = {}
    if mode != "old-only":
        settings["EXCULPATA_HTTPS_PORT"] = "8443"
    if mode != "new-only":
        settings["RECORDBENCH_HTTPS_PORT"] = "9443" if mode == "both-different" else "8443"
    path = tmp_path / "compose.env"
    path.write_text("".join(f'{key}="{value}"\n' for key, value in settings.items()))
    path.chmod(0o600)
    for module in (installer, backup):
        saved = module._dotenv(path)
        assert module._required_env("RECORDBENCH_HTTPS_PORT", environ=saved) == "8443"
    assert installer._gateway_probe_address(installer._dotenv(path)) == "127.0.0.1"
    installer._replace_env(path, "RECORDBENCH_HTTPS_PORT", "7443")
    assert installer._dotenv(path) == {key: "7443" for key in settings}
    path.write_text(installer._env_text(settings, "Synthetic node", aliases=True))
    assert installer._dotenv(path) == {"EXCULPATA_HTTPS_PORT": "8443"}
    generated = installer._env_text({"RECORDBENCH_RELEASE_ID": "synthetic-release"}, "Synthetic node", aliases=True)
    assert 'EXCULPATA_RELEASE_ID="synthetic-release"' in generated
    assert "RECORDBENCH_" not in generated


def test_compose_aliases_do_not_require_any_new_setting():
    import re
    root = Path(__file__).parents[1]
    for name in ("compose.yaml", "compose.kerberos.yaml", "compose.local-accounts.yaml"):
        text = (root / name).read_text()
        aliases = re.findall(r"\$\{EXCULPATA_([A-Z0-9_]+):-\$\{RECORDBENCH_([A-Z0-9_]+):-[^{}]*}}", text)
        assert aliases
        assert all(new == old for new, old in aliases)
        assert text.count("${EXCULPATA_") == len(aliases)
        assert text.count("${RECORDBENCH_") == len(aliases)


@pytest.mark.parametrize("mode", ["new-only", "old-only", "both-equal", "both-different"])
def test_response_aliases_emit_authoritative_equal_values_despite_inbound_headers(tmp_path, mode):
    from fastapi.testclient import TestClient
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.generation import UnavailableGenerator
    from tests.test_matter_media_workflow import _matter, _upload_and_wait, ImmediateMediaProcessor

    inbound = {}
    for suffix in ("Export", "Playback", "Theme"):
        if mode != "old-only":
            inbound[f"X-Exculpata-{suffix}"] = "synthetic-forged-new"
        if mode != "new-only":
            inbound[f"X-RecordBench-{suffix}"] = "synthetic-forged-old" if mode == "both-different" else "synthetic-forged-new"
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", generator=UnavailableGenerator(),
                              media_processor=ImmediateMediaProcessor(), media_poll_seconds=0.01)
    with TestClient(app) as client:
        slug = _matter(client, "Synthetic alias response matter")
        _, token = _upload_and_wait(client, slug)
        responses = [
            (client.post("/preferences/theme", data={"theme": "dusk"}, headers=inbound, follow_redirects=False), "Theme", "dusk", 303),
            (client.get(f"/matters/{slug}/notebook/export", headers=inbound), "Export", "work-product", 200),
            (client.get(f"/matters/{slug}/sources/{token}/content", headers=inbound), "Playback", "original", 200),
        ]
        for response, suffix, expected, status in responses:
            assert response.status_code == status
            assert response.headers[f"X-Exculpata-{suffix}"] == expected
            assert response.headers[f"X-RecordBench-{suffix}"] == expected


@pytest.mark.parametrize("mode", ["new-only", "old-only", "both-equal", "both-different"])
@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_saved_node_resume_accepts_all_alias_layouts_without_new_requirements(tmp_path, mode, names):
    import json
    from tests.test_first_run_handoff import configured_node
    from tests.test_oss_installer import installer

    root, _, paths = configured_node(tmp_path, names=names)
    path = root / "compose.env"
    # P2 generated new-key environments while retaining legacy paths.
    path.write_text(installer._env_text(installer._dotenv(path), "Synthetic P2 aliases", aliases=True))
    original = installer._dotenv(path)
    assert not any(key.startswith("RECORDBENCH_") for key in original)
    old = {key.replace("EXCULPATA_", "RECORDBENCH_", 1): value
           for key, value in original.items() if key.startswith("EXCULPATA_")}
    assert old
    selected = {}
    for key, value in old.items():
        if mode != "new-only":
            selected[key] = "synthetic-shadowed" if mode == "both-different" else value
        if mode != "old-only":
            selected[key.replace("RECORDBENCH_", "EXCULPATA_", 1)] = value
    path.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in selected.items()))
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume"])
    installer._saved_node_arguments(args, root)
    assert args.auth == "local" and args.models == "none"
    assert args.https_port == 8443
    assert args.storage_root == paths["storage"]
    assert args.account_root == paths["accounts"]


@pytest.mark.parametrize("mode", ["new-only", "old-only", "both-equal", "both-different"])
def test_backup_reads_aliases_and_restores_frozen_synthetic_state(tmp_path, monkeypatch, mode):
    import json
    from tests.test_backup_consistency import SyntheticNode, _version
    from tests.test_backup_contract import backup

    node = SyntheticNode(tmp_path, monkeypatch)
    try:
        path = node.node / "compose.env"
        original = backup._dotenv(path)
        selected = {}
        for key, value in original.items():
            if mode != "new-only":
                selected[key] = "synthetic-shadowed" if mode == "both-different" else value
            if mode != "old-only":
                selected[key.replace("RECORDBENCH_", "EXCULPATA_", 1)] = value
        path.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in selected.items()))
        assert node.backup() == 0
        assert node.restore() == 0
        assert _version(node.root / "restored/payload/runtime/workbench.sqlite") == "frozen"
    finally:
        node.close()


@pytest.mark.parametrize("module_name", ["workbench", "retrieval_worker"])
@pytest.mark.parametrize("new,old,allowed", [("1", None, True), (None, "1", True), ("1", "1", True), ("0", "1", False), ("1", "0", True)])
def test_container_listener_aliases_preserve_the_bind_guard(monkeypatch, module_name, new, old, allowed):
    import importlib
    module = importlib.import_module(f"case_intelligence.{module_name}")
    for prefix, value in (("EXCULPATA", new), ("RECORDBENCH", old)):
        key = f"{prefix}_ALLOW_CONTAINER_BIND"
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTH_MODE", "local")
    monkeypatch.setattr("sys.argv", ["synthetic-worker", "--host", "0.0.0.0"])
    if module_name == "workbench":
        monkeypatch.setattr(module, "create_workbench_app", lambda *args, **kwargs: object())
    calls = []
    monkeypatch.setattr(module.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    if allowed:
        module.main()
        assert calls[0]["host"] == "0.0.0.0"
    else:
        with pytest.raises(SystemExit) as failure:
            module.main()
        assert failure.value.code == 2
        assert calls == []


@pytest.mark.parametrize("new,old", [("1", None), (None, "1"), ("1", "1"), ("1", "0")])
def test_installed_auth_guard_recognizes_either_environment_name(monkeypatch, new, old):
    from case_intelligence.identity import resolve_auth_mode
    for prefix, value in (("EXCULPATA", new), ("RECORDBENCH", old)):
        key = f"{prefix}_ALLOW_CONTAINER_BIND"
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="installed service"):
        resolve_auth_mode("preview")


@pytest.mark.parametrize("mode", ["new-only", "old-only", "both-equal", "both-different"])
def test_backup_status_projection_reads_preferred_receipt(tmp_path, monkeypatch, mode):
    import json
    from case_intelligence.workbench import _backup_status_projection

    preferred, legacy = tmp_path / "preferred.json", tmp_path / "legacy.json"
    preferred.write_text(json.dumps({"state": "initialized"}))
    legacy.write_text(json.dumps({"state": "failed"}))
    for prefix in ("EXCULPATA", "RECORDBENCH"):
        monkeypatch.delenv(f"{prefix}_BACKUP_STATUS_FILE", raising=False)
    if mode != "old-only":
        monkeypatch.setenv("EXCULPATA_BACKUP_STATUS_FILE", str(preferred))
    if mode != "new-only":
        monkeypatch.setenv("RECORDBENCH_BACKUP_STATUS_FILE", str(legacy if mode == "both-different" else preferred))
    assert _backup_status_projection()["state"] == "initialized"


@pytest.mark.parametrize("prefix", ["EXCULPATA", "RECORDBENCH"])
def test_saved_required_settings_keep_missing_key_failures(prefix):
    with pytest.raises(KeyError):
        compat_env.required_env(f"{prefix}_STORAGE_ROOT", environ={})
    with pytest.raises(KeyError):
        compat_env.required_env(f"{prefix}_STORAGE_ROOT", environ={f"{prefix}_STORAGE_ROOT": ""})
