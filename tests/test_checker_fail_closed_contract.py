"""Shared rejection contract for checker interfaces present on main.

Rename and network source-policy adapters depend on #316 and #320; see
CODING_STANDARDS.md. This is a regression contract, not a soundness proof.
"""

import json
import importlib.util
from pathlib import Path
import subprocess
from textwrap import dedent

import pytest

from tests.fixture_write_policy import check_fixture_writes
from tests.test_publication_gate import publication


def _pr_body_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check-pr-body.py"
    spec = importlib.util.spec_from_file_location("pr_body_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pr_body(monkeypatch, unsupported):
    head = "not-a-sha" if unsupported else "a" * 40
    return _pr_body_module().check_body("Synthetic description", head, "main")


def _pr_body_allowance(monkeypatch, unsupported):
    allowed = "not-a-sha" if unsupported else "b1234567" * 5
    return _pr_body_module().check_body("Head: b123456", "a" * 40, "main", (allowed,))


def _fixture(monkeypatch, unsupported):
    handler = "except*" if unsupported else "except"
    source = dedent(f"""\
        from tests.fixture_write import fixture_write
        try:
            with fixture_write(bench.workspace) as connection:
                connection.execute('DELETE FROM workbench_matter')
        {handler} Exception:
            pass
    """)
    return [str(item) for item in check_fixture_writes(source, "synthetic.py")]


def _publication(monkeypatch, unsupported):
    # Only the external process is substituted; the content dispatcher, media
    # parser and byte scanner run unchanged on a synthetic response.
    payload = [] if unsupported else {"format": {"tags": {"title": "Synthetic"}}}
    monkeypatch.setattr(publication.shutil, "which", lambda name: "ffprobe")

    def probe(args, **kwargs):
        assert args[args.index("-show_entries") + 1] == "format_tags:stream_tags"
        assert args[args.index("-of") + 1] == "json"
        return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode())

    monkeypatch.setattr(publication.subprocess, "run", probe)
    return [str(item) for item in publication._scan_content(
        b"synthetic-media", name="synthetic.wav", location="synthetic.wav", deny=(),
    )]


def _gateway_logging(monkeypatch, unsupported):
    from tests.test_gateway_nginx import ROOT, assert_gateway_logging

    template = (ROOT / "deploy/gateway/default.conf.template").read_text()
    if unsupported:
        template += "\ninclude /synthetic/unreviewed.conf;\n"
    try:
        assert_gateway_logging(template)
    except AssertionError as error:
        return [str(error)]
    return []


def _law_gate(monkeypatch, unsupported):
    from tests.test_law_gate import load_gate

    try:
        load_gate().sha("unknown" if unsupported else "a" * 40)
    except ValueError as error:
        return [str(error)]
    return []


@pytest.mark.parametrize("check,helper", [
    pytest.param(_law_gate, "run", id="law-gate-invalid-sha"),
    pytest.param(_pr_body, "check_body", id="pr-body-invalid-head"),
    pytest.param(_pr_body_allowance, "check_body", id="pr-body-invalid-allowance"),
    pytest.param(_gateway_logging, "assert_gateway_logging", id="gateway-unreviewed-include"),
    pytest.param(_fixture, "fixture_write", id="fixture-python-exception-group"),
    pytest.param(_publication, "_scan_media", id="publication-non-object-metadata"),
])
def test_unmodeled_input_is_rejected_with_supported_helper(check, helper, monkeypatch):
    assert check(monkeypatch, False) == [], "supported control must remain accepted"
    findings = check(monkeypatch, True)
    assert findings, "checker silently accepted the selected unmodeled input"
    assert any("unsupported" in item and helper in item for item in findings), findings
