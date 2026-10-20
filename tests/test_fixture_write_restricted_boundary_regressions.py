"""Shared fixture carriers have a finite, non-suppressible syntax contract."""
import ast

import pytest

from tests.fixture_write_policy import check_fixture_writes


REASON = '# fixture-write: Isolated synthetic construction deliberately retains raw fixture ownership.'


@pytest.mark.parametrize('source', [
    'handles = [bench.workspace.connection]',
    'handles = {bench.workspace.connection}',
    "handles = {'shared': bench.workspace.connection}",
    'handles = dict(shared=bench.workspace.connection)',
    'handles = dict.fromkeys((bench.workspace.connection,))',
    "dict.get({}, 'missing', bench.workspace.connection)",
    "dict.setdefault({}, 'missing', bench.workspace.connection)",
    'handles = [handle for handle in (bench.workspace.connection,)]',
    'handles = {handle for handle in (bench.workspace.connection,)}',
    'handles = []\nhandles.append(bench.workspace.connection)',
    "handles = {}\nhandles.update(shared=bench.workspace.connection)",
    "handles = {}\nhandles['shared'] = bench.workspace.connection",
    'first, *rest = (None, bench.workspace.connection)',
    '*rest, last = (bench.workspace.connection, None)',
    'handles = tuple((bench.workspace.connection,))',
    'paths = tuple((bench.workspace.path,))',
    'paths = list((bench.workspace.path,))',
    'handles = (bench.workspace.connection,)\nhandles.__getitem__(0).close()',
    'handles = (bench.workspace.connection,)\ntuple.__getitem__(handles, 0).close()',
    'handles = (bench.workspace.connection,)\nnext(iter(handles)).close()',
    'handles = (bench.workspace.connection,)\ngetattr(handles, "__getitem__")(0).close()',
    'handles = list((bench.workspace.connection,))',
    'with fixture_write(bench.workspace) as connection:\n    handles = [connection.cursor()]',
    'with fixture_write(bench.workspace) as connection:\n    handles = [connection.execute("SELECT 1") for _ in (1,)]',
    "paths = {}\npaths['db'] = bench.workspace.path",
])
def test_shared_structures_cannot_be_authorized_by_operation_comments(source):
    annotated = '\n'.join(line + ' ' + REASON for line in source.splitlines())
    violations = check_fixture_writes(annotated)
    assert violations
    assert any('unsupported' in item.rule for item in violations)
    assert all('fixture_write' in str(item) for item in violations)


@pytest.mark.parametrize('source', [
    'first, second = (bench.workspace.connection, bench.workspace.connection)\nfirst.execute("SELECT 1")',
    '[first, second] = (bench.workspace.connection, bench.workspace.connection)\nfirst.execute("SELECT 1")',
    'rows = list(bench.workspace.connection.execute("SELECT 1"))',
    'handles = (bench.workspace.connection,)\nfor handle in handles:\n    handle.execute("SELECT 1")',
    'def handles(workspace):\n    return (workspace.connection,)\n(connection,) = handles(bench.workspace)\nconnection.execute("SELECT 1")',
    'with fixture_write(bench.workspace) as connection:\n    cursor, other = (connection.cursor(), connection)\n    cursor.execute("DELETE FROM synthetic")',
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nhandles = [independent]\nhandles.append(independent)\nfor handle in handles:\n    handle.close()",
])
def test_fixed_tuples_and_independent_containers_remain_supported(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('operation', [
    "holder['shared'] = bench.workspace.connection",
    "holder.update(shared=bench.workspace.connection)",
    "holder.setdefault('shared', bench.workspace.connection)",
    "holder.__setitem__('shared', bench.workspace.connection)",
    "holder['path'] = bench.workspace.path",
    "holder.update(path=bench.workspace.path)",
])
def test_rejected_shared_branch_does_not_taint_mutually_exclusive_independent_branch(operation):
    source = ('holder = {}\nif condition:\n'
              '    ' + operation + ' ' + REASON + '\n'
              'else:\n'
              "    holder['independent'] = independent\n"
              '    for entry in holder.values():\n        entry.close()\n')
    violations = check_fixture_writes(source)
    assert {item.line for item in violations} == {3}
    assert any('unsupported' in item.rule for item in violations)


@pytest.mark.parametrize('kind', ['expression', 'statement', 'field', 'subclass'])
@pytest.mark.parametrize('placement', ['module', 'deferred', 'default', 'annotation'])
def test_unknown_ast_syntax_is_rejected_before_it_can_hide_fixture_operations(monkeypatch, kind, placement):
    tree = ast.parse('bench.workspace.connection.close() ' + REASON)
    expression = tree.body[0].value
    if kind == 'expression':
        class FutureExpression(ast.expr):
            _fields = ('payload',)
        tree.body[0].value = ast.copy_location(FutureExpression(payload=expression), expression)
    elif kind == 'statement':
        class FutureStatement(ast.stmt):
            _fields = ('body',)
        tree.body[0] = ast.copy_location(FutureStatement(body=[tree.body[0]]), tree.body[0])
    elif kind == 'field':
        expression._fields = (*expression._fields, 'future_binding')
        expression.future_binding = ast.Name(id='shared', ctx=ast.Load())
    else:
        class FutureCall(ast.Call):
            pass
        tree.body[0].value = ast.copy_location(FutureCall(func=expression.func, args=[], keywords=[]), expression)
    if placement != 'module':
        wrapper = ast.parse('def probe(value=None):\n    pass')
        if placement == 'deferred' or kind == 'statement':
            wrapper.body[0].body = tree.body
        elif placement == 'default':
            wrapper.body[0].args.defaults = [tree.body[0].value]
        else:
            wrapper.body[0].args.args[0].annotation = tree.body[0].value
        tree = wrapper
    monkeypatch.setattr(ast, 'parse', lambda *args, **kwargs: tree)
    violations = check_fixture_writes('bench.workspace.connection.close() ' + REASON)
    assert any('unsupported syntax' in item.rule for item in violations)
    assert all('fixture_write' in str(item) for item in violations)


def test_parse_errors_fail_closed_with_fixture_guidance():
    violations = check_fixture_writes('if :')
    assert len(violations) == 1
    assert violations[0].structural
    assert 'fixture_write()' in str(violations[0])
    assert 'document this exact' not in str(violations[0])
