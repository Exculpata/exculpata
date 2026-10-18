"""Fixture helpers and imported scripts share a fail-closed write boundary."""
from pathlib import Path

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('source', [
    "exec(source)", "eval(source)", "execute = exec\nexecute(source)",
    "import builtins as b\nb.exec(source)",
    "from builtins import eval as evaluate\nevaluate(source)",
    "import builtins\ngetattr(builtins, 'exec')(source)",
    "exec(compile(source, '<synthetic>', 'exec'))",
    "with fixture_write(workspace):\n    eval(source)",
    "exec(source)  # fixture-write: Exercise deliberate isolated synthetic execution.",
    "# fixture-write: Exercise deliberate isolated synthetic execution.\neval(source)",
])
def test_opaque_dynamic_execution_is_structurally_rejected(source):
    violations = check_fixture_writes(source)
    assert any('dynamic execution' in item.rule and item.structural for item in violations)


@pytest.mark.parametrize('source', [
    'def exec(value):\n    return value\nexec(1)',
    'def eval(value):\n    return value\neval(1)',
    "workspace.connection.execute('SELECT 1')",
])
def test_dynamic_execution_rule_preserves_ordinary_calls(source):
    assert check_fixture_writes(source) == []


def test_corpus_media_seed_observes_fixture_boundary():
    path = Path(__file__).resolve().parents[1] / 'scripts/corpus_product.py'
    assert check_fixture_writes(path.read_text(), str(path)) == []


def test_tracked_scripts_are_checked_without_loading_imports(tmp_path):
    import subprocess
    from tests.fixture_inventory import check_repository_fixtures, inventory_paths

    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'tests/test_synthetic.py').write_text('import scripts.synthetic\n')
    (tmp_path / 'scripts/synthetic.py').write_text('import scripts.dependency\n')
    (tmp_path / 'scripts/dependency.py').write_text("bench.workspace.queue_media_job('synthetic')\nexec(source)\n")
    subprocess.run(['git', 'add', 'tests', 'scripts'], cwd=tmp_path, check=True)
    assert Path('scripts/dependency.py') in inventory_paths(tmp_path)
    violations = check_repository_fixtures(tmp_path)
    assert any('manual media fixture seed' in item.rule for item in violations)
    assert any('dynamic execution' in item.rule for item in violations)
    assert all(item.filename == 'scripts/dependency.py' for item in violations)
    (tmp_path / 'scripts/dependency.py').write_text(
        "from tests.fixture_write import fixture_write\n"
        "with fixture_write(bench.workspace, media=True):\n"
        "    bench.workspace.queue_media_job('synthetic')\n")
    assert check_repository_fixtures(tmp_path) == []


@pytest.mark.parametrize('filename', ['tests/fixture_probe.py', 'tests/runtime_probes.py'])
def test_runtime_probe_pin_is_narrow_and_fails_closed(filename, monkeypatch):
    import hashlib
    from tests.fixture_inventory import REVIEWED_DYNAMIC_PROBES, check_inventory_source

    root = Path(__file__).resolve().parents[1]
    source = (root / filename).read_text()
    assert check_inventory_source(source, filename) == []
    assert any('dynamic execution' in item.rule for item in check_fixture_writes(source, filename))
    changed = source + '\nexec(unreviewed_source)\n'
    assert any('reviewed runtime probe changed' in item.rule for item in check_inventory_source(changed, filename))
    assert any('dynamic execution' in item.rule for item in check_inventory_source(changed, filename))
    raw_write = source + "\nbench.workspace.connection.execute('DELETE FROM workbench_matter')\n"
    monkeypatch.setitem(REVIEWED_DYNAMIC_PROBES, filename, hashlib.sha256(raw_write.encode()).hexdigest())
    assert any('SQL fixture write' in item.rule for item in check_inventory_source(raw_write, filename))


@pytest.mark.parametrize('source', [
    '(lambda operation: operation)(exec)(payload)',
    'consume(exec)',
    'callback = lambda: eval\ncallback()(payload)',
    'import builtins as b\nconsume(b.eval)',
    'from builtins import exec as execute\nconsume(execute)',
    "import builtins\nconsume(getattr(builtins, 'eval'))",
    'def relay(operation):\n    return operation\nrelay(exec)(payload)',
    'def callback(operation=exec):\n    return operation',
    'operation = exec if flag else ordinary\noperation(payload)',
    'operation = exec  # fixture-write: Deliberately capture a synthetic executable callback.',
])
def test_dynamic_builtin_references_cannot_escape_through_callables(source):
    violations = check_fixture_writes(source)
    assert any('dynamic execution' in item.rule and item.structural for item in violations)


def test_local_callable_parameter_does_not_name_the_dynamic_builtin():
    assert check_fixture_writes('def consume(exec):\n    return exec(1)') == []


@pytest.mark.parametrize('filename', [
    'services/transcription/tests/test_synthetic.py',
    'packages/synthetic/tests/support.py',
    'services/transcription/tests/fixture_write.py',
])
def test_all_tracked_test_trees_observe_fixture_boundary(tmp_path, filename):
    import subprocess
    from tests.fixture_inventory import check_repository_fixtures, inventory_paths

    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, timeout=15)
    source = "bench.workspace.queue_media_job('synthetic')\nexec(source)\n"
    target = tmp_path / filename
    target.parent.mkdir(parents=True)
    target.write_text(source)
    raw_owner = tmp_path / 'tests/fixture_write.py'
    raw_owner.parent.mkdir()
    raw_owner.write_text(source)
    # Untracked editor copies never join the inventory; the exact raw owner
    # exemption does not extend to an identically named service helper.
    target.with_name('untracked copy.py').write_text(source)
    subprocess.run(['git', 'add', '--', filename, 'tests/fixture_write.py'],
                   cwd=tmp_path, check=True, timeout=15)
    assert inventory_paths(tmp_path) == [Path(filename)]
    violations = check_repository_fixtures(tmp_path)
    assert {item.filename for item in violations} == {filename}
    assert any('manual media fixture seed' in item.rule for item in violations)
    assert any('dynamic execution' in item.rule for item in violations)
    target.write_text(
        "from tests.fixture_write import fixture_write\n"
        "with fixture_write(bench.workspace, media=True):\n"
        "    bench.workspace.queue_media_job('synthetic')\n")
    assert check_repository_fixtures(tmp_path) == []
