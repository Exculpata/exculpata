import re
from pathlib import Path
import pytest
import yaml
ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize(('name', 'cron'), [('hygiene-report', '23 8 * * *'), ('flake-ledger', '37 3 * * 1')])
def test_report_only_workflow(name, cron):
    text = (ROOT / '.github/workflows' / f'{name}.yml').read_text()
    flow = yaml.safe_load(text)
    triggers = flow.get('on', flow.get(True))
    assert set(triggers) == {'schedule', 'workflow_dispatch'}
    assert triggers['schedule'] == [{'cron': cron}]
    assert flow['permissions'] == {}
    measure, filing = flow['jobs']['measure'], flow['jobs']['filing']
    assert measure['permissions'] == ({'contents': 'read', 'actions': 'read'} if name == 'flake-ledger' else {'contents': 'read'})
    assert filing['permissions'] == {'issues': 'write'} and filing['needs'] == 'measure'
    assert 'cancel-in-progress: false' in text
    for job in flow['jobs'].values():
        for step in job['steps']:
            if 'uses' in step:
                assert re.fullmatch(r'[\w/-]+@[0-9a-f]{40}', step['uses'])
                if step['uses'].startswith('actions/checkout@'):
                    assert step['with']['persist-credentials'] is False
                if step['uses'].startswith('actions/upload-artifact@'):
                    assert step['with']['retention-days'] == 14
                    assert f'{name}-report.json' in step['with']['path']
    assert '--require-hashes -r requirements/hygiene.lock' in text and '--only-binary=:all:' in text
    assert 'PUBLICATION_DENY_TERMS: ${{ secrets.PUBLICATION_DENY_TERMS }}' in text
    filing_text = '\n'.join(step.get('run', '') for step in filing['steps'])
    assert all(f'gh issue {verb}' in filing_text for verb in ('list', 'create', 'edit'))
    assert not any(f'gh issue {verb}' in filing_text for verb in ('close', 'reopen', 'comment'))
    assert all(flag in filing_text for flag in ('--report', '--issues', '--scanner', '--deny-file', '--body', '--decision', '--summary'))
    assert 'umask 077' in filing_text and 'trap ' in filing_text
