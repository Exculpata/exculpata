"""Synthetic acceptance for partial service replay, including the #518 sequence."""

import ast
import re

import pytest

from scripts import ci_parity_plan as plan
from tests.test_ci_parity import (
    ROOT, cli, document, git_commit, repository, write_skips,
)


STEP = 'Run synthetic postgres acceptance'
SENTINEL = 'ci-parity-replay-stub-dsn'
RESERVE = 'CASE_INTELLIGENCE_STORAGE_RESERVE_GIB'
DSN = 'CASE_REVIEW_POSTGRES_TEST_DSN'
FIXTURES = '''import os
import pytest

@pytest.fixture(scope="session")
def postgres_base_dsn():
    if not os.getenv("CASE_REVIEW_POSTGRES_TEST_DSN"):
        pytest.skip("database unavailable")
    raise AssertionError("real session fixture executed")

@pytest.fixture(scope="session")
def postgres_test_dsn(postgres_base_dsn):
    raise AssertionError("real schema fixture executed")

@pytest.fixture
def production(monkeypatch):
    assert os.environ["CASE_INTELLIGENCE_STORAGE_RESERVE_GIB"] == "0", "production reserve"
'''
TEST = '''def test_portable(production, postgres_test_dsn):
    raise AssertionError("database body executed")
'''


def synthetic(tmp_path, *, replay=True, command='python -m pytest -q test_postgres.py',
              env=None, job_env=None, workflow_env=None, fixtures=FIXTURES, tests=TEST):
    data = document([{'run': 'true'}, {'name': STEP, 'run': command, 'env': env or {}}],
                    env=job_env or {})
    data['env'] = workflow_env or {}
    root, _ = repository(tmp_path, data)
    entry = dict(workflow='synthetic', job='build', step=STEP, category='service',
                 reason='Synthetic database service is unavailable.')
    if replay:
        entry['replay'] = 'pytest-service-stub'
    write_skips(root, [entry])
    (root / 'conftest.py').write_text(fixtures)
    (root / 'test_postgres.py').write_text(tests)
    git_commit(root)
    return root


def run(root, tmp_path, expected):
    result, report = cli(root, tmp_path)
    assert result.returncode == expected, report + result.stderr
    assert re.fullmatch(r'SUMMARY ran=\d+ failed=\d+ not_run=\d+ not_run_unexpected=\d+ RESULT: .+',
                        report.splitlines()[-1])
    return report


def counts(report):
    line, = [line for line in report.splitlines() if line.startswith('REPLAY RESULT ')]
    return {key: int(value) for key, value in re.findall(r'(\w+)=(\d+)', line)}


def test_proving_sequence(tmp_path):
    root = synthetic(tmp_path, replay=False)
    assert 'GREEN OVERALL' in run(root, tmp_path, 0)
    entry = plan.load_skips(root)[0]
    entry['replay'] = 'pytest-service-stub'
    write_skips(root, [entry])
    git_commit(root)
    report = run(root, tmp_path, 1)
    failures = [line for line in report.splitlines() if line.startswith('REPLAY FAIL ')]
    assert len(failures) == 1
    assert 'test=test_postgres.py::test_portable phase=setup' in failures[0]
    assert 'production reserve' in failures[0]
    assert 'reserve=16384 (hostile) dsn=stub' in report
    (root / 'conftest.py').write_text(FIXTURES.replace(
        '    assert os.environ', '    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")\n    assert os.environ'))
    git_commit(root)
    report = run(root, tmp_path, 0)
    assert counts(report) == dict(stubbed_setup_ok=1, body_skipped=1, ran=0, passed=0,
                                  skipped=0, unreplayable=0, failed=0)


@pytest.mark.parametrize('layer', ['workflow_env', 'job_env', 'env'])
def test_declared_reserve_layers(tmp_path, layer):
    root = synthetic(tmp_path, **{layer: {RESERVE: '0'}})
    report = run(root, tmp_path, 0)
    assert 'reserve=0 (declared)' in report
    assert counts(report)['failed'] == 0


def test_environment_and_tool_owned_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv(DSN, 'postgresql://synthetic-parent.invalid/database')
    monkeypatch.setenv(RESERVE, '0')
    monkeypatch.setenv('CASE_INTELLIGENCE_TEST_MODE', 'inherited')
    fixtures = FIXTURES + '''
assert "CASE_INTELLIGENCE_TEST_MODE" not in os.environ
os.environ.setdefault("CASE_INTELLIGENCE_TEST_MODE", "1")
@pytest.fixture
def observe(postgres_base_dsn, postgres_test_dsn):
    assert postgres_base_dsn == postgres_test_dsn == "ci-parity-replay-stub-dsn"
    assert os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"] == postgres_base_dsn
    assert os.environ["CASE_INTELLIGENCE_STORAGE_RESERVE_GIB"] == "16384"
    assert os.environ["CASE_INTELLIGENCE_TEST_MODE"] == "1"
    assert "CLIENT_PREFIX" not in os.environ
    from pathlib import Path
    Path(os.environ["MARKER"]).write_text("fixture ran")
'''
    marker = tmp_path / 'marker'
    root = synthetic(tmp_path, fixtures=fixtures, tests='''def test_stub(observe):
    raise AssertionError("body executed")
def test_plain():
    assert True
''', job_env={DSN: 'postgresql://synthetic-job.invalid/database'}, env={
        'CLIENT_PREFIX': 'docker exec ${{ job.services.postgres.id }}', 'MARKER': str(marker)})
    # A module in the tree's scripts directory must never supply the replay plugin.
    (root / 'scripts/ci_parity_service_replay.py').write_text('raise AssertionError("wrong plugin")\n')
    git_commit(root)
    report = run(root, tmp_path, 0)
    assert marker.read_text() == 'fixture ran'
    assert 'REPLAY OMITTED ENV CLIENT_PREFIX' in report
    assert counts(report) == dict(stubbed_setup_ok=1, body_skipped=1, ran=1, passed=1,
                                  skipped=0, unreplayable=0, failed=0)


@pytest.mark.parametrize(('error', 'expected', 'kind'), [
    ('DatabaseError("synthetic")', 0, 'UNREPLAYABLE'),
    ('RuntimeError("synthetic")', 1, 'FAIL'),
    ('RuntimeError("ci-parity-replay-stub-dsn")', 0, 'UNREPLAYABLE'),
])
def test_setup_classification(tmp_path, error, expected, kind):
    fixtures = FIXTURES + '''
class DatabaseError(Exception):
    pass
DatabaseError.__module__ = "psycopg.errors"
@pytest.fixture
def connection(postgres_test_dsn):
    try:
        raise ERROR
    except Exception as error:
        raise ValueError("wrapper") from error
'''.replace('ERROR', error)
    root = synthetic(tmp_path, fixtures=fixtures, tests='def test_db(connection): pass\n')
    report = run(root, tmp_path, expected)
    assert f'REPLAY {kind} test=test_postgres.py::test_db phase=setup' in report
    if 'DatabaseError' in error:
        assert 'reason=DatabaseError' in report


@pytest.mark.parametrize(('name', 'expected', 'kind'), [('psycopg', 0, 'UNREPLAYABLE'),
                                                       ('unrelated', 1, 'FAIL')])
def test_collection_classification(tmp_path, name, expected, kind):
    root = synthetic(tmp_path, tests=f'raise ModuleNotFoundError("missing", name={name!r})\n',
                     command='python -m pytest -q test_postgres.py test_plain.py')
    (root / 'test_plain.py').write_text('def test_plain(): pass\n')
    git_commit(root)
    report = run(root, tmp_path, expected)
    assert f'REPLAY {kind} test=test_postgres.py phase=collect' in report


@pytest.mark.parametrize('command', ['echo synthetic', "cat <<'PY'\npython -m pytest test_postgres.py\nPY"])
def test_no_pytest_is_unmapped_before_any_execution(tmp_path, command):
    root = synthetic(tmp_path, command=command)
    assert 'UNMAPPED' in run(root, tmp_path, 5)
    assert not list(tmp_path.glob('parity-*.log'))


def test_extraction_and_omissions(tmp_path):
    marker = tmp_path / 'must-not-exist'
    command = f'''touch {marker}
cat <<'BODY'
python -m pytest nonexistent.py
BODY
python3 -m pytest -q \\
 test_postgres.py
'''
    root = synthetic(tmp_path, command=command, tests='def test_plain(): pass\n')
    report = run(root, tmp_path, 0)
    assert not marker.exists()
    assert f'REPLAY NOT REPLAYED touch {marker}'[:100] in report
    assert "REPLAY NOT REPLAYED cat <<'BODY'" in report
    assert counts(report)['passed'] == 1


@pytest.mark.parametrize(('command', 'fixtures', 'tests'), [
    ('python -m pytest -q test_postgres.py', '', ''),
    ('python -m pytest --synthetic-invalid-option', '', ''),
    ('python -m pytest -q test_postgres.py', 'def pytest_collection_modifyitems(): raise RuntimeError("internal")', TEST),
    ('python -m pytest -q test_postgres.py', 'import os\ndef pytest_sessionstart(): os._exit(1)', TEST),
])
def test_unclassified_exit_codes_fail(tmp_path, command, fixtures, tests):
    root = synthetic(tmp_path, command=command, fixtures=fixtures, tests=tests)
    report = run(root, tmp_path, 1)
    assert 'NOT GREEN, FAILURES' in report


@pytest.mark.parametrize(('category', 'replay'), [('install', 'pytest-service-stub'), ('service', 'unknown')])
def test_invalid_replay_data(tmp_path, category, replay):
    root = synthetic(tmp_path)
    entry = plan.load_skips(root)[0]
    entry.update(category=category, replay=replay)
    write_skips(root, [entry])
    git_commit(root)
    assert 'UNMAPPED' in run(root, tmp_path, 5)


def test_real_workflow_extraction_and_fixture_scope():
    entries = [entry for entry in plan.load_skips(ROOT) if 'replay' in entry]
    assert len(entries) == 1
    assert entries[0]['job'] == 'postgres-integration'
    workflow = plan.read_yaml(ROOT / '.github/workflows/quality-gates.yml')
    step, = [step for step in workflow['jobs']['postgres-integration']['steps']
             if step.get('name') == entries[0]['step']]
    commands, omitted = plan.extract_pytest(step['run'])
    assert len(commands) == 1
    assert 'tests/test_review_bench_v2_postgres.py' in commands[0]
    assert 'tests/test_defense_eval_postgres.py' in commands[0]
    assert omitted[0].startswith('python scripts/check-postgres-test-report.py')
    extended = step['run'] + "python - <<'PY'\npython -m pytest fake.py\nPY\n"
    assert plan.extract_pytest(extended)[0] == commands
    module = ast.parse((ROOT / 'tests/conftest.py').read_text())
    fixtures = [node for node in module.body if isinstance(node, ast.FunctionDef)
                and node.name in {'postgres_base_dsn', 'postgres_test_dsn'}]
    assert len(fixtures) == 2
    for fixture in fixtures:
        decorator, = fixture.decorator_list
        assert ast.unparse(decorator) == "pytest.fixture(scope='session')"


def test_real_step_runs_only_its_pytest_commands(tmp_path):
    workflow = plan.read_yaml(ROOT / '.github/workflows/quality-gates.yml')
    step, = [step for step in workflow['jobs']['postgres-integration']['steps']
             if step.get('name') == 'Run existing PostgreSQL and pgvector acceptance']
    commands, omitted = plan.extract_pytest(step['run'])
    root = synthetic(tmp_path, command=step['run'], env=step.get('env'), fixtures='', tests='')
    for filename in re.findall(r'\btests/[\w/]+\.py\b', '\n'.join(commands)):
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def test_synthetic(): pass\n')
    git_commit(root)
    report = run(root, tmp_path, 0)
    for command in omitted:
        assert 'REPLAY NOT REPLAYED ' + command[:80] in report
    assert counts(report)['ran'] > 0


def test_multiple_commands_aggregate_one_failed_step(tmp_path):
    root = synthetic(tmp_path, command='python -m pytest -q test_postgres.py\npython -m pytest -q test_plain.py')
    (root / 'test_plain.py').write_text('def test_plain(): pass\n')
    git_commit(root)
    report = run(root, tmp_path, 1)
    assert counts(report)['failed'] == 1
    assert counts(report)['passed'] == 1
    assert 'SUMMARY ran=2 failed=1 not_run=1' in report


def test_declared_test_mode_and_step_dsn(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_TEST_MODE', 'inherited')
    root = synthetic(tmp_path, env={DSN: 'postgresql://synthetic-step.invalid/database',
                                   'CASE_INTELLIGENCE_TEST_MODE': 'declared'}, tests='''import os
def test_plain():
    assert os.environ["CASE_INTELLIGENCE_TEST_MODE"] == "declared"
    assert os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"] == "ci-parity-replay-stub-dsn"
''')
    run(root, tmp_path, 0)


@pytest.mark.parametrize('phase', ['call', 'teardown'])
def test_non_database_failures_remain_failures(tmp_path, phase):
    fixtures = '''import pytest
@pytest.fixture
def lifecycle():
    yield
    raise RuntimeError("synthetic teardown")
''' if phase == 'teardown' else ''
    tests = 'def test_plain(lifecycle): pass\n' if phase == 'teardown' else 'def test_plain(): raise RuntimeError("synthetic call")\n'
    root = synthetic(tmp_path, fixtures=fixtures, tests=tests)
    assert f'test=test_postgres.py::test_plain phase={phase} RuntimeError:' in run(root, tmp_path, 1)


def test_regular_skips_are_separate_from_stubbed_bodies(tmp_path):
    root = synthetic(tmp_path, tests='''import pytest
@pytest.mark.skip(reason="synthetic")
def test_plain(): pass
''')
    result = counts(run(root, tmp_path, 0))
    assert result['skipped'] == 1
    assert result['body_skipped'] == result['ran'] == 0


@pytest.mark.parametrize('command', [
    'python -m pytest test_postgres.py; touch unexpected',
    'python -m pytest test_postgres.py $(touch unexpected)',
    "cat <<'EOF'\npython -m pytest fake.py",
    'python -m pytest test_postgres.py \\',
])
def test_unsupported_extraction_is_unmapped(tmp_path, command):
    root = synthetic(tmp_path, command=command)
    assert 'UNMAPPED' in run(root, tmp_path, 5)
    assert not list(tmp_path.glob('parity-*.log'))


def test_heredoc_tabs_and_multiple_bodies():
    commands, omitted = plan.extract_pytest('''cat <<ONE <<-'TWO'
python -m pytest fake_one.py
ONE
\tpython -m pytest fake_two.py
\tTWO
python -m pytest test_synthetic.py
''')
    assert commands == ['python -m pytest test_synthetic.py']
    assert omitted == ["cat <<ONE <<-'TWO'"]
