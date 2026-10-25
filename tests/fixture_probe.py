"""Execute isolated negative-fixture source with explicit, fixed inputs.

The source itself remains the input to the policy assertion. Compilation uses
an empty namespace; shared handles enter only as named function parameters.
Returning an exception also preserves observations made before a rollback probe
raises, without accepting a caller-supplied execution namespace.
"""
from textwrap import indent


def fixture_probe(source, result='None', *, workspace=None, handle=None,
                  fixture_write=None, sqlite3=None, matter=None, actor=None,
                  factory=None, bench=None, independent=None, condition=None,
                  resources=None, connection=None, sink=None, payload=None,
                  backup_source=None, resource=None, failed=None,
                  media_summary_attempts=None, global_handle=False):
    """Run source with fixed inputs and return its exception and named results.

    ``global_handle`` retains module-level handle rebinding for the original
    global/class-scope probes. Only that named binding becomes global; inputs
    still enter through fixed parameters rather than an execution namespace.
    """
    namespace = {}
    handle_parameter = 'initial_handle' if global_handle else 'handle'
    parameters = ('workspace, ' + handle_parameter + ', fixture_write, sqlite3, matter, actor, factory, '
                  'bench, independent, condition, resources, connection, sink, payload, source, '
                  'resource, failed, MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS')
    handle_binding = '    global handle\n    handle = initial_handle\n' if global_handle else ''
    wrapped = (f'def probe({parameters}):\n' + handle_binding +
               '    try:\n' + indent(source, '        ') + '\n'
               '    except BaseException as error:\n'
               f'        return error, ({result})\n'
               f'    return None, ({result})\n')
    exec(compile(wrapped, '<synthetic-fixture-probe>', 'exec'), namespace)
    probe = namespace['probe']
    return probe(workspace, handle, fixture_write, sqlite3, matter, actor, factory,
                 bench, independent, condition, resources, connection, sink, payload,
                 backup_source, resource, failed, media_summary_attempts)
