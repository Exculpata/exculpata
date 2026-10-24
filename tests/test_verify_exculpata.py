"""Verification evidence and CLI contracts, without starting an application."""

import builtins
from contextlib import nullcontext
import json
from pathlib import Path
import runpy
import subprocess
import sys
from textwrap import dedent
from types import SimpleNamespace

import httpx
import pytest

@pytest.fixture
def verifier():
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify-exculpata.py"))
    return namespace["main"].__globals__

def test_evidence_preserves_before_and_after_and_refuses_replacement(verifier, tmp_path):
    path = tmp_path / "evidence.json"
    before = {"passed": False, "checks": [{"status": 503, "elapsed_ms": 2.5}]}
    after = {"passed": True, "checks": [{"status": 200, "elapsed_ms": 1.25}]}
    verifier["write_evidence"](path, "before", before)
    verifier["write_evidence"](path, "after", after)
    assert json.loads(path.read_text()) == {"schema_version": 1, "synthetic_only": True,
                      "runs": {"before": before, "after": after}}
    original = path.read_bytes()
    with pytest.raises(ValueError, match="already recorded"):
        verifier["write_evidence"](path, "before", after)
    assert path.read_bytes() == original

@pytest.mark.parametrize("content", ["broken JSON", "[]", '{"schema_version": 99}',
    '{"schema_version":1,"synthetic_only":false,"runs":{}}', None])
def test_writer_preserves_files_on_failure(verifier, tmp_path, content):
    path = tmp_path / "evidence.json"
    if content is None:
        verifier["write_evidence"](path, "before", {"passed": True})
    else:
        # fixture-write: Synthetic malformed evidence must remain unchanged on rejection.
        path.write_text(content)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        verifier["write_evidence"](path, "after" if content is None else "before",
                                   {"elapsed_ms": float("nan")} if content is None else {"passed": True})
    assert path.read_bytes() == original
    if content is not None:
        assert path.read_text() == content
    assert list(tmp_path.iterdir()) == [path]

def test_arguments(verifier, tmp_path):
    path = tmp_path / "evidence.json"
    args = verifier["parse_args"](["--phase", "before", "--evidence", str(path)])
    assert args.phase == "before" and args.evidence == path
    assert args.timeout == 120.0
    assert args.checkout == verifier["ROOT"]
    assert args.allow_same_revision is False

@pytest.fixture
def selected_checkout(verifier, tmp_path, monkeypatch):
    checkout = tmp_path / "selected"
    checkout.mkdir()
    state = SimpleNamespace(checkout=checkout, path=tmp_path / "evidence.json",
                            revision="b" * 40, dirty=" M synthetic\n", commands=[], calls=[])
    def git_output(command, *, cwd, text):
        assert cwd == checkout and text is True
        state.commands.append(command)
        return state.revision + "\n" if command == ["git", "rev-parse", "HEAD"] else state.dirty
    monkeypatch.setattr(verifier["subprocess"], "check_output", git_output)
    monkeypatch.setitem(verifier, "run_verification", lambda args, report: state.calls.append(args.checkout))
    return state

def run_main(verifier, path, phase="before", checkout=None, extra=()):
    return verifier["main"](["--phase", phase, "--evidence", str(path),
                             "--checkout", str(checkout or verifier["ROOT"]), *extra])

@pytest.mark.parametrize("phase,before_revision,allow_same,accepted", [
    ("before", None, False, True), ("after", "a" * 40, False, True),
    ("after", "b" * 40, False, False), ("after", "b" * 40, True, True),
    ("after", None, False, False)])
def test_selected_revision_and_after_requires_distinct_revision(
        verifier, selected_checkout, phase, before_revision, allow_same, accepted):
    state = selected_checkout
    if phase == "before":
        state.revision, state.dirty = "a" * 40, ""
    if before_revision is not None:
        verifier["write_evidence"](state.path, "before", {"revision": before_revision})
    original = state.path.read_bytes() if state.path.exists() else None
    extra = ["--allow-same-revision"] if allow_same else []
    assert run_main(verifier, state.path, phase, state.checkout, extra) == (0 if accepted else 1)
    assert state.calls == ([state.checkout] if accepted else [])
    if accepted:
        report = json.loads(state.path.read_text())["runs"][phase]
        assert report["revision"] == state.revision
        assert report["working_tree_dirty"] is (phase == "after")
        assert report["allow_same_revision"] is allow_same
        assert state.commands == [["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"]]
    else:
        assert (state.path.read_bytes() if state.path.exists() else None) == original

@pytest.fixture
def forbid_verification(verifier, monkeypatch):
    def forbidden(*args):
        pytest.fail("Rejection must happen before starting the server")
    monkeypatch.setitem(verifier, "run_verification", forbidden)

@pytest.mark.parametrize("duplicate", [False, True])
def test_preflight_rejection(verifier, tmp_path, forbid_verification, duplicate):
    path = tmp_path / "evidence.json"
    if duplicate:
        verifier["write_evidence"](path, "before", {"passed": True})
    assert run_main(verifier, path, checkout=None if duplicate else tmp_path) == 1
    if not duplicate:
        assert not path.exists()

def test_evidence_inside_other_checkout_is_refused(verifier, selected_checkout, capsys):
    state = selected_checkout
    assert not state.checkout.is_relative_to(verifier["ROOT"])
    path = state.checkout / "evidence.json"
    assert run_main(verifier, path, checkout=state.checkout) == 1
    assert "Evidence must be outside the checkout" in capsys.readouterr().err
    assert not path.exists()
    assert state.commands == state.calls == []

def write_checkout(checkout, files):
    for relative, content in files.items():
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # fixture-write: Synthetic checkout sources exercise import selection and incomplete-tree rejection.
        path.write_text(content)

@pytest.mark.parametrize("missing", ["scripts/demo.py", "src/case_intelligence/__init__.py"])
def test_incomplete_checkout_cannot_fall_back_to_head_imports(verifier, tmp_path, monkeypatch, missing):
    write_checkout(tmp_path, {relative: "" for relative in
        ("scripts/demo.py", "src/case_intelligence/__init__.py") if relative != missing})
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "demo":
            pytest.fail("Incomplete checkout must be rejected before importing the demo")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    args = SimpleNamespace(checkout=tmp_path, timeout=120)
    with pytest.raises(ValueError, match="--checkout"):
        verifier["run_verification"](args, {})

def test_selected_checkout_supplies_seed_corpus_and_app(verifier, tmp_path):
    checkout = tmp_path / "selected"
    write_checkout(checkout, {"src/case_intelligence/__init__.py": 'SELECTED = True\n',
                             "scripts/demo.py": dedent('''\
        from contextlib import contextmanager
        from pathlib import Path
        from types import SimpleNamespace
        from case_intelligence import SELECTED
        CORPUS = Path(__file__).parent
        @contextmanager
        def seed_demo(path, *, timeout_seconds):
            assert SELECTED and timeout_seconds == 120.0
            yield SimpleNamespace(app="selected-app", client=SimpleNamespace(cookies={}))
    ''')})
    harness = dedent('''\
        from contextlib import contextmanager
        import runpy
        import sys
        ns = runpy.run_path(sys.argv[1])["main"].__globals__
        args = ns["parse_args"](["--phase", "before", "--checkout", sys.argv[2],
                                 "--evidence", sys.argv[3]])
        @contextmanager
        def serve(app, timeout):
            assert app == "selected-app"
            yield "http://127.0.0.1"
        def drive(client, demo, report, expected):
            assert expected >= 1
            report["selected_flow"] = True
        ns["serve_loopback"] = serve
        ns["drive_flows"] = drive
        report = {}
        ns["run_verification"](args, report)
        assert report["selected_flow"] is True
    ''')
    result = subprocess.run([sys.executable, "-c", harness, str(verifier["ROOT"] /
        "scripts/verify-exculpata.py"), str(checkout), str(tmp_path / "evidence.json")],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

@pytest.mark.parametrize("arguments", [[], ["--phase", "before"],
    ["--phase", "unknown", "--evidence", "/tmp/synthetic.json"],
    *[["--phase", "after", "--evidence", "/tmp/synthetic.json", "--timeout", value]
      for value in ("0", "-1", "nan", "inf", "invalid")],
    ["--phase", "before", "--evidence", "/tmp/synthetic.json", "--host", "192.0.2.1"],
])
def test_invalid_arguments_exit_without_running(verifier, forbid_verification, arguments):
    with pytest.raises(SystemExit) as exc:
        verifier["main"](arguments)
    assert exc.value.code == 2

def test_main_records_partial_failure_without_a_server(verifier, tmp_path, monkeypatch):
    def failed_run(args, report):
        report["checks"].append({"step": "load_case", "status": 500, "elapsed_ms": 3})
        raise RuntimeError("Synthetic failure")
    monkeypatch.setitem(verifier, "run_verification", failed_run)
    path = tmp_path / "evidence.json"
    assert run_main(verifier, path) == 1
    run = json.loads(path.read_text())["runs"]["before"]
    assert run["passed"] is False and run["error_type"] == "RuntimeError"
    assert run["checks"][0]["status"] == 500
    assert run["total_ms"] >= 0

@pytest.mark.parametrize("readable", [True, False])
def test_document_check_requires_extracted_text_without_a_server(verifier, readable):
    prefix = "/matters/m-synthetic"
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if request.url.path.endswith("processing-status"):
            return httpx.Response(200, json={"total_count": 40, "saved_count": 40,
                "searchable_count": 39, "attention_count": 1, "processing_count": 0,
                "can_query": True, "partial_query": True, "active_work": {}})
        if request.url.path.endswith("exact-search"):
            return httpx.Response(200, text='<h2 id="find-results-heading">24 sources found</h2>'
                f'<a class="find-open-source" href="{prefix}/sources/synthetic?unit=1">Open</a>')
        if "/sources/" in request.url.path:
            text = '<main data-review-workspace>Selected searchable section'
            return httpx.Response(200, text=text + ('<pre>Vela Quenrix</pre>' if readable else ''))
        return httpx.Response(200, text="Harbor Street")

    report = {"checks": []}
    demo = SimpleNamespace(matter=SimpleNamespace(slug="m-synthetic"), home_path=prefix + "/home")
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)) as client:
        with nullcontext() if readable else pytest.raises(RuntimeError, match="open_document"):
            verifier["drive_flows"](client, demo, report, 40)
        if readable:
            assert all(row["passed"] for row in report["checks"])
        else:
            assert report["checks"][-1]["passed"] is False
    assert requested == [prefix + suffix for suffix in
                         ("/home", "/processing-status", "/exact-search", "/sources/synthetic")]
