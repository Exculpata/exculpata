"""Offline installer policy is the same policy used by the runtime relay."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import idna
import pytest

from case_intelligence import _idna
from case_intelligence.oidc_network_policy import Denied, issuer_pins

ROOT = Path(__file__).parents[1]


def test_policy_imports_with_only_the_standard_library():
    code = "import sys; sys.path.insert(0,sys.argv[1]); from case_intelligence.oidc_network_policy import issuer_authority; assert issuer_authority('https://faß.example.test') == ('xn--fa-hia.example.test',443)"
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(ROOT/'src')],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_offline_source_manifest_and_transport_dependency_agree():
    root = ROOT/'src/case_intelligence/_idna'
    manifest = json.loads((root/'manifest.json').read_text())
    archive = root/'idna-3.20.pyz'
    assert manifest['version'] == _idna.__version__ == idna.__version__
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest['archive_sha256'] == _idna.ARCHIVE_SHA256
    assert manifest['upstream_wheel_sha256'] in (ROOT/'deploy/egress-proxy/requirements.lock').read_text()
    with zipfile.ZipFile(archive) as source:
        assert set(source.namelist()) == set(manifest['files'])
        for name, expected in manifest['files'].items():
            assert hashlib.sha256(source.read(name)).hexdigest() == expected
        assert source.read('LICENSE.md') == (root/'LICENSE.md').read_bytes()


def test_modified_offline_source_is_rejected_before_import(tmp_path):
    root = ROOT/'src/case_intelligence/_idna'
    shutil.copy(root/'__init__.py', tmp_path/'__init__.py')
    (tmp_path/'idna-3.20.pyz').write_bytes((root/'idna-3.20.pyz').read_bytes() + b'changed')
    spec = importlib.util.spec_from_file_location('synthetic_idna', tmp_path/'__init__.py')
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match='integrity'):
        spec.loader.exec_module(module)


def test_vendor_reproducer_rejects_unreviewed_wheel_without_writes(tmp_path):
    wheel = tmp_path/'unreviewed.whl'
    wheel.write_bytes(b'not the reviewed source')
    destination = tmp_path/'output'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/vendor-idna.py'), str(wheel), str(destination)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and not destination.exists()


def test_duplicate_pin_fields_do_not_select_a_later_policy():
    value = '{"issuer":"https://wrong.example.test","issuer":"https://issuer.example.test","addresses":["8.8.8.8"]}'
    with pytest.raises(Denied, match='duplicate'):
        issuer_pins(value, 'https://issuer.example.test')


def test_installer_cli_needs_no_host_site_packages():
    code = "import pathlib,runpy,sys; root=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(root/'scripts')); sys.argv=[str(root/'scripts/exculpata_install.py'),'--help']; runpy.run_path(sys.argv[0],run_name='__main__')"
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(ROOT)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and 'usage:' in result.stdout, result.stderr
