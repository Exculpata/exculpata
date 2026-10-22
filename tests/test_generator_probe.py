"""Offline regressions for the standalone generator CONNECT probe."""
from contextlib import nullcontext
import runpy
from pathlib import Path
import ssl
import socket
import threading

import pytest


PROBE = runpy.run_path(str(Path(__file__).parent / 'network_smoke/generator_probe.py'))


class ResponseSocket:
    def __init__(self, response):
        self.response = bytearray(response)
        self.request = b''

    def sendall(self, value):
        self.request += value

    def settimeout(self, timeout):
        assert 0 < timeout <= PROBE['TIMEOUT']

    def recv(self, size):
        value = bytes(self.response[:size])
        del self.response[:size]
        return value


@pytest.mark.parametrize('response,expect,success', [
    (b'HTTP/1.1 200 Connection Established\r\n\r\n', 'tunnel', True),
    (b'HTTP/1.1 403 Forbidden\r\n\r\n', 'tunnel', False),
    (b'HTTP/1.1 403 Forbidden\r\n\r\n', 'deny', True),
    (b'', 'deny', False),
    (b'', 'tunnel', False),
    (b'HTTP/1.1 403 Forbidden\r\n', 'deny', False),
    (b'HTTP/1.1 200 Connection Established\r\n\r\n', 'deny', False),
    (b'HTTP/1.1 200 Connection Established\r\n', 'deny', False),
    (b'HTTP/1.1 502 Bad Gateway\r\n\r\n', 'deny', False),
    (b'not HTTP\r\n\r\n', 'deny', False),
    (b'x' * 8193, 'deny', False),
])
def test_connect_expectation(monkeypatch, response, expect, success, capsys):
    client = ResponseSocket(response)
    monkeypatch.setattr(PROBE['socket'], 'create_connection', lambda *a, **k: nullcontext(client))
    result = PROBE['main'](['--proxy', 'relay.example.test:3128',
        '--target', 'generator.example.test:8443', '--expect', expect])
    assert result == (0 if success else 1)
    assert client.request == (b'CONNECT generator.example.test:8443 HTTP/1.1\r\n'
                              b'Host: generator.example.test:8443\r\n\r\n')
    assert len(capsys.readouterr().err.splitlines()) == (0 if success else 1)


def test_accept_then_close_is_not_a_denial(capsys):
    requests = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(3)
        def accept_and_close():
            with listener.accept()[0] as connection:
                connection.settimeout(3)
                request = bytearray()
                while not request.endswith(b'\r\n\r\n'):
                    value = connection.recv(1)
                    if not value:
                        break
                    request.extend(value)
                requests.append(bytes(request))
        thread = threading.Thread(target=accept_and_close, daemon=True)
        thread.start()
        try:
            result = PROBE['main'](['--proxy', f'127.0.0.1:{listener.getsockname()[1]}',
                '--target', 'generator.example.test:8443', '--expect', 'deny'])
        finally:
            thread.join(5)
        assert not thread.is_alive()
    assert requests == [b'CONNECT generator.example.test:8443 HTTP/1.1\r\n'
                        b'Host: generator.example.test:8443\r\n\r\n']
    assert result == 1
    assert capsys.readouterr().err.splitlines() == [
        'generator probe failed: proxy closed before a complete CONNECT response',
    ]


@pytest.mark.parametrize('expect', ['deny'])
def test_reset_before_complete_response_fails(monkeypatch, expect, capsys):
    client = ResponseSocket(b'HTTP/1.1 403 Forbidden\r\n')
    receive = client.recv
    def reset_at_end(size):
        if not client.response:
            raise ConnectionResetError('synthetic reset')
        return receive(size)
    monkeypatch.setattr(client, 'recv', reset_at_end)
    monkeypatch.setattr(PROBE['socket'], 'create_connection', lambda *a, **k: nullcontext(client))
    assert PROBE['main'](['--proxy', 'relay.example.test:3128',
        '--target', 'generator.example.test:8443', '--expect', expect]) == 1
    assert capsys.readouterr().err.splitlines() == [
        'generator probe failed: proxy closed before a complete CONNECT response',
    ]


@pytest.mark.parametrize('error', [TimeoutError('synthetic timeout'), ConnectionRefusedError('synthetic refusal')])
def test_unreachable_proxy_is_not_a_denial(monkeypatch, error):
    def unavailable(*args, **kwargs):
        raise error
    monkeypatch.setattr(PROBE['socket'], 'create_connection', unavailable)
    assert PROBE['main'](['--proxy', 'relay.example.test:3128',
        '--target', 'generator.example.test:8443', '--expect', 'deny']) == 1


@pytest.mark.parametrize('server_name', [None, 'alternate.example.test'])
@pytest.mark.parametrize('verified', [True, False])
def test_tunnel_requires_tls_verification_when_ca_given(monkeypatch, server_name, verified):
    client = ResponseSocket(b'HTTP/1.1 200 Connection Established\r\n\r\n')
    monkeypatch.setattr(PROBE['socket'], 'create_connection', lambda *a, **k: nullcontext(client))
    calls = []
    class Context:
        def wrap_socket(self, connection, *, server_hostname):
            assert connection is client
            calls.append(server_hostname)
            if not verified:
                raise ssl.SSLCertVerificationError('synthetic certificate failure')
            return nullcontext()
    def context(*, cafile):
        assert cafile == 'synthetic-ca.pem'
        return Context()
    monkeypatch.setattr(PROBE['ssl'], 'create_default_context', context)
    args = ['--proxy', 'relay.example.test:3128', '--target', 'generator.example.test:8443',
            '--expect', 'tunnel', '--cafile', 'synthetic-ca.pem']
    if server_name:
        args.extend(['--server-name', server_name])
    assert PROBE['main'](args) == (0 if verified else 1)
    assert calls == [server_name or 'generator.example.test']
