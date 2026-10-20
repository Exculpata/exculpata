"""Unknown method names must not erase a known workspace receiver."""
import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('lookup', [
    'getattr(bench.workspace, method_name)',
    'builtins.getattr(bench.workspace, method_name)',
    'lookup(bench.workspace, method_name)',
])
@pytest.mark.parametrize('form', ['direct', 'saved', 'returned'])
def test_dynamic_known_workspace_method_requires_ownership(lookup, form):
    imports = 'import builtins\nfrom builtins import getattr as lookup\n'
    if form == 'direct':
        source = imports + f'with fixture_write(bench.workspace):\n    {lookup}(synthetic)'
    elif form == 'saved':
        source = imports + f'operation = {lookup}\nwith fixture_write(bench.workspace):\n    operation(synthetic)'
    else:
        source = imports + f'def operation():\n    return {lookup}\nwith fixture_write(bench.workspace):\n    operation()(synthetic)'
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'with fixture_write(bench.workspace):\n    getattr(resource, method_name)(synthetic)',
    'with fixture_write(bench.workspace):\n    getattr(other.workspace, method_name)(synthetic)',
    'getattr(bench.workspace, method_name)(synthetic)',
    'with fixture_write(bench.workspace, media=True):\n    getattr(bench.workspace, method_name)(synthetic)',
])
def test_dynamic_workspace_rule_preserves_independent_calls_and_explicit_media_mode(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('lookup', ['builtins.getattr', 'lookup'])
def test_qualified_builtin_dynamic_sqlite_method_keeps_existing_fail_closed_rule(lookup):
    source = f'import builtins\nfrom builtins import getattr as lookup\n{lookup}(bench.workspace.connection, method_name)(synthetic)'
    assert check_fixture_writes(source)
