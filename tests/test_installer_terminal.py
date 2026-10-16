"""Synthetic console/subprocess contracts; these are not Linux install acceptance."""
from __future__ import annotations

import io
import errno
from contextlib import contextmanager
from types import SimpleNamespace
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from tests.test_oss_installer import installer
import installer_terminal as terminal


@pytest.fixture
def tty(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(output, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    return output


@pytest.mark.parametrize("mode", ["NO_COLOR", "CI", "non-tty", "--no-color", "--plain", "TERM=dumb"])
@pytest.mark.parametrize("env_value", ["", "1", "0"])
def test_plain_cli_has_no_escapes_redraws_or_sleep(tmp_path, monkeypatch, tty, mode, env_value):
    monkeypatch.setattr(sys, "stdout", tty)
    flags = []
    if mode.startswith("--"):
        flags.append(mode)
    elif mode == "TERM=dumb":
        monkeypatch.setenv("TERM", "dumb")
    elif mode == "non-tty":
        monkeypatch.setattr(tty, "isatty", lambda: False)
    else:
        monkeypatch.setenv(mode, env_value)
    monkeypatch.setattr(terminal.time, "sleep", lambda _: pytest.fail("plain output slept"))
    monkeypatch.setattr(terminal.threading, "Thread", lambda **_: pytest.fail("plain output animated"))
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    def doctor(console, *_):
        console.phase(1, "HOST HANDSHAKE", "Synthetic checks")
        console.ok("Synthetic check")
        console.note("Synthetic note")
        console.status("BLOCK", "Synthetic block")
        with console.progress("Synthetic work"):
            pass
    monkeypatch.setattr(installer, "_doctor", doctor)
    monkeypatch.setattr(installer, "_handoff", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["install", "doctor", "--root", str(tmp_path / "node"), *flags])
    start = time.monotonic()
    assert installer.main() == 0
    assert time.monotonic() - start < 1
    output = tty.getvalue()
    assert "\x1b" not in output and "\r" not in output
    for word in ("EVIDENCE NODE PROVISIONER", "PHASE 01", "OK", "NOTE", "BLOCK"):
        assert word in output
    assert not (tmp_path / "node").exists()


@pytest.mark.parametrize("colorterm,truecolor", [("truecolor", True), ("24bit", True), ("", False), ("256color", False)])
def test_palette_selection(monkeypatch, tty, colorterm, truecolor):
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setenv("COLORTERM", colorterm)
    console = terminal.Console(color=True)
    for name, value in terminal.BRAND.items():
        output = console.paint("synthetic", getattr(terminal.C, name))
        code = ("38;2;" + ";".join(str(part) for part in bytes.fromhex(value)) if truecolor
                else f"38;5;{terminal.nearest_xterm(value)}")
        assert f"\x1b[{code}m" in output
        assert "48;" not in output


def test_nearest_xterm_all_brand_colors():
    assert {name: terminal.nearest_xterm(value) for name, value in terminal.BRAND.items()} == {
        "cyan": 45, "green": 84, "amber": 214, "black": 233, "white": 254, "muted": 244,
    }


def test_wordmark_keeps_fixed_cells_and_vertical_stems_aligned():
    # Regression: the former joined artwork shifted U/L/P upper strokes by one.
    for glyph in terminal._WORDMARK_GLYPHS.values():
        assert len(glyph) == 5
        assert {len(row) for row in glyph} == {7}
    rows = terminal.WORDMARK.splitlines()
    assert len(rows) == 5 and {len(row) for row in rows} == {73}
    for index in (3, 4, 5):  # U, L, P in EXCULPATA
        stem = 2 + index * 8
        assert all(rows[row][stem] == "|" for row in (1, 2, 3))
    # Both A cells must be identical, including their baseline.
    assert all(row[50:57] == row[66:73] for row in rows)


def test_typing_budget_covers_banner_and_all_phases(monkeypatch, tty):
    monkeypatch.setattr(sys, "stdout", tty)
    delays = []
    monkeypatch.setattr(terminal.time, "sleep", delays.append)
    console = terminal.Console(color=True, animation=True)
    console.banner("install")
    for number in range(1, 9):
        console.phase(number, "SYNTHETIC PHASE TITLE", "Detail")
    assert 0 < sum(delays) <= 0.451
    assert len(delays) < 60
    assert all(len(terminal.terminal_text(line)) <= 80 for line in tty.getvalue().splitlines())


def test_no_animation_retains_color_without_delay(monkeypatch, tty):
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(terminal.time, "sleep", lambda _: pytest.fail("animation disabled"))
    console = terminal.Console(color=True, animation=False)
    console.banner("install")
    with console.progress("Synthetic work"):
        pass
    assert "\x1b" in tty.getvalue() and "\r" not in tty.getvalue()


def test_quiet_console_never_sleeps_or_starts_progress(monkeypatch, tty):
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(terminal.time, "sleep", lambda _: pytest.fail("quiet slept"))
    console = terminal.Console(color=True, quiet=True, animation=True)
    console.banner("install")
    console.phase(1, "SYNTHETIC", "detail")
    with console.progress("work"):
        pass
    assert tty.getvalue() == ""


@pytest.fixture
def synthetic_docker(tmp_path, monkeypatch):
    # Execute an actual bounded child, never an engine or a simulated install.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(f"#!{sys.executable}\nimport os, sys\n"
                      "for n in range(55): print(f'synthetic build line {n:02d}', flush=True)\n"
                      "print('synthetic final diagnostic' if os.environ.get('COMPOSE_ANSI') == 'never' else '\\x1b[31msynthetic final diagnostic\\x1b[0m', file=sys.stderr, flush=True)\n"
                      "sys.exit(int(os.environ.get('SYNTHETIC_EXIT', '17')))\n")
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    root = tmp_path / "synthetic-node"
    root.mkdir(mode=0o700)
    (root / "state").mkdir(mode=0o700)
    (root / "compose.env").write_text("# synthetic coordinates only\n")
    return root, ["docker", "compose", "--env-file", str(root / "compose.env"), "build", "app"]


def test_failed_child_has_private_full_log_tail_and_original_status(synthetic_docker, capsys):
    root, command = synthetic_docker
    with pytest.raises(subprocess.CalledProcessError) as caught:
        installer._run(installer.Console(color=False), command)
    assert caught.value.returncode == 17
    logs = list((root / "state").glob("installer-build-*.log"))
    assert len(logs) == 1
    assert stat.S_IMODE(logs[0].stat().st_mode) == 0o600
    log = logs[0].read_text()
    assert "synthetic build line 00" in log and "synthetic final diagnostic" in log
    output = capsys.readouterr().out
    assert str(logs[0]) in output and "synthetic final diagnostic" in output
    assert "synthetic build line 00" not in output
    assert "synthetic build line 15" not in output and "synthetic build line 16" in output
    assert "\x1b" not in output and "\r" not in output


def test_nonchecking_failure_and_success_preserve_return_codes(synthetic_docker, monkeypatch):
    root, command = synthetic_docker
    assert installer._run(installer.Console(color=False, quiet=True), command, check=False).returncode == 17
    monkeypatch.setenv("SYNTHETIC_EXIT", "0")
    assert installer._run(installer.Console(color=False, quiet=True), command).returncode == 0
    assert len(list((root / "state").glob("*.log"))) == 2


@pytest.mark.parametrize("quiet", [False, True])
def test_failed_build_main_still_exits_one(synthetic_docker, monkeypatch, capsys, quiet):
    root, command = synthetic_docker
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    monkeypatch.setattr(installer, "_doctor", lambda console, *_: installer._run(console, command))
    monkeypatch.setattr(sys, "argv", ["install", "doctor", "--root", str(root), "--plain", *(["--quiet"] if quiet else [])])
    assert installer.main() == 1
    output = capsys.readouterr().out
    assert "Docker build failed (exit 17)" in output
    assert "synthetic final diagnostic" in output and "Full Docker output" in output
    if not quiet:
        assert "BOOT SEQUENCE HALTED" in output


@pytest.mark.parametrize("mode", ["dry-run", "capture", "credential", "license-plan"])
def test_nonlogged_commands_preserve_contract(synthetic_docker, monkeypatch, capsys, mode):
    root, command = synthetic_docker
    options = {"check": False}
    if mode == "dry-run":
        options["dry_run"] = True
    elif mode == "capture":
        options["capture"] = True
    elif mode == "credential":
        command[-2:] = ["run", "--rm", "-T", "account-admin", "accounts", "init", "--password-stdin"]
        options["input_value"] = "synthetic-hidden-password\n"
    else:
        command[-2:] = ["run", "--rm", "-T", "model-stager", "plan"]
    result = installer._run(installer.Console(color=False), command, **options)
    assert not list((root / "state").iterdir())
    if mode == "capture":
        assert "synthetic build line 00" in result.stdout
    if mode == "dry-run":
        assert result.returncode == 0
    assert "synthetic-hidden-password" not in capsys.readouterr().out


@pytest.mark.parametrize("unsafe", ["state-symlink", "parent-symlink", "permissions", "checkout"])
def test_unsafe_log_creation_remains_refused(synthetic_docker, monkeypatch, unsafe):
    root, command = synthetic_docker
    if unsafe == "state-symlink":
        (root / "state").rmdir()
        target = root / "other"
        target.mkdir(mode=0o700)
        (root / "state").symlink_to(target, target_is_directory=True)
    elif unsafe == "parent-symlink":
        link = root.parent / "alias"
        link.symlink_to(root, target_is_directory=True)
        command[3] = str(link / "compose.env")
    elif unsafe == "permissions":
        (root / "state").chmod(0o755)
    else:
        monkeypatch.setattr(installer, "PROJECT", root)
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: pytest.fail("unsafe logger ran child"))
    with pytest.raises((RuntimeError, OSError)):
        with installer._build_log(Path(command[3]).parent, "build"):
            pytest.fail("unsafe logger yielded")
    assert not list((root / "state").iterdir())


@pytest.mark.parametrize("generation,username,display", [
    ("exculpata", "exculpata.admin", "Exculpata Administrator"),
    ("recordbench", "recordbench.admin", "RecordBench Administrator"),
])
def test_admin_defaults_follow_detected_generation(tmp_path, monkeypatch, generation, username, display):
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    root = tmp_path / "node"
    if generation == "recordbench":
        (root / "config").mkdir(parents=True)
        (root / "config/recordbench.env").write_text("# synthetic legacy node\n")
    args = installer._parser().parse_args(["--root", str(root), "--auth", "local", "--non-interactive"])
    assert installer._select_names(args, root) == generation
    installer._collect_identity_choices(args)
    assert (args.admin_username, args.admin_display_name) == (username, display)
    args.admin_username, args.admin_display_name = "synthetic.custom", "Synthetic Administrator"
    installer._collect_identity_choices(args)
    assert (args.admin_username, args.admin_display_name) == ("synthetic.custom", "Synthetic Administrator")


def test_json_remains_one_unstyled_document(tmp_path, monkeypatch, tty):
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    monkeypatch.setattr(installer, "_collect_preflight", lambda *a, **k: installer.PreflightResult(()))
    monkeypatch.setattr(terminal.time, "sleep", lambda _: pytest.fail("JSON slept"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(tmp_path / "node"), "--json"])
    assert installer.main() == 0
    assert json.loads(tty.getvalue()) == {"schema_version": 1, "ready": True, "checks": []}


@pytest.mark.parametrize("options", [["--plain"], ["--no-color"], []])
def test_plain_mode_controls_uncaptured_compose_output(synthetic_docker, monkeypatch, capfd, options):
    root, command = synthetic_docker
    command[-2:] = ["ps"]
    monkeypatch.setenv("COMPOSE_ANSI", "always")
    monkeypatch.setenv("COMPOSE_PROGRESS", "tty")
    monkeypatch.setenv("BUILDKIT_PROGRESS", "tty")
    monkeypatch.setenv("SYNTHETIC_EXIT", "0")
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    # Use the real Console/main flag boundary and actual inherited child FDs.
    monkeypatch.setattr(installer, "_doctor", lambda console, *_: installer._run(console, command))
    monkeypatch.setattr(installer, "_handoff", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["install", "doctor", "--root", str(root), *options])
    assert installer.main() == 0
    captured = capfd.readouterr()
    output = captured.out + captured.err
    assert "synthetic final diagnostic" in output
    assert "\x1b" not in output and "\r" not in output
    assert not list((root / "state").iterdir())


def test_logged_child_controls_are_removed_from_failure_tail(synthetic_docker, monkeypatch, capsys):
    root, command = synthetic_docker
    # A failing tool can ignore color preferences; tail rendering still cannot
    # execute OSC hyperlinks, SGR, cursor controls, or carriage-return redraws.
    docker = Path(os.environ["PATH"].split(os.pathsep)[0]) / "docker"
    docker.write_text(f"#!{sys.executable}\nimport sys\n"
                      "print('\\x1b]8;;https://example.test\\x07synthetic\\x1b]8;;\\x07\\x1b[31m error\\x1b[0m\\rline')\n"
                      "sys.exit(17)\n")
    installer._run(installer.Console(color=False), command, check=False)
    output = capsys.readouterr().out
    assert "synthetic error" in output
    assert "\x1b" not in output and "\r" not in output
    assert "https://example.test" not in output


@pytest.mark.parametrize("check", [True, False])
def test_quiet_failure_still_prints_warning_tail_and_private_log(synthetic_docker, capsys, check):
    root, command = synthetic_docker
    console = installer.Console(color=False, quiet=True)
    if check:
        with pytest.raises(subprocess.CalledProcessError) as error:
            installer._run(console, command)
        assert error.value.returncode == 17
    else:
        assert installer._run(console, command, check=False).returncode == 17
    output = capsys.readouterr().out
    log = next((root / "state").glob("*.log"))
    assert "WARN" in output and "exit 17" in output
    assert "synthetic final diagnostic" in output and str(log) in output
    assert "synthetic build line 00" not in output
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


@pytest.mark.parametrize("action", ["build", "up", "start", "stop"])
@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EROFS])
def test_log_creation_failure_does_not_block_recovery(synthetic_docker, monkeypatch, capfd, action, error_number):
    root, command = synthetic_docker
    command[-2] = action
    monkeypatch.setenv("SYNTHETIC_EXIT", "0")
    original_open = installer.os.open
    def no_log(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and str(path).startswith("installer-"):
            raise OSError(error_number, os.strerror(error_number))
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(installer.os, "open", no_log)
    assert installer._run(installer.Console(color=False, quiet=True), command).returncode == 0
    output = capfd.readouterr()
    assert "NOTE" in output.out and "without a retained log" in output.out
    assert output.out.count("synthetic build line 00") == 1
    assert "synthetic final diagnostic" in output.err
    assert not list((root / "state").iterdir())


@pytest.mark.parametrize("unsafe", ["permissions", "owner", "state-symlink", "parent-symlink", "checkout"])
def test_unsafe_log_falls_back_without_changing_filesystem(synthetic_docker, monkeypatch, capfd, unsafe):
    root, command = synthetic_docker
    state = root / "state"
    if unsafe == "permissions":
        state.chmod(0o755)
    elif unsafe == "owner":
        inode = state.stat().st_ino
        original_fstat = installer.os.fstat
        def other_owner(fd):
            result = original_fstat(fd)
            if result.st_ino == inode:
                return SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=result.st_mode)
            return result
        monkeypatch.setattr(installer.os, "fstat", other_owner)
    elif unsafe == "state-symlink":
        state.rmdir()
        target = root / "other"
        target.mkdir(mode=0o700)
        state.symlink_to(target, target_is_directory=True)
    elif unsafe == "parent-symlink":
        alias = root.parent / "alias"
        alias.symlink_to(root, target_is_directory=True)
        command[3] = str(alias / "compose.env")
    else:
        monkeypatch.setattr(installer, "PROJECT", root)
    before = state.lstat()
    with pytest.raises(subprocess.CalledProcessError) as error:
        installer._run(installer.Console(color=False, quiet=True), command)
    assert error.value.returncode == 17
    output = capfd.readouterr()
    assert "NOTE" in output.out and "WARN" in output.out and "exit 17" in output.out
    assert "synthetic build line 00" in output.out and "synthetic final diagnostic" in output.err
    assert not list(state.iterdir())
    assert (state.lstat().st_mode, state.lstat().st_uid) == (before.st_mode, before.st_uid)


def test_started_child_oserror_is_never_retried(synthetic_docker, monkeypatch, capsys):
    _, command = synthetic_docker
    calls = []
    def child(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError(errno.EIO, "synthetic child launch failure")
    monkeypatch.setattr(installer.subprocess, "run", child)
    with pytest.raises(OSError):
        installer._run(installer.Console(color=False), command)
    assert len(calls) == 1
    assert "streaming output directly" not in capsys.readouterr().out


def test_log_read_failure_keeps_original_child_exit(synthetic_docker, monkeypatch, capsys):
    _, command = synthetic_docker
    original = installer._build_log
    @contextmanager
    def unreadable(*args):
        with original(*args) as (path, stream):
            class Unreadable:
                def fileno(self):
                    return stream.fileno()
                def seek(self, *args):
                    raise OSError(errno.EIO, "synthetic read failure")
            yield path, Unreadable()
    monkeypatch.setattr(installer, "_build_log", unreadable)
    with pytest.raises(subprocess.CalledProcessError) as error:
        installer._run(installer.Console(color=False, quiet=True), command)
    assert error.value.returncode == 17
    output = capsys.readouterr().out
    assert "Could not read" in output and "Full Docker output" in output


@pytest.mark.parametrize("width", [1, 8, 9, 10, 12, 20, 40, 72, 73, 80])
def test_banner_falls_back_below_wordmark_width(monkeypatch, tty, width):
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(terminal, "terminal_width", lambda: width)
    terminal.Console(color=False).banner("install")
    output = tty.getvalue()
    if width < 73:
        assert output.splitlines()[1] == ("  EXCULPATA" if width >= 11 else "EXCULPATA"[:width])
        assert len(output.splitlines()[1]) <= width
        assert terminal.WORDMARK not in output
    else:
        assert terminal.WORDMARK in output


@pytest.mark.parametrize("width", [1, 12, 20, 40, 80, (80, 40, 12), (12, 40, 80)])
def test_spinner_frames_fit_cells_and_clear_without_blank_line(monkeypatch, tty, width):
    monkeypatch.setattr(sys, "stdout", tty)
    widths = (width,) * 3 if isinstance(width, int) else width
    sizes = iter(widths)
    monkeypatch.setattr(terminal, "terminal_width", lambda: next(sizes))
    class ClockedEvent:
        calls = 0
        def wait(self, _):
            self.calls += 1
            return self.calls > 3
        def set(self):
            pass
    class ImmediateThread:
        def __init__(self, target, **_):
            self.target = target
        def start(self):
            self.target()
        def join(self):
            pass
    monkeypatch.setattr(terminal.threading, "Event", ClockedEvent)
    monkeypatch.setattr(terminal.threading, "Thread", ImmediateThread)
    with terminal.Console(color=True, animation=True).progress("synthetic\t界e\u0301\x1b[31m" * 20):
        pass
    frames = tty.getvalue().split("\r")[1:]
    assert len(frames) == 4
    for frame, width in zip(frames[:-1], widths):
        assert "\n" not in frame
        visible = terminal.terminal_text(frame)
        cells = sum(0 if terminal.unicodedata.combining(c) else 2 if terminal.unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in visible)
        assert cells < width
    assert frames[-1] == "\x1b[2K"


def test_width_uses_real_stdout_instead_of_stale_columns(monkeypatch, tty):
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(tty, "fileno", lambda: 123)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(terminal.os, "get_terminal_size", lambda fd: os.terminal_size((40, 24)) if fd == 123 else pytest.fail("wrong fd"))
    assert terminal.terminal_width() == 40


def test_large_child_output_has_no_pipe_deadlock_and_bounded_tail(synthetic_docker, monkeypatch, capsys):
    root, command = synthetic_docker
    docker = Path(os.environ["PATH"].split(os.pathsep)[0]) / "docker"
    docker.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * (2 * 1024 * 1024))\nprint('synthetic final diagnostic')\nsys.exit(17)\n")
    original_run = installer.subprocess.run
    def bounded_run(*args, **kwargs):
        assert kwargs["stdout"] != subprocess.PIPE
        return original_run(*args, **kwargs, timeout=5)
    monkeypatch.setattr(installer.subprocess, "run", bounded_run)
    assert installer._run(installer.Console(color=False, quiet=True), command, check=False).returncode == 17
    output = capsys.readouterr().out
    assert "synthetic final diagnostic" in output and len(output) < 67000
    assert next((root / "state").glob("*.log")).stat().st_size > 2 * 1024 * 1024


def test_docker_activity_uses_reported_counts_and_skips_percentages():
    activity = terminal.DockerActivity(("app", "gateway"))
    assert activity.summary() == "images built 0 of 2"
    assert activity.observe("#3 [app 2/7] RUN apt-get update") == (
        "images built 0 of 2; app step 2 of 7; RUN apt-get update"
    )
    assert activity.observe("abcdef123456: Downloading [=====>] 50%") is None
    assert "%" not in activity.summary()
    assert activity.observe("Image app Built") == (
        "images built 1 of 2; app step 2 of 7; RUN apt-get update"
    )
    assert activity.observe("[+] Building 4.0s (8/15)") == (
        "images built 1 of 2; build steps 8 of 15; app step 2 of 7; RUN apt-get update"
    )
    assert activity.observe("gateway | #4 [gateway 1/3] RUN true") == (
        "images built 1 of 2; build steps 8 of 15; gateway step 1 of 3; RUN true"
    )
    unknown = terminal.DockerActivity(())
    assert unknown.summary() == "waiting for Docker output"
    assert unknown.observe("Container synthetic-app-1 Started") == "Container synthetic-app-1 Started"
    assert unknown.observe("Container synthetic-app-1  Waiting") == "Container synthetic-app-1 Waiting"
    assert unknown.observe("Network synthetic-net  Created") == "Network synthetic-net Created"
    assert unknown.observe("Volume synthetic-data  Created") == "Volume synthetic-data Created"
    assert "%" not in unknown.summary()


@pytest.mark.parametrize("mode", ["plain", "NO_COLOR", "CI", "TERM=dumb", "non-tty"])
@pytest.mark.parametrize("width", [80, 50])
def test_logged_build_progress_is_honest_and_fits(synthetic_docker, monkeypatch, capsys, mode, width):
    root, command = synthetic_docker
    docker = Path(os.environ["PATH"].split(os.pathsep)[0]) / "docker"
    docker.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "print('#1 [app 1/2] RUN echo synthetic-step', flush=True)\n"
        "print('synthetic-secret-marker stays in the log', flush=True)\n"
        "print('abcdef123456: Downloading [=====>] 50%', flush=True)\n"
        "print('#2 [app 2/2] RUN echo synthetic-step-two', flush=True)\n"
        "print('Image app Built', flush=True)\n"
        "raise SystemExit(0)\n"
    )
    monkeypatch.setattr(terminal, "terminal_width", lambda: width)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    if mode == "NO_COLOR":
        monkeypatch.setenv("NO_COLOR", "1")
    elif mode == "CI":
        monkeypatch.setenv("CI", "1")
    elif mode == "TERM=dumb":
        monkeypatch.setenv("TERM", "dumb")
    output = io.StringIO()
    monkeypatch.setattr(output, "isatty", lambda: mode != "non-tty")
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("SYNTHETIC_EXIT", "0")
    assert installer._run(terminal.Console(color=mode != "plain"), command).returncode == 0
    text = output.getvalue()
    assert "\x1b" not in text and "\r" not in text
    assert "images built" in text and "synthetic-secret-marker" not in text
    assert "50%" not in text and "%" not in text
    if width >= 80:
        assert "step 1 of 2" in text and "step 2 of 2" in text
        assert "images built 1 of 1" in text
    log = next((root / "state").glob("*.log")).read_text()
    assert "synthetic-secret-marker" in log and "50%" in log
    for line in text.splitlines():
        if "step " in line or "images built" in line or "waiting for Docker output" in line:
            assert terminal.cell_width(line) <= width - 1


def test_animated_progress_frames_stay_inside_50_columns(monkeypatch, tty):
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setattr(terminal, "terminal_width", lambda: 50)
    class ClockedEvent:
        calls = 0
        def wait(self, _):
            self.calls += 1
            return self.calls > 3
        def set(self):
            pass
    class ImmediateThread:
        def __init__(self, target, **_):
            self.target = target
        def start(self):
            self.target()
        def join(self):
            pass
    monkeypatch.setattr(terminal.threading, "Event", ClockedEvent)
    monkeypatch.setattr(terminal.threading, "Thread", ImmediateThread)
    status = {"elapsed": "2m 05s", "detail": "images built 1 of 4; app step 3 of 9; RUN " + "synthetic " * 20}
    with terminal.Console(color=True, animation=True).progress("Docker build", status):
        pass
    frames = tty.getvalue().split("\r")[1:]
    assert frames[-1] == "\x1b[2K"
    for frame in frames[:-1]:
        assert "\n" not in frame
        visible = terminal.terminal_text(frame)
        assert terminal.cell_width(visible) < 50
        assert "%" not in visible
