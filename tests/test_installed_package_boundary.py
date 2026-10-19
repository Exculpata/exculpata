"""Effective installed metadata checks and the explicit parent-bytecode limit."""
import io
import ast
import importlib.util
import json
import marshal
from pathlib import Path
import py_compile
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import exculpata_install as installer
import installed_package_probe as probe
sys.path.pop(0)


def protected_tree(tmp_path, monkeypatch):
    package = tmp_path / 'case_intelligence'
    package.mkdir()
    (package / '__init__.py').write_text('# synthetic package\n')
    (package / 'nested').mkdir()
    (package / 'nested/module.py').write_text('# synthetic module\n')
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda path: SimpleNamespace(
        st_mode=original(path).st_mode & ~0o022, st_uid=0))
    monkeypatch.setattr(probe.os, 'access', lambda path, mode, **kwargs: False)
    monkeypatch.setattr(probe.os, 'statvfs', lambda path: SimpleNamespace(f_flag=probe.os.ST_RDONLY))
    return package


def test_protected_package_and_ancestors_pass_without_writes(tmp_path, monkeypatch):
    package = protected_tree(tmp_path, monkeypatch)
    probe.verify_package(package)


@pytest.mark.parametrize('target', ['package', 'ancestor', 'file', 'nested'])
def test_app_writable_package_or_replacement_path_fails(tmp_path, monkeypatch, target):
    package = protected_tree(tmp_path, monkeypatch)
    unsafe = {'package': package, 'ancestor': package.parent,
              'file': package / '__init__.py', 'nested': package / 'nested'}[target]
    def access(path, mode, **kwargs):
        assert mode == probe.os.W_OK and kwargs == {'effective_ids': True}
        return Path(path) == unsafe
    monkeypatch.setattr(probe.os, 'access', access)
    with pytest.raises(probe.BoundaryError, match='package-writable'):
        probe.verify_package(package)


@pytest.mark.parametrize('target', ['package', 'ancestor', 'file'])
def test_read_only_does_not_substitute_for_root_ownership(tmp_path, monkeypatch, target):
    package = protected_tree(tmp_path, monkeypatch)
    unsafe = {'package': package, 'ancestor': package.parent, 'file': package / '__init__.py'}[target]
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda path: SimpleNamespace(
        st_mode=original(path).st_mode, st_uid=1000 if path == unsafe else 0))
    with pytest.raises(probe.BoundaryError, match='package-not-root-owned'):
        probe.verify_package(package)


@pytest.mark.parametrize('target', ['package', 'nested', 'file'])
def test_writable_mount_is_rejected_even_with_nonwritable_mode(tmp_path, monkeypatch, target):
    package = protected_tree(tmp_path, monkeypatch)
    unsafe = {'package': package, 'nested': package / 'nested', 'file': package / '__init__.py'}[target]
    monkeypatch.setattr(probe.os, 'statvfs', lambda path: SimpleNamespace(
        f_flag=0 if Path(path) == unsafe else probe.os.ST_RDONLY))
    with pytest.raises(probe.BoundaryError, match='package-mount-writable'):
        probe.verify_package(package)


def test_package_symlink_fails_without_following_target(tmp_path, monkeypatch):
    package = protected_tree(tmp_path, monkeypatch)
    (package / 'unexpected').symlink_to(tmp_path / 'missing-synthetic-target')
    with pytest.raises(probe.BoundaryError, match='package-entry-unsafe'):
        probe.verify_package(package)


def test_probe_checks_effective_root_before_package_metadata(monkeypatch):
    monkeypatch.setattr(probe, 'verify_app_user', lambda: None)
    monkeypatch.setattr(probe.os, 'statvfs', lambda path: SimpleNamespace(f_flag=0))
    monkeypatch.setattr(probe, 'installed_package', lambda: pytest.fail('writable root was accepted'))
    with pytest.raises(probe.BoundaryError, match='app-root-writable'):
        probe.verify()


def test_installed_location_uses_venv_without_importing_package(tmp_path,monkeypatch):
    package=tmp_path/'case_intelligence';package.mkdir()
    (package/'__init__.py').write_text('raise RuntimeError("must not import")\n')
    monkeypatch.setattr(probe.sys,'executable','/synthetic/venv/bin/python')
    def path(kind,vars):
        assert kind in ('purelib','platlib')
        assert vars=={'base':'/synthetic/venv','platbase':'/synthetic/venv'}
        return str(tmp_path)
    monkeypatch.setattr(probe.sysconfig,'get_path',path)
    assert probe.installed_package()==package


@pytest.mark.parametrize('uid,gid,groups,allowed', [(1000,1000,[1000],True),
    (0,1000,[1000],False),(1001,1000,[1000],False),(1000,1001,[1000],False),
    (1000,1000,[1000,2000],False)])
def test_probe_runs_as_effective_app_identity(monkeypatch,uid,gid,groups,allowed):
    import builtins
    monkeypatch.setattr(builtins, 'open', lambda *a, **kw: io.StringIO(
        'Uid:\t1000\t1000\t1000\t1000\nGid:\t1000\t1000\t1000\t1000\nGroups:\t1000\n'))
    monkeypatch.setattr(probe.os, 'geteuid', lambda: uid)
    monkeypatch.setattr(probe.os, 'getegid', lambda: gid)
    monkeypatch.setattr(probe.os, 'getgroups', lambda: groups)
    if allowed:
        probe.verify_app_user()
    else:
        with pytest.raises(probe.BoundaryError, match='app-user-'):
            probe.verify_app_user()


def test_missing_runtime_metadata_fails_without_private_output(monkeypatch,capsys):
    def fail(): raise OSError('synthetic-private-runtime-detail')
    monkeypatch.setattr(probe, 'verify', fail)
    assert probe.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {'installed_package_verified': False, 'reason': 'probe-unavailable'}
    assert 'synthetic-private-runtime-detail' not in output


def wire_runtime(monkeypatch, *, state=None, proof=None, returncode=0):
    calls = []
    monkeypatch.setattr(installer, '_installed_release', lambda root: ({'profiles': []}, ROOT))
    monkeypatch.setattr(installer, '_compose', lambda *args: ['docker','compose'])
    def inspect(command):
        calls.append(command)
        output = 'a'*64 if 'ps' in command else json.dumps(
            {'running': True, 'read_only': True} if state is None else state)
        return subprocess.CompletedProcess(command,0,output,'')
    monkeypatch.setattr(installer,'_probe',inspect)
    def streamed(command,**kwargs):
        calls.append(command)
        assert command == ['docker','exec','-i','a'*64,'python','-I','-S','-B','-']
        assert kwargs['input'] == (ROOT/'scripts/installed_package_probe.py').read_text()
        assert kwargs['capture_output'] and kwargs['timeout'] == 30
        assert 'import case_intelligence' not in kwargs['input']
        return subprocess.CompletedProcess(command,returncode,
            json.dumps(probe.PROOF if proof is None else proof),'synthetic-private-runtime-detail')
    monkeypatch.setattr(installer.subprocess,'run',streamed)
    return calls


def test_installed_diagnostic_checks_live_state_as_normal_app_user(monkeypatch,tmp_path):
    calls=wire_runtime(monkeypatch)
    installer._verify_installed_package(tmp_path)
    assert len(calls)==3
    assert all('--user' not in command and '.Config.Env' not in ' '.join(command) for command in calls)


@pytest.mark.parametrize('state', [{'running':False,'read_only':True},
    {'running':True,'read_only':False},{'running':1,'read_only':True},{}])
def test_container_configuration_failure_blocks_live_probe(monkeypatch,tmp_path,state):
    calls=wire_runtime(monkeypatch,state=state)
    with pytest.raises(RuntimeError,match='Installed package trust is unverified'):
        installer._verify_installed_package(tmp_path)
    assert len(calls)==2


@pytest.mark.parametrize('reason', ['app-root-writable','package-writable',
    'package-not-root-owned','package-mount-writable','package-mode-writable','app-user-root','probe-unavailable'])
def test_declared_readonly_never_substitutes_for_effective_proof(monkeypatch,tmp_path,reason):
    wire_runtime(monkeypatch,proof={'installed_package_verified':False,'reason':reason},returncode=1)
    with pytest.raises(RuntimeError,match='Installed package trust is unverified') as error:
        installer._verify_installed_package(tmp_path)
    assert 'synthetic-private-runtime-detail' not in str(error.value)


@pytest.mark.parametrize('proof', [{}, {'app_root_read_only':True},
    {**probe.PROOF,'app_root_read_only':1},{**probe.PROOF,'unexpected':True}])
def test_incomplete_or_non_boolean_proof_is_rejected(monkeypatch,tmp_path,proof):
    wire_runtime(monkeypatch,proof=proof)
    with pytest.raises(RuntimeError,match='Installed package trust is unverified'):
        installer._verify_installed_package(tmp_path)


@pytest.mark.parametrize('reason', ['package-writable','app-root-writable'])
def test_doctor_does_not_accept_healthy_app_with_unprotected_install(tmp_path,monkeypatch,reason):
    from tests.test_first_run_handoff import configured_node
    root,args,_=configured_node(tmp_path)
    monkeypatch.setattr(installer,'_run',lambda *a,**kw:None)
    monkeypatch.setattr(installer,'_wait_health',lambda *a,**kw:None)
    monkeypatch.setattr(installer,'_record_prepared_phases',lambda *a,**kw:None)
    # Preserve the real installed metadata: only runtime transport is synthetic.
    original=installer._installed_release
    wire_runtime(monkeypatch,proof={'installed_package_verified':False,'reason':reason},returncode=1)
    monkeypatch.setattr(installer,'_installed_release',original)
    with pytest.raises(RuntimeError,match='Installed package trust is unverified'):
        installer._doctor(installer.Console(color=False,quiet=True),args,root)


def test_dry_run_does_not_claim_effective_package_protection(tmp_path,monkeypatch,capsys):
    from tests.test_first_run_handoff import configured_node
    root,args,_=configured_node(tmp_path);args.dry_run=True
    monkeypatch.setattr(installer,'_verify_installed_package',
        lambda *a:pytest.fail('dry run attempted a live package check'))
    installer._doctor(installer.Console(color=False),args,root)
    assert 'installed package protection, running authentication mode, DNS boundary and live health are not verified' in capsys.readouterr().out


def test_parent_package_bytecode_is_outside_idna_integrity_boundary(tmp_path):
    package=tmp_path/'case_intelligence';package.mkdir()
    source=package/'__init__.py'
    shutil.copyfile(ROOT/'src/case_intelligence/__init__.py',source)
    cache=Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(str(source),doraise=True)
    payload='print("SYNTHETIC_PARENT_CACHE_EXECUTED")\n'
    cache.write_bytes(cache.read_bytes()[:16]+marshal.dumps(compile(payload,str(source),'exec')))
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',
        'import sys;sys.path.insert(0,sys.argv[1]);import case_intelligence',str(tmp_path)],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='SYNTHETIC_PARENT_CACHE_EXECUTED'


def test_documented_trust_anchor_explicitly_excludes_parent_bytecode():
    text=(ROOT/'docs/NETWORK_EGRESS.md').read_text()
    assert 'root-owned, read-only installed package' in text
    assert 'does not protect parent-package bytecode' in text
    assert 'case_intelligence/__pycache__/__init__' in text


def test_native_writable_package_control_runs_without_docker():
    tree=ast.parse((ROOT/'scripts/test-egress-smoke.py').read_text())
    code=next(ast.literal_eval(node.value) for node in ast.walk(tree)
        if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name)
            and target.id=='package_control' for target in node.targets))
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',code],
        input=(ROOT/'scripts/installed_package_probe.py').read_text(),
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'app_writable_package_control_rejected':True}


@pytest.mark.parametrize('target', ['package', 'ancestor', 'file', 'nested'])
@pytest.mark.parametrize('write_bits', [0o020, 0o002, 0o022])
def test_group_world_write_modes_fail_even_on_readonly_mount(tmp_path, monkeypatch, target, write_bits):
    package = protected_tree(tmp_path, monkeypatch)
    unsafe = {'package': package, 'ancestor': package.parent,
              'file': package / '__init__.py', 'nested': package / 'nested'}[target]
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda path: SimpleNamespace(
        st_mode=original(path).st_mode | (write_bits if path == unsafe else 0), st_uid=0))
    with pytest.raises(probe.BoundaryError, match='package-mode-writable'):
        probe.verify_package(package)


@pytest.mark.parametrize('mode', [0o444, 0o644, 0o640, 0o400])
def test_owner_write_bit_is_allowed_with_effective_protections(tmp_path, monkeypatch, mode):
    package = protected_tree(tmp_path, monkeypatch)
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda path: SimpleNamespace(
        st_mode=(stat.S_IFREG | mode) if path == package / '__init__.py' else original(path).st_mode,
        st_uid=0))
    probe.verify_package(package)


def completion_node(tmp_path, monkeypatch):
    from tests.test_first_run_handoff import configured_node
    root, args, paths = configured_node(tmp_path)
    (paths['accounts'] / 'local-accounts.json').write_text('{}')
    args.prepare_only = False
    args.no_backup = True
    events = []
    monkeypatch.setattr(installer, '_run', lambda console, command, **kwargs:
        events.append('startup' if 'up' in command else 'command') or subprocess.CompletedProcess(command, 0, '', ''))
    monkeypatch.setattr(installer, '_preflight', lambda *a, **kw: ())
    monkeypatch.setattr(installer, '_stage_release', lambda *a, **kw: ('synthetic-new', ROOT))
    monkeypatch.setattr(installer, '_wait_health', lambda *a, **kw:
        events.append('rollback-health' if kw.get('rollback') else 'health'))
    return root, args, events


@pytest.mark.parametrize('action', ['install', 'update'])
@pytest.mark.parametrize('protected', [False, True])
def test_install_update_require_package_protection_before_acceptance(tmp_path, monkeypatch, capsys, action, protected):
    root, args, events = completion_node(tmp_path, monkeypatch)
    before = {name: (root / name).read_bytes() for name in ('installation.json', 'compose.env',
        'config/transcription.env')}
    def verify(path):
        assert path == root and events[-1] == 'startup'
        events.append('package')
        if not protected:
            raise RuntimeError('synthetic package protection denied')
    monkeypatch.setattr(installer, '_verify_installed_package', verify)
    def run():
        if action == 'install':
            installer._provision(installer.Console(color=False), args, root, 'local', 'none',
                None, None, password_input=[])
        else:
            installer._update(installer.Console(color=False), args, root)
    capsys.readouterr()
    if protected:
        run()
        assert events.index('startup') < events.index('package') < events.index('health')
        assert 'rollback-health' not in events
    else:
        with pytest.raises(RuntimeError, match='synthetic package protection denied'):
            run()
        assert 'health' not in events
        output = capsys.readouterr().out
        assert 'node is online' not in output and 'Update committed' not in output
        if action == 'update':
            assert events[-2:] == ['startup', 'rollback-health']
            assert all((root / name).read_bytes() == value for name, value in before.items())
        else:
            assert installer._install_progress(root)['phases']['running'] == 'checking'


@pytest.mark.parametrize('action', ['install', 'update', 'prepare'])
def test_nonlive_completion_does_not_probe_installed_package(tmp_path, monkeypatch, action):
    root, args, events = completion_node(tmp_path, monkeypatch)
    args.dry_run = action != 'prepare'
    args.prepare_only = action == 'prepare'
    monkeypatch.setattr(installer, '_verify_installed_package', lambda *a:
        pytest.fail('nonlive action attempted package verification'))
    if action == 'update':
        installer._update(installer.Console(color=False, quiet=True), args, root)
    else:
        installer._provision(installer.Console(color=False, quiet=True), args, root, 'local', 'none',
            None, None, password_input=[])
    assert 'health' not in events


@pytest.mark.parametrize('output', ['', '\n', '  \n\t'])
def test_absent_app_reports_not_running_without_package_trust_claim(tmp_path, monkeypatch, output):
    monkeypatch.setattr(installer, '_installed_release', lambda root: ({'profiles': []}, ROOT))
    monkeypatch.setattr(installer, '_compose', lambda *a: ['docker', 'compose'])
    calls = []
    def inventory(command):
        calls.append(command)
        assert command == ['docker', 'compose', 'ps', '-q', 'app']
        return subprocess.CompletedProcess(command, 0, output, 'synthetic-private-detail')
    monkeypatch.setattr(installer, '_probe', inventory)
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k:
        pytest.fail('absent app reached a live package probe'))
    with pytest.raises(RuntimeError, match='App is not running') as error:
        installer._verify_installed_package(tmp_path)
    message = str(error.value)
    assert 'Package protection was not checked' in message
    assert 'Start or resume the configured node, then rerun doctor' in message
    assert 'trust is unverified' not in message and 'Rebuild' not in message
    assert 'synthetic-private-detail' not in message
    assert len(calls) == 1


@pytest.mark.parametrize('returncode,output', [(1, ''), (1, 'a'*64),
    (0, 'invalid-container'), (0, 'a'*64+'\n'+'b'*64)])
def test_failed_or_malformed_inventory_is_not_misreported_as_absent(tmp_path, monkeypatch, returncode, output):
    monkeypatch.setattr(installer, '_installed_release', lambda root: ({'profiles': []}, ROOT))
    monkeypatch.setattr(installer, '_compose', lambda *a: ['docker', 'compose'])
    def inventory(command):
        assert command == ['docker', 'compose', 'ps', '-q', 'app']
        return subprocess.CompletedProcess(command, returncode, output, 'synthetic-private-detail')
    monkeypatch.setattr(installer, '_probe', inventory)
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k:
        pytest.fail('unverified inventory reached a live package probe'))
    with pytest.raises(RuntimeError, match='Installed package trust is unverified') as error:
        installer._verify_installed_package(tmp_path)
    assert 'App is not running' not in str(error.value)
    assert 'synthetic-private-detail' not in str(error.value)


def test_rollback_package_verification_limit_is_explicit():
    text = (ROOT / 'docs/NETWORK_EGRESS.md').read_text()
    assert 'Rollback does not re-check the restored package.' in text
    assert 'run live doctor against the restored node' in text
