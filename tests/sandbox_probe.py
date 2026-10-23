"""Bounded checks for the local primitives used by TestClient."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable

from anyio.from_thread import start_blocking_portal

CHECK_TIMEOUT = 1.5


def skip_message(primitive: str, error: str) -> str:
    return (f'TestClient route tests cannot run in this sandbox: {primitive}: {error}. '
            'Run them in CI or an unrestricted shell.')


def _socketpair() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()


def _socketpair_wakeup() -> None:
    left, right = socket.socketpair()
    with left, right:
        left.settimeout(CHECK_TIMEOUT)
        right.settimeout(CHECK_TIMEOUT)
        left.sendall(b'\0')
        if right.recv(1) != b'\0':
            raise RuntimeError('socketpair wakeup byte was not received')


def _portal() -> None:
    with start_blocking_portal() as portal:
        portal.call(lambda: None)


def _bounded_check(name: str, check: Callable[[], None], timeout: float) -> str | None:
    errors: list[str] = []

    def run() -> None:
        try:
            check()
        except BaseException as exc:
            errors.append(f'{type(exc).__name__}: {exc}')

    thread = threading.Thread(target=run, daemon=True, name='testclient-sandbox-probe')
    try:
        thread.start()
    except Exception as exc:
        return skip_message(name, f'{type(exc).__name__}: {exc}')
    thread.join(timeout)
    if thread.is_alive():
        return skip_message(name, f'timed out after {timeout:g} seconds')
    return skip_message(name, errors[0]) if errors else None


def probe_testclient(*, timeout: float = CHECK_TIMEOUT) -> str | None:
    """Return the first failure; three bounded checks take at most 4.5 seconds."""
    for name, check in (
        ('socket.socketpair()', _socketpair),
        ('socketpair send/receive', _socketpair_wakeup),
        ('anyio portal start/call/stop', _portal),
    ):
        failure = _bounded_check(name, check, timeout)
        if failure is not None:
            return failure
    return None
