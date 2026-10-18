"""Fixed repository-source probes for installer and workflow regression tests.

These entry points take no executable source or namespace from callers. The
fixture inventory pins this module while checking all other boundary rules.
"""
import json
from pathlib import Path


def run_auth_mode_probe():
    import exculpata_install as installer
    exec(installer._AUTH_MODE_PROBE, {})


def browser_workflow_condition(event, result, docs, browser):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/quality-gates.yml').read_text()
    block = workflow.split('\n  synthetic-browser:\n', 1)[1]
    condition = block.split('${{', 1)[1].split('}}', 1)[0]
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
