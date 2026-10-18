"""Minimum Engine and live DNS proof are independent, synthetic acceptance gates."""
import io
import json
import subprocess

import pytest
import yaml

from case_intelligence.legacy_names import CURRENT_PRODUCT
from tests.test_oss_installer import ROOT, installer, ready_host, preflight_args, checks_by_name
from tests.test_first_run_handoff import configured_node

ENGINE_CHECK = installer._dns_engine_known_fix


@pytest.mark.parametrize('version,allowed', [
    ('23.0.11', False), ('25.0.5', False), ('25.99.99', False),
    ('26.0.0', True), ('28.0.4', True), ('29.9.1+package1', True),
    ('26.0.0-rc1', False), ('99.0.0-dev', False), ('unknown', False), ('26', False),
])
def test_preflight_engine_floor_is_blocking_before_writes(tmp_path, ready_host, monkeypatch, version, allowed):
    monkeypatch.setattr(installer, '_dns_engine_known_fix', ENGINE_CHECK)
    original = installer._probe
    monkeypatch.setattr(installer, '_probe', lambda command:
        subprocess.CompletedProcess(command, 0, version, '') if '{{.ServerVersion}}' in command else original(command))
    args = preflight_args(tmp_path)
    result = installer._collect_preflight('none', args)
    check = checks_by_name(result)['docker-dns-version']
    assert check.blocking
    assert (check.state == 'pass') is allowed
    assert result.ready is allowed
    assert not args.root.exists()


@pytest.mark.parametrize('version', ['23.0.11', '25.0.5', '26.0.0', '99.0.0', 'unknown'])
@pytest.mark.parametrize('behavior', [False, True])
def test_live_acceptance_requires_both_engine_and_observed_dns(tmp_path, monkeypatch, version, behavior):
    calls = []
    monkeypatch.setattr(installer, '_installed_release', lambda root: ({'profiles': []}, ROOT))
    monkeypatch.setattr(installer, '_compose', lambda *a: ['docker','compose'])
    monkeypatch.setattr(installer, '_compose_environment', lambda *a: {})
    def inspect(command):
        if 'info' in command: value = version
        elif 'ps' in command: value = json.dumps([{'Service': n, 'ID': n} for n in ('app','postgres','clamav')])
        elif 'network' in command: value = 'true'
        else: value = json.dumps({'dns':['127.0.0.1'], 'search':['.'], 'networks':{'synthetic':{}}})
        return subprocess.CompletedProcess(command, 0, value, '')
    monkeypatch.setattr(installer, '_probe', inspect)
    def proof(*a, **kw):
        calls.append('live')
        return subprocess.CompletedProcess([], 0, json.dumps({'internal_dns_udp_tcp':True,
            'container_loopback_challenges':6 if behavior else 0, 'dns_behavior_verified':behavior}), '')
    monkeypatch.setattr(installer.subprocess, 'run', proof)
    supported = version in ('26.0.0', '99.0.0')
    if supported and behavior:
        installer._verify_installed_dns(tmp_path)
    else:
        with pytest.raises(RuntimeError):
            installer._verify_installed_dns(tmp_path)
    assert calls == (['live'] if supported else [])


@pytest.mark.parametrize('rollback', [False, True])
@pytest.mark.parametrize('contract', ['legacy', 'marked', 'unmarked-egress'])
def test_only_legacy_rollback_skips_live_proof_but_never_engine_floor(tmp_path, monkeypatch, rollback, contract):
    root, _, _ = configured_node(tmp_path)
    release = tmp_path / 'synthetic-release'
    release.mkdir()
    (release/'compose.yaml').write_text('services: {}\n' if contract != 'unmarked-egress' else 'services:\n  egress-proxy: {}\n')
    if contract == 'marked':
        (release/'RELEASE_MANIFEST.json').write_text(json.dumps({'network_contract_version':1}))
    record = json.loads((root/'installation.json').read_text()); record['release_path'] = str(release)
    (root/'installation.json').write_text(json.dumps(record))
    monkeypatch.setattr(installer, '_running_auth_mode', lambda *a:'local')
    monkeypatch.setattr(installer, '_login_reachable', lambda *a:True)
    payload={'product':CURRENT_PRODUCT,'status':'ok','storage':{'status':'ready'},
             'capabilities':{'source_review':'ready','malware_scan':'ready'}}
    monkeypatch.setattr(installer.urllib.request, 'urlopen', lambda *a,**kw:io.BytesIO(json.dumps(payload).encode()))
    def reject(root): raise RuntimeError('synthetic DNS proof rejected')
    monkeypatch.setattr(installer, '_verify_installed_dns', reject)
    monkeypatch.setattr(installer, '_dns_engine_known_fix', lambda:True)
    if rollback and contract == 'legacy':
        installer._wait_health(installer.Console(color=False, quiet=True),root,rollback=rollback)
        monkeypatch.setattr(installer, '_dns_engine_known_fix', lambda:False)
        with pytest.raises(RuntimeError, match='Docker Engine 26'):
            installer._wait_health(installer.Console(color=False, quiet=True),root,rollback=rollback)
    else:
        with pytest.raises(RuntimeError, match='synthetic DNS proof rejected'):
            installer._wait_health(installer.Console(color=False, quiet=True),root,rollback=rollback)


def test_relay_build_context_allows_only_reviewed_public_inputs():
    service=yaml.safe_load((ROOT/'compose.yaml').read_text())['services']['egress-proxy']
    assert service['build']=={'context':'.','dockerfile':'deploy/egress-proxy/Dockerfile'}
    path=ROOT/(service['build']['dockerfile']+'.dockerignore')
    rules=[line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith('#')]
    assert rules[0]=='**'
    allowed={line[1:] for line in rules[1:]}
    assert all(line.startswith('!') and '*' not in line for line in rules[1:])
    assert allowed=={'deploy/egress-proxy/Dockerfile','deploy/egress-proxy/proxy.py',
        'src/case_intelligence/__init__.py','src/case_intelligence/oidc_network_policy.py',
        *('src/case_intelligence/_idna/'+name for name in ('__init__.py','idna-3.20.pyz','manifest.json','LICENSE.md','README.md'))}
    assert all((ROOT/name).is_file() for name in allowed)


@pytest.mark.parametrize('mode', ['resume', 'update'])
def test_unsupported_engine_stops_saved_lifecycle_before_mutation(tmp_path, request, monkeypatch, mode):
    from tests.test_oss_installer import synthetic_saved_provider
    root, args = synthetic_saved_provider(tmp_path, request, monkeypatch, 'oidc')
    args.command = 'install' if mode == 'resume' else 'update'
    before = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setattr(installer, '_dns_engine_known_fix', lambda:False)
    monkeypatch.setattr(installer, '_verify_installed_dns', lambda root:None)
    monkeypatch.setattr(installer, '_run', lambda *a,**k:pytest.fail('unsupported engine reached runtime mutation'))
    monkeypatch.setattr(installer, '_stage_release', lambda *a,**k:pytest.fail('unsupported engine staged a release'))
    operation = installer._resume_node if mode == 'resume' else installer._update
    with pytest.raises(RuntimeError, match='prerequisites'):
        operation(installer.Console(color=False, quiet=True),args,root)
    assert {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_unsupported_engine_stops_fresh_install_before_mutation(tmp_path, ready_host, monkeypatch):
    import sys
    root=tmp_path/'fresh-node'
    monkeypatch.setattr(installer, '_dns_engine_known_fix', lambda:False)
    monkeypatch.setattr(installer, '_verify_installed_dns', lambda root:None)
    monkeypatch.setattr(installer, '_prepare_directories', lambda *a,**k:pytest.fail('unsupported engine prepared state'))
    monkeypatch.setattr(installer, '_stage_release', lambda *a,**k:pytest.fail('unsupported engine staged a release'))
    secret=tmp_path/'synthetic-input'
    secret.write_text('synthetic-oidc-secret-value');secret.chmod(0o600)
    monkeypatch.setattr(sys,'argv',['install','--root',str(root),'--models','none','--auth','oidc',
        '--server-name','app.example.test','--oidc-issuer','https://issuer.example.test/realm',
        '--oidc-client-id','synthetic-client','--oidc-client-secret-file',str(secret),'--non-interactive','--plain'])
    assert installer.main()==1
    assert not root.exists()
