"""Fixture helpers and imported scripts share a fail-closed write boundary."""
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from tests.fixture_probe import fixture_probe
from tests.fixture_write_policy import check_fixture_writes


def assert_only_explicit_reflective_denials(source):
    """Retain scalar/key-only runtime controls under the stricter syntax contract.

    These probes still must not acquire delayed-class, namespace or write errors.
    Their explicit reflective spelling now requires supported explicit syntax even
    where the runtime result is harmless; supported spellings have separate positive probes.
    """
    rules = {
        'builtin __call__ access is unsupported; use named calls with fixture_write()',
        'starred getattr arguments are unsupported; use explicit arguments with fixture_write()',
        'fromkeys keys must be literal; use an explicit synchronous helper with fixture_write()',
    }
    violations = check_fixture_writes(source)
    assert violations
    assert all(item.structural and item.rule in rules for item in violations)


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


@pytest.mark.parametrize('operation', ['exec', 'eval'])
@pytest.mark.parametrize('source', [
    "__import__('builtins').OP(payload)",
    "__import__(name='builtins').OP(payload)",
    "__builtins__['OP'](payload)",
    "__builtins__.OP(payload)",
    "namespace = __builtins__\nnamespace.get('OP')(payload)",
    "import builtins as b\nb.__dict__['OP'](payload)",
    "from builtins import __dict__ as namespace\nnamespace['OP'](payload)",
    "import builtins as b\nvars(b)['OP'](payload)",
    "import builtins as b\nb.__import__('builtins').OP(payload)",
    "from builtins import __import__ as load\nload('builtins').OP(payload)",
    "load = __import__\nload('builtins').OP(payload)",
    "import importlib as imports\nimports.import_module('builtins').OP(payload)",
    "from importlib import import_module as load\nload('builtins').OP(payload)",
    "getattr(__import__('builtins'), 'OP')(payload)",
    "import builtins as b\nnamespace = getattr(b, '__dict__')\nnamespace['OP'](payload)",
    "import builtins as b\nmodules = (b,)\nmodules[0].__dict__['OP'](payload)",
    "import builtins as b\nb.__getattribute__('OP')(payload)",
    "import builtins as b\nread = b.__getattribute__\nread('OP')(payload)",
    "from builtins import __getattribute__ as read\nread('OP')(payload)",
    "from builtins import vars as namespace_of\nimport builtins as b\nnamespace_of(b)['OP'](payload)",
])
def test_reflected_builtin_execution_is_rejected(operation, source):
    source = source.replace('OP', operation)
    violations = check_fixture_writes(source)
    assert any('dynamic execution' in item.rule and item.structural for item in violations)


@pytest.mark.parametrize('operation', ['exec', 'eval'])
@pytest.mark.parametrize('expression', ["__builtins__['OP']", "__import__('builtins').OP"])
@pytest.mark.parametrize('transport', [
    'consume(EXPRESSION)',
    'saved = EXPRESSION\nsaved(payload)',
    'saved = [EXPRESSION]\nsaved[0](payload)',
    "saved = {'operation': EXPRESSION}\nsaved['operation'](payload)",
    'callback = lambda: EXPRESSION\ncallback()(payload)',
    'def callback(operation=EXPRESSION):\n    return operation',
])
def test_reflected_dynamic_values_reject_before_alias_or_container_transport(operation, expression, transport):
    source = transport.replace('EXPRESSION', expression.replace('OP', operation))
    violations = check_fixture_writes(source)
    assert any('dynamic execution' in item.rule and item.structural for item in violations)


@pytest.mark.parametrize('source', [
    "import builtins\ngetattr(builtins, name)(payload)",
    "import builtins as b\nb.__getattribute__(name)(payload)",
    "import builtins as b\nnamespace = b.__dict__  # fixture-write: Deliberately capture an isolated synthetic mapping.",
    "# fixture-write: Deliberately capture an isolated synthetic mapping.\nnamespace = __builtins__",
])
def test_unresolved_builtin_reflection_and_comments_cannot_hide_dynamic_access(source):
    assert any(item.structural and 'dynamic execution' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "import builtins as b\nb.len(values)",
    "import builtins as b\ngetattr(b, 'len')(values)",
    "from builtins import len as size\nsize(values)",
    "import builtins as b\nb.__getattribute__('len')(values)",
    "from builtins import __import__ as load\nload('pathlib').Path('synthetic.txt')",
    "from importlib import import_module as load\nload('synthetic_module')",
    "vars(ordinary_resource)",
    "__import__('pathlib').Path('synthetic.txt')",
    "import importlib\nimportlib.import_module('synthetic_module')",
    "def read(__builtins__):\n    return __builtins__['ordinary']",
    "class Ordinary:\n    def __import__(self, name):\n        return name\nOrdinary().__import__('builtins')",
    "__builtins__ = {'ordinary': 1}\nvalue = __builtins__['ordinary']",
])
def test_reflection_rejection_preserves_explicit_safe_builtins_and_ordinary_resources(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('operation', ['exec', 'eval'])
@pytest.mark.parametrize('scope', [
    'globals()', 'locals()', 'vars()',
    'function.__globals__', 'frame.f_globals', 'frame.f_locals',
])
@pytest.mark.parametrize('lookup', [
    "SCOPE['__builtins__']",
    "SCOPE.get('__builtins__')",
    "SCOPE.__getitem__('__builtins__')",
    "dict.get(SCOPE, '__builtins__')",
    "SCOPE.copy()['__builtins__']",
])
def test_namespace_lookup_rejects_builtin_scope_extraction(operation, scope, lookup):
    source = lookup.replace('SCOPE', scope) + f"['{operation}'](payload)"
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('operation', ['exec', 'eval'])
@pytest.mark.parametrize('source', [
    "import sys\nsys.modules['builtins'].OP(payload)",
    "import sys as system\nsystem.modules.get('builtins').OP(payload)",
    "from sys import modules as registry\nregistry.__getitem__('builtins').OP(payload)",
    "import sys\nlookup = sys.modules.get\nlookup('builtins').OP(payload)",
    "import sys\nlookup = sys.modules.__getitem__\nlookup('builtins').OP(payload)",
    "import sys\nregistry = sys.modules.copy()\nregistry['builtins'].OP(payload)",
    "import sys\nregistry = dict(sys.modules)\nregistry.get('builtins').OP(payload)",
    "import sys\nlookup = dict.get\nlookup(sys.modules, 'builtins').OP(payload)",
    "import operator as op\nimport sys\nop.getitem(sys.modules, 'builtins').OP(payload)",
    "from operator import getitem as lookup\nlookup(globals(), '__builtins__')['OP'](payload)",
    "from builtins import globals as scope\nscope()['__builtins__']['OP'](payload)",
    "import builtins as b\nscope = b.locals\nscope().get('__builtins__')['OP'](payload)",
    "scope = vars\nlookup = scope().__getitem__\nlookup('__builtins__')['OP'](payload)",
    "scope = globals()\nlookup = scope.get\nlookup('__builtins__')['OP'](payload)",
    "lookup = getattr(globals(), 'get')\nlookup('__builtins__')['OP'](payload)",
    "import sys\nregistry = getattr(sys, 'modules')\nregistry['builtins'].OP(payload)",
    "registries = (globals(),)\nregistries[0]['__builtins__']['OP'](payload)",
    "registry = {'scope': globals()}\nregistry['scope']['__builtins__']['OP'](payload)",
    "import sys\nkey = 'built' + 'ins'\nsys.modules[key].OP(payload)",
    "scope = dict(globals())\nscope['__builtins__']['OP'](payload)",
    "frame.f_builtins['OP'](payload)",
    "getattr(frame, 'f_builtins')['OP'](payload)",
    "import inspect\ninspect.currentframe().f_builtins['OP'](payload)",
    "import sys\nsys._getframe().f_builtins['OP'](payload)",
    "import sys\nconsume(sys.modules['builtins'])",
    "callback = lambda: globals()['__builtins__']",
    "def callback(namespace=globals()['__builtins__']):\n    return namespace",
    "namespace = globals()['__builtins__']  # fixture-write: Deliberately capture an isolated synthetic mapping.",
])
def test_namespace_lookup_retains_imports_accessors_and_transports(operation, source):
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source.replace('OP', operation)))


@pytest.mark.parametrize('source', [
    "monkeypatch.setitem(globals(), 'ROOT', tmp_path)",
    "value = globals().get('ROOT')",
    "value = locals()['root']",
    "value = vars().get('root')",
    "value = vars(args)",
    "import sys\nsys.modules[__name__] = module",
    "import sys\nmonkeypatch.setitem(sys.modules, 'synthetic_module', module)",
    "import sys\nvalue = sys.modules[__name__]",
    "import sys\nvalue = sys.modules.get('synthetic_module')",
    "import sys\nbefore = set(sys.modules)",
    "monkeypatch.setitem(function.__globals__, 'generator_from_environment', forbidden)",
    "value = function.__globals__['ROOT']",
    "value = frame.f_globals.get('ROOT')",
    "value = frame.f_locals['root']",
    "def read(globals):\n    return globals()['__builtins__']",
    "def read(vars):\n    return vars()['__builtins__']",
    "class Ordinary:\n    def globals(self):\n        return {'__builtins__': 1}\nvalue = Ordinary().globals()['__builtins__']",
    "registry = {'builtins': ordinary}\nregistry['builtins'].run()",
])
def test_namespace_lookup_preserves_ordinary_introspection(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source', [
    "getattr(function, '__globals__')['__builtins__']['exec'](payload)",
    "getattr(function, '__globals__', None).get('__builtins__')['eval'](payload)",
    "getattr(frame, 'f_globals', None)['__builtins__']['exec'](payload)",
    "getattr(frame, 'f_builtins', None)['eval'](payload)",
    "import sys\ngetattr(sys, 'modules', {}).get('builtins').exec(payload)",
    "scope = {'scope': globals()}\nscope.get('scope')['__builtins__']['exec'](payload)",
    "import sys\nmodules = {'modules': sys.modules}\nmodules.get('modules').get('builtins').eval(payload)",
    "scope = {**globals()}\nscope['__builtins__']['exec'](payload)",
    "import sys\nmodules = {**sys.modules}\nmodules['builtins'].eval(payload)",
    "scope = dict(scope=globals())\nscope.get('scope')['__builtins__']['exec'](payload)",
    "scope = {'scope': globals()}\nscope.copy().get('scope')['__builtins__']['exec'](payload)",
    "scope = {'nested': {'scope': globals()}}\nscope.get('nested').get('scope')['__builtins__']['exec'](payload)",
    "scope = dict(**globals())\nscope.get('__builtins__')['exec'](payload)",
    "scope = {**globals()}\nlookup = scope.get\nlookup('__builtins__')['eval'](payload)",
])
def test_namespace_lookup_literal_reflection_and_mapping_copies(source):
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "import builtins as b\ngetattr(b, 'len', None)(values)",
    "value = getattr(function, '__globals__', {}).get('ROOT')",
    "scope = {'scope': {'__builtins__': ordinary}}\nscope.get('scope')['__builtins__'].run()",
    "scope = {**{'__builtins__': ordinary}}\nscope.get('__builtins__').run()",
    "scope = dict(scope=ordinary)\nscope.copy().get('scope').run()",
    "value = {**globals()}['ROOT']",
    "import sys\nvalue = {**sys.modules}.get('synthetic_module')",
])
def test_namespace_lookup_literal_reflection_preserves_safe_values(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('operation', ['exec', 'eval'])
@pytest.mark.parametrize('namespace,key', [
    ('globals()', '__builtins__'), ('locals()', '__builtins__'),
    ('vars()', '__builtins__'), ('sys.modules', 'builtins'),
])
@pytest.mark.parametrize('merge', [
    'NAMESPACE | {}', '{} | NAMESPACE',
    'NAMESPACE.__or__({})', 'NAMESPACE.__ror__({})',
    '{}.__or__(NAMESPACE)', '{}.__ror__(NAMESPACE)',
    'dict.__or__(NAMESPACE, {})', 'dict.__ror__(NAMESPACE, {})',
])
def test_namespace_dictionary_unions_preserve_builtin_entries(operation, namespace, key, merge):
    source = "import sys\nmerged = " + merge.replace('NAMESPACE', namespace)
    source += f"\ngetattr(merged['{key}'], '{operation}')(payload)"
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "merge = globals().__or__\nmerge({})['__builtins__']['exec'](payload)",
    "merge = dict.__ror__\nmerge(globals(), {})['__builtins__']['exec'](payload)",
    "getattr(globals(), '__or__', None)({})['__builtins__']['exec'](payload)",
    "({'scope': globals()} | {}).get('scope')['__builtins__']['exec'](payload)",
    "({} | {'scope': globals()}).get('scope')['__builtins__']['exec'](payload)",
    "({'scope': ordinary} | {'scope': globals()}).get('scope')['__builtins__']['exec'](payload)",
    "({'__builtins__': ordinary} | globals())['__builtins__']['exec'](payload)",
    "globals().__ror__({'__builtins__': ordinary})['__builtins__']['exec'](payload)",
    "({'first': globals(), 'second': globals()} | {'first': ordinary})['second']['__builtins__']['exec'](payload)",
    "(globals() | unknown)['__builtins__']['exec'](payload)",
    "(unknown | globals())['__builtins__']['exec'](payload)",
    "dict.__or__(globals(), unknown)['__builtins__']['exec'](payload)",
    "merged = {**globals(), **unknown}\nmerged['__builtins__']['exec'](payload)",
    "merged = dict(globals(), **unknown)\nmerged['__builtins__']['exec'](payload)",
])
def test_namespace_dictionary_union_aliases_overrides_and_opaque_inputs_reject(source):
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "scope = {}\nalias = scope\nscope |= globals()\nalias['__builtins__']['exec'](payload)",
    "scope = globals()\nscope |= {}\nscope['__builtins__']['exec'](payload)",
    "scope = {}\nscope.update(globals())\nscope['__builtins__']['exec'](payload)",
    "scope = {}\nupdate = scope.update\nupdate(globals())\nscope['__builtins__']['exec'](payload)",
    "scope = {}\ndict.update(scope, globals())\nscope['__builtins__']['exec'](payload)",
    "scope = {}\nscope.update(nested=globals())\nscope['nested']['__builtins__']['exec'](payload)",
    "scope = {}\nscope.__ior__(globals())['__builtins__']['exec'](payload)",
    "scope = {}\ndict.__ior__(scope, globals())['__builtins__']['exec'](payload)",
    "scope = {}\nscope.setdefault('nested', globals())['__builtins__']['exec'](payload)",
    "scope = {}\nscope.__setitem__('nested', globals())\nscope['nested']['__builtins__']['exec'](payload)",
    "scope = {}\nscope['nested'] = globals()\nscope['nested']['__builtins__']['exec'](payload)",
    "scope = {}\nscope.update(globals())  # fixture-write: Deliberately capture an isolated synthetic mapping.",
    "scope = globals()\nscope.values()",
    "scope = globals()\nscope.items()",
    "scope = {'nested': globals()}\nscope.popitem()",
    "dict.values(globals())",
    "dict.items(globals())",
    "dict.popitem({'nested': globals()})",
])
def test_namespace_dictionary_mutations_and_value_views_fail_closed(source):
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "(globals() | {'__builtins__': ordinary})['__builtins__'].run()",
    "globals().__or__({'__builtins__': ordinary})['__builtins__'].run()",
    "dict.__or__(globals(), {'__builtins__': ordinary})['__builtins__'].run()",
    "dict.__ror__({'__builtins__': ordinary}, globals())['__builtins__'].run()",
    "import sys\n({'builtins': ordinary}.__ror__(sys.modules))['builtins'].run()",
    "({'scope': globals()} | {'scope': ordinary}).get('scope').run()",
    "(globals() | {})['ROOT']",
    "import sys\n(sys.modules | {}).get('synthetic_module')",
    "scope = {'one': 1}\nscope |= {'two': 2}",
    "scope = {}\nscope.update(ordinary)\nscope.setdefault('one', 1)",
    "scope = {}\ndict.update(scope, {'one': 1})\nscope.__ior__({'two': 2})",
    "scope = {'one': 1}\nscope.values()\nscope.items()\nscope.popitem()",
    "scope = {}\nscope['ordinary'] = value",
    "monkeypatch.setitem(globals(), 'ROOT', tmp_path)",
    "import sys\nsys.modules[__name__] = module",
])
def test_namespace_dictionary_operations_preserve_safe_overrides_and_ordinary_mappings(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source', [
    "dict.copy(globals())['__builtins__']['exec'](payload)",
    "copy = dict.copy\ncopy(globals())['__builtins__']['exec'](payload)",
    "scope = {}.get('scope', globals())\nscope['__builtins__']['exec'](payload)",
    "scope = {}.pop('scope', globals())\nscope['__builtins__']['exec'](payload)",
    "scope = dict.get({}, 'scope', globals())\nscope['__builtins__']['exec'](payload)",
    "scopes = dict.fromkeys(['scope'], globals())\nscopes['scope']['__builtins__']['exec'](payload)",
    "scopes = {}.fromkeys(['scope'], globals())\nscopes['scope']['__builtins__']['exec'](payload)",
])
def test_namespace_dictionary_remaining_copy_default_and_constructor_paths(source):
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "scope = {}.get('scope', ordinary)",
    "scope = {}.pop('scope', ordinary)",
    "values = dict.fromkeys(globals(), ordinary)",
    "dict.copy({'one': 1})",
    "dict.__getitem__({'one': 1}, 'one')",
])
def test_namespace_dictionary_defaults_and_key_only_construction_controls(source):
    if 'fromkeys' in source:
        assert_only_explicit_reflective_denials(source)
    else:
        assert check_fixture_writes(source) == []



def run_isolated_namespace_probe(source, result='None', *, resource=None):
    sink = []
    registry_before = (tuple(sys.modules), sys.modules.get('__builtins__'),
                       sys.modules.get('probe'), sys.modules.get('synthetic_fixture_namespace_probe'))
    try:
        with patch.dict(sys.modules):
            error, observed = fixture_probe(source, result, sink=sink, resource=resource,
                                            payload="sink.append('synthetic payload executed')")
    finally:
        assert tuple(sys.modules) == registry_before[0]
        assert sys.modules.get('__builtins__') is registry_before[1]
        assert sys.modules.get('probe') is registry_before[2]
        assert sys.modules.get('synthetic_fixture_namespace_probe') is registry_before[3]
    if error is not None:
        raise error
    return observed, sink


def assert_namespace_runtime_effect_and_rejection(source, result='None', *, resource=None, rejection_line=None):
    observed, sink = run_isolated_namespace_probe(source, result, resource=resource)
    assert sink == ['synthetic payload executed']
    assert any(item.structural and 'dynamic execution' in item.rule
               and (rejection_line is None or item.line == rejection_line)
               for item in check_fixture_writes(source))
    return observed


@pytest.mark.parametrize('receiver', [
    'globals()', 'locals()', 'vars()', 'sys.modules',
    'function.__globals__', '{}', 'dict', 'builtins.dict',
])
@pytest.mark.parametrize('access', [
    'construct = RECEIVER.fromkeys',
    "construct = getattr(RECEIVER, 'fromkeys')",
    'construct = RECEIVER.fromkeys\nalias = construct\nconstruct = alias',
])
def test_namespace_accessor_fromkeys_receiver_matrix(receiver, access):
    source = 'import sys\nimport builtins\ndef function():\n    pass\n' + access.replace('RECEIVER', receiver)
    constructor_line = len(source.splitlines()) + 1
    source += "\nscopes = construct(['scope'], globals())\nscopes['scope']['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=constructor_line)


@pytest.mark.parametrize('receiver,arguments', [
    ('scope', 'globals()'), ('globals()', '{}'),
    ('sys.modules', '{}'), ('dict', 'scope, globals()'),
    ('builtins.dict', 'scope, globals()'),
])
@pytest.mark.parametrize('access', [
    'initialize = RECEIVER.__init__',
    "initialize = getattr(RECEIVER, '__init__')",
    'initialize = RECEIVER.__init__\nalias = initialize\ninitialize = alias',
])
def test_namespace_accessor_init_receiver_matrix(receiver, arguments, access):
    source = 'import sys\nimport builtins\nscope = {}\n' + access.replace('RECEIVER', receiver)
    source += '\ninitialize(' + arguments + ')'
    initialization_line = len(source.splitlines())
    source += ("\nsys.modules['builtins'].exec(payload)" if receiver == 'sys.modules'
               else "\nglobals()['__builtins__']['exec'](payload)" if receiver == 'globals()'
               else "\nscope['__builtins__']['exec'](payload)")
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=initialization_line)


@pytest.mark.parametrize('accessor', [
    'globals().copy', 'sys.modules.copy',
    'globals().__or__', 'sys.modules.__ror__',
    'globals().get', 'sys.modules.__getitem__',
    'globals().values', 'sys.modules.items',
])
@pytest.mark.parametrize('store', [
    "holder = {}\nholder['method'] = ACCESSOR",
    "holder = {}\nholder.update(method=ACCESSOR)",
    "holder = {}\nholder.setdefault('method', ACCESSOR)",
])
def test_namespace_accessor_bound_receiver_survives_mutation_carriers(accessor, store):
    source = 'import sys\n' + store.replace('ACCESSOR', accessor)
    store_line = len(source.splitlines())
    invocations = {
        'globals().copy': "holder['method']()['__builtins__']['exec'](payload)",
        'sys.modules.copy': "holder['method']()['builtins'].exec(payload)",
        'globals().__or__': "holder['method']({})['__builtins__']['exec'](payload)",
        'sys.modules.__ror__': "holder['method']({})['builtins'].exec(payload)",
        'globals().get': "holder['method']('__builtins__')['exec'](payload)",
        'sys.modules.__getitem__': "holder['method']('builtins').exec(payload)",
        'globals().values': "next(value for value in holder['method']() if isinstance(value, dict) and 'exec' in value)['exec'](payload)",
        'sys.modules.items': "next(value for key, value in holder['method']() if key == 'builtins').exec(payload)",
    }
    assert_namespace_runtime_effect_and_rejection(source + '\n' + invocations[accessor], rejection_line=store_line)


@pytest.mark.parametrize('source', [
    "holder = {'copy': globals().copy}\ncopy = holder.get('copy')\ncopy()['__builtins__']['exec'](payload)",
    "import sys\nholder = {'copy': sys.modules.copy}\nholder.get('copy')()['builtins'].eval(payload)",
    "holder = {'nested': {'copy': globals().copy}}\nholder.get('nested').get('copy')()['__builtins__']['exec'](payload)",
    "holder = {'copy': (globals().copy,)}\nholder.get('copy')[0]()['__builtins__']['exec'](payload)",
    "holder = {'copy': globals().copy} | {}\nholder.get('copy')()['__builtins__']['exec'](payload)",
    "holder = {}\nholder['copy'] = globals().copy  # fixture-write: Deliberately transport a synthetic bound callable.\nholder['copy']()['__builtins__']['exec'](payload)",
])
def test_namespace_accessor_bound_receiver_survives_literal_carriers(source):
    assert_namespace_runtime_effect_and_rejection(source)


@pytest.mark.parametrize('source', [
    "construct = globals().fromkeys\nconstruct(['scope'], ordinary)",
    "import sys\nsys.modules.fromkeys(['scope'], ordinary)",
    "construct = globals().fromkeys\nholder = {}\nholder['construct'] = construct",
    "scope = {}\nscope.__init__({'ordinary': 1})",
    "scope = {}\ndict.__init__(scope, {'ordinary': 1})",
    "scope = {}\ninitialize = getattr(scope, '__init__')\ninitialize(ordinary=1)",
    "ordinary = {'value': 1}\nholder = {}\nholder['copy'] = ordinary.copy",
    "ordinary = {'value': 1}\nholder = {}\nholder.update(copy=ordinary.copy)",
    "ordinary = {'value': 1}\nholder = {'copy': ordinary.copy}\nholder.get('copy')()",
])
def test_namespace_accessor_receiver_controls_remain_supported(source):
    """Static acceptance controls; these do not claim forbidden runtime effects."""
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('store,extract', [
    ('holder = []\nholder.append(globals().copy)', 'holder[0]'),
    ('holder = []\nappend = holder.append\nappend(globals().copy)', 'holder[0]'),
    ('holder = []\nlist.append(holder, globals().copy)', 'holder[0]'),
    ('holder = []\nholder.extend([globals().copy])', 'holder[0]'),
    ('holder = []\nholder.insert(0, globals().copy)', 'holder[0]'),
    ('holder = []\nholder.__iadd__([globals().copy])', 'holder[0]'),
    ('holder = []\nholder.append((globals().copy,))', 'holder[0][0]'),
    ('holder = set()\nholder.add(globals().copy)', 'next(iter(holder))'),
    ('holder = set()\nholder.update([globals().copy])', 'next(iter(holder))'),
    ('holder = set()\nupdate = holder.update\nupdate([globals().copy])', 'next(iter(holder))'),
    ('holder = set()\nset.update(holder, [globals().copy])', 'next(iter(holder))'),
    ('holder = []\nholder += [globals().copy]', 'holder[0]'),
])
def test_namespace_sequence_store_executes_before_rejection(store, extract):
    source = store + '\ncopy = ' + extract + "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source)


@pytest.mark.parametrize('expression', [
    '{}.copy()', 'dict.copy({})', '{}.get("receiver", {})',
    '{"receiver": {}}.get("receiver")', 'dict.get({"receiver": {}}, "receiver")',
    '{} | {}', '{}.__or__({})', '{}.__ror__({})',
    'dict.__or__({}, {})', 'dict.__ror__({}, {})',
    '{}.fromkeys([])', 'dict.fromkeys(["ordinary"])',
    'dict.fromkeys(["receiver"], {}).get("receiver")',
])
def test_namespace_dictionary_identity_executes_before_rejection(expression):
    source = 'receiver = ' + expression
    source += "\nscopes = receiver.fromkeys(['scope'], globals())\nscopes['scope']['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source)


def test_namespace_dictionary_unknown_keys_preserve_type_without_inventing_entries():
    source = "receiver = dict.fromkeys(resource)\nscopes = receiver.fromkeys(['scope'], globals())\nscopes['scope']['__builtins__']['exec'](payload)"
    receiver = assert_namespace_runtime_effect_and_rejection(source, 'receiver', resource=('ordinary',))
    assert receiver == {'ordinary': None}


def test_namespace_runtime_probe_restores_module_registry_on_failure():
    source = "import sys\nsys.modules['synthetic_fixture_namespace_probe'] = object()\nraise RuntimeError('synthetic probe failure')"
    before = sys.modules.get('synthetic_fixture_namespace_probe')
    with pytest.raises(RuntimeError, match='synthetic probe failure'):
        assert_namespace_runtime_effect_and_rejection(source)
    assert sys.modules.get('synthetic_fixture_namespace_probe') is before


@pytest.mark.parametrize('source', [
    'holder = []\nholder.append(resource)\nholder.extend([resource])\nholder.insert(0, resource)',
    'holder = set()\nholder.add(resource)\nholder.update([resource])',
    "holder = []\nordinary = {}.copy\nholder.append(ordinary)\nholder[0]()",
    "receiver = {}.copy()\nresult = receiver.fromkeys(['ordinary'], resource)",
    "receiver = {'receiver': {}}.get('receiver')\nreceiver.__init__({'ordinary': resource})",
    "receiver = {} | {}\nreceiver.update({'ordinary': resource})",
    "receiver = dict.fromkeys(resource)\nreceiver.copy()",
])
def test_namespace_sequence_and_dictionary_identity_static_controls(source):
    if 'fromkeys(resource)' in source:
        assert_only_explicit_reflective_denials(source)
    else:
        assert check_fixture_writes(source) == []



@pytest.mark.parametrize('source,rejection_line', [
    ("holder = []\nholder.extend(item for item in [globals().copy])\ncopy = holder[0]", 2),
    ("holder = set()\nholder.update(item for item in [globals().copy])\ncopy = next(iter(holder))", 2),
    ("holder = []\nholder.extend([item for item in [globals().copy]])\ncopy = holder[0]", 2),
    ("holder = set()\nholder.update({item for item in [globals().copy]})\ncopy = next(iter(holder))", 2),
    ("holder = {'copy': item for item in [globals().copy]}\ncopy = holder['copy']", 1),
    ("holder = {item: None for item in [globals().copy]}\ncopy = next(iter(holder))", 1),
    ("holder = []\nholder.extend(item for group in [[globals().copy]] for item in group)\ncopy = holder[0]", 2),
    ("holder = []\nholder.extend(item for item in (nested for nested in [globals().copy]))\ncopy = holder[0]", 2),
    ("holder = [item for item in (nested for nested in [globals().copy])]\ncopy = holder[0]", 1),
    ("holder = {key: value for key, value in [('copy', globals().copy)]}\ncopy = holder['copy']", 1),
    ("holder = [item for item in [globals().copy] if resource]\ncopy = holder[0]", 1),
    ("holder = [item for item in {nested for nested in [globals().copy]}]\ncopy = holder[0]", 1),
    ("holder = []\nholder.extend(globals().copy for item in resource)\ncopy = holder[0]", 2),
    ("holder = {'copy': [item for item in [globals().copy]]}\ncopy = holder['copy'][0]", 1),
])
def test_namespace_comprehension_carrier_executes_before_rejection(source, rejection_line):
    source += "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource=('synthetic',), rejection_line=rejection_line)


@pytest.mark.parametrize('expression', [
    '{item: None for item in resource}',
    '{item: None for item in (nested for nested in resource)}',
    '{item: {nested: None for nested in resource} for item in resource}',
    '{item: None for item in resource if item}',
    '{item: None for item in resource}.copy()',
])
def test_namespace_comprehension_dictionary_type_executes_before_rejection(expression):
    source = 'receiver = ' + expression
    source += "\nscopes = receiver.fromkeys(['scope'], globals())\nscopes['scope']['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource=('synthetic',), rejection_line=2)


@pytest.mark.parametrize('source', [
    "holder = [*resource, globals().copy]\ncopy = holder[-1]",
    "holder = (*resource, globals().copy)\ncopy = holder[-1]",
    "holder = {*resource, globals().copy}\ncopy = next(item for item in holder if callable(item))",
])
def test_namespace_comprehension_payload_boundary_retains_opaque_carriers(source):
    source += "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource=('synthetic',), rejection_line=1)


@pytest.mark.parametrize('source', [
    'holder = []\nholder.extend(item for item in resource)',
    'holder = set()\nholder.update(item for item in resource)',
    'holder = [item for item in resource]',
    'holder = {item for item in resource}',
    'holder = {item: None for item in resource}',
    'holder = [item for item in (nested for nested in resource)]',
    'holder = [key for key in globals()]',
    'holder = {key: None for key in globals()}',
    'holder = {*resource, 1}',
    'holder = (*resource, 1)',
    "receiver = {item: None for item in resource}\nreceiver.fromkeys(['ordinary'], None)",
])
def test_namespace_comprehension_static_ordinary_controls(source):
    """Static acceptance controls make no claim of forbidden runtime effects."""
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('mapping', [
    'globals()', 'sys.modules', 'globals().copy()', 'dict(globals())',
    'sys.modules.copy()', 'dict.copy(sys.modules)',
    "{'copy': globals().copy}", "{'namespace': globals()}",
    "{'nested': {'copy': globals().copy}}", 'globals() | {}',
])
@pytest.mark.parametrize('expansion', [
    '[*mapping]', '(*mapping,)', '{*mapping}',
    '[*(*mapping,)]', '(*[*mapping],)', '{*[*mapping]}',
    'capture(*mapping)', '[key for key in mapping]',
])
def test_namespace_mapping_key_expansion_runtime_acceptance(mapping, expansion):
    source = 'import sys\ndef capture(*names):\n    return names\nmapping = ' + mapping
    source += '\nexpected = set(mapping)\nkeys = ' + expansion
    (keys, expected), sink = run_isolated_namespace_probe(source, '(keys, expected)')
    assert all(isinstance(key, str) for key in keys)
    assert set(keys) == expected
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source,rejection_line', [
    ("holder = [*{'copy': globals().copy}.values()]\ncopy = holder[0]", 1),
    ("holder = (*{'copy': globals().copy}.values(),)\ncopy = holder[0]", 1),
    ("holder = {*{'copy': globals().copy}.values()}\ncopy = next(iter(holder))", 1),
    ("key = globals().copy\nmapping = {key: None}\nholder = [*mapping]\ncopy = holder[0]", 2),
    ("holder = [*[globals().copy]]\ncopy = holder[0]", None),
    ("holder = (*[globals().copy],)\ncopy = holder[0]", None),
    ("holder = {*[globals().copy]}\ncopy = next(iter(holder))", 1),
])
def test_namespace_mapping_key_expansion_retains_actual_carriers(source, rejection_line):
    source += "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=rejection_line)


_COMPUTED_MAPPING_LOOKUPS = [
    'mapping[key]', 'mapping.get(key)', 'mapping.__getitem__(key)',
    'dict.get(mapping, key)', 'dict.__getitem__(mapping, key)',
    'get(key)', 'unbound(mapping, key)', 'lookup(mapping, key)',
]


@pytest.mark.parametrize('lookup_expression', _COMPUTED_MAPPING_LOOKUPS)
@pytest.mark.parametrize('mapping,key_source', [
    ("{'copy': globals().copy}", 'keys = [*mapping]\nkey = keys[0]'),
    ("{'copy': globals().copy}.copy()", 'keys = (*mapping,)\nkey = keys[0]'),
    ("{'copy': globals().copy} | {'ordinary': 'safe'}", 'keys = [key for key in mapping]\nkey = keys[0]'),
    ("{'nested': {'copy': globals().copy}}['nested']", 'key = resource'),
    ("{'copy': globals().copy}", "key = ''.join(['co', 'py'])"),
])
def test_namespace_mapping_computed_lookup_runtime_rejection(mapping, key_source, lookup_expression):
    source = 'from operator import getitem as lookup\nmapping = ' + mapping
    source += '\nget = mapping.get\nunbound = dict.get\n' + key_source
    rejection_line = len(source.splitlines()) + 1
    source += '\ncopy = ' + lookup_expression
    source += "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource='copy', rejection_line=rejection_line)


@pytest.mark.parametrize('lookup_expression', _COMPUTED_MAPPING_LOOKUPS)
@pytest.mark.parametrize('mapping,key_source', [
    ("{'copy': globals().copy, 'ordinary': 'safe'}", "key = 'ordinary'"),
    ("{'copy': globals().copy, 'ordinary': 'safe'}", "name = 'ordinary'\nkey = name"),
    ("{'ordinary': 'safe'}", 'keys = [*mapping]\nkey = keys[0]'),
])
def test_namespace_mapping_computed_lookup_safe_runtime_controls(mapping, key_source, lookup_expression):
    source = 'from operator import getitem as lookup\nmapping = ' + mapping
    source += '\nget = mapping.get\nunbound = dict.get\n' + key_source
    source += '\nobserved = ' + lookup_expression
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 'safe'
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('helper,call,rejection_line', [
    ('lookup = lambda mapping, key: mapping[key]', 'lookup(mapping, key)', 1),
    ('lookup = lambda mapping, key: mapping.get(key)', 'lookup(mapping, key)', 1),
    ('def lookup(mapping, key):\n    return mapping[key]', 'lookup(mapping, key)', 2),
    ('def lookup(mapping, *, key):\n    return mapping[key]', 'lookup(mapping, key=key)', 2),
    ('def lookup(mapping, key):\n    return mapping[key]', 'lookup(*(mapping, key))', 2),
    ('def lookup(mapping, key):\n    return mapping[key]', "lookup(**{'mapping': mapping, 'key': key})", 2),
])
def test_namespace_local_lookup_runtime_rejection(helper, call, rejection_line):
    source = helper + "\nmapping = {'copy': globals().copy}\nkey = resource\ncopy = " + call
    source += "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource='copy', rejection_line=rejection_line)


@pytest.mark.parametrize('helper,call', [
    ('lookup = lambda mapping: [*mapping]', 'lookup(mapping)'),
    ('def lookup(mapping):\n    return [key for key in mapping]', 'lookup(mapping)'),
    ('def lookup(mapping, key):\n    return mapping[key]', "lookup(mapping, 'ordinary')"),
])
def test_namespace_local_lookup_safe_runtime_controls(helper, call):
    source = helper + "\nmapping = {'copy': globals().copy, 'ordinary': 'safe'}\nobserved = " + call
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed in (['copy', 'ordinary'], 'safe')
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('helper,call,rejection_line', [
    ('lookup = lambda *values: values[0]', 'lookup(mapping)', 3),
    ('async def lookup(mapping):\n    return mapping\nimport asyncio', 'asyncio.run(lookup(mapping))', 5),
    ('def lookup(mapping):\n    yield mapping', 'next(lookup(mapping))', 4),
    ('def lookup(mapping, repeat):\n    if repeat:\n        return lookup(mapping, False)\n    return mapping', 'lookup(mapping, True)', 3),
])
def test_namespace_local_transport_unsupported_runtime_rejection(helper, call, rejection_line):
    source = helper + "\nmapping = {'copy': globals().copy}\ncopy = " + call + "['copy']"
    source += "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=rejection_line)


@pytest.mark.parametrize('source', [
    'def helper(value):\n    yield getattr(value, "real")\nobserved = next(helper(resource))',
    'import asyncio\nasync def helper(value):\n    return getattr(value, "real")\nobserved = asyncio.run(helper(resource))',
    'def helper(value, repeat):\n    if repeat:\n        return helper(value, False)\n    return getattr(value, "real")\nobserved = helper(resource, True)',
    'import builtins\ndef helper(value):\n    yield builtins.int(value)\nobserved = next(helper(resource))',
])
def test_namespace_local_ambient_provider_safe_runtime_controls(source):
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource=1)
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('provider,invoke', [
    ('globals', "()['__builtins__']['exec'](payload)"),
    ('builtins', '.exec(payload)'),
])
def test_namespace_local_returned_provider_runtime_rejection(provider, invoke):
    source = 'import builtins\ndef lookup(ignored):\n    return ' + provider
    source += '\nselected = lookup(resource)\nselected' + invoke
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=5)


_NAMESPACE_PROVIDERS = [
    ('builtins', '.exec(payload)'),
    ('globals', "()['__builtins__']['exec'](payload)"),
    ('locals', "()['builtins'].exec(payload)"),
    ('vars', "()['builtins'].exec(payload)"),
    ('builtins.globals', "()['__builtins__']['exec'](payload)"),
    ('builtins.locals', "()['builtins'].exec(payload)"),
    ('builtins.vars', "()['builtins'].exec(payload)"),
    ('__import__', "('builtins').exec(payload)"),
    ('builtins.__import__', "('builtins').exec(payload)"),
    ('importlib.import_module', "('builtins').exec(payload)"),
    ('namespace_provider', "()['__builtins__']['exec'](payload)"),
    ('module_provider', "('builtins').exec(payload)"),
    ('getattr', "(builtins, 'exec')(payload)"),
    ('builtins.getattr', "(builtins, 'exec')(payload)"),
    ('builtins.__getattribute__', "('exec')(payload)"),
]


@pytest.mark.parametrize('provider,invoke', _NAMESPACE_PROVIDERS)
def test_namespace_provider_mapping_runtime_rejection(provider, invoke):
    source = 'import builtins\nimport importlib\nfrom builtins import globals as namespace_provider'
    source += '\nfrom importlib import import_module as module_provider\nmapping = {"provider": ' + provider + '}'
    source += '\nkeys = [*mapping]\nselected = mapping[keys[0]]\nselected' + invoke
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=7)


@pytest.mark.parametrize('provider,invoke', _NAMESPACE_PROVIDERS)
def test_namespace_provider_mapping_safe_runtime_controls(provider, invoke):
    source = 'import builtins\nimport importlib\nfrom builtins import globals as namespace_provider'
    source += '\nfrom importlib import import_module as module_provider\nmapping = {"ordinary": "safe", "provider": ' + provider + '}'
    source += '\nkeys = [*mapping]\nobserved = mapping["ordinary"]'
    (keys, observed), sink = run_isolated_namespace_probe(source, '(keys, observed)')
    assert keys == ['ordinary', 'provider']
    assert observed == 'safe'
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('key', ['0', 'None', "b'copy'", 'False', '1.0', '1j', '...'])
@pytest.mark.parametrize('lookup_expression', _COMPUTED_MAPPING_LOOKUPS)
def test_namespace_mapping_nonstring_key_runtime_acceptance(key, lookup_expression):
    source = "from operator import getitem as lookup\nmapping = {'copy': globals().copy}\nget = mapping.get\nunbound = dict.get\nkey = " + key
    source += '\ntry:\n    observed = ' + lookup_expression
    source += "\nexcept KeyError:\n    observed = None"
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed is None
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('key_expression,actual_key', [
    ('f"{\'copyX\':.4}"', 'copy'),
    ('f"{\'copy\'!r}"', "'copy'"),
    ('f"{\'copé\'!a}"', "'cop\\xe9'"),
    ('f"{\'copy\':>6}"', '  copy'),
])
@pytest.mark.parametrize('lookup_expression', ['mapping[key]', 'mapping.get(key)', 'dict.get(mapping, key)'])
def test_namespace_mapping_formatted_key_runtime_rejection(key_expression, actual_key, lookup_expression):
    source = 'mapping = {' + repr(actual_key) + ': globals().copy}\nkey = ' + key_expression
    source += '\ncopy = ' + lookup_expression + "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=3)


def test_namespace_mapping_empty_formatted_literal_runtime_rejection():
    source = "mapping = {'copy': globals}\nkey = f\"{''}copy\"\nselected = mapping[key]"
    source += "\nselected()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=4)


@pytest.mark.parametrize('branch', [
    'key = 0 if resource else None',
    'if resource:\n    key = 0\nelse:\n    key = None',
])
@pytest.mark.parametrize('condition', [True, False])
def test_namespace_mapping_nonstring_branch_runtime_acceptance(branch, condition):
    source = "mapping = {'copy': globals().copy}\n" + branch + '\nobserved = mapping.get(key)'
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource=condition)
    assert observed is None
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('branch', [
    'key = 0 if False else resource',
    'if False:\n    key = 0\nelse:\n    key = resource',
])
def test_namespace_mapping_mixed_branch_runtime_rejection(branch):
    source = "mapping = {'copy': globals().copy}\n" + branch
    rejection_line = len(source.splitlines()) + 1
    source += "\ncopy = mapping.get(key)\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource='copy', rejection_line=rejection_line)


@pytest.mark.parametrize('join', [
    'provider = globals if resource else builtins.globals',
    'provider = globals or builtins.globals',
    'if resource:\n    provider = globals\nelse:\n    provider = builtins.globals',
    'for provider in (globals, builtins.globals):\n    pass',
    'provider = globals\nwhile resource:\n    provider = builtins.globals\n    break',
    'match resource:\n    case True:\n        provider = globals\n    case _:\n        provider = builtins.globals',
    'provider = globals\ntry:\n    provider = builtins.globals\nexcept ValueError:\n    provider = globals',
    'provider = globals\ndef obtain(ignored):\n    return provider\nprovider = builtins.globals\nprovider = obtain(resource)',
    'def obtain(flag):\n    if flag:\n        return globals\n    return builtins.globals\nprovider = obtain(resource)',
])
def test_namespace_provider_join_runtime_rejection(join):
    source = 'import builtins\n' + join
    rejection_line = len(source.splitlines()) + 1
    source += "\nmapping = {'provider': provider}\nkeys = [*mapping]\nselected = mapping[keys[0]]"
    source += "\nselected()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource=True, rejection_line=rejection_line)


@pytest.mark.parametrize('selection', ['mapping["ordinary"]', 'mapping.get("ordinary")', '[*mapping]'])
def test_namespace_provider_join_safe_runtime_controls(selection):
    source = 'import builtins\nmapping = ({"provider": globals, "ordinary": "safe"} if resource else '
    source += '{"provider": builtins.globals, "ordinary": "safe"})\nobserved = ' + selection
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource=True)
    assert observed in ('safe', ['provider', 'ordinary'])
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('definition,acquisition,rejection_line', [
    ('def provider():\n    yield globals', 'next(provider())', 2),
    ('def provider():\n    yield from [globals]', 'next(provider())', 2),
    ('provider = lambda: (yield globals)', 'next(provider())', 1),
    ('async def provider():\n    return globals\nimport asyncio', 'asyncio.run(provider())', 2),
    ('async def provider():\n    return {"provider": globals}\nimport asyncio', 'asyncio.run(provider())["provider"]', 2),
    ('async def provider():\n    yield globals\nasync def consume():\n    return await anext(provider())\nimport asyncio', 'asyncio.run(consume())', 2),
])
def test_namespace_delayed_provider_result_runtime_rejection(definition, acquisition, rejection_line):
    source = definition + '\nselected = ' + acquisition
    source += "\nselected()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=rejection_line)


@pytest.mark.parametrize('definition,acquisition', [
    ('def provider():\n    yield from globals()', 'list(provider())'),
    ('def provider():\n    yield from {"provider": globals}', 'list(provider())'),
    ('async def provider():\n    return [*globals()]\nimport asyncio', 'asyncio.run(provider())'),
])
def test_namespace_delayed_key_only_safe_runtime_controls(definition, acquisition):
    source = definition + '\nkeys = ' + acquisition
    keys, sink = run_isolated_namespace_probe(source, 'keys')
    assert keys and all(isinstance(key, str) for key in keys)
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('lookup_expression', _COMPUTED_MAPPING_LOOKUPS)
@pytest.mark.parametrize('value', ['0', 'None'])
def test_namespace_nonstring_accessor_result_runtime_acceptance(lookup_expression, value):
    source = "from operator import getitem as lookup\nmapping = {'key': " + value + "}\nkey = 'key'\nget = mapping.get\nunbound = dict.get"
    source += '\nselected_key = ' + lookup_expression
    source += "\nmapping = {'copy': globals().copy}\nobserved = mapping.get(selected_key)"
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed is None
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('call', [
    "read(mapping, 'key')", "read(mapping, key='key')", "read(*(mapping, 'key'))",
    "read(**{'mapping': mapping, 'key': 'key'})",
])
def test_namespace_nonstring_helper_result_runtime_acceptance(call):
    source = 'def read(mapping, key):\n    return mapping.get(key)'
    source += "\nmapping = {'provider': globals, 'key': 0}\nselected_key = " + call
    source += '\nobserved = mapping.get(selected_key)'
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed is None
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('lookup_expression', _COMPUTED_MAPPING_LOOKUPS)
def test_namespace_unknown_accessor_result_runtime_rejection(lookup_expression):
    source = "from operator import getitem as lookup\nmapping = {'key': resource}\nkey = 'key'\nget = mapping.get\nunbound = dict.get"
    source += '\nselected_key = ' + lookup_expression
    source += "\nmapping = {'copy': globals().copy}\ncopy = mapping.get(selected_key)"
    source += "\ncopy()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource='copy', rejection_line=8)


@pytest.mark.parametrize('setup', [
    'mapping = globals() if resource else locals()\nkeys = [*mapping]',
    'provider = globals if resource else globals\nkeys = [*provider()]',
    'import builtins\nfrom builtins import globals as alias\nprovider = builtins.globals if resource else alias\nkeys = [*provider()]',
])
def test_namespace_identical_join_key_only_runtime_acceptance(setup):
    keys, sink = run_isolated_namespace_probe(setup, 'keys', resource=True)
    assert keys and all(isinstance(key, str) for key in keys)
    assert sink == []
    assert check_fixture_writes(setup) == []


@pytest.mark.parametrize('values', ['{}', "{'key': 'safe'}"])
@pytest.mark.parametrize('lookup_expression', [
    "values.get('missing')", "dict.get(values, 'missing')", "get('missing')",
    'values.get(0)', 'values.get(None)', "values.get(b'key')",
    'values.get(0, 0)', 'dict.get(values, 0, 0)',
])
def test_namespace_nonstring_accessor_default_runtime_acceptance(values, lookup_expression):
    source = 'values = ' + values + '\nget = values.get\nkey = ' + lookup_expression
    source += "\nmapping = {'copy': globals().copy}\nobserved = mapping.get(key)"
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed is None
    assert sink == []
    assert check_fixture_writes(source) == []


def test_namespace_generator_return_value_runtime_rejection():
    source = 'def provider():\n    if False:\n        yield None\n    return globals'
    source += '\ntry:\n    next(provider())\nexcept StopIteration as finished:\n    selected = finished.value'
    source += "\nselected()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=4)


@pytest.mark.parametrize('definition,acquisition,invoke,rejection_line', [
    ('async def provider():\n    return lambda: globals()', 'asyncio.run(provider())', 'selected()', 2),
    ('async def provider():\n    selected = lambda: globals()\n    return selected', 'asyncio.run(provider())', 'selected()', 3),
    ('async def provider():\n    def selected():\n        return globals()\n    return selected', 'asyncio.run(provider())', 'selected()', 4),
    ('async def provider():\n    return lambda factory=globals: factory()', 'asyncio.run(provider())', 'selected()', 2),
    ('factory = globals\nasync def provider():\n    return lambda: factory()', 'asyncio.run(provider())', 'selected()', 3),
    ('async def provider():\n    return lambda: (lambda: globals())', 'asyncio.run(provider())', 'selected()()', 2),
    ('async def provider():\n    return {"callback": lambda: globals()}', 'asyncio.run(provider())["callback"]', 'selected()', 2),
    ('async def provider():\n    return (lambda: globals(),)', 'asyncio.run(provider())[0]', 'selected()', 2),
    ('def provider():\n    yield lambda: globals()', 'next(provider())', 'selected()', 2),
    ('def provider():\n    yield from [lambda: globals()]', 'next(provider())', 'selected()', 2),
    ('async def provider():\n    yield lambda: globals()\nasync def consume():\n    return await anext(provider())', 'asyncio.run(consume())', 'selected()', 2),
    ('def selected(depth):\n    if depth:\n        return selected(depth - 1)\n    return globals\nasync def provider():\n    return selected', 'asyncio.run(provider())', 'selected(1)()', 6),
])
def test_namespace_delayed_callable_result_runtime_rejection(definition, acquisition, invoke, rejection_line):
    source = definition + '\nimport asyncio\nselected = ' + acquisition
    source += '\n' + invoke + "['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=rejection_line)


@pytest.mark.parametrize('definition,acquisition', [
    ('async def provider():\n    return lambda: 1', 'asyncio.run(provider())'),
    ('async def provider():\n    return lambda value=resource: getattr(value, "real")', 'asyncio.run(provider())'),
    ('async def provider():\n    def callback():\n        return getattr(resource, "real")\n    return callback', 'asyncio.run(provider())'),
    ('import builtins\nasync def provider():\n    return lambda: builtins.len([resource])', 'asyncio.run(provider())'),
    ('async def provider():\n    return {"callback": lambda: 1}', 'asyncio.run(provider())["callback"]'),
    ('def provider():\n    yield lambda: getattr(resource, "real")', 'next(provider())'),
    ('def provider():\n    yield from [lambda: getattr(resource, "real")]', 'next(provider())'),
    ('async def provider():\n    yield lambda: 1\nasync def consume():\n    return await anext(provider())', 'asyncio.run(consume())'),
])
def test_namespace_delayed_callable_safe_runtime_controls(definition, acquisition):
    source = definition + '\nimport asyncio\nselected = ' + acquisition + '\nobserved = selected()'
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource=1)
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


_DELAYED_CALLABLE_TRANSPORTS = [
    ('[callback for callback in [CALLBACK]]', '[0]'),
    ('{callback for callback in [CALLBACK]}', '.pop()'),
    ('{"callback": callback for callback in [CALLBACK]}', '["callback"]'),
    ('CALLBACK if resource else CALLBACK', ''),
    ('{"callback": CALLBACK}[resource]', ''),
    ('{"callback": CALLBACK}.get(resource)', ''),
    ('dict.get({"callback": CALLBACK}, resource)', ''),
    ('{"callback": CALLBACK}.copy()', '["callback"]'),
    ('{}.get("missing", CALLBACK)', ''),
    ('dict.fromkeys(["callback"], CALLBACK)', '["callback"]'),
    ('{"nested": (CALLBACK,)}', '["nested"][0]'),
    ('(callback for callback in [CALLBACK])', '.__next__()'),
    ('dict.fromkeys(list(("callback",)), CALLBACK)', '["callback"]'),
    ('{}.get(resource, CALLBACK)', ''),
]


@pytest.mark.parametrize('transport,selection', _DELAYED_CALLABLE_TRANSPORTS)
def test_namespace_delayed_callable_transport_runtime_rejection(transport, selection):
    source = 'async def provider():\n    return ' + transport.replace('CALLBACK', '(lambda: globals())')
    source += '\nimport asyncio\nselected = asyncio.run(provider())' + selection
    source += "\nselected()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, resource='callback', rejection_line=2)


@pytest.mark.parametrize('transport,selection', _DELAYED_CALLABLE_TRANSPORTS)
def test_namespace_delayed_callable_transport_safe_runtime_controls(transport, selection):
    source = 'async def provider():\n    return ' + transport.replace('CALLBACK', '(lambda: getattr(1, "real"))')
    source += '\nimport asyncio\nselected = asyncio.run(provider())' + selection + '\nobserved = selected()'
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource='callback')
    assert observed == 1
    assert sink == []
    if 'fromkeys(list(' in transport:
        assert_only_explicit_reflective_denials(source)
    else:
        assert check_fixture_writes(source) == []



@pytest.mark.parametrize('mapping', [
    '{"callback": lambda: globals()}',
    '{"callback": lambda: globals()}.copy()',
    'dict.fromkeys(["callback"], lambda: globals())',
])
def test_namespace_delayed_callable_key_iteration_runtime_acceptance(mapping):
    source = 'async def provider():\n    return [*' + mapping + ']\nimport asyncio\nkeys = asyncio.run(provider())'
    keys, sink = run_isolated_namespace_probe(source, 'keys')
    assert keys == ['callback']
    assert sink == []
    assert check_fixture_writes(source) == []


def test_namespace_generator_returned_callable_runtime_rejection():
    source = 'def provider():\n    if False:\n        yield None\n    return lambda: globals()'
    source += '\ntry:\n    next(provider())\nexcept StopIteration as finished:\n    selected = finished.value'
    source += "\nselected()['__builtins__']['exec'](payload)"
    assert_namespace_runtime_effect_and_rejection(source, rejection_line=4)


_LOCAL_CALLBACK_CLASS = ('class Callback:\n    def __call__(self):\n        return globals()\n'
                         '    def selected(self):\n        return globals()\nclass Other(Callback):\n    pass')


@pytest.mark.parametrize('expression,selection', [
    ('Callback()', ''), ('Callback().selected', ''), ('Alias()', ''),
    ('{"callback": Callback()}[resource]', ''),
    ('{"callback": Callback().selected}.get(resource)', ''),
    ('[item for item in [Callback()]]', '[0]'),
    ('Callback() if resource else Alias()', ''),
    ('{"nested": (Callback().selected,)}', '["nested"][0]'),
    ('{"callback": Callback()}.copy()', '["callback"]'),
    ('{}.get("missing", Callback().selected)', ''),
    ('construct(Alias)', ''),
    ('(Callback if resource else Other)()', ''),
    ('next(iter([Alias]))()', ''),
    ('getattr(Callback(), "selected")', ''),
    ('(lambda factory=Alias: factory())()', ''),
    ('dict.fromkeys(["callback"], Callback())', '["callback"]'),
    ('Callback', '()'), ('Alias', '()'), ('Callback if resource else Other', '()'),
])
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_delayed_local_class_runtime_rejection(expression, selection, mode):
    source = _LOCAL_CALLBACK_CLASS + '\nAlias = Callback\ndef construct(factory):\n    return factory()'
    if mode == 'async':
        source += '\nasync def provider():\n    return ' + expression
        rejection_line = len(source.splitlines())
        source += '\nimport asyncio\nselected = asyncio.run(provider())' + selection
    elif mode == 'generator':
        source += '\ndef provider():\n    yield ' + expression
        rejection_line = len(source.splitlines())
        source += '\nselected = next(provider())' + selection
    else:
        source += '\nprovider = lambda: ' + expression
        rejection_line = len(source.splitlines())
        source += '\nselected = provider()' + selection
    source += "\nselected()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source, resource='callback')
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'delayed local class' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
@pytest.mark.parametrize('value', ['Callback()', 'Callback'])
def test_delayed_local_class_key_iteration_runtime_acceptance(mode, value):
    source = _LOCAL_CALLBACK_CLASS
    expression = '[*{"callback": ' + value + '}]'
    if mode == 'async':
        source += '\nasync def provider():\n    return ' + expression + '\nimport asyncio\nkeys = asyncio.run(provider())'
    elif mode == 'generator':
        source += '\ndef provider():\n    yield from ' + expression + '\nkeys = list(provider())'
    else:
        source += '\nprovider = lambda: ' + expression + '\nkeys = provider()'
    keys, sink = run_isolated_namespace_probe(source, 'keys')
    assert keys == ['callback']
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source', [
    'class Callback:\n    def __call__(self):\n        return getattr(1, "real")\nobserved = Callback()()',
    'async def provider():\n    return lambda: getattr(1, "real")\nimport asyncio\nobserved = asyncio.run(provider())()',
    'def provider():\n    yield lambda: getattr(1, "real")\nobserved = next(provider())()',
    'def helper():\n    return getattr(1, "real")\nprovider = lambda: helper\nobserved = provider()()',
])
def test_delayed_local_class_supported_helper_runtime_controls(source):
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


def test_local_class_marker_preserves_fixture_ownership_runtime_control():
    source = ('from types import SimpleNamespace\nfrom threading import RLock\nimport sqlite3\n'
              'from tests.fixture_write import fixture_write\nclass Holder:\n    pass\nholder = Holder()\n'
              'connection = sqlite3.connect(":memory:")\n'
              'holder.workspace = SimpleNamespace(connection=connection, _lock=RLock())\n'
              'with fixture_write(holder.workspace) as handle:\n'
              '    handle.execute("CREATE TABLE synthetic (value INTEGER)")\n'
              '    holder.workspace.connection.execute("INSERT INTO synthetic VALUES (1)")\n'
              'observed = holder.workspace.connection.execute("SELECT value FROM synthetic").fetchone()[0]\n'
              'connection.close()')
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


def test_local_class_argument_does_not_make_external_result_a_local_instance():
    source = ('from types import SimpleNamespace\nclass Callback:\n    pass\n'
              'external = SimpleNamespace(callback=Callback(), answer=1)\n'
              'provider = lambda: external.answer\nobserved = provider()')
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


_INDIRECT_LOCAL_CLASS_RESULTS = [
    ('type.__call__(Callback)', ''), ('object.__new__(Callback)', ''),
    ('builtins.type.__call__(Callback)', ''), ('builtins.object.__new__(Callback)', ''),
    ('allocate(Callback)', ''), ('construct(Callback)', ''),
    ('getattr(object, "__new__")(Callback)', ''),
    ('getattr(builtins, "type").__call__(Callback)', ''),
    ('__import__("builtins").object.__new__(Callback)', ''),
    ('modules.import_module("builtins").type.__call__(Callback)', ''),
    ('{"new": object.__new__}["new"](Callback)', ''),
    ('object.__new__({"callback": Callback}[resource])', ''),
    ('allocate(*(Callback,))', ''),
    ('copies.copy(Callback())', ''), ('copies.deepcopy(Callback())', ''),
    ('clone(Callback())', ''), ('deep_clone(Callback())', ''),
    ('getattr(copies, "copy")(Callback())', ''),
    ('{"copy": copies.deepcopy}["copy"](Callback())', ''),
    ('copies.copy(Callback().selected)', ''),
    ('copies.deepcopy({"callback": Callback()})', '["callback"]'),
    ('copies.copy(Callback)', '()'),
    ('(type.__call__ if resource else object.__new__)(Callback)', ''),
    ('{"callback": object.__new__}[resource](Callback)', ''),
    ('dict.fromkeys(["callback"], object.__new__)[resource](Callback)', ''),
    ('(copies.copy if resource else copies.deepcopy)(Callback())', ''),
    ('{"callback": copies.copy}.get(resource)(Callback())', ''),
    ('{}.get("missing", copies.deepcopy)(Callback())', ''),
    ('apply(allocate, Callback)', ''), ('apply(clone, Callback())', ''),
    ('clone(x=Callback())', ''), ('deep_clone(**{"x": Callback()})', ''),
    ('deep_clone(memo={}, x=Callback())', ''),
    ('deep_clone(**{"memo": {}, "x": Callback()})', ''),
    ('type.__call__(dict, callback=Callback())', '["callback"]'),
    ('type.__call__(dict, **{"callback": Callback})', '["callback"]()'),
    ('construct(dict, {"callback": Callback()})', '["callback"]'),
    ('type.__call__(*(dict,), callback=Callback())', '["callback"]'),
    ('type.__call__(list, [Callback()])', '[0]'),
    ('type.__call__(tuple, (Callback().selected,))', '[0]'),
    ('type.__call__(set, [Callback()])', '.pop()'),
    ('type.__call__(frozenset, [Callback()])', '.__iter__().__next__()'),
    ('builtin_type.__call__(Callback)', ''), ('builtin_object.__new__(Callback)', ''),
    ('getattr(builtin_type, "__call__")(Callback)', ''),
    ('type.__call__(*(item for item in ()), dict, callback=Callback())', '["callback"]'),
    ('type.__call__(choose([dict]), callback=Callback())', '["callback"]'),
    ('type.__call__(dict if resource else list, [("callback", Callback())])', '["callback"]'),
    ('type.__call__(list if resource else dict, [("callback", Callback())])', '[0][1]'),
    ('type.__call__.__call__(Callback)', ''), ('object.__new__.__call__(Callback)', ''),
    ('copies.copy.__call__(Callback())', ''), ('copies.deepcopy.__call__(Callback())', ''),
    ('type.__call__(choose([item for item in [dict]]), callback=Callback())', '["callback"]'),
    ('type.__call__([item for item in [dict]][0], callback=Callback())', '["callback"]'),
    ('type.__call__(next(iter([item for item in [dict]])), callback=Callback())', '["callback"]'),
    ('type.__call__(next(iter({item for item in [dict]})), callback=Callback())', '["callback"]'),
    ('type.__call__({key: dict for key in ["callback"]}.get(resource), callback=Callback())', '["callback"]'),
    ('type.__call__(next(item for item in [dict]), callback=Callback())', '["callback"]'),
    ('type.__call__(choose([{"callback": dict}])["callback"], callback=Callback())', '["callback"]'),
]


@pytest.mark.parametrize('expression,selection', _INDIRECT_LOCAL_CLASS_RESULTS)
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_indirect_local_class_delayed_result_runtime_rejection(expression, selection, mode):
    source = (_LOCAL_CALLBACK_CLASS + '\nimport builtins\nimport importlib as modules\nimport copy as copies\n'
              'from copy import copy as clone, deepcopy as deep_clone\n'
              'from builtins import type as builtin_type, object as builtin_object\n'
              'allocate = object.__new__\nconstruct = getattr(type, "__call__")\n'
              'def apply(operation, value):\n    return operation(value)\n'
              'def choose(values):\n    return values[0]')
    if mode == 'async':
        source += '\nasync def provider():\n    return ' + expression
        rejection_line = len(source.splitlines())
        source += '\nimport asyncio\nselected = asyncio.run(provider())' + selection
    elif mode == 'generator':
        source += '\ndef provider():\n    yield ' + expression
        rejection_line = len(source.splitlines())
        source += '\nselected = next(provider())' + selection
    else:
        source += '\nprovider = lambda: ' + expression
        rejection_line = len(source.splitlines())
        source += '\nselected = provider()' + selection
    source += "\nselected()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source, resource='callback')
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'delayed local class' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('expression', [
    'type.__call__(Callback)()', 'object.__new__(Callback)()',
    'copies.copy(Callback())()', 'copies.deepcopy(Callback())()',
    '(lambda: copies.copy({"answer": 1}))()["answer"]',
    '(lambda: copies.deepcopy({"answer": 1}))()["answer"]',
    '(lambda: [*copies.copy({"callback": Callback()})])() == ["callback"]',
    '(lambda: isinstance(Callback(), Callback))()',
    '(lambda: external.answer)()',
    '(lambda: [*{"callback": object.__new__}])() == ["callback"]',
    '(lambda: [*{"callback": copies.deepcopy}])() == ["callback"]',
    '(lambda: copies.deepcopy(memo={"unused": Callback()}, x={"answer": 1}))()["answer"]',
])
def test_indirect_local_class_safe_runtime_controls(expression):
    source = ('import copy as copies\nfrom types import SimpleNamespace\nclass Callback:\n'
              '    def __call__(self):\n        return getattr(1, "real")\n'
              'external = SimpleNamespace(callback=Callback(), answer=1)\nobserved = ' + expression)
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    if expression.startswith('type.__call__'):
        assert_only_explicit_reflective_denials(source)
    else:
        assert check_fixture_writes(source) == []



@pytest.mark.parametrize('expression', [
    'type.__call__(str, Callback)', 'allocate(str, Callback)',
    'getattr(type, "__call__")(str, Callback)', 'builtins.type.__call__(str, Callback)',
    'type.__call__(*(str, Callback))', 'type.__call__(*(), *(str, Callback))',
    'type.__call__(str, *[Callback])',
])
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_indirect_constructor_conversion_arguments_runtime_acceptance(expression, mode):
    source = 'import builtins\nclass Callback:\n    pass\nallocate = type.__call__'
    if mode == 'async':
        source += '\nasync def provider():\n    return ' + expression
        source += '\nimport asyncio\nobserved = asyncio.run(provider())'
    elif mode == 'generator':
        source += '\ndef provider():\n    yield ' + expression + '\nobserved = next(provider())'
    else:
        source += '\nprovider = lambda: ' + expression + '\nobserved = provider()'
    source += '\naccepted = isinstance(observed, str) and observed.endswith("Callback\'>")'
    accepted, sink = run_isolated_namespace_probe(source, 'accepted')
    assert accepted is True
    assert sink == []
    assert_only_explicit_reflective_denials(source)



@pytest.mark.parametrize('container', ['list', 'tuple', 'set', 'frozenset'])
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_indirect_builtin_container_keys_runtime_acceptance(container, mode):
    source = 'class Callback:\n    pass'
    expression = 'type.__call__(' + container + ', {"callback": Callback()})'
    if mode == 'async':
        source += '\nasync def provider():\n    return ' + expression
        source += '\nimport asyncio\nobserved = asyncio.run(provider())'
    elif mode == 'generator':
        source += '\ndef provider():\n    yield ' + expression + '\nobserved = next(provider())'
    else:
        source += '\nprovider = lambda: ' + expression + '\nobserved = provider()'
    source += '\naccepted = list(observed) == ["callback"]'
    accepted, sink = run_isolated_namespace_probe(source, 'accepted')
    assert accepted is True
    assert sink == []
    assert_only_explicit_reflective_denials(source)



@pytest.mark.parametrize('expression', [
    'copy.copy(Callback())', 'copy.deepcopy(Callback())', 'alias.copy(Callback)',
    'getattr(copy, "copy")(Callback())', '{"copy": copy.deepcopy}["copy"](Callback())',
])
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_unverified_copy_parameter_runtime_acceptance(expression, mode):
    source = ('from types import SimpleNamespace\nclass Callback:\n    pass\n'
              'def ordinary(value):\n    return 1\n'
              'fake = SimpleNamespace(copy=ordinary, deepcopy=ordinary)')
    if mode == 'async':
        source += '\nasync def provider(copy):\n    alias = copy\n    return ' + expression
        source += '\nimport asyncio\nobserved = asyncio.run(provider(fake))'
    elif mode == 'generator':
        source += '\ndef provider(copy):\n    alias = copy\n    yield ' + expression
        source += '\nobserved = next(provider(fake))'
    else:
        source += '\ndef helper(copy):\n    alias = copy\n    return lambda: ' + expression
        source += '\nobserved = helper(fake)()'
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('prelude', [
    'import copy\ncopy = fake', 'from copy import copy\ncopy = ordinary',
    'copy = fake\nalias = copy',
])
def test_copy_import_rebinding_runtime_acceptance(prelude):
    source = ('from types import SimpleNamespace\nclass Callback:\n    pass\n'
              'def ordinary(value):\n    return 1\n'
              'fake = SimpleNamespace(copy=ordinary)\n' + prelude)
    expression = 'copy(Callback())' if 'from copy import' in prelude else 'copy.copy(Callback())'
    source += '\nprovider = lambda: ' + expression + '\nobserved = provider()'
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('name,access', [
    ('type', 'type.__call__'), ('object', 'object.__new__'),
    ('type', 'getattr(type, "__call__")'), ('object', 'getattr(object, "__new__")'),
    ('type', 'alias.__call__'),
])
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_unverified_allocator_parameter_runtime_acceptance(name, access, mode):
    source = ('from types import SimpleNamespace\nclass Callback:\n    pass\n'
              'def ordinary(value):\n    return 1\n'
              'fake = SimpleNamespace(__call__=ordinary, __new__=ordinary)')
    expression = access + '(Callback)'
    if mode == 'async':
        source += '\nasync def provider(' + name + '):\n    alias = ' + name + '\n    return ' + expression
        source += '\nimport asyncio\nobserved = asyncio.run(provider(fake))'
    elif mode == 'generator':
        source += '\ndef provider(' + name + '):\n    alias = ' + name + '\n    yield ' + expression
        source += '\nobserved = next(provider(fake))'
    else:
        source += '\ndef helper(' + name + '):\n    alias = ' + name + '\n    return lambda: ' + expression
        source += '\nobserved = helper(fake)()'
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('mode', ['for', 'async-for', 'generator', 'comprehension'])
def test_local_iterator_result_runtime_rejection(mode):
    source = (_LOCAL_CALLBACK_CLASS + '\nclass IterableCallback(Callback):\n'
              '    def __iter__(self):\n        yield self\n'
              '    async def __aiter__(self):\n        yield self')
    if mode == 'generator':
        source += '\ndef provider():\n    for item in IterableCallback():\n        yield item'
        rejection_line = len(source.splitlines())
        source += '\nselected = next(provider())'
    else:
        source += '\nasync def provider():\n'
        if mode == 'comprehension':
            source += '    return [item for item in IterableCallback()]'
        else:
            source += '    ' + ('async ' if mode == 'async-for' else '') + 'for item in IterableCallback():\n        return item'
        rejection_line = len(source.splitlines())
        source += '\nimport asyncio\nselected = asyncio.run(provider())' + ('[0]' if mode == 'comprehension' else '')
    source += "\nselected()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source)
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'delayed local class' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('body', [
    'return type.__call__(*(item for item in ()), dict, answer=1)["answer"]',
    'return type.__call__(dict if resource else list, [("answer", 1)])["answer"]',
    'for key in {"answer": dict}:\n        return int(key == "answer")',
    'return int([key for key in {"answer": dict}] == ["answer"])',
    'return int([key for key in {"answer": Callback()}] == ["answer"])',
    'for item in [1]:\n        return item',
    'return ordinary.__call__(Callback())',
    'return int(getattr(dict, "__name__") == "dict")',
])
def test_builtin_carrier_transport_safe_runtime_controls(body):
    source = ('class Callback:\n    pass\ndef ordinary(value):\n    return 1\n'
              'async def provider():\n    ' + body + '\nimport asyncio\nobserved = asyncio.run(provider())')
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource=True)
    assert observed == 1
    assert sink == []
    if 'type.__call__' in body:
        assert_only_explicit_reflective_denials(source)
    else:
        assert check_fixture_writes(source) == []



def test_builtin_generator_marker_preserves_creation_and_consumption_runtime_timing():
    source = ('def record(value):\n    sink.append("consumed")\n    return value\n'
              'callback = dict\npending = (record(callback) for item in [1])\n'
              'before = list(sink)\ncallback = list\nselected = next(pending)\n'
              'observed = (before, selected is list)')
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == ([], True)
    assert sink == ['consumed']
    assert check_fixture_writes(source) == []


_LOOKUP_DEFAULT_CARRIER_RESULTS = [
    ('[dict][resource]', 'type.__call__(constructor, callback=Callback())', '["callback"]'),
    ('(dict, int)[resource]', 'type.__call__(constructor, callback=Callback())', '["callback"]'),
    ('[{"factory": dict}][resource]["factory"]', 'type.__call__(constructor, callback=Callback())', '["callback"]'),
    ('operator.getitem([dict], resource)', 'type.__call__(constructor, callback=Callback())', '["callback"]'),
    ('[type.__call__][resource]', 'constructor(Callback)', ''),
    ('[copies.copy][resource]', 'constructor(Callback())', ''),
    ('[Callback][resource]', 'constructor()', ''),
    ('getattr(object(), "missing", dict)', 'type.__call__(constructor, callback=Callback())', '["callback"]'),
    ('getattr(object(), "missing", type.__call__)', 'constructor(Callback)', ''),
    ('getattr(object(), "missing", copies.copy)', 'constructor(Callback())', ''),
    ('getattr(object(), "missing", Callback)', 'constructor()', ''),
    ('next(iter(dict.fromkeys([dict], int)))', 'type.__call__(constructor, callback=Callback())', '["callback"]'),
    ('list(dict.fromkeys([type.__call__], int))[resource]', 'constructor(Callback)', ''),
]


@pytest.mark.parametrize('lookup,expression,selection', _LOOKUP_DEFAULT_CARRIER_RESULTS)
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_lookup_default_carrier_runtime_rejection(lookup, expression, selection, mode):
    source = (_LOCAL_CALLBACK_CLASS + '\nimport operator\nimport copy as copies\n'
              'constructor = ' + lookup)
    if mode == 'async':
        source += '\nasync def provider():\n    return ' + expression
        rejection_line = len(source.splitlines())
        source += '\nimport asyncio\nselected = asyncio.run(provider())' + selection
    elif mode == 'generator':
        source += '\ndef provider():\n    yield ' + expression
        rejection_line = len(source.splitlines())
        source += '\nselected = next(provider())' + selection
    else:
        source += '\nprovider = lambda: ' + expression
        rejection_line = len(source.splitlines())
        source += '\nselected = provider()' + selection
    source += "\nselected()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source, resource=0)
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'delayed local class' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('adapter,target,iterable,expression', [
    ('enumerate', 'index, constructor', '[dict]', 'type.__call__(constructor, callback=Callback())'),
    ('reversed', 'constructor', '[dict]', 'type.__call__(constructor, callback=Callback())'),
    ('zip', 'constructor, index', '[dict], [0]', 'type.__call__(constructor, callback=Callback())'),
    ('builtins.enumerate', 'index, constructor', '[dict]', 'type.__call__(constructor, callback=Callback())'),
    ('reverse', 'constructor', '[dict]', 'type.__call__(constructor, callback=Callback())'),
    ('pair', 'constructor, index', '[dict], [0]', 'type.__call__(constructor, callback=Callback())'),
    ('enumerate', 'index, constructor', '[type.__call__]', 'constructor(Callback)'),
    ('reversed', 'constructor', '[copies.copy]', 'constructor(Callback())'),
    ('zip', 'constructor, index', '[Callback], [0]', 'constructor()'),
    ('enumerate', 'index, constructor', '[Callback()]', 'constructor'),
    ('reversed', 'constructor', '[Callback().selected]', 'constructor'),
    ('reversed', 'constructor', 'dict.fromkeys([dict], int)', 'type.__call__(constructor, callback=Callback())'),
])
def test_iterator_adapter_carrier_runtime_rejection(adapter, target, iterable, expression):
    source = (_LOCAL_CALLBACK_CLASS + '\nimport builtins\nimport copy as copies\n'
              'from builtins import reversed as reverse, zip as pair\n'
              'async def provider():\n    for ' + target + ' in ' + adapter + '(' + iterable + '):')
    rejection_line = len(source.splitlines())
    source += '\n        return ' + expression + '\nimport asyncio\nselected = asyncio.run(provider())'
    if 'callback=' in expression:
        source += '["callback"]'
    source += "\nselected()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source)
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'iterator adapter' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('keys,lookup', [
    ('[dict]', '[dict]'), ('[dict]', '.get(dict)'),
    ('[type.__call__]', '[type.__call__]'), ('[Numeric]', '[Numeric]'),
    ('[numeric]', '[numeric]'), ('[dict]', '.get(resource)'),
    ('[dict, list]', '.copy().get(dict)'),
    ('[type.__call__]', '.copy()[type.__call__]'),
])
@pytest.mark.parametrize('mode', ['async', 'generator', 'lambda'])
def test_fromkeys_key_carrier_scalar_value_runtime_acceptance(keys, lookup, mode):
    source = ('class Numeric:\n    def __int__(self):\n        return 1\n'
              'numeric = Numeric()\nmapping = dict.fromkeys(' + keys + ', int)\n'
              'constructor = mapping' + lookup)
    expression = 'type.__call__(constructor, numeric)'
    if mode == 'async':
        source += '\nasync def provider():\n    return ' + expression
        source += '\nimport asyncio\nobserved = asyncio.run(provider())'
    elif mode == 'generator':
        source += '\ndef provider():\n    yield ' + expression + '\nobserved = next(provider())'
    else:
        source += '\nprovider = lambda: ' + expression + '\nobserved = provider()'
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource=dict)
    assert observed == 1
    assert sink == []
    assert_only_explicit_reflective_denials(source)



@pytest.mark.parametrize('body', [
    'return type.__call__([dict, int][1], numeric)',
    'return [dict, globals][0].__name__ == "dict"',
    'return type.__call__(getattr(object(), "missing", int), numeric)',
    'return getattr(dict, "__name__", "missing") == "dict"',
    'return list(enumerate([1])) == [(0, 1)]',
    'return list(reversed([1])) == [1]',
    'return list(zip([1], [2])) == [(1, 2)]',
    'return list(enumerate({"key": dict})) == [(0, "key")]',
    'return list(reversed({"key": type.__call__})) == ["key"]',
    'return list(zip({"key": Numeric}, [1])) == [("key", 1)]',
    'return list(reversed(dict.fromkeys({"key": dict}, int))) == ["key"]',
    'return list(enumerate(dict.fromkeys(["key"], Numeric))) == [(0, "key")]',
])
def test_lookup_adapter_key_only_runtime_acceptance(body):
    source = ('class Numeric:\n    def __int__(self):\n        return 1\nnumeric = Numeric()\n'
              'async def provider():\n    ' + body + '\nimport asyncio\nobserved = asyncio.run(provider())')
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    if 'type.__call__' in body:
        assert_only_explicit_reflective_denials(source)
    else:
        assert check_fixture_writes(source) == []



@pytest.mark.parametrize('name', ['enumerate', 'reversed', 'zip'])
def test_iterator_adapter_local_parameter_identity_runtime_acceptance(name):
    source = ('class Callback:\n    pass\ndef ordinary(values):\n    return [1]\n'
              'async def provider(' + name + '):\n    return ' + name + '([Callback()])[0]\n'
              'import asyncio\nobserved = asyncio.run(provider(ordinary))')
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert check_fixture_writes(source) == []


_FROMKEYS_TRANSPORTS = [
    'mapping', 'mapping.copy()', 'copies.copy(mapping)', 'dict(mapping)',
    'mapping | {}', '{} | mapping', 'mapping.__or__({})',
    'mapping if resource else dict.fromkeys([list], int)',
]


@pytest.mark.parametrize('transport', _FROMKEYS_TRANSPORTS)
def test_fromkeys_transport_keeps_scalar_values_runtime_acceptance(transport):
    source = ('import copy as copies\nclass Numeric:\n    def __int__(self):\n        return 1\n'
              'numeric = Numeric()\nmapping = dict.fromkeys([dict], int)\n'
              'selected = ' + transport + '\nconstructor = selected.get(dict)\n'
              'async def provider():\n    return type.__call__(constructor, numeric)\n'
              'import asyncio\nobserved = asyncio.run(provider())')
    observed, sink = run_isolated_namespace_probe(source, 'observed', resource=True)
    assert observed == 1
    assert sink == []
    assert_only_explicit_reflective_denials(source)



@pytest.mark.parametrize('transport', _FROMKEYS_TRANSPORTS)
def test_fromkeys_transport_keeps_key_carriers_runtime_rejection(transport):
    source = (_LOCAL_CALLBACK_CLASS + '\nimport copy as copies\nmapping = dict.fromkeys([dict], int)\n'
              'selected = ' + transport + '\nconstructor = next(iter(selected))\n'
              'async def provider():\n    return type.__call__(constructor, callback=Callback())')
    rejection_line = len(source.splitlines())
    source += "\nimport asyncio\nasyncio.run(provider())['callback']()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source, resource=True)
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'delayed local class' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('expression,selection', [
    ('dict.fromkeys(["key"], dict).values()', '[0]'),
    ('dict.fromkeys([dict], int).items()', '[0][0]'),
    ('dict.fromkeys([dict], int).popitem()', '[0]'),
])
def test_fromkeys_enumeration_carrier_runtime_rejection(expression, selection):
    source = _LOCAL_CALLBACK_CLASS + '\nselected = ' + expression
    source += ('\nconstructor = list(selected)' + selection + '\n'
               'async def provider():\n    return type.__call__(constructor, callback=Callback())')
    rejection_line = len(source.splitlines())
    source += "\nimport asyncio\nasyncio.run(provider())['callback']()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source)
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'delayed local class' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


def test_fromkeys_value_enumeration_does_not_inherit_key_carriers():
    source = ('class Numeric:\n    def __int__(self):\n        return 1\n'
              'mapping = dict.fromkeys([dict], int)\nconstructor = next(iter(mapping.values()))\n'
              'async def provider():\n    return type.__call__(constructor, Numeric())\n'
              'import asyncio\nobserved = asyncio.run(provider())')
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == 1
    assert sink == []
    assert_only_explicit_reflective_denials(source)



@pytest.mark.parametrize('operation', ['items', 'popitem'])
@pytest.mark.parametrize('placement', ['key', 'value'])
def test_fromkeys_destructured_carrier_runtime_rejection(operation, placement):
    entry = 'dict.fromkeys([dict], int)' if placement == 'key' else 'dict.fromkeys(["key"], dict)'
    source = _LOCAL_CALLBACK_CLASS + '\nmapping = ' + entry + '\nasync def provider():\n'
    if operation == 'items':
        source += '    for key, value in mapping.items():\n        return type.__call__(' + placement + ', callback=Callback())'
    else:
        source += '    key, value = mapping.popitem()\n    return type.__call__(' + placement + ', callback=Callback())'
    rejection_line = len(source.splitlines())
    source += "\nimport asyncio\nasyncio.run(provider())['callback']()['__builtins__']['exec'](payload)"
    _, sink = run_isolated_namespace_probe(source)
    assert sink == ['synthetic payload executed']
    assert any(item.structural and item.line == rejection_line and 'delayed local class' in item.rule
               and 'fixture_write' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('operation', ['items', 'popitem', 'values'])
def test_dictionary_enumeration_scalar_consumption_runtime_acceptance(operation):
    source = ('class Number:\n    def __str__(self):\n        return "1"\n'
              'mapping = {"VALUE": Number()}\noutput = []\n')
    if operation == 'items':
        source += 'for key, value in mapping.items():\n    output.append((str(key), str(value)))'
    elif operation == 'popitem':
        source += 'key, value = mapping.popitem()\noutput.append((str(key), str(value)))'
    else:
        source += 'for value in mapping.values():\n    output.append(("VALUE", str(value)))'
    source += '\nobserved = output'
    observed, sink = run_isolated_namespace_probe(source, 'observed')
    assert observed == [('VALUE', '1')]
    assert sink == []
    assert check_fixture_writes(source) == []
