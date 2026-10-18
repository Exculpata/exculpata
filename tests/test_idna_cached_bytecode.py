"""Executable cache-boundary regressions, including matching timestamp/size forgery."""
import importlib.util
import compileall
import marshal
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]


def isolated_policy(tmp_path):
    package = tmp_path/'case_intelligence'
    package.mkdir()
    for name in ('__init__.py', 'oidc_network_policy.py'):
        shutil.copyfile(ROOT/'src/case_intelligence'/name, package/name)
    shutil.copytree(ROOT/'src/case_intelligence/_idna', package/'_idna',
                    ignore=shutil.ignore_patterns('__pycache__'))
    return package


def run_policy(root, optimize=0):
    code = 'import sys; sys.path.insert(0,sys.argv[1]); from case_intelligence.oidc_network_policy import issuer_authority; print(issuer_authority("https://faß.example"))'
    return subprocess.run([sys.executable,'-I','-S','-B',*(['-'+('O'*optimize)] if optimize else []),
                           '-c',code,str(root)],capture_output=True,text=True,timeout=10)


@pytest.mark.parametrize('optimize', [0,1,2])
@pytest.mark.parametrize('mode', [py_compile.PycInvalidationMode.TIMESTAMP,
                                  py_compile.PycInvalidationMode.CHECKED_HASH,
                                  py_compile.PycInvalidationMode.UNCHECKED_HASH])
@pytest.mark.parametrize('filename', ['absolute','dot-relative','relocated'])
def test_forged_loader_cache_cannot_replace_verified_import(tmp_path,optimize,mode,filename):
    package = isolated_policy(tmp_path)
    source = package/'_idna/__init__.py'
    cache = Path(importlib.util.cache_from_source(str(source),optimization='' if not optimize else str(optimize)))
    py_compile.compile(str(source),cfile=str(cache),doraise=True,optimize=optimize,invalidation_mode=mode)
    header = cache.read_bytes()[:16]
    payload = 'print("PLANTED_BYTECODE_EXECUTED")\nIDNAError=ValueError\ndef encode(*a,**kw): return b"evaded.example"\n'
    recorded={'absolute':str(source),'dot-relative':'./case_intelligence/_idna/__init__.py',
              'relocated':'/synthetic/install/case_intelligence/_idna/__init__.py'}[filename]
    cache.write_bytes(header + marshal.dumps(compile(payload,recorded,'exec',optimize=optimize)))
    result = run_policy(tmp_path,optimize)
    assert 'PLANTED_BYTECODE_EXECUTED' not in result.stdout, result.stdout
    assert result.returncode != 0, result.stdout
    assert 'IDNA bytecode' in result.stderr


@pytest.mark.parametrize('optimize', [0,1,2])
@pytest.mark.parametrize('mode', [py_compile.PycInvalidationMode.TIMESTAMP,
                                  py_compile.PycInvalidationMode.CHECKED_HASH,
                                  py_compile.PycInvalidationMode.UNCHECKED_HASH])
@pytest.mark.parametrize('filename', ['absolute','relative','basename','dot-relative','parent-relative',
                                       'relocated','unicode-relocated','long-relocated','renamed-relocated'])
def test_genuine_loader_cache_retains_offline_idna(tmp_path,optimize,mode,filename):
    package=isolated_policy(tmp_path)
    source=package/'_idna/__init__.py'
    recorded={'absolute':str(source),'relative':str(source.relative_to(tmp_path)),
              'basename':source.name,'dot-relative':'./'+str(source.relative_to(tmp_path)),
              'parent-relative':'../synthetic-build/'+str(source.relative_to(tmp_path)),
              'relocated':'/synthetic/install/lib/'+str(source.relative_to(tmp_path)),
              'renamed-relocated':'/synthetic/install/loader.py',
              'unicode-relocated':'/synthetic/é/'+str(source.relative_to(tmp_path)),
              'long-relocated':'/synthetic/'+('nested/'*50)+str(source.relative_to(tmp_path))}[filename]
    py_compile.compile(str(source),dfile=recorded,doraise=True,optimize=optimize,invalidation_mode=mode)
    result=run_policy(tmp_path,optimize)
    assert result.returncode == 0, result.stderr
    assert "('xn--fa-hia.example', 443)" in result.stdout


@pytest.mark.parametrize('layout', ['destination','strip-prepend'])
def test_compileall_relocated_cache_retains_offline_idna(tmp_path,layout):
    package=isolated_policy(tmp_path)
    source=package/'_idna/__init__.py'
    options=({'ddir':'/synthetic/install'} if layout=='destination' else
             {'stripdir':str(tmp_path),'prependdir':'/synthetic/install'})
    assert compileall.compile_file(str(source),quiet=2,force=True,**options)
    result=run_policy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "('xn--fa-hia.example', 443)" in result.stdout


@pytest.mark.parametrize('shared', [False,True])
def test_genuine_cache_supports_writer_reference_ownership(tmp_path,shared):
    package=isolated_policy(tmp_path)
    source=package/'_idna/__init__.py'
    cache=Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(str(source),doraise=True)
    if shared:
        compiled=compile(source.read_bytes(),str(source),'exec',dont_inherit=True)
        references=(compiled,compiled)
        payload=marshal.dumps(references[0])
        del references,compiled
    else:
        payload=marshal.dumps(compile(source.read_bytes(),str(source),'exec',dont_inherit=True))
    cache.write_bytes(cache.read_bytes()[:16]+payload)
    result=run_policy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "('xn--fa-hia.example', 443)" in result.stdout


def test_untrusted_filename_candidates_do_not_authorize_modified_payload(tmp_path):
    package=isolated_policy(tmp_path)
    source=package/'_idna/__init__.py'
    cache=Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(str(source),doraise=True)
    original=cache.read_bytes()
    cache.write_bytes(original+marshal.dumps('/synthetic/install/__init__.py'))
    result=run_policy(tmp_path)
    assert result.returncode != 0
    assert 'IDNA bytecode' in result.stderr


def test_oversized_cache_payload_fails_closed(tmp_path):
    package=isolated_policy(tmp_path)
    source=package/'_idna/__init__.py'
    cache=Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(str(source),doraise=True)
    cache.write_bytes(cache.read_bytes()+b'x'*20000)
    result=run_policy(tmp_path)
    assert result.returncode != 0
    assert 'IDNA bytecode' in result.stderr


def test_reload_cannot_select_a_newly_planted_cache(tmp_path):
    package=isolated_policy(tmp_path)
    source=package/'_idna/__init__.py'
    cache=Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(str(source),doraise=True)
    payload='print("PLANTED_BYTECODE_EXECUTED")\n'
    forged=tmp_path/'synthetic-cache-payload'
    forged.write_bytes(cache.read_bytes()[:16]+marshal.dumps(compile(payload,str(source),'exec')))
    code='''import importlib,pathlib,sys
sys.path.insert(0,sys.argv[1])
from case_intelligence import _idna
pathlib.Path(sys.argv[2]).write_bytes(pathlib.Path(sys.argv[3]).read_bytes())
importlib.reload(_idna)
'''
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',code,str(tmp_path),str(cache),str(forged)],
                          capture_output=True,text=True,timeout=10)
    assert 'PLANTED_BYTECODE_EXECUTED' not in result.stdout
    assert result.returncode != 0
    assert 'IDNA bytecode' in result.stderr


@pytest.mark.parametrize('artifact', ['core.cpython-312.pyc','unexpected.data'])
def test_unreviewed_cache_artifacts_fail_closed(tmp_path,artifact):
    package=isolated_policy(tmp_path)
    cache=package/'_idna/__pycache__';cache.mkdir()
    (cache/artifact).write_bytes(b'synthetic unreviewed cache artifact')
    result=run_policy(tmp_path)
    assert result.returncode != 0
    assert 'IDNA bytecode' in result.stderr


def test_cache_symlink_is_rejected_without_reading_target(tmp_path):
    package=isolated_policy(tmp_path)
    cache=package/'_idna/__pycache__';cache.mkdir()
    (cache/'core.cpython-312.pyc').symlink_to(tmp_path/'nonexistent-synthetic-target')
    result=run_policy(tmp_path)
    assert result.returncode != 0
    assert 'IDNA bytecode' in result.stderr


def test_other_interpreter_cache_is_inert_and_never_executed(tmp_path):
    package=isolated_policy(tmp_path)
    cache=package/'_idna/__pycache__';cache.mkdir()
    tag='999' if sys.implementation.cache_tag != 'cpython-999' else '998'
    payload='print("PLANTED_BYTECODE_EXECUTED")\n'
    source=package/'_idna/__init__.py'
    (cache/('__init__.cpython-'+tag+'.pyc')).write_bytes(
        importlib.util.MAGIC_NUMBER+b'\0'*12+marshal.dumps(compile(payload,str(source),'exec')))
    result=run_policy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'PLANTED_BYTECODE_EXECUTED' not in result.stdout
    assert "('xn--fa-hia.example', 443)" in result.stdout
