"""Fixed repository-source probes for installer, workflow and tier regressions.

These entry points take no executable source or namespace from callers. The
fixture inventory pins this module while checking all other boundary rules.
The executed UTF-8 source also has its own pin here: reviewing a payload change
requires updating its digest and then the inventory's wrapper digest.
"""
import hashlib
import json
from pathlib import Path


_INSTALLER_SOURCE_SHA256 = 'adb0fbfd70a3e2cade142c7da019ab7806f76c134990952c54ffb06ce12ac65c'
_BROWSER_CONDITION_SHA256 = 'dc038d841bac3d663026ff8c96b96bdc953bc201fc257e521d2736b717f8e5ad'
_TIER_VALIDATOR_SHA256 = 'd688849ef71e901ed414d4a8859f7f93ef11fb2bdb5ba9d753db39ca62f74efb'


def _require_reviewed_source(source, expected, label):
    if type(source) is not str or hashlib.sha256(source.encode('utf-8')).hexdigest() != expected:
        raise RuntimeError(
            f'{label} source changed; review the executed text and update its pin in '
            'tests/runtime_probes.py and the wrapper pin in tests/fixture_inventory.py'
        )


def run_auth_mode_probe():
    import exculpata_install as installer
    source = installer._AUTH_MODE_PROBE
    _require_reviewed_source(source, _INSTALLER_SOURCE_SHA256, 'Installer authentication probe')
    exec(source, {})


def tier_validator_without_guard(message):
    """Disable one guard in the pinned validator, retaining its real namespace."""
    import ast
    import inspect
    from case_intelligence import tier_evals

    source = inspect.getsource(tier_evals.validate_scorecard)
    _require_reviewed_source(source, _TIER_VALIDATOR_SHA256, 'Tier validator')
    tree = ast.parse(source)
    guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
              and any(isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call)
                      and child.exc.args and isinstance(child.exc.args[0], ast.Constant)
                      and child.exc.args[0].value == message for child in node.body)]
    if len(guards) != 1:
        raise ValueError('select exactly one tier validator guard by its fixed message')
    guards[0].test = ast.Constant(False)
    namespace = vars(tier_evals).copy()
    exec(compile(ast.fix_missing_locations(tree), '<synthetic guard mutation>', 'exec'), namespace)
    return namespace['validate_scorecard']


def browser_workflow_condition(event, result, docs, browser):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/quality-gates.yml').read_text()
    block = workflow.split('\n  synthetic-browser:\n', 1)[1]
    condition = block.split('${{', 1)[1].split('}}', 1)[0]
    _require_reviewed_source(condition, _BROWSER_CONDITION_SHA256, 'Browser workflow condition')
    for name, value in {
        'github.event_name': event, 'needs.change-scope.result': result,
        'needs.change-scope.outputs.docs_only': docs,
        'needs.change-scope.outputs.browser_changed': browser,
    }.items():
        condition = condition.replace(name, repr(value))
    condition = ' '.join(condition.replace('&&', ' and ').replace('||', ' or ').split())
    return eval(condition, {'__builtins__': {}}, {
        'always': lambda: True, 'fromJSON': json.loads,
        'contains': lambda values, value: value in values,
    })
