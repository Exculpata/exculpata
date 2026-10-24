"""Standalone, bounded CONNECT/TLS probe for a configured generator relay slot."""
import argparse
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit


TIMEOUT = 5
MAX_HEADER = 8192


def authority(value):
    try:
        parsed = urlsplit('//' + value)
        if (not parsed.hostname or parsed.port is None or not 0 < parsed.port < 65536
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or any(character.isspace() or ord(character) < 32 for character in value)):
            raise ValueError
        value.encode('ascii')
        return parsed.hostname, parsed.port
    except (ValueError, UnicodeError):
        raise argparse.ArgumentTypeError('expected HOST:PORT with a valid port') from None


def probe(proxy, target, expect, *, cafile=None, server_name=None):
    target_host, _ = authority(target)
    context = ssl.create_default_context(cafile=cafile) if cafile else None
    with socket.create_connection(proxy, timeout=TIMEOUT) as client:
        client.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode('ascii'))
        header = bytearray()
        status = None
        deadline = time.monotonic() + TIMEOUT
        while not header.endswith(b'\r\n\r\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('CONNECT response timed out')
            client.settimeout(remaining)
            try:
                value = client.recv(1)
            except ConnectionResetError:
                value = b''
            if not value:
                raise ValueError('proxy closed before a complete CONNECT response')
            header.extend(value)
            if status is None and b'\r\n' in header:
                fields = bytes(header).split(b'\r\n', 1)[0].split(b' ', 2)
                if (len(fields) < 2 or fields[0] not in (b'HTTP/1.0', b'HTTP/1.1')
                        or len(fields[1]) != 3 or not fields[1].isdigit()):
                    raise ValueError('invalid CONNECT response status')
                status = int(fields[1])
                if expect == 'deny' and status == 200:
                    raise ValueError('expected denial, received HTTP 200')
            if len(header) > MAX_HEADER:
                raise ValueError('CONNECT response header too large')
        if expect == 'deny':
            if status != 403:
                raise ValueError(f'expected HTTP 403, received HTTP {status}')
        elif status != 200:
            raise ValueError(f'expected HTTP 200, received HTTP {status}')
        elif context is not None:
            client.settimeout(TIMEOUT)
            with context.wrap_socket(client, server_hostname=server_name or target_host):
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', required=True, type=authority)
    parser.add_argument('--target', required=True)
    parser.add_argument('--expect', required=True, choices=('tunnel', 'deny'))
    parser.add_argument('--cafile')
    parser.add_argument('--server-name')
    args = parser.parse_args(argv)
    try:
        probe(args.proxy, args.target, args.expect, cafile=args.cafile, server_name=args.server_name)
    except (OSError, ValueError, argparse.ArgumentTypeError) as error:
        reason = ' '.join(str(error).splitlines())
        print(f'generator probe failed: {reason}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
