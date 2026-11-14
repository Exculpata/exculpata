"""Tests for tools/check_spec.py.

Run: python -m pytest tests/test_check_spec.py   (from the repository root)
Real-repo regression cases use CHECK_SPEC_REPO (required; no default)
and are skipped, loudly, when that checkout is missing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "tools" / "check_spec.py"
FIXTURES = HERE / "fixtures" / "check_spec"
REAL_REPO = Path(os.environ["CHECK_SPEC_REPO"]) if os.environ.get("CHECK_SPEC_REPO") else None

FORBIDDEN_RULE = '''
def _forbidden(source, target, layers):
    source_layer = layers[source]
    target_layer = layers[target]
    if source_layer in {"app_shell", "evals"}:
        return False
    if source_layer.startswith("core."):
        return not target_layer.startswith("core.")
    if source_layer.startswith("modules."):
        return not (target_layer.startswith("core.") or target_layer == source_layer)
    raise AssertionError(source)
'''


def run(spec: Path, repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), str(spec), "--repo", str(repo), *extra],
        capture_output=True, text=True, timeout=120,
    )


def flags(out: str, check: str) -> list[str]:
    # A run that never reached its summary is not a clean run.
    assert "\ncheck_spec: " in "\n" + out, f"no summary line in output:\n{out}"
    return [line for line in out.splitlines() if line.startswith(f"FLAG ({check}")]


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def synthetic_repo(root: Path) -> Path:
    write(root, ".github/workflows/weekly.yml", """
        name: Weekly report
        on:
          workflow_dispatch:
        jobs:
          measure:
            runs-on: ubuntu-24.04
            steps:
              - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567
              - name: Build report
                run: python scripts/report.py --output report.json
              - name: File findings
                run: |
                  for kind in a b; do
                    python scripts/filer.py --kind "$kind" --report report.json
                  done
        """)
    write(root, "scripts/report.py", """
        import argparse
        import report_helper
        def main():
            p = argparse.ArgumentParser()
            p.add_argument("--output", required=True)
            p.parse_args()
        """)
    write(root, "scripts/report_helper.py", "VALUE = 1\n")
    write(root, "scripts/filer.py", """
        import argparse
        import importlib.util
        SCANNER = 'scripts/scanner.py'
        def main():
            p = argparse.ArgumentParser()
            for name in ('kind', 'run'):
                p.add_argument('--' + name, required=True)
            p.add_argument('--report', required=True)
            p.parse_args()
        """)
    write(root, "scripts/scanner.py", "def scan(text):\n    return True\n")
    write(root, "src/pkg/__init__.py", "")
    write(root, "src/pkg/core_store.py", "STORE = 1\n")
    write(root, "src/pkg/insight_gaps.py", "def gaps():\n    return []\n")
    write(root, "src/pkg/source_scan.py", "")
    write(root, "src/pkg/old_core.py", "from pkg import insight_gaps\n")
    layers = {
        "pkg": "app_shell",
        "pkg.core_store": "core.storage",
        "pkg.insight_gaps": "modules.insight",
        "pkg.source_scan": "core.sources",
        "pkg.old_core": "core.sources",
        "pkg.messages": "core.contracts",
    }
    write(root, "tests/snapshots/import_layers.json", json.dumps(layers, indent=2))
    write(root, "tests/snapshots/import_graph.json", json.dumps({
        "forbidden_edges": ["pkg.old_core -> pkg.insight_gaps"],
        "component_sizes": [1], "cycles": [],
    }))
    write(root, "tests/test_import_boundaries.py",
          'LAYER_SNAPSHOT = "tests/snapshots/import_layers.json"\n' + FORBIDDEN_RULE)
    write(root, "src/pkg/messages.py", 'NOTICE = "Bates labels not read: unlabeled document"\n')
    return root


class SyntheticCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = synthetic_repo(self.root / "repo")
        self.specs = self.root / "specs"
        self.specs.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def spec(self, text: str) -> Path:
        return write(self.specs, "spec.md", text)


class TestAllowedPaths(SyntheticCase):
    def test_flags_called_script_outside_allowed_paths(self) -> None:
        spec = self.spec("""
            # Fix weekly report
            APPROVED: Owner 9:00 AM ET 11/2, item 1: "1 yes".
            Symptom: `.github/workflows/weekly.yml` fails at the filer step.
            Files: only .github/workflows/weekly.yml and tests/.
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        flagged = "\n".join(flags(result.stdout, "a"))
        self.assertIn("scripts/filer.py", flagged)
        self.assertIn("scripts/report.py", flagged)

    def test_follows_local_imports_and_path_loads(self) -> None:
        spec = self.spec("""
            # Fix
            Failing check: .github/workflows/weekly.yml
            Files: .github/workflows/weekly.yml
            """)
        out = run(spec, self.repo).stdout
        flagged = "\n".join(flags(out, "a"))
        self.assertIn("scripts/report_helper.py", flagged)
        self.assertIn("scripts/scanner.py", flagged)

    def test_approved_line_widening_counts_as_allowed(self) -> None:
        spec = self.spec("""
            # Fix
            APPROVED: Owner 9:00 AM ET 11/2, item 2: "2 yes". Change only .github/workflows/, scripts/ and tests/.
            Failing check: .github/workflows/weekly.yml
            Files: only that workflow file.
            """)
        result = run(spec, self.repo)
        self.assertEqual(flags(result.stdout, "a"), [], result.stdout)

    def test_workflow_named_by_title_on_failing_line(self) -> None:
        spec = self.spec("""
            # Fix
            The Weekly report workflow fails every Monday.
            Files: tests/test_x.py
            """)
        self.assertIn("scripts/filer.py", "\n".join(flags(run(spec, self.repo).stdout, "a")))

    def test_no_named_check_is_reported_unchecked_not_passed(self) -> None:
        spec = self.spec("""
            # Docs change
            Files: docs/X.md
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r"(?m)^UNCHECKED \(a\)")


class TestCallArgs(SyntheticCase):
    def test_missing_required_and_unknown_flags_are_flagged(self) -> None:
        spec = self.spec("""
            # Fix
            Failing check: .github/workflows/weekly.yml
            Files: .github/workflows/, scripts/
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 1, result.stdout)
        args = [line for line in flags(result.stdout, "c") if "scripts/filer.py" in line]
        self.assertTrue(args, result.stdout)
        self.assertIn("--run", "\n".join(args))

    def test_matching_call_args_pass(self) -> None:
        wf = self.repo / ".github/workflows/weekly.yml"
        wf.write_text(wf.read_text().replace(
            '--report report.json', '--report report.json --run "$URL"'))
        spec = self.spec("""
            # Fix
            Failing check: .github/workflows/weekly.yml
            Files: .github/workflows/, scripts/
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(flags(result.stdout, "c"), [])


class TestImportBoundary(SyntheticCase):
    def test_prescribed_reuse_across_layers_is_flagged(self) -> None:
        spec = self.spec("""
            # New source parser
            ## Files
            - New `src/pkg/source_new.py` (ADR family `source_*`, core.sources).
            ## What it must do
            1. Report gaps (reuse `insight_gaps.py`).
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 1, result.stdout)
        flagged = "\n".join(flags(result.stdout, "b"))
        self.assertIn("pkg.source_new -> pkg.insight_gaps", flagged)

    def test_layer_inferred_from_family_prefix(self) -> None:
        spec = self.spec("""
            # New source parser
            Files: new src/pkg/source_other.py
            It must import `pkg.insight_gaps` for gap math.
            """)
        self.assertTrue(flags(run(spec, self.repo).stdout, "b"))

    def test_clean_reuse_of_core_passes(self) -> None:
        spec = self.spec("""
            # New source parser
            ## Files
            - New `src/pkg/source_new.py` (core.sources).
            - `tests/snapshots/import_layers.json` (one new entry).
            ## What it must do
            1. Store results (reuse `core_store.py`).
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r"(?m)^OK \(b\).*pkg.source_new -> pkg.core_store")

    def test_negated_reuse_is_not_a_prescription(self) -> None:
        spec = self.spec("""
            # New source parser
            Files: new `src/pkg/source_new.py` (core.sources).
            Do not import `insight_gaps.py`; pin parity in tests instead.
            No new imports from `insight_gaps.py`.
            """)
        result = run(spec, self.repo)
        edges = [line for line in flags(result.stdout, "b") if "insight_gaps" in line]
        self.assertEqual(edges, [], result.stdout)

    def test_new_module_needs_layer_map_inside_fence(self) -> None:
        spec = self.spec("""
            # New source parser
            Files: new `src/pkg/source_new.py` (core.sources), tests/test_source_new.py
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("tests/snapshots/import_layers.json", "\n".join(flags(result.stdout, "b")))

    def test_new_module_with_layer_map_in_fence_passes(self) -> None:
        spec = self.spec("""
            # New source parser
            Files: new `src/pkg/source_new.py` (core.sources), tests/snapshots/import_layers.json
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_existing_baseline_edge_passes(self) -> None:
        spec = self.spec("""
            # Touch old core
            Files: src/pkg/old_core.py
            Keep it: reuse `insight_gaps.py` as today.
            """)
        result = run(spec, self.repo)
        self.assertEqual(flags(result.stdout, "b"), [], result.stdout)


class TestExpectedOutput(SyntheticCase):
    def test_spec_expecting_absent_line_that_main_emits_is_flagged(self) -> None:
        spec = self.spec("""
            # Fix
            Files: src/pkg/messages.py
            Expected output: no "Bates labels not read" line for b.pdf.
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("src/pkg/messages.py", "\n".join(flags(result.stdout, "c")))

    def test_new_expected_line_is_unchecked_not_silent(self) -> None:
        spec = self.spec("""
            # Fix
            Files: src/pkg/messages.py
            The report prints "Load file parsed cleanly" at the end.
            """)
        result = run(spec, self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r'(?m)^UNCHECKED \(c\).*Load file parsed cleanly')

    def test_summary_line_counts(self) -> None:
        spec = self.spec("# Empty\nFiles: docs/X.md\n")
        out = run(spec, self.repo).stdout
        self.assertRegex(out, r"(?m)^check_spec: \d+ flagged, \d+ unchecked, \d+ ok$")


class TestCli(unittest.TestCase):
    def test_missing_spec_is_usage_error(self) -> None:
        result = run(Path("/nonexistent/spec.md"), Path("/nonexistent/repo"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("spec not found", result.stderr)

    def test_stdlib_only(self) -> None:
        import ast
        allowed = set(sys.stdlib_module_names) | {"__future__"}
        tree = ast.parse(TOOL.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                self.assertIn(name.split(".")[0], allowed, name)


@unittest.skipUnless(REAL_REPO is not None and (REAL_REPO / ".github/workflows").is_dir(),
                     "set CHECK_SPEC_REPO to a checkout of the repository at the base these cases were recorded on")
class TestRegressionsOnMain(unittest.TestCase):
    """The three cases that cost approval rounds on 11/2."""

    def assert_filer_flagged(self, name: str) -> None:
        result = run(FIXTURES / name, REAL_REPO)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        flagged = flags(result.stdout, "a")
        self.assertTrue(any("scripts/hygiene_issue.py" in line for line in flagged), result.stdout)

    def test_item33_spec_flags_filer_script(self) -> None:
        self.assert_filer_flagged("ci-hygiene-checkout-token.item33.md")

    def test_item36_widened_spec_still_flags_filer_script(self) -> None:
        self.assert_filer_flagged("ci-hygiene-checkout-token.item36.md")

    def test_scripts_widening_covers_filer(self) -> None:
        out = run(FIXTURES / "ci-hygiene-checkout-token.item39-synthetic.md", REAL_REPO).stdout
        self.assertFalse(any("scripts/hygiene_issue.py" in line for line in flags(out, "a")), out)

    def test_lf1_spec_flags_bates_gaps_reuse(self) -> None:
        result = run(FIXTURES / "roadmap-load-files.lf1-approved.md", REAL_REPO)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        flagged = "\n".join(flags(result.stdout, "b"))
        self.assertIn("case_intelligence.source_load_files -> case_intelligence.bates_gaps", flagged)

    def test_lf1_r2_wording_has_no_bates_gaps_edge(self) -> None:
        out = run(FIXTURES / "roadmap-load-files.lf1-r2-synthetic.md", REAL_REPO).stdout
        edges = [line for line in flags(out, "b") if "-> case_intelligence.bates_gaps" in line]
        self.assertEqual(edges, [], out)

    def test_lf1_fence_matches_current_layer_map(self) -> None:
        out = run(FIXTURES / "roadmap-load-files.lf1-approved.md", REAL_REPO).stdout
        layers = json.loads((REAL_REPO / "tests/snapshots/import_layers.json").read_text())
        needs_entry = "case_intelligence.source_load_files" not in layers
        self.assertEqual(any("tests/snapshots/import_layers.json" in line
                             for line in flags(out, "b")), needs_entry, out)


def test_rule_headers_never_execute(tmp_path):
    for header in ("decorator", "default", "annotation", "body"):
        repo = synthetic_repo(tmp_path / header)
        marker = tmp_path / (header + ".marker")
        effect = f"__import__('pathlib').Path({str(marker)!r}).touch()"
        rule = FORBIDDEN_RULE
        if header == "decorator":
            rule = "@" + effect + "\n" + rule.lstrip()
        elif header == "default":
            rule = rule.replace("layers):", f"layers={effect}):")
        elif header == "annotation":
            rule = rule.replace("source,", f"source: {effect},")
        else:
            rule = f"def _forbidden(source, target, layers):\n    return True or {effect}\n"
        write(repo, "tests/test_import_boundaries.py", '# import_layers.json\n' + rule)
        spec = write(tmp_path, header + ".md", "# Synthetic\nFiles: src/pkg/source_scan.py\nReuse `insight_gaps.py`.\n")
        result = run(spec, repo)
        assert not marker.exists(), header
        assert result.returncode == 0, result.stderr
        assert "UNCHECKED (b)" in result.stdout
        assert "rule not statically interpretable" in result.stdout


def test_unsupported_rule_nodes_are_unchecked(tmp_path):
    from tools import check_spec

    bodies = [
        "return source != target",
        "return source.endswith('core.')",
        "return layers.get(source) == target",
        "return source in ['x']",
        "return source in target",
        "return source + target",
        "return (x := True)",
        "return any((True, False))",
        "for x in layers:\n        return False",
        "return True\n    print('unreachable')",
        "if False:\n        import socket\n    return False",
        "return True or unknown()",
        "raise RuntimeError(source)",
        "raise AssertionError(unknown())",
        "layers[source] = 'core.x'\n    return False",
    ]
    repo = synthetic_repo(tmp_path / "repo")
    spec = check_spec.Spec.load(write(tmp_path, "note.md", "# Synthetic\n"))
    for body in bodies:
        write(repo, "tests/test_import_boundaries.py",
              '# import_layers.json\ndef _forbidden(source, target, layers):\n    ' + body + '\n')
        report = check_spec.Report()
        check_spec.check_import_boundary(spec, repo, report)
        assert any(line.startswith("UNCHECKED (b)") and
                   "rule not statically interpretable" in line for line in report.lines), body


def test_static_rule_supported_grammar(tmp_path):
    from tools import check_spec

    repo = synthetic_repo(tmp_path / "repo")
    write(repo, "tests/test_import_boundaries.py", '''
        # import_layers.json
        def _forbidden(source, target, layers):
            source_layer = layers[source]
            if source_layer in ("core.a", "core.b") and target not in {"safe"}:
                return not (layers[target] == source_layer or target.startswith("ok"))
            else:
                if source == "unknown":
                    raise AssertionError(source)
                return False
    ''')
    rule, _ = check_spec.load_forbidden(repo, repo / "tests/snapshots/import_layers.json")
    layers = {"a": "core.a", "b": "modules.b", "same": "core.a"}
    assert rule("a", "b", layers) is True
    assert rule("a", "same", layers) is False
    assert rule("b", "a", layers) is False
    assert rule("a", "safe", layers) is False
    import pytest
    with pytest.raises(AssertionError):
        rule("unknown", "b", {**layers, "unknown": "unknown"})


def test_static_rule_matches_real_rule_in_temporary_copy(tmp_path):
    import importlib.util
    import shutil
    from tools import check_spec

    base = REAL_REPO or HERE.parent
    repo = tmp_path / "repo"
    rule_path = repo / "tests/test_import_boundaries.py"
    rule_path.parent.mkdir(parents=True)
    shutil.copyfile(base / "tests/test_import_boundaries.py", rule_path)
    layer_map = repo / "tests/snapshots/import_layers.json"
    layer_map.parent.mkdir()
    shutil.copyfile(base / "tests/snapshots/import_layers.json", layer_map)
    spec = importlib.util.spec_from_file_location("synthetic_boundary_reference", rule_path)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    rule, _ = check_spec.load_forbidden(repo, layer_map)
    assert rule is not None
    layers = json.loads(layer_map.read_text())
    for source in layers:
        for target in layers:
            assert rule(source, target, layers) == reference._forbidden(source, target, layers), (source, target)


DESIGN_NOTE = '''
# Synthetic design
## Acceptance
1. 0 failures across 8 runs
2. tests/b.py has 0 raw calls
## Dry run, recorded
`pytest -q tests/c.py` -> 3 passed in 1.2s
## Fence
Derived with `grep -rl executescript src tests`:
- src/a.py
- tests/b.py
'''


def test_design_gate_cli(tmp_path):
    repo = tmp_path / "repo"
    write(repo, "src/a.py", "conn.executescript('x')\n")
    write(repo, "tests/b.py", "conn.executescript('y')\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, timeout=60)
    for missing in (False, True):
        text = DESIGN_NOTE.replace("- tests/b.py\n", "") if missing else DESIGN_NOTE
        note = write(tmp_path, "note.md", text)
        result = run(note, repo, "--design-note")
        assert result.returncode == (1 if missing else 0), result.stdout + result.stderr
        assert bool(flags(result.stdout, "f")) is missing
        if missing:
            assert "fence omits" in result.stdout and "tests/b.py" in result.stdout


def test_design_gate_selftest_cli(tmp_path):
    result = subprocess.run([sys.executable, str(TOOL), "--selftest"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "20 passed, 0 failed" in result.stdout


def test_selftest_setup_timeouts_fail(monkeypatch, capsys):
    from tools import check_spec

    for command in ("init", "add"):
        calls = []

        def timed_out(args, **kwargs):
            calls.append(args[3])
            assert kwargs.get("timeout") == 60
            if args[3] == command:
                raise subprocess.TimeoutExpired(args, 60)
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr(subprocess, "run", timed_out)
        assert check_spec.selftest() == 1
        output = capsys.readouterr().out
        assert "FAIL" in output and "TimeoutExpired" in output
        assert "1 failed" in output
        assert calls == (["init"] if command == "init" else ["init", "add"])


if __name__ == "__main__":
    unittest.main()
