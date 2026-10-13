"""Synthetic integration fixtures using real preservation archives and sessions."""
from datetime import datetime, timedelta
import re
import tempfile
from pathlib import Path

from case_intelligence.matter_archive_verifier import verify_matter_archive


def prepare_kerberos_exit(client, matter, principal, headers):
    """Exercise begin/export/download/confirm over HTTP and sign in after time advances."""
    bench = client.app.state.workbench
    page = client.get(f'/matters/{matter.slug}/close', headers=headers(principal))
    assert page.status_code == 200
    csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', page.text).group(1)
    current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    if current is None:
        begun = client.post(f'/matters/{matter.slug}/close/begin', data={'csrf_token': csrf},
                           headers=headers(principal), follow_redirects=False)
        assert begun.status_code == 303 and 'error=' not in begun.headers['location'], begun.headers.get('location')
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    if current.export_id is None:
        exported = client.post(f'/matters/{matter.slug}/close/export', data={'csrf_token': csrf},
                              headers=headers(principal), follow_redirects=False)
        assert exported.status_code == 303 and 'error=' not in exported.headers['location'], exported.headers.get('location')
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        download = client.get(f'/matters/{matter.slug}/close/export', headers=headers(principal))
        assert download.status_code == 200
        with tempfile.TemporaryDirectory(prefix='synthetic-downloaded-archive-') as temp:
            path = Path(temp) / 'download.zip'
            path.write_bytes(download.content)
            verify_matter_archive(path, expected_sha256=current.archive_digest)
    now = max(bench.workspace.current_time(), datetime.fromisoformat(current.eligible_after.replace('Z', '+00:00')))
    bench.workspace._clock = lambda: now
    client.app.state.identity._clock = lambda: now
    client.cookies.clear()
    assert client.get('/auth/login', headers=headers(principal), follow_redirects=False).status_code == 303
    page = client.get(f'/matters/{matter.slug}/close', headers=headers(principal))
    csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', page.text).group(1)
    confirmed = client.post(f'/matters/{matter.slug}/close/confirm',
        data={'csrf_token': csrf, 'export_id': current.export_id}, headers=headers(principal), follow_redirects=False)
    assert confirmed.status_code == 200, confirmed.headers.get('location')
    token = re.search(r'name="token" value="([^"]+)"', confirmed.text).group(1)
    return dict(csrf_token=csrf, token=token, export_id=current.export_id, export_acknowledged='yes',
                acknowledge='yes', confirmed_name=matter.display_name)
