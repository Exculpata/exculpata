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
            assert _load_file_scan_policy_text(mode) in _load_file_status_text(page.text)
            assert 'class="intake-security-ready" role="status"' in page.text
        else:
            assert ('Security scanning needs setup. Files that require a malware scan '
                    'cannot be added until scanning is available.') in _load_file_status_text(page.text)
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
        visible = _load_file_status_text(page.text)
        assert _load_file_scan_policy_text(mode) in visible
        from case_intelligence.source_admission import LOAD_FILE_INTAKE_TEXT
        assert LOAD_FILE_INTAKE_TEXT in visible
        assert _load_file_support_text() in visible
        for suffix in ('dat', 'OPT'):
            name = f'synthetic-status.{suffix}'
            selected = client.post(f'/matters/{slug}/upload-preflight', json={'files': [
                {'name': name, 'relative_path': f'Generated/{name}',
                 'size': 80, 'media_type': 'text/plain'}]})
            assert selected.status_code == 200
            load_file = selected.json()['items'][0]
            assert load_file['expected_type'] == 'text/plain'
            assert load_file['detected_type'] is None
            assert load_file['readability'] == 'pending_upload'
            assert load_file['source_version'] == 'pending_upload'
            assert load_file['scan'] == item['scan']
            assert load_file['eligible'] is (mode == 'disabled' or scanner_ready)
            assert load_file['state'] == ('valid' if mode == 'disabled' or scanner_ready
                                          else 'needs_attention')


def _load_file_status_text(markup):
    from html import unescape
    import re

    return ' '.join(unescape(re.sub(r'<[^>]*>', ' ', markup)).split())


def _load_file_scan_policy_text(mode):
    from case_intelligence.source_admission import LOAD_FILE_SCAN_TYPES

    return {
        'extended': ('The current policy requires scanning for images, email, '
                     f'spreadsheets, and {LOAD_FILE_SCAN_TYPES} load files.'),
        'all': 'The current policy requires scanning for every uploaded file.',
        'disabled': ('Security scanning is disabled. Files are not scanned for '
                     'malware before they enter this matter.'),
    }[mode]


def _load_file_support_text():
    return (
        'DAT support requires at least two nonempty, distinct column headers and '
        'one data record, U+0014 field separators, optional thorn (þ) quoting, and '
        'the same number of fields in every record. OPT support requires no header, '
        'exactly seven comma-separated fields per record, and at least one '
        'nonblank record; quoted commas are not supported. Supported encodings '
        'are UTF-8 with an optional BOM, UTF-16 LE or BE with a BOM, and '
        'Windows-1252 detected from the first 64 KiB; later decoding errors are '
        'rejected. Each record must fit the 1 MiB record limit.'
    )


@pytest.mark.parametrize('mode', ['extended', 'all', 'disabled'])
@pytest.mark.parametrize('scanner_ready', [False, True])
def test_admin_scan_status_names_load_files(tmp_path, mode, scanner_ready):
    from case_intelligence.identity import LocalAccountSettings
    from tests.test_local_authentication import _accounts_file, _challenge

    scanner = SyntheticStatusScanner(scanner_ready)
    app = create_workbench_app(
        tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='local',
        secure_cookie=True, local_settings=LocalAccountSettings(_accounts_file(tmp_path)),
        malware_scan_mode=mode, malware_scanner=scanner,
    )
    with TestClient(app, base_url='https://testserver') as client:
        challenge = _challenge(client.get('/auth/login').text)
        login = client.post('/auth/local', data={
            'username': 'alice.reviewer', 'password': 'synthetic-local-password',
            'login_challenge': challenge, 'next': '/admin',
        }, follow_redirects=False)
        assert login.status_code == 303
        page = client.get('/admin')
        assert page.status_code == 200
        visible = _load_file_status_text(page.text)
        assert _load_file_scan_policy_text(mode) in visible
        if mode != 'disabled' and not scanner_ready:
            assert ('Security scanning needs setup. Files that require a malware scan '
                    'cannot be added until scanning is available.') in visible
        if mode == 'disabled':
            assert 'Private security checks are active.' not in visible
            assert 'Security scanning needs setup.' not in visible
        if mode == 'all':
            assert 'intake continues normally' not in visible
        assert app.state.workbench.malware_scan_mode == mode
