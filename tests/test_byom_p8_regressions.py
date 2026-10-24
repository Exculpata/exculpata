"""Synthetic regressions for the two P2 findings reproduced at 9c60d65."""
from types import SimpleNamespace

import pytest

from case_intelligence import embedding_index as indexes, external_generator as endpoints
from case_intelligence.service_endpoints import configured_service_hosts, validate_service_endpoint
from case_intelligence.workbench import CaseIntelligenceWorkbench
from tests.test_egress_proxy import proxy
from tests.test_oss_installer import installer


def test_empty_index_promotes_matching_identity(tmp_path):
    path = tmp_path / "embedding-index.json"
    assert indexes.record_embedding_index(path, model="m", revision="r", populated=False) == ""
    assert indexes.embedding_change_warning(indexes.load_embedding_index(path), model="m", revision="r") == indexes.UNKNOWN_WARNING
    for _ in range(2):
        assert indexes.record_embedding_index(path, model="m", revision="r", populated=True) == ""
        assert indexes.load_embedding_index(path) == {"model": "m", "revision": "r", "populated": True}
        assert indexes.embedding_change_warning(indexes.load_embedding_index(path), model="m", revision="r") == ""


def test_note_populated_index_and_read_only_status(tmp_path, monkeypatch):
    path = tmp_path / "state" / "embedding-index.json"
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", str(path))
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_MODEL", "m")
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_REVISION", "r")
    app_values = {"CASE_INTELLIGENCE_GENERATOR_BACKEND": "ollama"}
    compose_values = {"EXCULPATA_EMBEDDING_MODEL": "m", "EXCULPATA_EMBEDDING_REVISION": "r"}
    monkeypatch.setattr(installer, "_dotenv", lambda file: compose_values if file.name == "compose.env" else app_values)
    monkeypatch.setattr(installer, "_print_hardware_suggestion", lambda console: None)
    warnings = []
    console = SimpleNamespace(note=lambda message: None, warn=warnings.append)
    workbench = SimpleNamespace(postgres_ready=False)
    assert indexes.note_populated_index(populated=False) == ""
    assert CaseIntelligenceWorkbench._embedding_index_warning(workbench) == indexes.UNKNOWN_WARNING
    installer._doctor_generator(console, tmp_path, dry_run=True)
    assert warnings == [indexes.UNKNOWN_WARNING]
    assert indexes.note_populated_index(populated=True) == ""
    assert indexes.load_embedding_index(path) == {"model": "m", "revision": "r", "populated": True}
    assert indexes.note_populated_index(populated=True) == ""
    assert CaseIntelligenceWorkbench._embedding_index_warning(workbench) == ""
    warnings.clear()
    installer._doctor_generator(console, tmp_path, dry_run=True)
    assert warnings == []


@pytest.mark.parametrize("model,revision", [("m", "other"), ("other", "r")])
@pytest.mark.parametrize("populated", [False, True])
def test_changed_identity_is_not_overwritten(tmp_path, model, revision, populated):
    path = tmp_path / "embedding-index.json"
    path.write_text('{"model": "m", "revision": "r", "populated": ' + str(populated).lower() + '}')
    before = path.read_bytes()
    warning = indexes.record_embedding_index(path, model=model, revision=revision, populated=True)
    assert warning == (indexes.WARNING if populated else indexes.UNKNOWN_WARNING)
    assert path.read_bytes() == before


@pytest.mark.parametrize("contents", [None, "unreadable json"])
def test_unknown_populated_index_is_not_written(tmp_path, contents):
    path = tmp_path / "embedding-index.json"
    if contents is not None:
        path.write_text(contents)
    assert indexes.record_embedding_index(path, model="m", revision="r", populated=True) == indexes.UNKNOWN_WARNING
    assert path.read_text() == contents if contents is not None else not path.exists()


@pytest.mark.parametrize("host,ascii_host", [
    ("bücher.example", "xn--bcher-kva.example"),
    ("faß.example", "xn--fa-hia.example"),
    ("answers.example.test", "answers.example.test"),
])
def test_endpoint_host_and_url_agree(monkeypatch, host, ascii_host):
    resolved = []
    monkeypatch.setattr(endpoints, "_resolve_addresses", lambda name: resolved.append(name) or ())
    endpoint = endpoints.parse_generator_endpoint("ollama", f"https://{host}:8443", "m", remote_confirmed=True)
    assert endpoint.host == endpoint.allowed_host == ascii_host
    assert endpoint.url == f"https://{ascii_host}:8443"
    assert resolved == [ascii_host]
    assert not endpoint.local_network and not endpoint.container_loopback


def test_installer_saved_idn_is_accepted_by_app_and_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoints, "_resolve_addresses", lambda host: ())
    args = installer._parser().parse_args([
        "install", "--models", "none", "--external-generator", "ollama",
        "--external-generator-url", "https://bücher.example:8443",
        "--external-generator-model", "m", "--confirm-remote-generator",
    ])
    app_values, compose_values = {}, {}
    installer._apply_external_generator(args, app_values, compose_values, {"secrets": tmp_path})
    setting = "CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS"
    monkeypatch.setenv(setting, app_values[setting])
    assert "xn--bcher-kva.example" in configured_service_hosts(setting)
    assert compose_values["EXCULPATA_GENERATOR_ALLOWED_HOSTS"] == app_values[setting] == "xn--bcher-kva.example"
    saved_url = app_values["CASE_INTELLIGENCE_GENERATOR_URL"]
    assert validate_service_endpoint(saved_url, environment_name=setting, label="generator") == "https://xn--bcher-kva.example:8443"
    assert proxy.configured_destinations({"GENERATOR_EGRESS_URL": saved_url}) == {
        ("xn--bcher-kva.example", 8443): frozenset(),
    }


def test_invalid_idn_has_safe_actionable_error(monkeypatch):
    monkeypatch.setattr(endpoints, "_resolve_addresses", lambda host: pytest.fail("invalid IDN reached DNS"))
    with pytest.raises(endpoints.EndpointRejected, match=r"^The server host name is not a valid internationalized domain name\. Enter its ASCII \(xn--\) form\.$"):
        endpoints.parse_generator_endpoint("ollama", "https://☃.example", "m", remote_confirmed=True)
