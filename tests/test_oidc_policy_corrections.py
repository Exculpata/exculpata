"""Synthetic regressions for the policy boundaries present in 93f21856."""
from pathlib import Path
import shutil
import ipaddress
import subprocess
import sys

import pytest

from case_intelligence.oidc_network_policy import Denied, validated_https_url

ROOT = Path(__file__).parents[1]


def ipv4(*octets):
    return '.'.join(map(str, octets))


def ipv6(*words):
    if words[:6] == (0, 0, 0, 0, 0, 0xffff):
        return '::ffff:' + format(words[6], 'x') + ':' + format(words[7], 'x')
    return str(ipaddress.IPv6Address(':'.join(format(word, 'x') for word in words)))


@pytest.mark.parametrize('sibling', ['core.py', 'core.pyc', 'core/__init__.py'])
def test_extra_idna_modules_fail_before_import(tmp_path, sibling):
    target = tmp_path/'synthetic_idna'
    shutil.copytree(ROOT/'src/case_intelligence/_idna', target, ignore=shutil.ignore_patterns('__pycache__'))
    injected = target/sibling
    injected.parent.mkdir(exist_ok=True)
    injected.write_text('IDNAError = ValueError\ndef encode(*args, **kwargs): return b"evaded.example"\n')
    code = 'import sys; sys.path.insert(0,sys.argv[1]); import synthetic_idna; print(synthetic_idna.encode("faß.example"))'
    result = subprocess.run([sys.executable,'-I','-S','-c',code,str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert result.returncode != 0, result.stdout
    assert 'Unexpected bundled IDNA sibling' in result.stderr
    assert 'evaded.example' not in result.stdout


def test_idna_deferred_import_uses_verified_bytes_after_archive_replacement(tmp_path):
    target = tmp_path/'synthetic_idna'
    shutil.copytree(ROOT/'src/case_intelligence/_idna', target, ignore=shutil.ignore_patterns('__pycache__'))
    code = '''import pathlib,sys
sys.path.insert(0,sys.argv[1])
import synthetic_idna
pathlib.Path(synthetic_idna.archive).write_bytes(b'not an archive')
assert synthetic_idna.encode('faß.example', uts46=True) == b'xn--fa-hia.example'
assert synthetic_idna.__path__ == []
'''
    result = subprocess.run([sys.executable,'-I','-S','-c',code,str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('host', [
    ipv6(0x0, 0x0, 0x0, 0x0, 0x0, 0xffff, 0x7f00, 0x1), '::ffff:' + ipv4(127, 0, 0, 1), ipv6(0x0, 0x0, 0x0, 0x0, 0x0, 0xffff, 0xa9fe, 0xa9fe), '::ffff:' + ipv4(169, 254, 169, 254),
    ipv6(0x0, 0x0, 0x0, 0x0, 0x0, 0xffff, 0x808, 0x808), ipv6(0x2002, 0x7f00, 0x1, 0x0, 0x0, 0x0, 0x0, 0x0), ipv6(0x2002, 0x808, 0x808, 0x0, 0x0, 0x0, 0x0, 0x0), ipv6(0x2001, 0x0, 0x4136, 0xe378, 0x8000, 0x63bf, 0x3fff, 0xfdd2),
    '::1', '::', ipv6(0xfe80, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x1), ipv6(0xff02, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x1), ipv6(0xfd00, 0xec2, 0x0, 0x0, 0x0, 0x0, 0x0, 0x254), ipv6(0xfd20, 0xce, 0x0, 0x0, 0x0, 0x0, 0x0, 0x254),
    ipv4(169, 254, 169, 254), '127.0.0.1', '100.64.0.1',
])
def test_unsafe_literal_https_destinations_are_rejected(host):
    value = 'https://' + ('['+host+']' if ':' in host else host) + '/realm'
    with pytest.raises(Denied):
        validated_https_url(value)


@pytest.mark.parametrize('host', ['issuer.example.test', '8.8.8.8', '[2606:4700:4700::1111]',
                                  ipv4(10, 12, 0, 1), ipv4(192, 168, 1, 2), '[' + ipv6(0xfd12,0,0,0,0,0,0,1) + ']'])
def test_ordinary_public_and_explicitly_pinnable_private_hosts_retain_url_support(host):
    value='https://'+host+'/realm'
    assert validated_https_url(value)==value
