"""Actual Docker context filtering; no container is started or port published."""
import io
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import uuid

import pytest

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.skipif(os.environ.get('RUN_NETWORK_SMOKE') != '1', reason='requires approved disposable Docker runtime')
PUBLIC = ('__init__.py', 'idna-3.20.pyz', 'manifest.json', 'LICENSE.md', 'README.md')


def context_files(tmp_path, builder, *, baseline=False):
    docker = ['docker', '--context', os.environ['NETWORK_SMOKE_DOCKER_CONTEXT']]
    image = 'synthetic-context-' + uuid.uuid4().hex
    container = None
    def run(*args, **kw):
        return subprocess.run([*docker,*args],check=True,capture_output=True,timeout=180,**kw)
    root = tmp_path/'context'; root.mkdir()
    for relative in ('.dockerignore','deploy/egress-proxy/Dockerfile.dockerignore',
                     *(f'src/case_intelligence/_idna/{name}' for name in PUBLIC)):
        destination=root/relative; destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/relative,destination)
    if baseline:
        shutil.copyfile(ROOT/'tests/fixtures/network/root-93f21856.dockerignore', root/'.dockerignore')
    path=root/'deploy/egress-proxy/Dockerfile'
    path.write_text('FROM scratch\nCOPY . /context/\nCMD ["/never-started"]\n')
    for relative in ('src/case_intelligence/_idna/core.py','src/case_intelligence/_idna/extra/module.py',
                     'src/case_intelligence/_idna/operator.txt','.env','state/synthetic.db'):
        destination=root/relative; destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text('synthetic context sentinel')
    try:
        run('build','--network=none','--pull=false','-f',str(path),'-t',image,str(root),
            env={**os.environ,'DOCKER_BUILDKIT':builder})
        container=run('create','--network=none',image).stdout.decode().strip()
        exported=run('export',container).stdout
        with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
            return {member.name for member in archive.getmembers() if member.isfile()}
    finally:
        if container: run('rm',container)
        run('image','rm','-f',image)


def assert_reviewed_context(files):
    prefix='context/src/case_intelligence/_idna/'
    assert {name[len(prefix):] for name in files if name.startswith(prefix)} == set(PUBLIC)
    assert 'context/.env' not in files and 'context/state/synthetic.db' not in files


@pytest.mark.parametrize('builder', ['0','1'])
def test_actual_legacy_and_buildkit_context_exclude_unreviewed_idna(tmp_path,builder):
    assert_reviewed_context(context_files(tmp_path,builder))


def test_93f21856_legacy_context_negative_control(tmp_path, monkeypatch):
    archive=tmp_path/'source-archive'
    for relative in ('.dockerignore','deploy/egress-proxy/Dockerfile.dockerignore',
                     'tests/fixtures/network/root-93f21856.dockerignore',
                     *(f'src/case_intelligence/_idna/{name}' for name in PUBLIC)):
        destination=archive/relative; destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/relative,destination)
    historical=(archive/'tests/fixtures/network/root-93f21856.dockerignore').read_bytes()
    assert hashlib.sha256(historical).hexdigest() == 'b08f17c51e2059441ae086bf9d4e16dbec4a4ec3b41f3377c26b96a2dc2fcad5'
    monkeypatch.setitem(globals(), 'ROOT', archive)
    files=context_files(tmp_path,'0',baseline=True)
    assert 'context/src/case_intelligence/_idna/core.py' in files
    with pytest.raises(AssertionError):
        assert_reviewed_context(files)
