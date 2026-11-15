"""Synthetic process-lifetime evidence for browser renditions."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from case_intelligence import pilot_uploads
from case_intelligence.media_playback import BrowserPlaybackCoordinator
from case_intelligence.pilot_uploads import PilotStore


def _wait(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "condition did not become true within its bound"


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _writers(directory):
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if arguments and arguments[0] == b"/usr/bin/ffmpeg" and any(
            str(directory).encode() in value for value in arguments
        ):
            found.append(int(entry.name))
    return found


def _store(root):
    source = root / "synthetic.mov"
    subprocess.run([
        "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=25:duration=1", "-c:v", "mpeg4",
        str(source),
    ], check=True, timeout=10)
    store = PilotStore(root / "runtime", malware_scan_mode="disabled")
    with source.open("rb") as stream:
        document, _ = store.store_stream(
            source.name, "video/quicktime", stream, defer_processing=True,
        )
    return store, document


def _slow_command(method, source, destination, maximum):
    return [
        "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-re", "-stream_loop", "-1",
        "-i", str(source), "-c:v", "mpeg4", "-q:v", "2", "-threads", "1",
        "-fs", str(maximum), "-f", "mp4", str(destination),
    ]


def _application(root, jobs):
    store, document = _store(root)
    matter = SimpleNamespace(matter_id="synthetic")
    actual = pilot_uploads._browser_playback_command
    calls = []
    actual_popen = subprocess.Popen

    def popen(arguments, **kwargs):
        process = actual_popen(arguments, **kwargs)
        if arguments[0] == "/usr/bin/ffmpeg" or "-I" in arguments:
            poll = process.poll

            def observed_poll():
                with (root / "wait-owners").open("a") as output:
                    output.write(f"{threading.get_ident()}\n")
                return poll()

            process.poll = observed_poll
        return process

    subprocess.Popen = popen

    def command(*args):
        calls.append(threading.get_ident())
        (root / "owners").write_text("\n".join(map(str, calls)))
        return actual(*args) if len(calls) < jobs else _slow_command(*args)

    pilot_uploads._browser_playback_command = command
    coordinator = BrowserPlaybackCoordinator(
        resolve_matter=lambda _: matter, resolve_store=lambda _: store,
        reserve_capacity=lambda *args: None, release_capacity=lambda *args: None,
        admit_matter=lambda _: True,
    )
    coordinator.ensure(matter, document)
    if jobs == 2:
        _wait(lambda: store.get(document.document_id).playback_state == "ready")
        coordinator.ensure(matter, store.get(document.document_id), retry=True)
    threading.Event().wait(25)


@pytest.mark.parametrize("jobs", [1, 2])
def test_application_death_stops_playback(tmp_path, jobs):
    application = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path), str(jobs)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    writers = []
    try:
        derived = tmp_path / "runtime" / "derived"
        owners_path = tmp_path / "owners"
        _wait(lambda: owners_path.exists() and len(owners_path.read_text().splitlines()) == jobs, 10)
        _wait(lambda: list(derived.glob(".*.tmp.mp4")), 5)
        temporary = next(derived.glob(".*.tmp.mp4"))
        initial = temporary.stat().st_size
        _wait(lambda: temporary.stat().st_size > max(initial, 262144), 8)
        writers = _writers(derived)
        assert len(writers) == 1
        owners = (tmp_path / "owners").read_text().splitlines()
        assert len(owners) == jobs and len(set(owners)) == 1
        assert set((tmp_path / "wait-owners").read_text().splitlines()) == set(owners)
        application.kill()
        assert application.wait(timeout=2) == -signal.SIGKILL
        _wait(lambda: not any(_alive(pid) for pid in writers))
        size = temporary.stat().st_size
        time.sleep(2)
        assert temporary.stat().st_size == size
        assert not _writers(derived)
    finally:
        if application.poll() is None:
            application.kill()
        application.wait(timeout=2)
        for pid in writers or _writers(tmp_path / "runtime" / "derived"):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        application.stderr.close()


def _prepare(store, document, cancelled=lambda: False):
    return store.prepare_browser_playback(
        document.document_id, document.version_id,
        maximum_output_bytes=store.playback_reservation_bytes(document.document_id),
        cancelled=cancelled, force_full=True,
    )


def test_normal_conversion_has_only_ready_output(tmp_path, monkeypatch):
    from case_intelligence import playback_process_helper as helper

    store, document = _store(tmp_path)
    before = set(store.derived.iterdir())
    launched = []
    owners = []
    actual = subprocess.Popen

    def popen(arguments, **kwargs):
        process = actual(arguments, **kwargs)
        if "-I" in arguments:
            launched.append((arguments, kwargs, process))
            owners.append(threading.get_ident())
            poll = process.poll

            def observed_poll():
                owners.append(threading.get_ident())
                return poll()

            process.poll = observed_poll
        return process

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    result = _prepare(store, document)
    assert result.playback_state == "ready"
    assert set(store.derived.iterdir()) == before | {store.derived / result.playback_name}
    assert len(launched) == 1
    arguments, options, process = launched[0]
    assert arguments[:4] == [sys.executable, "-I", "-S", helper.__file__]
    assert arguments[4] == "/usr/bin/ffmpeg"
    assert options["start_new_session"] is True
    assert options["env"]["PLAYBACK_PARENT_PID"] == str(os.getpid())
    assert process.poll() == 0 and not _alive(process.pid)
    assert len(set(owners)) == 1


def test_cancel_stops_group_and_requeues(tmp_path, monkeypatch):
    from case_intelligence import playback_process_helper as helper

    store, document = _store(tmp_path)
    monkeypatch.setattr(pilot_uploads, "_browser_playback_command", _slow_command)
    signals = []
    actual = os.killpg

    def killpg(pid, sig):
        signals.append((pid, sig))
        actual(pid, sig)

    monkeypatch.setattr(helper.os, "killpg", killpg)
    started = time.monotonic()
    result = _prepare(store, document, lambda: bool(_writers(store.derived)))
    assert time.monotonic() - started < 5
    assert result.playback_state == "queued"
    assert result.playback_message == "Browser playback preparation will resume."
    assert signals and signals[0][1] == signal.SIGTERM
    assert not _writers(store.derived)
    assert not list(store.derived.glob(".*.tmp.mp4"))


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_stuck_child_retains_reservation_and_blocks_queue(tmp_path, monkeypatch, cleanup_failure):
    from case_intelligence import playback_process_helper as helper

    store, document = _store(tmp_path)
    with (tmp_path / "synthetic.mov").open("rb") as stream:
        second, _ = store.store_stream(
            "second.mov", "video/quicktime", stream, defer_processing=True,
        )
    started = threading.Event()
    proceed = threading.Event()
    launched, reservations, releases, raised, signals = [], [], [], [], []
    original_popen = subprocess.Popen
    original_stuck = pilot_uploads.PlaybackChildStuck

    class CountedStuck(original_stuck):
        def __init__(self, message):
            raised.append(self)
            super().__init__(message)

    class Unreaped:
        pid = 1000000000
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout):
            assert timeout == 2
            raise subprocess.TimeoutExpired("synthetic conversion", timeout)

    def popen(arguments, **kwargs):
        if "-I" not in arguments:
            return original_popen(arguments, **kwargs)
        launched.append(arguments)
        Path(arguments[-1]).write_bytes(b"synthetic partial rendition")
        started.set()
        assert proceed.wait(5)
        return Unreaped()

    def cancelled(*args):
        if started.is_set() and cleanup_failure:
            raise RuntimeError("synthetic interruption")
        return started.is_set()

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    monkeypatch.setattr(helper.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(pilot_uploads, "PlaybackChildStuck", CountedStuck)
    matter = SimpleNamespace(matter_id="synthetic")
    coordinator = BrowserPlaybackCoordinator(
        resolve_matter=lambda _: matter, resolve_store=lambda _: store,
        reserve_capacity=lambda *args: reservations.append(args),
        release_capacity=lambda *args: releases.append(args), admit_matter=lambda _: True,
    )
    monkeypatch.setattr(coordinator, "_cancelled", cancelled)
    try:
        coordinator.ensure(matter, document)
        assert started.wait(5)
        coordinator.ensure(matter, second)
        proceed.set()
        _wait(lambda: store.get(second.document_id).playback_state == "failed")
        assert len(raised) == 1 and isinstance(raised[0], original_stuck)
        assert len(launched) == len(reservations) == 1 and releases == []
        assert [sig for _, sig in signals] == [signal.SIGTERM, signal.SIGKILL]
        for item in (document, second):
            failed = store.get(item.document_id)
            assert failed.playback_state == "failed"
            assert failed.playback_message == helper.STUCK_MESSAGE
        assert len(list(store.derived.glob(".*.tmp.mp4"))) == 1
        assert coordinator.has_matter_work(matter.matter_id)
        assert not coordinator.cancel_document(matter.matter_id, document.document_id)
        assert not coordinator.cancel_matter(matter.matter_id)
        assert coordinator._thread.is_alive()
    finally:
        proceed.set()
        coordinator.close()
    assert releases == []


@pytest.mark.parametrize("arguments", [[], ["relative"], ["/missing-conversion-command"]])
def test_helper_rejects_invalid_command(arguments):
    from case_intelligence import playback_process_helper as helper

    result = subprocess.run(
        [sys.executable, "-I", "-S", helper.__file__, *arguments],
        env={"PLAYBACK_PARENT_PID": str(os.getpid())}, timeout=3, check=False,
    )
    assert result.returncode == 125


@pytest.mark.parametrize("failure", ["platform", "prctl", "parent"])
def test_helper_protection_failures_start_nothing(monkeypatch, failure):
    from case_intelligence import playback_process_helper as helper

    executed = []
    monkeypatch.setenv("PLAYBACK_PARENT_PID", str(os.getppid()))
    monkeypatch.setattr(helper.os, "execv", lambda *args: executed.append(args))
    if failure == "platform":
        monkeypatch.setattr(helper.sys, "platform", "unsupported")
    else:
        def prctl(*args):
            assert args == (1, signal.SIGKILL, 0, 0, 0)
            if failure == "parent":
                monkeypatch.setattr(helper.os, "getppid", lambda: 1)
            return -1 if failure == "prctl" else 0

        monkeypatch.setattr(helper.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=prctl))
    assert helper.main(["/usr/bin/true"]) == 125
    assert executed == []


def test_unavailable_protection_retains_original(tmp_path, monkeypatch):
    from case_intelligence import playback_process_helper as helper

    store, document = _store(tmp_path)
    original = store.source_path(document.document_id).read_bytes()
    actual = subprocess.Popen
    children = []

    def popen(arguments, **kwargs):
        if "-I" in arguments:
            arguments = [sys.executable, str(Path(__file__).resolve()), "unsupported", str(tmp_path / "executed")]
            kwargs["env"] = dict(os.environ)
            process = actual(arguments, **kwargs)
            children.append(process)
            return process
        return actual(arguments, **kwargs)

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    result = _prepare(store, document)
    assert len(children) == 1 and children[0].returncode == 125
    assert not (tmp_path / "executed").exists()
    assert result.playback_state == "failed"
    assert result.playback_message == helper.PROTECTION_MESSAGE
    path, media_type, size, compatible = store.browser_playback_source(document.document_id)
    assert path.read_bytes() == original and size == len(original)
    assert media_type == "video/quicktime" and compatible is False
    assert not list(store.derived.glob(".*.tmp.mp4"))


@pytest.mark.parametrize("method,codec_arguments", [
    ("remux", ["-c:v", "copy", "-c:a", "copy"]),
    ("audio_only", ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]),
    ("full_transcode", [
        "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-maxrate", "6000k",
        "-bufsize", "12000k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
    ]),
])
def test_conversion_arguments_unchanged(method, codec_arguments):
    assert pilot_uploads._browser_playback_command(
        method, Path("source.mov"), Path("rendition.mp4"), 123456,
    ) == [
        "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", "source.mov",
        "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
        *codec_arguments, "-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
        "-threads", "2", "-fs", "123456", "-f", "mp4", "rendition.mp4",
    ]


def test_parent_exits_before_signal_is_armed(tmp_path):
    parent = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "race-parent", str(tmp_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child = None
    try:
        assert parent.wait(timeout=5) == 0
        child = int((tmp_path / "child").read_text())
        (tmp_path / "arm").touch()
        result = tmp_path / "result"
        _wait(lambda: result.exists() and result.read_text() == "125")
        _wait(lambda: not _alive(child))
        assert not (tmp_path / "executed").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=2)
        if child is not None and _alive(child):
            os.kill(child, signal.SIGKILL)


if __name__ == "__main__":
    if sys.argv[1] == "unsupported":
        from case_intelligence import playback_process_helper

        playback_process_helper.sys.platform = "unsupported"
        os.environ["PLAYBACK_PARENT_PID"] = str(os.getppid())
        raise SystemExit(playback_process_helper.main(["/usr/bin/touch", sys.argv[2]]))
    elif sys.argv[1] == "race-parent":
        root = Path(sys.argv[2])
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "race-child", str(root)],
            env={**os.environ, "PLAYBACK_PARENT_PID": str(os.getpid())},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        (root / "child").write_text(str(child.pid))
    elif sys.argv[1] == "race-child":
        from case_intelligence import playback_process_helper

        root = Path(sys.argv[2])
        _wait(lambda: (root / "arm").exists())
        result = playback_process_helper.main(["/usr/bin/touch", str(root / "executed")])
        (root / "result").write_text(str(result))
    else:
        _application(Path(sys.argv[1]), int(sys.argv[2]))
