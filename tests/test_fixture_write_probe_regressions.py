"""Fixed probe inputs preserve scope, returned observations and raised errors."""
import inspect
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_probe import fixture_probe
from tests.fixture_write_policy import check_fixture_writes


def test_global_handle_mode_preserves_same_source_rebinding_and_isolates_runs():
    source = '''handle = independent
def redirect():
    class Local:
        global handle
        handle = bench.workspace.connection
redirect()
handle.close()
'''
    raw, independent = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    try:
        error, rebound = fixture_probe(source, 'handle', bench=bench,
                                       independent=independent, global_handle=True)
        if error is not None:
            raise error
        assert rebound is raw
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert independent.execute('SELECT 1').fetchone() == (1,)
        assert check_fixture_writes(source)
        error, next_handle = fixture_probe('pass', 'handle', global_handle=True)
        assert error is None and next_handle is None
    finally:
        raw.close()
        independent.close()


def test_probe_preserves_observation_before_raising_original_exception():
    source = "sink.append('observed')\nraise ValueError('synthetic probe failure')"
    error, changed = fixture_probe(source, 'sink', sink=[])
    assert changed == ['observed']
    with pytest.raises(ValueError, match='synthetic probe failure'):
        if error is not None:
            raise error


def test_probe_accepts_only_fixed_named_inputs():
    assert all(parameter.kind is not inspect.Parameter.VAR_KEYWORD
               for parameter in inspect.signature(fixture_probe).parameters.values())
    with pytest.raises(TypeError, match='namespace'):
        fixture_probe('pass', namespace={})
