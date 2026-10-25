"""Reusable fixture-only cases for the explicit reflective deny contract.

REFLECTIVE_DENY_CASES exports (name, identical source, required diagnostic,
origin line) tuples for other test callers. Execution stays in fixture_probe;
this module neither resolves imports nor supplies a general checker harness.
"""
from unittest.mock import patch
import sys

import pytest

from tests.fixture_probe import fixture_probe
from tests.fixture_write_policy import check_fixture_writes


_CALLBACK = 'class Callback:\n    def __call__(self):\n        return globals()\n'
_ENDING = ("\nimport asyncio\nselected = asyncio.run(provider())['callback']\n"
           "selected()['__builtins__']['exec'](payload)")

REFLECTIVE_DENY_CASES = (
    ('starred-getattr-default', _CALLBACK +
     "constructor = getattr(*(item for item in (object(), 'missing', dict)))\n"
     'async def provider():\n    return type.__call__(constructor, callback=Callback())' + _ENDING,
     'starred getattr arguments are unsupported', 4),
    *tuple((f'fromkeys-provider-{name}',
            'mapping = dict.fromkeys([globals], int)\nprovider = ' + selection +
            "\nprovider()['__builtins__']['exec'](payload)",
            'fromkeys keys must be literal', 1)
           for name, selection in [('iter', 'next(iter(mapping))'),
                                   ('keys', 'next(iter(mapping.keys()))'),
                                   ('list', 'list(mapping)[0]')]),
    *tuple((f'{name}-call', _CALLBACK +
            f'async def provider():\n    for {target} in {name}.__call__({arguments}):\n'
            '        return type.__call__(constructor, callback=Callback())' + _ENDING,
            'builtin __call__ access is unsupported', 5)
           for name, target, arguments in [('enumerate', 'index, constructor', '[dict]'),
                                           ('reversed', 'constructor', '[dict]'),
                                           ('zip', 'constructor, index', '[dict], [0]')]),
)


@pytest.mark.parametrize('name,source,rule,line', REFLECTIVE_DENY_CASES,
                         ids=[case[0] for case in REFLECTIVE_DENY_CASES])
def test_fixture_reflective_deny_contract(name, source, rule, line):
    sink = []
    with patch.dict(sys.modules):
        error, _ = fixture_probe(source, sink=sink,
                                payload="sink.append('synthetic payload executed')")
    assert error is None
    assert sink == ['synthetic payload executed']
    violations = check_fixture_writes(source, name + '.py')
    assert any(item.structural and item.line == line and rule in item.rule
               and 'fixture_write()' in item.rule for item in violations)


@pytest.mark.parametrize('source,rule', [
    ('sink.append(len.__call__([1]))', 'builtin __call__'),
    ('alias = len\nsink.append(alias.__call__([1]))', 'builtin __call__'),
    ('import builtins as b\nsink.append(b.len.__call__([1]))', 'builtin __call__'),
    ('from builtins import len as length\nsink.append(length.__call__([1]))', 'builtin __call__'),
    ('sink.append(getattr(len, "__call__")([1]))', 'builtin __call__'),
    ('lookup = getattr\nsink.append(lookup(len, "__call__")([1]))', 'builtin __call__'),
    ('values = [len]\nsink.append(values[0].__call__([1]))', 'builtin __call__'),
    ('values = {"length": len}\nsink.append(values["length"].__call__([1]))', 'builtin __call__'),
    ('sink.append(type.__call__(int, "1"))', 'builtin __call__'),
    ('sink.append(dict.fromkeys.__call__(["a"], 1)["a"])', 'builtin __call__'),
    ('make = dict.fromkeys\nsink.append(make.__call__(["a"], 1)["a"])', 'builtin __call__'),
    ('keys = ["a"]\nsink.append(getattr(dict.fromkeys, "__call__")(keys, 1)["a"])', 'builtin __call__'),
    ('sink.append(dict.get.__call__({"a": 1}, "a"))', 'builtin __call__'),
    ('lookup = {"a": 1}.get\nsink.append(lookup.__call__("a"))', 'builtin __call__'),
    ('sink.append(getattr(dict.get, "__call__")({"a": 1}, "a"))', 'builtin __call__'),
    ('class Ordinary:\n    pass\nsink.append(int(isinstance(object.__new__.__call__(Ordinary), Ordinary)))', 'builtin __call__'),
    ('class Ordinary:\n    pass\nmake = object.__new__\nsink.append(int(isinstance(getattr(make, "__call__")(Ordinary), Ordinary)))', 'builtin __call__'),

    ('selected = next(iter({"length": len}.values()))\nsink.append(selected.__call__([1]))', 'builtin __call__'),
    ('key, selected = {"length": len}.popitem()\nsink.append(selected.__call__([1]))', 'builtin __call__'),
    ('for key, selected in {"length": len}.items():\n    sink.append(selected.__call__([1]))', 'builtin __call__'),

    ('import builtins\nsink.append(getattr(builtins, "len").__call__([1]))', 'builtin __call__'),
    ('values = [len]\nindex = 0\nsink.append(values[index].__call__([1]))', 'builtin __call__'),
    ('values = [item for item in [len]]\nsink.append(values[0].__call__([1]))', 'builtin __call__'),
    ('values = (item for item in [len])\nsink.append(next(values).__call__([1]))', 'builtin __call__'),
    ('selected = len if sink == [] else abs\nsink.append(selected.__call__([1]))', 'builtin __call__'),
    ('def apply(operation):\n    return operation.__call__([1])\nsink.append(apply(len))', 'builtin __call__'),
    ('def choose(operation):\n    return operation\nsink.append(choose(len).__call__([1]))', 'builtin __call__'),
    ('import copy\nselected = copy.copy(len)\nsink.append(selected.__call__([1]))', 'builtin __call__'),
    ('sink.append(getattr(object(), "missing", len).__call__([1]))', 'builtin __call__'),

    ('sink.append(getattr(*("a", "count"))("a"))', 'starred getattr'),
    ('lookup = getattr\nsink.append(lookup(*("a", "count"))("a"))', 'starred getattr'),
    ('import builtins\nsink.append(builtins.getattr(*("a", "count"))("a"))', 'starred getattr'),
    ('keys = ["a"]\nsink.append(dict.fromkeys(keys, 1)["a"])', 'fromkeys keys must be literal'),
    ('sink.append(dict.fromkeys((key for key in ["a"]), 1)["a"])', 'fromkeys keys must be literal'),
    ('sink.append(dict.fromkeys([len], 1)[len])', 'fromkeys keys must be literal'),
    ('make = dict.fromkeys\nkeys = ["a"]\nsink.append(make(keys, 1)["a"])', 'fromkeys keys must be literal'),
    ('import builtins\nkeys = ["a"]\nsink.append(builtins.dict.fromkeys(keys, 1)["a"])', 'fromkeys keys must be literal'),
    ('make = getattr(dict, "fromkeys")\nkeys = ["a"]\nsink.append(make(keys, 1)["a"])', 'fromkeys keys must be literal'),
    ('keys = ["a"]\nsink.append({}.fromkeys(keys, 1)["a"])', 'fromkeys keys must be literal'),
    ('keys = ["a"]\nsink.append(dict.fromkeys([*keys], 1)["a"])', 'fromkeys keys must be literal'),
    ('keys = {"a": None}\nsink.append(dict.fromkeys({**keys}, 1)["a"])', 'fromkeys keys must be literal'),
    ('sink.append(dict.fromkeys(*(["a"], 1))["a"])', 'fromkeys keys must be literal'),

])
def test_fixture_reflective_deny_syntactic_boundaries(source, rule):
    sink = []
    error, _ = fixture_probe(source, sink=sink)
    assert error is None
    assert sink == [1]
    assert any(item.structural and rule in item.rule and 'fixture_write()' in item.rule
               for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    'sink.append(len([1]))',
    'alias = len\nsink.append(alias([1]))',
    'sink.append(getattr("a", "count")("a"))',
    'sink.append(getattr(object(), "missing", 1))',
    'sink.append(dict.fromkeys("a", 1)["a"])',
    'sink.append(dict.fromkeys(["a"], 1)["a"])',
    'sink.append(dict.fromkeys(("a",), 1)["a"])',
    'sink.append(dict.fromkeys({"a"}, 1)["a"])',
    'sink.append(dict.fromkeys({"a": None}, 1)["a"])',
    'sink.append(dict.fromkeys([None, 0, -1, b"a", ("a", 2)], 1)[None])',
    'make = dict.fromkeys\nsink.append(make(["a"], 1)["a"])',
    'class Ordinary:\n    def __call__(self, value):\n        return value\nsink.append(Ordinary().__call__(1))',
    'def ordinary(value):\n    return value\nsink.append(ordinary.__call__(1))',
    'def apply(len):\n    return len.__call__(1)\ndef ordinary(value):\n    return value\nsink.append(apply(ordinary))',
    'def apply(getattr):\n    return getattr(*(1,))\ndef ordinary(value):\n    return value\nsink.append(apply(ordinary))',
    'class Ordinary:\n    @staticmethod\n    def fromkeys(keys, default):\n        return {key: default for key in keys}\nkeys = ["a"]\nsink.append(Ordinary.fromkeys(keys, 1)["a"])',
    'def ordinary(*values):\n    return 1\ngetattr = ordinary\nsink.append(getattr(*(1, 2)))',
    'def ordinary(values):\n    return 1\nlen = ordinary\nsink.append(len.__call__([1]))',
    'import builtins\ndef ordinary(values):\n    return 1\nbuiltins = ordinary\nsink.append(builtins.__call__([1]))',
    'from builtins import len as length\ndef ordinary(values):\n    return 1\nlength = ordinary\nsink.append(length.__call__([1]))',
    'keys = [*{"key": len}]\nsink.append(int(keys == ["key"]))',
    'sink.append(int(getattr(len, "__name__") == "len"))',
    'class Ordinary:\n    @staticmethod\n    def fromkeys(keys, default):\n        return {key: default for key in keys}\ndef apply(dict):\n    keys = ["a"]\n    return dict.fromkeys(keys, 1)["a"]\nsink.append(apply(Ordinary))',
    'class Ordinary:\n    @staticmethod\n    def fromkeys(keys, default):\n        return {key: default for key in keys}\ndef apply(dict):\n    keys = ["a"]\n    return getattr(dict, "fromkeys")(keys, 1)["a"]\nsink.append(apply(Ordinary))',
    'class Ordinary:\n    @staticmethod\n    def fromkeys(keys, default):\n        return {key: default for key in keys}\nsink.append(Ordinary.fromkeys.__call__(["a"], 1)["a"])',
    'class Ordinary:\n    def get(self, key):\n        return 1\nsink.append(getattr(Ordinary().get, "__call__")("a"))',
    'sink.append(dict.get({"a": 1}, "a"))',
    'class Ordinary:\n    pass\nsink.append(int(isinstance(object.__new__(Ordinary), Ordinary)))',
])
def test_fixture_reflective_deny_preserves_explicit_calls(source):
    sink = []
    error, _ = fixture_probe(source, sink=sink)
    assert error is None
    assert sink == [1]
    assert check_fixture_writes(source) == []
