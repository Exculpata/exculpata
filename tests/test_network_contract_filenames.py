"""Synthetic source inventory cases for local filenames and network targets."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "filename_contract", ROOT / "scripts" / "network-contract.py"
)
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


def reported_hosts(tmp_path, source):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    # fixture-write: Synthetic source is parsed by the scanner, never executed.
    (source_dir / "example.py").write_text(source + "\n", encoding="utf-8")
    return sorted(
        detail
        for _, _, kind, _, detail in contract.source_inventory(tmp_path)
        if kind == "host"
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('from pathlib import Path\nPath("x") / "suite.json"', id="path-division"),
        pytest.param('open("passages.jsonl")', id="open-jsonl"),
        pytest.param('answers_file = "answers.jsonl"', id="file-assignment"),
        pytest.param('from pathlib import Path\nPath("manifest.json")', id="path-argument"),
        pytest.param('import os\nos.path.join(d, "config.toml")', id="path-join"),
        pytest.param('notes_path = "notes.md"', id="path-assignment"),
    ],
)
def test_local_file_not_reported(tmp_path, source):
    assert reported_hosts(tmp_path, source) == []


@pytest.mark.parametrize(
    "source",
    [
        # Accepted gap: these host-shaped literals count as files in file context.
        pytest.param('from pathlib import Path\nPath("evil.py")', id="path-py"),
        pytest.param('open("evil.md")', id="open-md"),
        # Accepted gap: division counts syntactically, even with a non-Path left side.
        pytest.param('base = 1\nbase / "x.json"', id="non-path-division"),
    ],
)
def test_accepted_gap_file_not_reported(tmp_path, source):
    assert reported_hosts(tmp_path, source) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('"example.com"', "example.com", id="bare-host"),
        pytest.param('"api.example.org"', "api.example.org", id="subdomain"),
        pytest.param('"https://suite.json/x"', "suite.json", id="https-file-suffix"),
        pytest.param('"http://192.0.2.10"', "192.0.2.10", id="http-ip"),
        pytest.param('"203.0.113.8"', "203.0.113.8", id="bare-ip"),
        pytest.param('"evil.py"', "evil.py", id="bare-py"),
        pytest.param('"evil.md"', "evil.md", id="bare-md"),
        pytest.param('server_name = "evil.py"', "evil.py", id="server-name"),
        pytest.param('host_name = "evil.md"', "evil.md", id="host-name"),
        pytest.param('domain_name = "evil.json"', "evil.json", id="domain-name"),
        pytest.param(
            'from pathlib import Path\nPath(host="suite.json")',
            "suite.json", id="path-host-keyword",
        ),
        pytest.param(
            'from pathlib import Path\nPath(proxy="notes.md")',
            "notes.md", id="path-proxy-keyword",
        ),
        pytest.param(
            'from pathlib import Path\nPath(server="data.json")',
            "data.json", id="path-server-keyword",
        ),
        pytest.param(
            'from pathlib import Path\nPath(url="a.txt")',
            "a.txt", id="path-url-keyword",
        ),
        pytest.param('from pathlib import Path\nPath("evil.zip")', "evil.zip", id="path-zip"),
        pytest.param('from pathlib import Path\nPath("evil.sh")', "evil.sh", id="path-sh"),
        pytest.param('from pathlib import Path\nPath("evil.mov")', "evil.mov", id="path-mov"),
        pytest.param(
            'import httpx\nhttpx.Client(base_url="data.json")',
            "data.json", id="http-client",
        ),
        pytest.param(
            'import socket\nsocket.create_connection(("data.json", 443))',
            "data.json", id="socket-tuple",
        ),
        pytest.param(
            'import requests\nrequests.get(url="notes.md")',
            "notes.md", id="request-url",
        ),
        pytest.param('host="suite.json"', "suite.json", id="host-assignment"),
    ],
)
def test_network_target_still_reported(tmp_path, source, expected):
    assert reported_hosts(tmp_path, source) == [expected]


# Documented gap: wrapper calls such as fetch(file="evil.md") can hide host intent.
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            '"example.com:8443"', "example.com",
            marks=pytest.mark.xfail(
                strict=True, reason="Known gap: bare host:port literals are not inventoried"
            ),
            id="host-port",
        ),
        pytest.param(
            'x = "api"\nf"{x}.example.com"', "api.example.com",
            marks=pytest.mark.xfail(
                strict=True, reason="Known gap: f-string hosts are not reconstructed"
            ),
            id="f-string",
        ),
    ],
)
def test_known_host_gap(tmp_path, source, expected):
    assert reported_hosts(tmp_path, source) == [expected]
