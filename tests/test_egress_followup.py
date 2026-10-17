"""Independent follow-up regressions: no external requests or private fixtures."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sys

import httpcore
import httpx
import pytest
import yaml

from case_intelligence.identity import AuthlibOidcClient, OidcAuthenticationError, OidcSettings

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('followup_proxy', ROOT / 'deploy/egress-proxy/proxy.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)
sys.path.insert(0, str(ROOT / 'scripts'))
import dns_boundary_probe as dns
sys.path.pop(0)


@pytest.mark.parametrize('host', ['010.0.0.1', '010.1.1.1', '0x8.8.8.8', '127.1', '2130706433', '0177.0.0.1', '0x7f000001'])
@pytest.mark.parametrize('target', ['relay', 'identity'])
def test_issuer_rejects_noncanonical_numeric_hosts_before_resolution(host, target):
    if target == 'relay':
        with pytest.raises(ValueError):
            relay.issuer_authority('https://' + host + '/realm')
        return
    with pytest.raises(RuntimeError):
        OidcSettings(issuer='https://' + host, external_origin='https://app.example.test',
                     client_id='synthetic', client_secret='synthetic-secret')


@pytest.mark.skipif(sys.platform != 'linux', reason='reproduces glibc numeric-host interpretation on supported Linux nodes')
@pytest.mark.parametrize('alias,canonical', [('127.1','127.0.0.1'), ('010.0.0.1','8.0.0.1'), ('010.1.1.1','8.1.1.1'), ('0x8.8.8.8','8.8.8.8')])
def test_native_resolver_demonstrates_numeric_alias_without_connecting(alias, canonical):
    answers = socket.getaddrinfo(alias, 443, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    assert all(row[4][0] == canonical for row in answers)


def test_forwarded_nxdomain_is_not_dns_boundary_proof(monkeypatch):
    class NoLocalTraffic:
        def __init__(self, names): self.seen = set()
        def close(self): pass
    monkeypatch.setattr(dns, 'LoopbackChallenge', NoLocalTraffic, raising=False)
    monkeypatch.setattr(dns, 'query', lambda name, *a, **kw: (0, 1) if name == 'postgres' else (3, 0))
    with pytest.raises((ValueError, OSError)):
        dns.verify(['postgres'])


@pytest.mark.parametrize('url', ['http://example.test', 'http://x.postgres', 'https://evil.generator', 'https://x.retrieval'])
def test_production_environment_routes_http_and_suffix_hosts_to_relay(monkeypatch, url):
    env = yaml.safe_load((ROOT / 'compose.yaml').read_text())['services']['app']['environment']
    for key in ('HTTPS_PROXY','https_proxy','HTTP_PROXY','http_proxy','ALL_PROXY','all_proxy','NO_PROXY','no_proxy'):
        monkeypatch.setenv(key, env[key])
    with httpx.Client() as client:
        assert isinstance(client._transport_for_url(httpx.URL(url))._pool, httpcore.HTTPProxy)


@pytest.mark.parametrize('stage', ['discovery','token','jwks'])
def test_real_authlib_rejects_redirects_even_with_transport_metadata(stage):
    from joserfc import jwt
    from joserfc.jwk import RSAKey
    import time
    key = RSAKey.generate_key(2048, parameters={'kid': 'synthetic'})
    settings = OidcSettings(issuer='https://issuer.example.test', external_origin='https://app.example.test',
                            client_id='synthetic', client_secret='synthetic-secret')
    events=[]
    def handler(request):
        path=request.url.path; events.append(path)
        if path == '/redirect-target':
            return httpx.Response(400)
        selected = {'discovery':'/.well-known/openid-configuration','token':'/token','jwks':'/jwks'}[stage]
        if path == selected:
            return httpx.Response(302, headers={'location':'https://issuer.example.test/redirect-target'})
        if path == '/.well-known/openid-configuration':
            return httpx.Response(200,json={'issuer':settings.issuer,'authorization_endpoint':settings.issuer+'/authorize',
                'token_endpoint':settings.issuer+'/token','jwks_uri':settings.issuer+'/jwks',
                'code_challenge_methods_supported':['S256'],'id_token_signing_alg_values_supported':['RS256'],
                'follow_redirects':True,'trust_env':True,'verify':False})
        if path == '/token':
            now=int(time.time());token=jwt.encode({'alg':'RS256','kid':'synthetic'},
                {'iss':settings.issuer,'aud':'synthetic','sub':'synthetic','iat':now,'exp':now+300,'nonce':'synthetic-nonce'},key)
            return httpx.Response(200,json={'access_token':'synthetic','token_type':'Bearer','id_token':token})
        return httpx.Response(200,json={'keys':[key.as_dict(private=False)]})
    async def exercise():
        client=AuthlibOidcClient(settings)
        client.remote.client_kwargs['transport']=httpx.MockTransport(handler)
        with pytest.raises(OidcAuthenticationError):
            await client.authenticate(code='synthetic',nonce='synthetic-nonce',code_verifier='v'*64)
        assert {'discovery':'/.well-known/openid-configuration','token':'/token','jwks':'/jwks'}[stage] in events
        assert '/redirect-target' not in events
    asyncio.run(exercise())


def test_discovery_metadata_cannot_replace_transport_policy():
    settings=OidcSettings(issuer='https://issuer.example.test',external_origin='https://app.example.test',
                          client_id='synthetic',client_secret='synthetic-secret')
    metadata={'issuer':settings.issuer,'authorization_endpoint':settings.issuer+'/authorize',
        'token_endpoint':settings.issuer+'/token','jwks_uri':settings.issuer+'/jwks',
        'code_challenge_methods_supported':['S256'],'id_token_signing_alg_values_supported':['RS256'],
        'follow_redirects':True,'trust_env':True,'verify':False,'proxy':'http://foreign.example.test:3128',
        'timeout':0,'headers':{'Authorization':'synthetic-override'},'client_secret':'synthetic-override'}
    async def exercise():
        client=AuthlibOidcClient(settings)
        client.remote.client_kwargs['transport']=httpx.MockTransport(lambda req:httpx.Response(200,json=metadata))
        await client._validated_metadata()
        assert not {'follow_redirects','trust_env','verify','proxy','timeout','headers','client_secret'} & client.remote.server_metadata.keys()
    asyncio.run(exercise())


def test_live_smoke_uses_production_compose_and_keeps_routing_environment():
    source=(ROOT/'scripts/test-egress-smoke.py').read_text()
    assert 'ROOT / "compose.yaml"' in source
    assert '"NO_PROXY": ""' not in source  # harness must inherit the production policy


@pytest.mark.parametrize('version,behavior', [('25.0.5', True), ('23.0.11', True), ('99.0.0', False)])
def test_doctor_uses_observed_behavior_not_version(monkeypatch, tmp_path, version, behavior):
    sys.path.insert(0,str(ROOT/'scripts'))
    import exculpata_install as installer
    sys.path.pop(0)
    import subprocess
    monkeypatch.setattr(installer,'_installed_release',lambda root:({'profiles':[]}, ROOT))
    monkeypatch.setattr(installer,'_compose',lambda *a:['docker','compose'])
    monkeypatch.setattr(installer,'_compose_environment',lambda *a:{})
    def inspect(command):
        if 'info' in command: value=version
        elif 'ps' in command: value=json.dumps([{'Service':n,'ID':n} for n in ('app','postgres','clamav')])
        elif 'network' in command: value='true'
        else: value=json.dumps({'dns':['127.0.0.1'],'search':['.'],'networks':{'synthetic':{}}})
        return subprocess.CompletedProcess(command,0,value,'')
    monkeypatch.setattr(installer,'_probe',inspect)
    monkeypatch.setattr(installer.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess([],0,
        json.dumps({'internal_dns_udp_tcp':True,'container_loopback_challenges':6 if behavior else 0,'dns_behavior_verified':behavior}),''))
    if behavior: installer._verify_installed_dns(tmp_path)
    else:
        with pytest.raises(RuntimeError,match='DNS isolation is unverified'):
            installer._verify_installed_dns(tmp_path)


def test_internal_transport_uses_exact_hosts_without_proxy_or_redirect(monkeypatch):
    import urllib.request
    from case_intelligence import service_endpoints as service
    monkeypatch.setenv('SYNTHETIC_SERVICE_HOSTS', 'retrieval')
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return 'synthetic response'
    def build(*handlers):
        assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        redirect = next(h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
        assert redirect.redirect_request(None,None,302,'',{},'http://foreign.example.test') is None
        return Opener()
    monkeypatch.setattr(service.urllib.request, 'build_opener', build)
    assert service.open_service_request('http://retrieval:8080/',timeout=3,environment_name='SYNTHETIC_SERVICE_HOSTS') == 'synthetic response'
    for host in ('x.retrieval','foreign.example.test'):
        with pytest.raises(ValueError):
            service.open_service_request('http://'+host,timeout=3,environment_name='SYNTHETIC_SERVICE_HOSTS')
    assert len(calls) == 1


def test_authlib_uses_explicit_proxy_and_ignores_environment(monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_OIDC_PROXY','http://egress-proxy:3128')
    monkeypatch.setenv('HTTPS_PROXY','http://foreign.example.test:3128')
    monkeypatch.setenv('NO_PROXY','*')
    client=AuthlibOidcClient(OidcSettings(issuer='https://issuer.example.test', external_origin='https://app.example.test',
                                       client_id='synthetic',client_secret='synthetic-secret'))
    options=client.remote.client_kwargs
    assert options['trust_env'] is False and options['proxy'] == 'http://egress-proxy:3128'
    assert options['follow_redirects'] is False and options['verify'] is True


def test_smoke_failure_diagnostics_are_bounded_and_remove_sensitive_values():
    spec=importlib.util.spec_from_file_location('smoke_diagnostic',ROOT/'scripts/test-egress-smoke.py')
    smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
    raw=('permission denied: /tmp/synthetic-fixture/file at https://issuer.example.test/path 192.0.2.9\n'
         'Authorization: Bearer synthetic-private-value\n')
    diagnostic=smoke.failure_diagnostic(raw)
    assert all(marker in diagnostic for marker in ('<path>', '<url>', '<address>', '[sensitive diagnostic line omitted]'))
    assert len(smoke.failure_diagnostic('x'*6000))<=3000
    assert 'synthetic-private-value' not in diagnostic
    assert '/tmp/synthetic-fixture' not in diagnostic and 'https://' not in diagnostic and '192.0.2.9' not in diagnostic
    assert 'permission denied' in smoke.failure_diagnostic('permission denied')
