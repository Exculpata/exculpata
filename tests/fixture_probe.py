"""Execute isolated negative-fixture source with explicit, fixed inputs.

The source itself remains the input to the policy assertion. Compilation uses
an empty namespace; shared handles enter only as named function parameters.
Returning an exception also preserves observations made before a rollback probe
raises, without storing handles in an execution namespace.
"""
from textwrap import indent


def fixture_probe(source, result='None', *, workspace=None, handle=None,
                  fixture_write=None, sqlite3=None, matter=None, actor=None,
                  factory=None):
    namespace = {}
    parameters = 'workspace, handle, fixture_write, sqlite3, matter, actor, factory'
    wrapped = (f'def probe({parameters}):\n'
               '    try:\n' + indent(source, '        ') + '\n'
               '    except BaseException as error:\n'
               f'        return error, ({result})\n'
               f'    return None, ({result})\n')
    exec(compile(wrapped, '<synthetic-fixture-probe>', 'exec'), namespace)
    probe = namespace['probe']
    return probe(workspace, handle, fixture_write, sqlite3, matter, actor, factory)
