"""Bind a browser conversion to its creating thread and stop its process group."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Callable

PR_SET_PDEATHSIG = 1

STUCK_MESSAGE = (
    "The original video is retained, but the playback conversion process could not "
    "be stopped. Playback preparation is unavailable until restart. [PlaybackChildStuck]"
)
PROTECTION_MESSAGE = (
    "The original video is retained, but a browser-compatible copy could not be "
    "prepared because child lifetime protection is unavailable. [PlaybackProtectionUnavailable]"
)


class PlaybackProtectionUnavailable(RuntimeError):
    """The isolated child could not establish lifetime protection."""


def _arm_parent_death(expected_parent: int) -> None:
    if sys.platform != "linux" or expected_parent <= 1:
        raise OSError("parent-death protection unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "parent-death protection unavailable")
    if os.getppid() != expected_parent:
        raise OSError("conversion owner exited")


def main(arguments: list[str]) -> int:
    try:
        if not arguments or not os.path.isabs(arguments[0]):
            return 125
        _arm_parent_death(int(os.environ["PLAYBACK_PARENT_PID"]))
        if os.getsid(0) != os.getpid():
            os.setsid()
        os.execv(arguments[0], arguments)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return 125
    return 125


class PlaybackChild:
    """Own launch, bounded stopping and temporary output until the child exits."""

    def __init__(
        self, temporary: Path, stuck_error: type[Exception], failed: Callable[[str], object]
    ) -> None:
        self.temporary = temporary
        self.stuck_error = stuck_error
        self.failed = failed
        self.process: subprocess.Popen[bytes] | None = None
        self.stuck = False

    def start(self, command: list[str]) -> subprocess.Popen[bytes]:
        self.stop()
        helper_path = Path(__file__).parent / "playback_process_helper.py"
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", str(helper_path), *command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "PLAYBACK_PARENT_PID": str(os.getpid()),
            },
        )
        return self.process

    def stop(self) -> None:
        if self.stuck:
            return
        process = self.process
        if process is None or process.poll() is not None:
            return
        for stop_signal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, stop_signal)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
                return
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is not None:
                    return
        self.stuck = True
        try:
            self.failed(STUCK_MESSAGE)
        finally:
            raise self.stuck_error(STUCK_MESSAGE)

    def close(self) -> None:
        if not self.stuck:
            self.stop()
            self.temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
