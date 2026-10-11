"""Synthetic UI status regressions; no malware scanning is performed or attested."""
import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.malware_scan import MalwareScannerStatus
from case_intelligence.workbench import create_workbench_app


class SyntheticStatusScanner:
    def __init__(self, ready):
        self.ready = ready

    def status(self, *, force=False):
        return MalwareScannerStatus('ready' if self.ready else 'unavailable', 'synthetic-status')

    def scan(self, path):
        raise AssertionError('Rendering intake status must not claim or perform a file scan.')


@pytest.mark.parametrize('mode,scanner_ready,capability,heading', [
    ('disabled', False, 'not required', 'Security scanning is disabled.'),
    ('disabled', True, 'not required', 'Security scanning is disabled.'),
    ('extended', True, 'ready', 'Private security checks are active.'),
    ('all', True, 'ready', 'Private security checks are active.'),
    ('extended', False, 'administrator setup required', 'Security scanning needs setup.'),
    ('all', False, 'administrator setup required', 'Security scanning needs setup.'),
])
def test_intake_status_matches_policy_and_scanner_readiness(tmp_path, mode, scanner_ready, capability, heading):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test', generator=UnavailableGenerator(),
                              malware_scan_mode=mode, malware_scanner=SyntheticStatusScanner(scanner_ready))
    with TestClient(app) as client:
        created = client.post('/matters', data={'name': 'Synthetic scan-status matter'}, follow_redirects=False)
        assert created.status_code == 303
        page = client.get(created.headers['location'])
        assert page.status_code == 200
        assert app.state.workbench.capabilities()['malware_scan'] == capability
        for expected_heading in ('Security scanning is disabled.', 'Private security checks are active.',
                                 'Security scanning needs setup.'):
            assert (expected_heading in page.text) is (expected_heading == heading)
        if mode == 'disabled':
            assert 'Files are not scanned for malware before they enter this matter.' in page.text
            assert 'intake-security-ready' not in page.text
            assert 'are scanned before they enter the matter' not in page.text
        elif scanner_ready:
            assert 'Images, email, and spreadsheets are scanned before they enter the matter.' in page.text
            assert 'class="intake-security-ready" role="status"' in page.text
        else:
            assert 'Files that require a malware scan cannot enter the matter' in page.text
            assert 'intake-security-ready' not in page.text
            # All-source scanning must not promise that PDF/Word/media bypass setup.
            assert 'intake continues normally' not in page.text
        slug = created.headers['location'].split('/')[2]
        selected = client.post(f'/matters/{slug}/upload-preflight', json={'files': [
            {'name': 'synthetic-status.png', 'relative_path': 'synthetic-status.png',
             'size': 24, 'media_type': 'image/png'}]})
        assert selected.status_code == 200
        item = selected.json()['items'][0]
        assert item['scan'] == ({'required': False, 'capability': 'not_required', 'result': 'not_required'}
                                if mode == 'disabled' else
                                {'required': True, 'capability': 'ready' if scanner_ready else 'unavailable',
                                 'result': 'not_run'})
        assert item['eligible'] is (mode == 'disabled' or scanner_ready)
        assert app.state.workbench.malware_scan_mode == mode  # Presentation never changes policy.
