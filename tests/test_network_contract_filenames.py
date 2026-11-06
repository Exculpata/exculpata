"""Synthetic source inventory cases for local filenames and network targets."""

import ast
from collections import Counter
import importlib.util
from pathlib import Path
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "filename_contract", ROOT / "scripts" / "network-contract.py"
)
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


def reported_hosts(tmp_path, source):
    source_dir = tmp_path / "src"
    source_dir.mkdir(parents=True)
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
        pytest.param('notes_path = "notes.md"', id="path-assignment"),
        pytest.param('answers_file = "answers.jsonl"', id="file-assignment"),
        pytest.param('FILENAME = "suite.json"', id="filename-assignment"),
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
        # Accepted gap: neutral variable indirection is not tracked across statements.
        pytest.param(
            'from pathlib import Path\nimport requests\nu = Path("evil.md")\nrequests.get(u)',
            id="neutral-variable-indirection",
        ),
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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            '"example.com:8443"', "example.com",
            marks=pytest.mark.xfail(
                strict=True, reason=("Known gap: bare host:port literals are not inventoried; "
                                     "https://github.com/Exculpata/exculpata/issues/487; remove when host:port is inventoried")
            ),
            id="host-port",
        ),
        pytest.param(
            'x = "api"\nf"{x}.example.com"', "api.example.com",
            marks=pytest.mark.xfail(
                strict=True, reason=("Known gap: f-string hosts are not reconstructed; "
                                     "https://github.com/Exculpata/exculpata/issues/487; remove when f-string hosts are inventoried")
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
        'from pathlib import PurePath\nPurePath("a.json")',
        'output_dir = "a.json"',
        'output_fname = "a.json"',
        'OUTPUT_FILENAME: str = "a.json"',
        'obj.output_file = "a.json"',
        'import pathlib\npathlib.Path("a.json")',
        'from pathlib import Path as P\nP("a.json")',
        'from os.path import join as j\nj(d, "a.json")',
        'from pathlib import Path\nPath(value="a.json")',
        '"a.json" / base',
    ],
)
def test_file_context_syntax(tmp_path, source):
    assert reported_hosts(tmp_path, source) == []


@pytest.mark.parametrize(
    "source",
    [
        'from pathlib import Path\nimport requests\nrequests.get(str(Path("a.json")))',
        'obj.join(d, "a.json")',
        'obj.open("a.json")',
        'obj.output_file[0] = "a.json"',
        '(output_file,) = "a.json"',
        'name = "a.json"',
        'output_file = identity("a.json")',
        'from pathlib import Path\nPath(["a.json"])',
        'output_file = ["a.json"]',
        'output_file: "a.json" = value',
        'obj.host_file = "a.json"',
        'host_file = output_file = "a.json"',
        'PROXIES_FILE: str = "a.json"',
        'from pathlib import Path\nPath(url_path="a.json")',
        'from pathlib import Path\nPath(proxy_file="a.json")',
        'from pathlib import Path\nPath(proxies_file="a.json")',
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
        'from pathlib import Path\nPath(host=("a.json",))',
        'from pathlib import Path\nPath(host=["a.json"])',
        'from pathlib import Path\nPath(host={"a.json"})',
        'from pathlib import Path\nPath(host={"key": "a.json"})',
        'from pathlib import Path\nPath(host=[*("a.json",)])',
    ],
)
def test_context_does_not_clear_host(tmp_path, source):
    assert reported_hosts(tmp_path, source) == ["a.json"]


@pytest.mark.parametrize("extension", "json jsonl toml yaml yml txt csv tsv md py pdf log".split())
def test_closed_extensions_need_file_context(tmp_path, extension):
    literal = "a." + extension
    source = f'from pathlib import Path\n"{literal}"\nPath("{literal}")\nPath(host="{literal}")'
    assert reported_hosts(tmp_path, source) == [literal, literal]


@pytest.mark.parametrize("literal", ["evil.pl", "evil.cc", "example.com", "api.example.org"])
def test_other_extensions_stay_reported_in_file_context(tmp_path, literal):
    assert reported_hosts(tmp_path, f'from pathlib import Path\nPath("{literal}")') == [literal]


def test_context_depth_limit_retains_host(tmp_path, monkeypatch):
    # A file keyword is insufficient when the bounded host check cannot finish.
    monkeypatch.setattr(contract, "MAX_LITERAL_CONTEXT_DEPTH", 1)
    source = 'from pathlib import Path\nPath(file="a.json")'
    assert reported_hosts(tmp_path, source) == ["a.json"]


def test_absolute_dns_name_retained(tmp_path):
    assert reported_hosts(tmp_path, 'from pathlib import Path\nPath("a.json.")') == ["a.json."]


@pytest.mark.parametrize("source", [
    'from pathlib import Path\nimport requests\nrequests.get(Path("evil.md"))',
    'from pathlib import Path\nimport socket\nsocket.create_connection((str(Path("evil.md")), 443))',
    'from pathlib import Path\nimport httpx\nhttpx.Client(base_url=str(Path("evil.md")))',
    'import requests\nrequests.get(base / "evil.md")',
    'import websockets\nwebsockets.connect(root / "evil.md")',
    'import webbrowser\nwebbrowser.open("evil.md")',
    'import telnetlib\ntelnetlib.Telnet().open("evil.md")',
    'from pathlib import Path\nserver_host = Path("evil.md")',
    'proxy_url = base / "evil.md"',
    'base_url = open("evil.md")',
    'from pathlib import Path\nhost = str(Path("evil.md"))',
    'proxy_url = base / "evil.json"',
    'from pathlib import Path\nimport smtplib\nsmtplib.SMTP(Path("evil.md"))',
    'from pathlib import Path\nimport ftplib\nftplib.FTP(Path("evil.py"))',
    'from pathlib import Path\nimport socket\nsocket.gethostbyname(Path("evil.py"))',
    'from pathlib import Path\nimport ssl\nssl.get_server_certificate((Path("evil.py"), 443))',
    'from pathlib import Path\nimport asyncio\nasyncio.open_connection(Path("evil.md"), 80)',
    'from pathlib import Path\nimport httpx\nhttpx.stream("GET", Path("evil.md"))',
    'from pathlib import Path\nimport httpx\nhttpx.Client().build_request("GET", Path("evil.md"))',
    'from pathlib import Path\nimport requests\nrequests.Request("GET", Path("evil.md"))',
    'from pathlib import Path\nimport aiohttp\nawait s.ws_connect(Path("evil.md"))',
    'from pathlib import Path\nimport websockets\nwebsockets.serve(h, Path("evil.md"))',
    'from pathlib import Path\nimport telnetlib\ntelnetlib.Telnet(Path("evil.md"))',
    'from pathlib import Path\nimport imaplib\nimaplib.IMAP4_SSL(Path("evil.md"))',
    'from pathlib import Path\nimport poplib\npoplib.POP3(Path("evil.md"))',
    'from pathlib import Path\nimport webbrowser\nwebbrowser.open_new(Path("evil.md"))',
    'from pathlib import Path\nimport webbrowser\nwebbrowser.open_new_tab(Path("evil.md"))',
    'from pathlib import Path\nimport webbrowser\nwebbrowser.open(Path("evil.md"))',
    'import webbrowser\nwebbrowser.open(base / "evil.md")',
    'from pathlib import Path\nfrom webbrowser import open as browse\nbrowse(Path("evil.md"))',
    'import webbrowser as browser\nbrowser.open(base / "evil.md")',
    'from pathlib import Path\nfrom smtplib import SMTP as MailClient\nMailClient(Path("evil.md"))',
    'from pathlib import Path\nfrom socket import gethostbyname as lookup\nlookup(Path("evil.py"))',
])
def test_review_host_regressions(tmp_path, source):
    expected = next(host for host in ("evil.json", "evil.py", "evil.md") if host in source)
    assert reported_hosts(tmp_path, source) == [expected]


@pytest.mark.parametrize("source", [
    'import os.path as paths\npaths.join(root, "a.json")',
    'from pathlib import PurePath as P\nP("a.json")',
    'data_file = other = "a.json"',
    'obj.DATA_PATH: str = "a.json"',
    'from pathlib import Path\nopen(Path("a.json"))',
    'open(base / "a.json")',
])
def test_supported_file_forms(tmp_path, source):
    assert reported_hosts(tmp_path, source) == []


@pytest.mark.parametrize("source", [
    'join(root, "a.json")',
    'from pathlib import Path\nobj.Path("a.json")',
    'from pathlib import Path\nobj.PurePath("a.json")',
    'from webbrowser import open as browse\nbrowse("a.json")',
    'from telnetlib import Telnet\nTelnet().open("a.json")',
    'from pathlib import Path\nobj.server_file = str(Path("a.json"))',
    'PROXIES_FILE: str = root / "a.json"',
    'from pathlib import Path\nhost = output_file = Path("a.json")',
    'from pathlib import Path\nfrom requests import get as fetch\nfetch(Path("a.json"))',
    'requests.get((root / "a.json").read_text())',
    'from pathlib import Path\nrequests.get(Path("a.json") if ready else fallback)',
    'from pathlib import Path\nrequests.get([Path("a.json")][0])',
    'from pathlib import Path\nrequests.get({"target": Path("a.json")})',
    'from pathlib import Path\nrequests.get(*(Path("a.json"),))',
    'from pathlib import Path\nhost = (path := Path("a.json"))',
    'from pathlib import Path\nwrap(wrap(wrap(wrap(wrap(wrap(wrap(wrap(Path("a.json")))))))))',
])
def test_nested_host_and_unmodeled_forms(tmp_path, source):
    assert reported_hosts(tmp_path, source) == ["a.json"]


@pytest.mark.parametrize("call", [
    'smtplib.SMTP_SSL(Path("evil.md"))', 'smtplib.LMTP(Path("evil.md"))',
    'imaplib.IMAP4(Path("evil.md"))', 'imaplib.IMAP4_stream(Path("evil.md"))',
    'poplib.POP3_SSL(Path("evil.md"))', 'ftplib.FTP_TLS(Path("evil.md"))',
    'nntplib.NNTP(Path("evil.md"))', 'xmlrpc.client.ServerProxy(Path("evil.md"))',
    'socket.gethostbyname_ex(Path("evil.md"))', 'socket.gethostbyaddr(Path("evil.md"))',
    'socket.getfqdn(Path("evil.md"))',
    'socketserver.TCPServer((Path("evil.md"), 443), handler)',
    'http.server.HTTPServer((Path("evil.md"), 443), handler)',
    'asyncio.start_server(handler, Path("evil.md"), 443)',
    'sock.bind((Path("evil.md"), 443))', 'sock.sendto(data, (Path("evil.md"), 443))',
    'urllib3.connection_from_url(Path("evil.md"))', 'frobnicate(Path("evil.md"))',
])
def test_unknown_outer_call_keeps_host(tmp_path, call):
    assert reported_hosts(tmp_path, 'from pathlib import Path\n' + call) == ["evil.md"]


@pytest.mark.parametrize("source", [
    'fetch(file="a.json")', 'export(FILENAME="a.json")',
    'import posixpath\nposixpath.join(root, "a.json")',
    'import ntpath\nntpath.join(root, "a.json")',
    'sep.join([root / "a.json"])',
    'json.load(open("a.json"))', 'pd.read_csv(Path("a.json"))',
    'json.loads(Path("a.json").read_text())', 'shutil.copy(Path("a.json"), dest)',
    'await Path("a.json")', '(lambda: Path("a.json"))',
    '[Path("a.json") for x in xs]', '(p := Path("a.json"))',
])
def test_unknown_calls_and_nodes_fail_closed(tmp_path, source):
    assert reported_hosts(tmp_path, 'from pathlib import Path\n' + source) == ["a.json"]


@pytest.mark.parametrize("source", [
    'Path("a.json")',
    'from pathlib import Path\nPath = local\nPath("a.json")',
    'from pathlib import Path\ndef f(Path):\n return Path("a.json")',
    'from pathlib import Path\ndef f():\n Path = local\n return Path("a.json")',
    'def f():\n from pathlib import Path\nPath("a.json")',
    'from other import Path\nPath("a.json")',
    'from .pathlib import Path\nPath("a.json")',
    'open = local\nopen("a.json")',
    'def f(open):\n return open("a.json")',
    'import os\nos = local\nos.path.join(d, "a.json")',
    'from os import path\npath.join = local\npath.join(d, "a.json")',
])
def test_file_calls_require_unshadowed_identity_including_rebound_names(tmp_path, source):
    assert reported_hosts(tmp_path, source) == ["a.json"]


@pytest.mark.parametrize("source", [
    'with open("a.json") as f:\n pass',
    'from os import path\npath.join(d, "a.json")',
    'import pathlib as p\np.Path("a.json")',
    'import builtins as b\nb.open("a.json")',
    'def f():\n from pathlib import Path as P\n return P("a.json")',
    'from pathlib import Path\nx = Path("a.json")\nrequests.get(x)',
])
def test_pinned_file_controls(tmp_path, source):
    assert reported_hosts(tmp_path, source) == []


@pytest.mark.parametrize("wrappers, expected", [(30, []), (31, ["a.json"])])
def test_pinned_depth_boundary(tmp_path, wrappers, expected):
    source = 'from pathlib import Path\n' + 'open(' * wrappers + 'Path("a.json")' + ')' * wrappers
    assert contract.MAX_LITERAL_CONTEXT_DEPTH == 32
    assert reported_hosts(tmp_path, source) == expected


def test_safe_set_mutation_is_detected(tmp_path, monkeypatch):
    source = 'from pathlib import Path\nPath("a.json")'
    assert reported_hosts(tmp_path / "control", source) == []
    monkeypatch.setattr(contract, "FILE_SAFE_CALLS", frozenset())
    assert reported_hosts(tmp_path / "mutant", source) == ["a.json"]


def test_nearest_call_mutation_is_detected(tmp_path, monkeypatch):
    source = 'from pathlib import Path\nfrobnicate(Path("a.json"))'
    assert reported_hosts(tmp_path / "control", source) == ["a.json"]

    def nearest_only(node, parents, resolve):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.Call):
                return resolve(node.func) not in contract.FILE_SAFE_CALLS
        return False

    monkeypatch.setattr(contract, "host_literal_context", nearest_only)
    assert reported_hosts(tmp_path / "mutant", source) == []


def test_exact_safe_identities():
    assert contract.FILE_SAFE_CALLS == frozenset({
        ("pathlib", "Path"), ("pathlib", "PurePath"),
        ("builtins", "open"), ("os", "path", "join"),
    })


@pytest.mark.parametrize("expression", [
    'open(P)', 'P.name', '[P][0]', 'P / base', '-P', 'P and ready',
    'P == other', 'P if ready else other', '(*[P],)', '{"key": P}', '{P}',
    'f"{P}"', 'open(file=P)',
])
def test_every_listed_ancestor_reaches_outer_call(tmp_path, expression):
    source = 'from pathlib import Path\nfrobnicate(' + expression.replace('P', 'Path("a.json")') + ')'
    assert reported_hosts(tmp_path, source) == ["a.json"]


@pytest.mark.parametrize("source", [
    'Path = remote\n@Path("a.json")\ndef f():\n from pathlib import Path',
    'Path = remote\nclass C(Path("a.json")):\n from pathlib import Path',
    'Path = remote\ndef f() -> Path("a.json"):\n from pathlib import Path',
    'Path = remote\nasync def f() -> Path("a.json"):\n from pathlib import Path',
])
def test_definition_expressions_use_enclosing_scope(tmp_path, source):
    assert reported_hosts(tmp_path, source) == ["a.json"]


def test_nonlocal_rebinding_cannot_grant_file_safety(tmp_path):
    source = '''def outer():
 from pathlib import Path
 def mutate():
  nonlocal Path
  Path = remote
 mutate()
 return Path("a.json")
'''
    assert reported_hosts(tmp_path, source) == ["a.json"]


def test_measured_deleted_occurrences_have_no_remaining_findings(monkeypatch):
    rows = []
    for line in (ROOT / "docs/NETWORK_EGRESS.md").read_text().splitlines():
        columns = [cell.strip().strip('`') for cell in line.split('|')[1:-1]]
        if len(columns) == 6 and columns[0].isdigit():
            _, path, digest, detail, extension, context = columns
            assert extension in contract.FILE_EXTENSIONS
            assert context in {"call", "division", "identifier"}
            rows.append((path, "host", digest, detail))
    assert rows
    deleted = Counter(rows)
    observed = Counter((p, k, d, h) for p, _, k, d, h in contract.source_inventory(ROOT))
    monkeypatch.setattr(contract, "file_literal_context", lambda *args: None)
    unsuppressed = Counter((p, k, d, h) for p, _, k, d, h in contract.source_inventory(ROOT))
    assert unsuppressed - observed == deleted
    # Occurrences are a multiset: retained duplicates must not be removed.
    assert unsuppressed - deleted == observed
    policy = tomllib.loads((ROOT / "config/egress-allowlist.toml").read_text())
    assert Counter((r["path"], r["kind"], r["digest"], r["detail"])
                   for r in policy["source"]) == observed
