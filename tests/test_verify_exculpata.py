"""Verification evidence and CLI contracts, without starting an application."""

import builtins
import json
from pathlib import Path
import runpy
import subprocess
import sys
from textwrap import dedent
from types import SimpleNamespace

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
    result = json.loads(path.read_text())
    assert result == {"schema_version": 1, "synthetic_only": True,
                      "runs": {"before": before, "after": after}}
    original = path.read_bytes()
    with pytest.raises(ValueError, match="already recorded"):
        verifier["write_evidence"](path, "before", after)
    assert path.read_bytes() == original


@pytest.mark.parametrize("content", ["broken JSON", "[]", '{"schema_version": 99}',
    '{"schema_version":1,"synthetic_only":false,"runs":{}}'])
def test_writer_preserves_unrecognized_files(verifier, tmp_path, content):
    path = tmp_path / "existing.json"
    # fixture-write: Synthetic malformed evidence must remain unchanged on rejection.
    path.write_text(content)
    with pytest.raises(ValueError):
        verifier["write_evidence"](path, "before", {"passed": True})
    assert path.read_text() == content


def test_serialization_failure_does_not_damage_baseline(verifier, tmp_path):
    path = tmp_path / "evidence.json"
    verifier["write_evidence"](path, "before", {"passed": True})
    original = path.read_bytes()
    with pytest.raises(ValueError):
        verifier["write_evidence"](path, "after", {"elapsed_ms": float("nan")})
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_arguments(verifier, tmp_path):
    path = tmp_path / "evidence.json"
    args = verifier["parse_args"](["--phase", "before", "--evidence", str(path)])
    assert args.phase == "before" and args.evidence == path
    assert args.timeout == 120.0
    assert args.checkout == verifier["ROOT"]
    assert args.allow_same_revision is False


@pytest.mark.parametrize("before_revision,allow_same,accepted", [
    ("a" * 40, False, True),
    ("b" * 40, False, False),
    ("b" * 40, True, True),
    (None, False, False),
])
def test_after_requires_distinct_checkout_revision(
        verifier, tmp_path, monkeypatch, before_revision, allow_same, accepted):
    checkout = tmp_path / "selected"
    checkout.mkdir()
    path = tmp_path / "evidence.json"
    if before_revision is not None:
        verifier["write_evidence"](path, "before", {"revision": before_revision})
    original = path.read_bytes() if path.exists() else None
    commands = []

    def git_output(command, *, cwd, text):
        assert cwd == checkout and text is True
        commands.append(command)
        return "b" * 40 + "\n" if command == ["git", "rev-parse", "HEAD"] else " M synthetic\n"

    calls = []
    monkeypatch.setattr(verifier["subprocess"], "check_output", git_output)
    monkeypatch.setitem(verifier, "run_verification", lambda args, report: calls.append(args.checkout))
    arguments = ["--phase", "after", "--checkout", str(checkout), "--evidence", str(path)]
    if allow_same:
        arguments.append("--allow-same-revision")
    assert verifier["main"](arguments) == (0 if accepted else 1)
    assert calls == ([checkout] if accepted else [])
    if accepted:
        report = json.loads(path.read_text())["runs"]["after"]
        assert report["revision"] == "b" * 40
        assert report["working_tree_dirty"] is True
        assert report["allow_same_revision"] is allow_same
        assert commands == [["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"]]
    else:
        assert (path.read_bytes() if path.exists() else None) == original


def test_before_records_selected_revision(verifier, tmp_path, monkeypatch):
    checkout = tmp_path / "base"
    checkout.mkdir()
    path = tmp_path / "evidence.json"

    def git_output(command, *, cwd, text):
        assert cwd == checkout
        return "a" * 40 + "\n" if command == ["git", "rev-parse", "HEAD"] else ""

    monkeypatch.setattr(verifier["subprocess"], "check_output", git_output)
    monkeypatch.setitem(verifier, "run_verification", lambda args, report: None)
    assert verifier["main"](["--phase", "before", "--checkout", str(checkout),
                             "--evidence", str(path)]) == 0
    report = json.loads(path.read_text())["runs"]["before"]
    assert report["revision"] == "a" * 40
    assert report["working_tree_dirty"] is False


def test_evidence_cannot_be_inside_selected_checkout(verifier, tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("Evidence location must be checked before starting the server")
    monkeypatch.setitem(verifier, "run_verification", forbidden)
    path = tmp_path / "evidence.json"
    assert verifier["main"](["--phase", "before", "--checkout", str(tmp_path),
                             "--evidence", str(path)]) == 1
    assert not path.exists()


@pytest.mark.parametrize("missing", ["scripts/demo.py", "src/case_intelligence/__init__.py"])
def test_incomplete_checkout_cannot_fall_back_to_head_imports(verifier, tmp_path, monkeypatch, missing):
    for relative in ("scripts/demo.py", "src/case_intelligence/__init__.py"):
        if relative != missing:
            path = tmp_path / relative
            path.parent.mkdir(parents=True)
            # fixture-write: A partial synthetic checkout must be rejected before imports can fall back.
            path.write_text("")
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
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    package = checkout / "src" / "case_intelligence"
    package.mkdir(parents=True)
    # fixture-write: A synthetic package distinguishes selected source from the installed checkout.
    (package / "__init__.py").write_text('SELECTED = True\n')
    # fixture-write: A minimal demo exercises checkout imports without application startup or runtime storage.
    (scripts / "demo.py").write_text(dedent('''\
        from contextlib import contextmanager
        from pathlib import Path
        from types import SimpleNamespace
        from case_intelligence import SELECTED
        CORPUS = Path(__file__).parent
        @contextmanager
        def seed_demo(path, *, timeout_seconds):
            assert SELECTED and timeout_seconds == 120.0
            yield SimpleNamespace(app="selected-app", client=SimpleNamespace(cookies={}))
    '''))
    harness = dedent('''\
        from contextlib import contextmanager
        import runpy
        import sys
        import httpx
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
def test_invalid_arguments_exit_without_running(verifier, monkeypatch, arguments):
    def forbidden(*args):
        pytest.fail("Argument rejection must not start the server")
    monkeypatch.setitem(verifier, "run_verification", forbidden)
    with pytest.raises(SystemExit) as exc:
        verifier["main"](arguments)
    assert exc.value.code == 2


def test_main_records_partial_failure_without_a_server(verifier, tmp_path, monkeypatch):
    def failed_run(args, report):
        report["checks"].append({"step": "load_case", "status": 500, "elapsed_ms": 3})
        raise RuntimeError("Synthetic failure")
    monkeypatch.setitem(verifier, "run_verification", failed_run)
    path = tmp_path / "evidence.json"
    assert verifier["main"](["--phase", "before", "--evidence", str(path)]) == 1
    run = json.loads(path.read_text())["runs"]["before"]
    assert run["passed"] is False and run["error_type"] == "RuntimeError"
    assert run["checks"][0]["status"] == 500
    assert run["total_ms"] >= 0


def test_duplicate_phase_is_rejected_before_running(verifier, tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("Existing evidence must be checked before starting the server")
    monkeypatch.setitem(verifier, "run_verification", forbidden)
    path = tmp_path / "evidence.json"
    verifier["write_evidence"](path, "before", {"passed": True})
    assert verifier["main"](["--phase", "before", "--evidence", str(path)]) == 1


@pytest.mark.parametrize("readable", [True, False])
def test_document_check_requires_extracted_text_without_a_server(verifier, readable):
    import httpx

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
        if readable:
            verifier["drive_flows"](client, demo, report, 40)
            assert all(row["passed"] for row in report["checks"])
        else:
            with pytest.raises(RuntimeError, match="open_document"):
                verifier["drive_flows"](client, demo, report, 40)
            assert report["checks"][-1]["passed"] is False
    assert requested == [prefix + suffix for suffix in
                         ("/home", "/processing-status", "/exact-search", "/sources/synthetic")]
