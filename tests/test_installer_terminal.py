"""Synthetic console/subprocess contracts; these are not Linux install acceptance."""
from __future__ import annotations

import io
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
    return output


@pytest.mark.parametrize("mode", ["NO_COLOR", "CI", "non-tty", "--no-color", "--plain"])
@pytest.mark.parametrize("env_value", ["", "1", "0"])
def test_plain_cli_has_no_escapes_redraws_or_sleep(tmp_path, monkeypatch, tty, mode, env_value):
    monkeypatch.setattr(sys, "stdout", tty)
    flags = []
    if mode.startswith("--"):
        flags.append(mode)
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
        "cyan": 45, "green": 84, "amber": 214, "black": 233, "white": 254, "muted": 8,
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


def test_failed_build_main_still_exits_one(synthetic_docker, monkeypatch, capsys):
    root, command = synthetic_docker
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    monkeypatch.setattr(installer, "_doctor", lambda console, *_: installer._run(console, command))
    monkeypatch.setattr(sys, "argv", ["install", "doctor", "--root", str(root), "--plain"])
    assert installer.main() == 1
    assert "BOOT SEQUENCE HALTED" in capsys.readouterr().out


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
def test_unsafe_logs_refuse_before_child(synthetic_docker, monkeypatch, unsafe):
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
        installer._run(installer.Console(color=False), command)
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
