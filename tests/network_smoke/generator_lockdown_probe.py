"""Content-free evidence for this container topology, host and execution only."""
import argparse
import contextlib
import ctypes
import errno
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

TCP_TARGETS = {
    'link_local_metadata': (str(ipaddress.IPv4Address(bytes((169, 254, 169, 254)))), 80),
    'shared_metadata': ('100.100.100.200', 80),
    'ipv6_metadata': (str(ipaddress.IPv6Address((0xfd00 << 112) | (0xec2 << 96) | 0x254)), 80),
    'public_https': ('1.1.1.1', 443), 'public_dns': ('8.8.8.8', 53),
}
TARGETS = (*TCP_TARGETS, 'udp_dns', 'getaddrinfo')
CACHE_ENV = ('HOME', 'XDG_CACHE_HOME', 'TMPDIR', 'TRITON_CACHE_DIR',
             'TORCHINDUCTOR_CACHE_DIR', 'VLLM_CACHE_ROOT')


def has_default_route(ipv4, ipv6):
    for line in ipv4.splitlines():
        fields = line.split()
        if fields and fields[0] != 'Iface':
            if fields[1] == fields[7] == '00000000' and int(fields[3], 16) & 1:
                return True
    for line in ipv6.splitlines():
        fields = line.split()
        if fields and fields[0] == '0' * 32 and fields[1] == '00' and int(fields[8], 16) & 1:
            return True
    return False


def connection_result(error=None):
    if error is None:
        return 'leak'
    if isinstance(error, TimeoutError) or error.errno in (
            errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT):
        return 'blocked'
    return 'invalid'


def tcp_result(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return 'leak'
    except OSError as error:
        return connection_result(error)


def peer_answers(host, port):
    try:
        with socket.create_connection((host, port), timeout=3) as peer:
            peer.sendall(b'GET / HTTP/1.0\r\nHost: retrieval\r\n\r\n')
            with peer.makefile('rb') as response:
                return response.readline(128).startswith((b'HTTP/1.0 200 ', b'HTTP/1.1 200 '))
    except OSError:
        return False


def udp_dns_result():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(3)
            client.connect(('8.8.8.8', 53))
            client.send(b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
                        b'\x07example\x03com\x00\x00\x01\x00\x01')
            client.recv(512)
            return 'leak'
    except OSError as error:
        return connection_result(error)


def verdict(result):
    targets = result['targets']
    if (not result.get('peer_control') or set(targets) != set(TARGETS)
            or any(value not in ('blocked', 'leak') for value in targets.values())):
        return 'invalid'
    return 'leak' if result['default_route'] or 'leak' in targets.values() else 'passed'


def hardening():
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    checks = {'nonroot': os.getuid() != 0, 'no_new_privileges': int(status['NoNewPrivs']) == 1,
              'seccomp': int(status['Seccomp']) == 2}
    checks.update({key: int(status[key], 16) == 0 for key in ('CapEff', 'CapPrm', 'CapBnd')})
    libc = ctypes.CDLL(None, use_errno=True)
    checks['user_namespace_denied'] = libc.unshare(0x10000000) == -1 and ctypes.get_errno() == errno.EPERM
    for directory in ('/', '/models'):
        try:
            with tempfile.TemporaryFile(dir=directory) as stream:
                # fixture-write: Write a synthetic byte inside the disposable container only.
                stream.write(b'x')
        except OSError as error:
            checks[directory] = error.errno == errno.EROFS
        else:
            checks[directory] = False
    for key in CACHE_ENV:
        directory = Path(os.environ[key])
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory) as stream:
            # fixture-write: Exercise each configured synthetic cache inside the container.
            stream.write(b'x')
        checks[key] = True
    import _json
    for directory, expected in (('/tmp/vllm-cache', True), ('/tmp', False)):
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            artifact = Path(temporary) / 'synthetic-native.so'
            # fixture-write: Copy a local native module into a disposable container cache.
            shutil.copyfile(_json.__file__, artifact)
            try:
                ctypes.CDLL(str(artifact))
                loaded = True
            except OSError:
                loaded = False
            checks['native_load_' + directory] = loaded == expected
    with contextlib.redirect_stdout(sys.stderr):
        import vllm
    checks['vllm_import'] = bool(vllm)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--peer', required=True)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--hardening', action='store_true')
    args = parser.parse_args()
    result = {'peer_control': False, 'targets': {}, 'status': 'invalid'}
    try:
        # Bound libc resolver retries before the first name lookup in this process.
        os.environ['RES_OPTIONS'] = 'attempts:1 timeout:2'
        result['peer_control'] = peer_answers(args.peer, args.port)
        result['default_route'] = has_default_route(Path('/proc/net/route').read_text(),
                                                    Path('/proc/net/ipv6_route').read_text())
        result['targets'] = {name: tcp_result(*target) for name, target in TCP_TARGETS.items()}
        result['targets']['udp_dns'] = udp_dns_result()
        lookup = "import socket,sys\ntry: socket.getaddrinfo('example.com',443)\nexcept socket.gaierror: sys.exit(2)"
        try:
            resolved = subprocess.run([sys.executable, '-c', lookup], capture_output=True, timeout=3)
            result['targets']['getaddrinfo'] = {0: 'leak', 2: 'blocked'}.get(resolved.returncode, 'invalid')
        except subprocess.TimeoutExpired:
            result['targets']['getaddrinfo'] = 'blocked'
        result['status'] = verdict(result)
        if args.hardening:
            result['hardening'] = hardening()
            if not all(result['hardening'].values()):
                result['status'] = 'invalid'
    except Exception as error:
        result.update(status='invalid', error=type(error).__name__)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
