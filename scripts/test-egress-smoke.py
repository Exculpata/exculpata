#!/usr/bin/env python3
"""Opt-in production-Compose boundary smoke in an explicitly selected engine.

App/proxy build targets, networks, DNS and routing environment come from the real
Compose file. Fixture-only changes remove data/dependencies, replace app startup
with a probe process, and add bounded synthetic endpoints. No host ports/mounts
or host/daemon DNS changes. The caller owns the disposable runtime.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]


def failure_diagnostic(value):
    """Bounded fixture diagnostics; never print auth values, URLs or local paths."""
    lines = []
    for line in value.splitlines()[-20:]:
        if re.search(r"(?i)password|secret|token|authorization|bearer|private.key", line):
            lines.append("[sensitive diagnostic line omitted]")
            continue
        line = re.sub(r"https?://[^\s]+", "<url>", line)
        line = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s'\"]+)", "<path>", line)
        line = re.sub(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", "<address>", line)
        lines.append(line)
    return "\n".join(lines)[-3000:]


def production_graph(project, proxy_context):
    source = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = {name: copy.deepcopy(source['services'][name]) for name in ('app', 'egress-proxy')}
    networks = {name for service in services.values() for name in service['networks']}
    graph = {'services': services, 'networks': {name: copy.deepcopy(source['networks'][name]) for name in networks}}
    for name, service in services.items():
        service['image'] = project + '-' + name
        service['restart'] = 'no'
        service['pull_policy'] = 'never'  # Unique images were built in this runtime.
        service['build']['context'] = str(ROOT if name == 'app' else proxy_context)
    app = services['app']
    for key in ('volumes', 'env_file', 'depends_on'):
        app.pop(key, None)
    app.update(command=['python','-c','import time; time.sleep(1800)'],
               healthcheck={'test': ['CMD', 'python', '-c', 'import case_intelligence.identity'],
                            'interval': '2s', 'timeout': '5s', 'retries': 30},
               cpus=1, mem_limit='512m')
    # Nothing overrides production proxy environment, network membership or DNS.
    for key in ('environment', 'networks', 'dns', 'dns_search', 'user', 'read_only', 'cap_drop', 'security_opt'):
        assert app[key] == source['services']['app'][key]
    assert all(source['networks'][name]['internal'] for name in app['networks'])
    return graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker-context', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    project = 'issue319-smoke-' + uuid.uuid4().hex[:10]
    docker = ['docker','--context',args.docker_context]
    results = {'project':project,'passed':False}
    with tempfile.TemporaryDirectory(prefix='egress-smoke-') as temporary:
        root = Path(temporary)
        fixture = root/'fixture'
        shutil.copytree(ROOT/'tests/network_smoke', fixture)
        shutil.copy(ROOT/'scripts/dns_boundary_probe.py',fixture/'dns_boundary_probe.py')
        key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'issuer.example.test')])
        now = datetime.now(timezone.utc)
        certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('issuer.example.test')]),critical=False)
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
        (fixture/'issuer.pem').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        private=fixture/'issuer-key.pem'
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        private.chmod(0o600)
        proxy_context=root/'proxy'
        shutil.copytree(ROOT/'deploy/egress-proxy',proxy_context/'deploy/egress-proxy')
        policy = proxy_context/'src/case_intelligence'
        policy.mkdir(parents=True)
        for name in ('__init__.py', 'oidc_network_policy.py'):
            shutil.copy(ROOT/'src/case_intelligence'/name, policy/name)
        shutil.copytree(ROOT/'src/case_intelligence/_idna', policy/'_idna',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for path in proxy_context.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        data=production_graph(project,proxy_context)
        original_dns=copy.deepcopy(data['services']['app']['dns'])
        limits={'cpus':.5,'mem_limit':'128m','pids_limit':64,'read_only':True,
                'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'user':'65532:65532',
                'image':project+'-endpoint','pull_policy':'never',
                'healthcheck':{'test':['CMD','python','-c',
                    "import json,urllib.request; r=urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8080/',timeout=2); assert json.load(r)=={'synthetic':True}"],
                    'interval':'2s','timeout':'5s','retries':30}}
        data['services'].update({
            'issuer':{**limits,'networks':{'front':{'aliases':['issuer.example.test']}}},
            'foreign':{**limits,'networks':{'front':{'aliases':['foreign.example.test']}}},
            'canary':{**limits,'networks':{
                'app-egress':{'aliases':['x.postgres','evil.generator','x.retrieval']},
                'services':{'aliases':['postgres','clamav','transcription-api']},
                'models':{'aliases':['retrieval','generator']}}}})
        (fixture/'Dockerfile').write_text('FROM '+project+'-app\nCOPY --chown=65532:65532 endpoint.py dns_trap.py dns_boundary_probe.py issuer.pem issuer-key.pem /fixture/\nUSER 65532:65532\nHEALTHCHECK NONE\nCMD ["python","/fixture/endpoint.py"]\n')
        compose_file=root/'compose.json'
        def save(): compose_file.write_text(json.dumps(data))
        save()
        compose=[*docker,'compose','-p',project,'-f',str(compose_file)]
        def run(argv, *, timeout=600, filename=None, input_value=None, expected=0):
            completed=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,input=input_value)
            if filename:
                (args.output/filename).write_text(completed.stdout+completed.stderr)
            if completed.returncode != expected:
                results['failed_step'] = filename or 'runtime diagnostics'
                results['failed_exit'] = completed.returncode
                print('Smoke failure: ' + results['failed_step'] + '; exit ' + str(completed.returncode), file=sys.stderr)
                print(failure_diagnostic(completed.stdout + completed.stderr), file=sys.stderr)
                if filename and filename.endswith('up.log'):
                    diagnostic = subprocess.run([*compose,'logs','--no-color','--tail','12'],
                        capture_output=True,text=True,timeout=20)
                    (args.output/(filename+'.runtime.log')).write_text(diagnostic.stdout+diagnostic.stderr)
                    print(failure_diagnostic(diagnostic.stdout+diagnostic.stderr),file=sys.stderr)
                raise RuntimeError('smoke command failed; see '+(filename or 'runtime diagnostics'))
            return completed.stdout
        def stream():
            for name in ('issuer.pem','probe.py','dns_probe.py','dns_trap.py','dns_boundary_probe.py'):
                run([*compose,'exec','-T','app','python','-c',
                    "import os,sys; from pathlib import Path; p=Path('/tmp/issue319-fixture'); p.mkdir(exist_ok=True,mode=0o700); fd=os.open(p/sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400); f=os.fdopen(fd,'w'); f.write(sys.stdin.read()); f.close()",name],input_value=(fixture/name).read_text())
        def address(name, network):
            identifier=run([*compose,'ps','-q',name]).strip()
            return run([*docker,'inspect','--format','{{(index .NetworkSettings.Networks "'+project+'_'+network+'").IPAddress}}',identifier]).strip()
        try:
            results['engine_version'] = run([*docker,'version','--format','{{.Server.Version}}']).strip()[:80]
            results['compose_version'] = run([*docker,'compose','version','--short']).strip()[:80]
            graph=json.loads(run([*compose,'config','--format','json']))
            for service in graph['services'].values():
                assert not any(service.get(field) for field in ('ports','volumes','secrets','devices'))
            run([*compose,'build','app','egress-proxy'],timeout=1200,filename='build.log')
            run([*docker,'build','-t',project+'-endpoint',str(fixture)],filename='fixture-build.log')
            run([*compose,'up','-d','--no-deps','--wait','--wait-timeout','90'],filename='up.log')
            issuer_address,foreign_address=address('issuer','front'),address('foreign','front')
            recorder_address=address('canary','app-egress')
            issuer='https://issuer.example.test:8443/realm'
            data['services']['egress-proxy']['environment']={'OIDC_ISSUER':issuer,
                'OIDC_ISSUER_PINS':json.dumps({'issuer':issuer,'addresses':[issuer_address]})}
            save()
            run([*compose,'up','-d','--no-deps','--wait','--wait-timeout','90','egress-proxy'],filename='allow-proxy.log')
            ids=run([*compose,'ps','-q']).split()
            for identifier in ids:
                fields=json.loads(run([*docker,'inspect','--format',
                    '{"mounts":{{json .Mounts}},"ports":{{json .HostConfig.PortBindings}},"privileged":{{json .HostConfig.Privileged}},"readonly":{{json .HostConfig.ReadonlyRootfs}}}',identifier]))
                assert not fields['mounts'] and not fields['ports'] and not fields['privileged'] and fields['readonly']
            results.update(json.loads(run([*compose,'exec','-T','app','python','-I','-S','-B','-'],
                input_value=(ROOT/'scripts/installed_package_probe.py').read_text(),filename='installed-package.log')))
            package_control = r"""import json,sys,tempfile
from pathlib import Path
namespace={'__name__':'synthetic_package_probe'}
exec(sys.stdin.read(),namespace)
with tempfile.TemporaryDirectory(prefix='synthetic-package-') as temporary:
    package=Path(temporary)/'case_intelligence';package.mkdir()
    (package/'__init__.py').write_text('# synthetic fixture\n')
    try:
        namespace['verify_package'](package)
    except namespace['BoundaryError'] as error:
        assert str(error)=='package-writable'
    else:
        raise RuntimeError('writable package control was accepted')
print(json.dumps({'app_writable_package_control_rejected':True}))
"""
            results.update(json.loads(run([*compose,'exec','-T','app','python','-I','-S','-B','-c',package_control],
                input_value=(ROOT/'scripts/installed_package_probe.py').read_text(),filename='installed-package-control.log')))
            stream()
            command=[*compose,'exec','-T','-e','ISSUER_ADDRESS='+issuer_address,'-e','FOREIGN_ADDRESS='+foreign_address,
                     '-e','DNS_RECORDER_ADDRESS='+recorder_address,'app','python']
            run([*compose,'exec','-T','app','python','-c',
                 "import httpx,ssl,time; c=ssl.create_default_context(cafile='/tmp/issue319-fixture/issuer.pem'); exec(\"for attempt in range(30):\\n try:\\n  httpx.get('https://issuer.example.test:8443/',verify=c,timeout=3).raise_for_status(); break\\n except httpx.HTTPError:\\n  time.sleep(.2)\\nelse: raise RuntimeError('issuer unavailable')\")"],filename='readiness.log')
            # A live foreign TLS port is required before accepting app denials.
            relay_id = run([*compose,'ps','-q','egress-proxy']).strip()
            front_gateway = run([*docker,'inspect','--format','{{(index .NetworkSettings.Networks "'+project+'_front").Gateway}}',relay_id]).strip()
            results.update(json.loads(run([*compose,'exec','-T','egress-proxy','python','-I','-',foreign_address,front_gateway],
                input_value=(ROOT/'tests/network_smoke/front_control.py').read_text(),filename='foreign-tls-control.log')))
            results.update(json.loads(run([*command,'/tmp/issue319-fixture/probe.py'],filename='probe.log')))
            for mode in ('controls','production'):
                results.update(json.loads(run([*command,'/tmp/issue319-fixture/dns_probe.py',mode],filename='dns-'+mode+'.log')))
            # Deliberately forwarding equivalent app, same networks/image/hardening.
            data['services']['app']['dns']=[recorder_address];save()
            run([*compose,'up','-d','--no-deps','app'],filename='dns-forwarding-control-up.log');stream()
            results.update(json.loads(run([*command,'/tmp/issue319-fixture/dns_probe.py','forwarding-control'],filename='dns-forwarding-control.log')))
            data['services']['app']['dns']=original_dns;save()
            run([*compose,'up','-d','--no-deps','app'],filename='dns-restored-up.log');stream()
            results.update(json.loads(run([*command,'/tmp/issue319-fixture/dns_probe.py','production'],filename='dns-restored.log')))
            data['services']['egress-proxy']['environment']={'OIDC_ISSUER':'','OIDC_ISSUER_PINS':''};save()
            run([*compose,'up','-d','--no-deps','--wait','--wait-timeout','90','egress-proxy'],filename='deny-all-up.log')
            results.update(json.loads(run([*command,'/tmp/issue319-fixture/probe.py','deny-all'],filename='deny-all.log')))
            results.update(production_compose_derived=True,production_app_image_built=True,
                production_proxy_environment_preserved=True,app_no_external_membership=True,
                no_mounts_or_published_ports=True,owner_only_proxy_source_readable=True,passed=True)
        finally:
            cleanup=subprocess.run([*compose,'down','--volumes','--rmi','all','--remove-orphans'],capture_output=True,text=True,timeout=120)
            (args.output/'cleanup.log').write_text(cleanup.stdout+cleanup.stderr)
            results['compose_cleanup_passed']=cleanup.returncode==0
            if cleanup.returncode: results['passed']=False
            (args.output/'summary.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results))
    return 0 if results['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
