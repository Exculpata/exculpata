"""Mixed cleanup controls and sequential setter targets match Python semantics."""
import io
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('items', [
    '[independent, resource]', '[resource, independent]',
    '(independent, resource, independent)',
])
@pytest.mark.parametrize('form', ['loop', 'helper', 'comprehension', 'bound_methods'])
def test_mixed_independent_cleanup_is_not_shared(items, form):
    prefix = "import sqlite3\nimport io\nindependent = sqlite3.connect(':memory:')\nresource = io.StringIO()\n"
    if form == 'loop':
        source = f'for handle in {items}:\n    handle.close()'
    elif form == 'helper':
        source = f'def dispose(handles):\n    for handle in handles:\n        handle.close()\ndispose({items})'
    elif form == 'comprehension':
        source = f'[handle.close() for handle in {items}]'
    else:
        items = items.replace('independent', 'independent.close').replace('resource', 'resource.close')
        source = f'for close in {items}:\n    close()'
    assert check_fixture_writes(prefix + source) == []


@pytest.mark.parametrize('attribute,value', [('isolation_level', 'None'), ('row_factory', 'None'), ('text_factory', 'str')])
@pytest.mark.parametrize('form', ['tuple', 'nested', 'starred', 'loop', 'comprehension'])
@pytest.mark.parametrize('to_shared', [False, True])
def test_target_rebinding_precedes_later_attribute_setter(attribute, value, form, to_shared):
    before = 'independent' if to_shared else 'bench.workspace.connection'
    after = 'bench.workspace.connection' if to_shared else 'independent'
    prefix = f"import sqlite3\nindependent = sqlite3.connect(':memory:')\nhandle = {before}\n"
    target, rhs = f'(handle, handle.{attribute})', f'({after}, {value})'
    if form == 'nested':
        target, rhs = f'(marker, {target})', f'(1, {rhs})'
    elif form == 'starred':
        target, rhs = f'(handle, *handle.{attribute})', f'({after}, {value})'
    if form == 'loop':
        source = f'for {target} in [{rhs}]:\n    pass'
    elif form == 'comprehension':
        source = f'[None for {target} in [{rhs}]]'
    else:
        source = f'{target} = {rhs}'
    violations = check_fixture_writes(prefix + source)
    assert bool(violations) is to_shared, violations


@pytest.mark.parametrize('source', [
    'for handle in [independent, resource, bench.workspace.connection]:\n    handle.close()',
    'for operation in [independent.close, resource.close, bench.workspace.connection.close]:\n    operation()',
    '(handle, handle.isolation_level) = (bench.workspace.connection, None)',
])
def test_shared_member_still_requires_boundary(source):
    prefix = "import sqlite3\nimport io\nindependent = sqlite3.connect(':memory:')\nresource = io.StringIO()\nhandle = independent\n"
    assert check_fixture_writes(prefix + source)


def test_mixed_cleanup_closes_real_independent_resources_and_passes_inventory():
    source = '''import sqlite3
import io
independent = sqlite3.connect(':memory:')
resource = io.StringIO()
for handle in [independent, resource]:
    handle.close()
'''
    namespace = {}
    try:
        exec(source, namespace)
        assert namespace['resource'].closed
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            namespace['independent'].execute('SELECT 1')
        assert check_fixture_writes(source) == []
    finally:
        if 'independent' in namespace:
            namespace['independent'].close()
        if 'resource' in namespace:
            namespace['resource'].close()


def test_rebound_setter_commits_actual_pending_transaction_before_inventory_rejects(tmp_path):
    path = tmp_path / 'synthetic-rebound-setter.sqlite'
    raw, independent = sqlite3.connect(path), sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = '''import sqlite3
handle = sqlite3.connect(':memory:')
(handle, handle.isolation_level) = (bench.workspace.connection, None)
'''
    try:
        raw.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        # fixture-write: Deliberately retain pending synthetic data to demonstrate the sequential raw setter commit.
        raw.execute("INSERT INTO synthetic_fixture VALUES ('pending')")
        # Supply an independent receiver before rebinding it to the live handle.
        exec('(handle, handle.isolation_level) = (bench.workspace.connection, None)', {'bench': bench, 'handle': independent})
        with sqlite3.connect(path) as observer:
            assert observer.execute('SELECT value FROM synthetic_fixture').fetchall() == [('pending',)]
        assert not raw.in_transaction
        assert check_fixture_writes(source)
    finally:
        raw.close()
        independent.close()
