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
    assert check_fixture_writes(source) == []


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
    source = 'import sys\nimport builtins\n' + access.replace('RECEIVER', receiver)
    source += "\nscopes = construct(['scope'], globals())\nscopes['scope']['__builtins__']['exec'](payload)"
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


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
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


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
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "holder = {'copy': globals().copy}\ncopy = holder.get('copy')\ncopy()['__builtins__']['exec'](payload)",
    "import sys\nholder = {'copy': sys.modules.copy}\nholder.get('copy')()['builtins'].eval(payload)",
    "holder = {'nested': {'copy': globals().copy}}\nholder.get('nested').get('copy')()['__builtins__']['exec'](payload)",
    "holder = {'copy': (globals().copy,)}\nholder.get('copy')[0]()['__builtins__']['exec'](payload)",
    "holder = {'copy': globals().copy} | {}\nholder.get('copy')()['__builtins__']['exec'](payload)",
    "holder = {}\nholder['copy'] = globals().copy  # fixture-write: Deliberately transport a synthetic bound callable.",
])
def test_namespace_accessor_bound_receiver_survives_literal_carriers(source):
    assert any(item.structural and 'dynamic execution' in item.rule
               for item in check_fixture_writes(source))


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
    assert check_fixture_writes(source) == []
