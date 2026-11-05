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
        pytest.param('from pathlib import Path\nPath("manifest.json")', id="path-argument"),
        pytest.param('import os\nos.path.join(d, "config.toml")', id="path-join"),
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
        pytest.param('FILENAME = "suite.json"', "suite.json", id="filename-assignment"),
        pytest.param('answers_file = "answers.jsonl"', "answers.jsonl", id="file-assignment"),
        pytest.param('notes_path = "notes.md"', "notes.md", id="path-assignment"),
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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            '"example.com:8443"', "example.com",
            marks=pytest.mark.xfail(
                strict=True, reason=("Known gap: bare host:port literals are not inventoried; "
                                     "issue: to be filed; remove when host:port is inventoried")
            ),
            id="host-port",
        ),
        pytest.param(
            'x = "api"\nf"{x}.example.com"', "api.example.com",
            marks=pytest.mark.xfail(
                strict=True, reason=("Known gap: f-string hosts are not reconstructed; "
                                     "issue: to be filed; remove when f-string hosts are inventoried")
            ),
            id="f-string",
        ),
    ],
)
def test_known_host_gap(tmp_path, source, expected):
    assert reported_hosts(tmp_path, source) == [expected]


@pytest.mark.parametrize(
    "source",
    [
        'PurePath("a.json")',
        'import pathlib\npathlib.Path("a.json")',
        'from pathlib import Path as P\nP("a.json")',
        'from os.path import join as j\nj(d, "a.json")',
        'obj.open("a.json")',
        'obj.join(d, "a.json")',
        'Path(value="a.json")',
        '"a.json" / base',
        # Accepted gap: the nearest call ends the walk, even inside a request.
        'import requests\nrequests.get(str(Path("a.json")))',
    ],
)
def test_file_context_syntax(tmp_path, source):
    assert reported_hosts(tmp_path, source) == []


@pytest.mark.parametrize(
    "source",
    [
        'obj.output_file = "a.json"',
        'OUTPUT_FILENAME: str = "a.json"',
        'output_fname = "a.json"',
        'output_dir = "a.json"',
        'fetch(file="a.json")',
        'obj.output_file[0] = "a.json"',
        '(output_file,) = "a.json"',
        'name = "a.json"',
        'output_file = identity("a.json")',
        'Path(["a.json"])',
        'output_file = ["a.json"]',
        'output_file: "a.json" = value',
        'obj.host_file = "a.json"',
        'host_file = output_file = "a.json"',
        'PROXIES_FILE: str = "a.json"',
        'Path(url_path="a.json")',
        'Path(proxy_file="a.json")',
        'Path(proxies_file="a.json")',
        'connect(file="a.json")',
        'getaddrinfo(file="a.json")',
        'socket(file="a.json")',
        'HTTPConnection(file="a.json")',
        'HTTPSConnection(file="a.json")',
        'AsyncClient(file="a.json")',
        'Session(file="a.json")',
        'ClientSession(file="a.json")',
        'request(file="a.json")',
        'import requests\nrequests.get(file="a.json")',
        'from socket import connect as dial\ndial(file="a.json")',
        # Host keywords survive each allowed container on the bounded walk.
        'Path(host=("a.json",))',
        'Path(host=["a.json"])',
        'Path(host={"a.json"})',
        'Path(host={"key": "a.json"})',
        'Path(host=[*("a.json",)])',
    ],
)
def test_context_does_not_clear_host(tmp_path, source):
    assert reported_hosts(tmp_path, source) == ["a.json"]


@pytest.mark.parametrize("extension", "json jsonl toml yaml yml txt csv tsv md py pdf log".split())
def test_closed_extensions_need_file_context(tmp_path, extension):
    literal = "a." + extension
    source = f'"{literal}"\nPath("{literal}")\nPath(host="{literal}")'
    assert reported_hosts(tmp_path, source) == [literal, literal]


@pytest.mark.parametrize("literal", ["evil.pl", "evil.cc", "example.com", "api.example.org"])
def test_other_extensions_stay_reported_in_file_context(tmp_path, literal):
    assert reported_hosts(tmp_path, f'Path("{literal}")') == [literal]


def test_context_depth_limit_retains_host(tmp_path, monkeypatch):
    # A file keyword is insufficient when the bounded host check cannot finish.
    monkeypatch.setattr(contract, "MAX_LITERAL_CONTEXT_DEPTH", 1)
    source = 'Path(file="a.json")'
    assert reported_hosts(tmp_path, source) == ["a.json"]


def test_absolute_dns_name_retained(tmp_path):
    assert reported_hosts(tmp_path, 'Path("a.json.")') == ["a.json."]
