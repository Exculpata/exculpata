from datetime import timedelta

import pytest

from case_intelligence.identity import SESSION_COOKIE
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_case_lifecycle_web import case, confirm, login, post, prepare  # noqa: F401


def test_purge_requires_the_export_to_have_actually_been_downloaded(case):
    client, bench, matter, source, document, clock, csrf = case
    assert post(client, matter, 'close/begin', csrf).status_code == 303
    current = prepare(client, bench, matter, csrf)
    # Deliberately never GET /close/export.
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    denied = post(client, matter, 'close/confirm', csrf, export_id=current.export_id)
    assert denied.status_code == 303 and 'Download+the+complete+current+archive' in denied.headers['location']
    assert bench.workspace.connection.execute('SELECT COUNT(*) FROM workbench_case_confirmation').fetchone()[0] == 0
    forged = post(client, matter, 'close', csrf, token='synthetic-forged-token-' + 'a' * 32,
                  export_id=current.export_id, confirmed_name=matter.display_name,
                  acknowledge='yes', export_acknowledged='yes')
    assert forged.status_code == 303 and 'error=' in forged.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    assert (source.files / document.stored_name).read_bytes() == b'Generated original evidence.\n'


@pytest.mark.parametrize('mode', ['export_exit', 'delete_without_export'])
@pytest.mark.parametrize('entrypoint', ['http', 'service'])
def test_revocation_after_purge_claim_preserves_sources_and_allows_fresh_retry(case, monkeypatch, mode, entrypoint):
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf, mode=mode)
    current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    if mode == 'export_exit':
        current = prepare(client, bench, matter, csrf)
        assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    token = confirm(client, matter, csrf, current.export_id, mode=mode)
    session = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True).session
    common = dict(token=token, export_id=current.export_id or '', mode=mode)
    begin = bench.begin_matter_purge
    claimed = []

    def revoke_after_claim(*args, **kwargs):
        result = begin(*args, **kwargs)
        claimed.append(result[1].purge_id)
        bench.workspace.revoke_session(session.session_id)
        return result

    if entrypoint == 'http':
        monkeypatch.setattr(bench, 'begin_matter_purge', revoke_after_claim)
        response = post(client, matter, 'close', csrf, **common, confirmed_name=matter.display_name,
            acknowledge='yes', export_acknowledged='yes', no_backup_acknowledged='yes')
        assert response.status_code == 404
        monkeypatch.setattr(bench, 'begin_matter_purge', begin)
    else:
        claimed_matter, lifecycle = revoke_after_claim(matter.slug, matter.owner_id, matter.display_name,
            **common, session_id=session.session_id, export_acknowledged=True, no_backup_acknowledged=True)
        with pytest.raises(WorkspaceProblem, match='Sign in again'):
            bench.execute_matter_purge(claimed_matter, lifecycle)

    failed = bench.workspace.matter_lifecycle(matter.matter_id)
    assert failed.state == 'purge_failed' and failed.error_code == 'authority'
    assert claimed == [failed.purge_id]
    original = source.files / document.stored_name
    assert original.read_bytes() == b'Generated original evidence.\n'
    assert not (bench.storage.purging / failed.purge_id).exists()
    assert bench.workspace.case_lifecycle.current_exit(matter.matter_id).state == 'confirmed'

    csrf = login(client)
    fresh_session = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True).session
    assert fresh_session.session_id != session.session_id
    fresh_token = confirm(client, matter, csrf, current.export_id, mode=mode)
    assert fresh_token != token
    retried = post(client, matter, 'close', csrf, **{**common, 'token': fresh_token},
        confirmed_name=matter.display_name, acknowledge='yes',
        export_acknowledged='yes', no_backup_acknowledged='yes')
    assert retried.status_code == 303 and retried.headers['location'].startswith('/matters/new?')
    completed = bench.workspace.matter_lifecycle(matter.matter_id)
    assert completed.state == 'deleted' and completed.purge_id == failed.purge_id
    assert not original.exists()
    authority = bench.workspace.connection.execute(
        'SELECT session_id FROM workbench_case_purge_authority WHERE purge_id=?', (failed.purge_id,)).fetchone()
    assert authority['session_id'] == fresh_session.session_id
