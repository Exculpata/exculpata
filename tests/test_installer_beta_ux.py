"""Password re-prompts, NODE ONLINE, and uninstall refusal. Synthetic fixtures only."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_ENV_PREFIX,
    LEGACY_STEM,
)

import io
import json
import os
from pathlib import Path
import sys

import pytest

from tests.test_oss_installer import installer


PASSWORD = "synthetic-passphrase-ok"
OTHER = "synthetic-passphrase-no"


def _tty(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: value)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


def _interactive_args(root: Path):
    args = installer._parser().parse_args([
        "install", "--root", str(root), "--auth", "local", "--models", "none",
        "--admin-username", "synthetic.admin", "--admin-display-name", "Synthetic Administrator",
    ])
    return args


def test_password_rules_reprompt_and_keep_earlier_answers(tmp_path, monkeypatch, capsys):
    asked = []

    def ask(prompt, default, *, non_interactive):
        asked.append(prompt)
        return "synthetic.admin" if "username" in prompt.casefold() else "Synthetic Administrator"

    replies = iter(["short-secret", PASSWORD, OTHER, PASSWORD, PASSWORD])
    monkeypatch.setattr(installer, "_ask", ask)
    monkeypatch.setattr(installer.getpass, "getpass", lambda prompt="": next(replies))
    _tty(monkeypatch)
    args = _interactive_args(tmp_path / "node")
    args.admin_username = None
    args.admin_display_name = None
    installer._collect_identity_choices(args)
    before = list(asked)
    assert installer._password(args) == PASSWORD
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert asked == before
    assert args.admin_username == "synthetic.admin"
    assert "Unicode code points" in output and "14" in output and "combining marks" in output
    assert "did not match" in output and "Attempt 1" in output and "Attempt 2" in output and "of 3" in output
    for secret in ("short-secret", PASSWORD, OTHER):
        assert secret not in output


def test_password_attempts_exhaust_without_echo_or_writes(tmp_path, monkeypatch, capsys):
    from tests.test_first_run_handoff import configured_node
    root, args, _paths = configured_node(tmp_path)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(installer, "_ask", lambda *a, **k: pytest.fail("re-asked"))
    monkeypatch.setattr(installer, "_preflight", lambda *a, **k: ())
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("password failure ran a command"))
    monkeypatch.setattr(installer.getpass, "getpass", lambda prompt="": "short-secret")
    _tty(monkeypatch)
    args.non_interactive = False
    args.password_stdin = False
    with pytest.raises(RuntimeError, match="after 3 attempts") as error:
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    captured = capsys.readouterr()
    output = captured.out + captured.err + str(error.value)
    assert "short-secret" not in output
    assert "Unicode code points" in output
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("value", ["short-secret\n", "synthetic-secret\x00tail\n", "x" * 1025 + "\n"])
def test_password_stdin_fails_fast_without_echo(tmp_path, monkeypatch, capsys, value):
    monkeypatch.setattr(sys, "stdin", io.StringIO(value))
    monkeypatch.setattr(installer.getpass, "getpass", lambda prompt="": pytest.fail("stdin re-prompted"))
    args = _interactive_args(tmp_path / "node")
    args.password_stdin = True
    with pytest.raises(RuntimeError, match="administrator password") as error:
        installer._password(args)
    output = capsys.readouterr().out + capsys.readouterr().err + str(error.value)
    assert "14 to 1,024" in output
    for secret in ("short-secret", "synthetic-secret", "tail"):
        assert secret not in output


def test_password_without_a_terminal_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.getpass, "getpass", lambda prompt="": pytest.fail("prompted without a terminal"))
    monkeypatch.setattr(installer, "_dev_tty_openable", lambda: False)
    _tty(monkeypatch, False)
    args = _interactive_args(tmp_path / "node")
    with pytest.raises(RuntimeError, match="--password-stdin") as error:
        installer._password(args)
    assert "14 to 1,024 Unicode code points" in str(error.value)


def test_plain_password_rules_have_no_color(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer.getpass, "getpass", lambda prompt="": PASSWORD)
    _tty(monkeypatch, True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    args = installer._parser().parse_args([
        "install", "--root", str(tmp_path / "node"), "--auth", "local", "--plain",
    ])
    assert installer._password(args) == PASSWORD
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "Unicode code points" in output


def test_password_prompt_accepts_piped_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(installer.getpass, "getpass", lambda prompt="": PASSWORD)
    args = _interactive_args(tmp_path / "node")
    assert installer._password(args) == PASSWORD


def test_spaces_count_as_unicode_code_points(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO((" " * 14) + "\n"))
    args = _interactive_args(tmp_path / "node")
    args.password_stdin = True
    assert installer._password(args) == " " * 14
    monkeypatch.setattr(sys, "stdin", io.StringIO((" " * 13) + "\n"))
    with pytest.raises(RuntimeError, match="13 Unicode code points"):
        installer._password(args)


def test_node_online_screen_hides_password_and_plain_output(tmp_path, capsys):
    from tests.test_first_run_handoff import configured_node
    root, _args, _paths = configured_node(tmp_path)
    for phase in installer.INSTALL_PHASES:
        state = "not-selected" if phase == "models" else "complete"
        installer._install_phase(root, phase, state)
    installer._handoff(installer.Console(color=False), root)
    output = capsys.readouterr().out
    assert "NODE ONLINE" in output
    assert f"https://{LEGACY_STEM}.example.test:8443/auth/login" in output
    assert "Administrator username: alice.admin" in output
    assert "./install doctor --root " in output
    assert "The administrator password is not shown" in output
    assert "\x1b" not in output and "\r" not in output
    assert PASSWORD not in output


def test_incomplete_node_does_not_claim_it_is_online(tmp_path, capsys):
    from tests.test_first_run_handoff import configured_node
    root, _args, _paths = configured_node(tmp_path)
    installer._install_phase(root, "prepared", "complete")
    installer._handoff(installer.Console(color=False), root)
    output = capsys.readouterr().out
    assert "NODE ONLINE" not in output
    assert "running: not verified" in output
    assert "--resume" in output


def _fake_docker(tmp_path: Path, project: str, namespace: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "docker-state"
    state.mkdir()
    (state / "calls").write_text("")
    script = bindir / "docker"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
state = Path(os.environ["SYNTHETIC_DOCKER_STATE"])
project = os.environ["SYNTHETIC_PROJECT"]
namespace = os.environ["SYNTHETIC_NAMESPACE"]
with (state / "calls").open("a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
joined = " ".join(args)
if args[:1] == ["ps"]:
    if project not in joined:
        raise SystemExit(1)
    print("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\tsynthetic-app-1")
elif args[:2] == ["volume", "ls"]:
    if project not in joined:
        raise SystemExit(1)
    print(project + "_postgres-data")
elif args[:2] == ["network", "ls"]:
    if project not in joined:
        raise SystemExit(1)
    print("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\tsynthetic-net")
elif args[:2] == ["image", "ls"]:
    print("cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\\t" + namespace + "/app:synthetic")
    print("dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\\tother/app:synthetic")
    print("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\\t" + namespace + "/gateway:synthetic")
elif args[:1] == ["inspect"] or (len(args) >= 2 and args[1] == "inspect" and args[0] != "image"):
    print(project)
elif args[:2] == ["image", "inspect"]:
    if args[-1].startswith("e"):
        print(json.dumps([namespace + "/gateway:synthetic", "other/gateway:synthetic"]))
    else:
        print(json.dumps([namespace + "/app:synthetic"]))
elif args[:2] == ["image", "rm"] or args[:2] == ["volume", "rm"] or args[:2] == ["network", "rm"] or args[:1] == ["rm"]:
    raise SystemExit(0)
else:
    raise SystemExit("unexpected docker command: " + joined)
""")
    script.chmod(0o700)
    return bindir


def _calls() -> list[list[str]]:
    path = Path(os.environ["SYNTHETIC_DOCKER_STATE"]) / "calls"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _removed(calls: list[list[str]]) -> bool:
    return any(call[:1] == ["rm"] or call[:2] in (["volume", "rm"], ["image", "rm"], ["network", "rm"]) for call in calls)


def _project(root: Path) -> tuple[str, str]:
    env = installer._dotenv(root / "compose.env")
    project = env[f"{LEGACY_ENV_PREFIX}COMPOSE_PROJECT"]
    return project, env[f"{LEGACY_ENV_PREFIX}IMAGE_NAMESPACE"]


@pytest.fixture
def node(tmp_path, monkeypatch):
    from tests.test_first_run_handoff import configured_node
    root, _args, paths = configured_node(tmp_path)
    project, namespace = _project(root)
    bindir = _fake_docker(tmp_path, project, namespace)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SYNTHETIC_DOCKER_STATE", str(tmp_path / "docker-state"))
    monkeypatch.setenv("SYNTHETIC_PROJECT", project)
    monkeypatch.setenv("SYNTHETIC_NAMESPACE", namespace)
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    return root, paths


def test_uninstall_dry_run_lists_resources_and_removes_nothing(node, monkeypatch, capsys):
    root, paths = node
    secret = next(paths["secrets"].iterdir()).read_text()
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--dry-run", "--plain"])
    assert installer.main() == 0
    output = capsys.readouterr().out
    assert "UNINSTALL PLAN" in output and "Dry run: nothing was removed" in output
    assert "synthetic-app-1" in output and "Volumes to keep" in output
    assert "Service account and group are not changed" in output
    assert secret not in output and "\x1b" not in output
    assert root.is_dir()
    assert not _removed(_calls())


def test_uninstall_refuses_wrong_confirmation_and_noninteractive_without_yes(node, monkeypatch, capsys):
    root, _paths = node
    state = Path(os.environ["SYNTHETIC_DOCKER_STATE"])
    _tty(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--plain"])
    assert installer.main() == 1
    output = capsys.readouterr().out
    assert "did not match" in output and root.is_dir()
    assert not _removed(_calls())
    (state / "calls").write_text("")
    _tty(monkeypatch, False)
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--plain"])
    assert installer.main() == 1
    assert "requires a terminal or --yes" in capsys.readouterr().out
    assert not _removed(_calls())


def test_uninstall_yes_keeps_data_and_delete_data_removes_only_owned_resources(node, monkeypatch, capsys, tmp_path):
    root, _paths = node
    state = Path(os.environ["SYNTHETIC_DOCKER_STATE"])
    project, namespace = _project(root)
    external = tmp_path / "external-matter"
    external.mkdir()
    (external / f".{LEGACY_STEM}-managed-storage.json").write_text("synthetic marker")
    untouched = tmp_path / "untouched-directory"
    untouched.mkdir()
    (untouched / "keep.txt").write_text("synthetic")
    installation = json.loads((root / "installation.json").read_text())
    installation["storage_root"] = str(external)
    (root / "installation.json").write_text(json.dumps(installation))
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--yes", "--plain"])
    assert installer.main() == 0
    output = capsys.readouterr().out
    assert "Volumes, images, and the node root were kept" in output
    assert root.is_dir() and external.is_dir() and untouched.is_dir()
    calls = _calls()
    assert ["rm", "-f", "--", "a" * 64] in calls
    assert ["network", "rm", "b" * 64] in calls
    assert not any(call[:2] == ["volume", "rm"] or call[:2] == ["image", "rm"] for call in calls)
    (state / "calls").write_text("")
    monkeypatch.setattr(sys, "argv", [
        "install", "uninstall", "--root", str(root), "--yes", "--delete-data", "--plain",
    ])
    assert installer.main() == 0
    calls = _calls()
    assert ["volume", "rm", f"{project}_postgres-data"] in calls
    assert ["image", "rm", "c" * 64] in calls
    assert not any(call[:2] == ["image", "rm"] and call[-1].startswith(("d", "e")) for call in calls)
    deleted = capsys.readouterr().out
    assert "outside this namespace" in deleted
    assert f"{namespace}/gateway:synthetic" in deleted
    assert not root.exists()
    assert not external.exists()
    assert (untouched / "keep.txt").read_text() == "synthetic"
    assert namespace in capsys.readouterr().out or True


def test_uninstall_leaves_an_empty_root_when_its_parent_is_not_writable(node, monkeypatch, capsys):
    root, _paths = node
    parent = root.parent
    mode = parent.stat().st_mode
    parent.chmod(mode & ~0o222)
    try:
        monkeypatch.setattr(sys, "argv", [
            "install", "uninstall", "--root", str(root), "--yes", "--delete-data", "--plain",
        ])
        assert installer.main() == 0
        output = capsys.readouterr().out
        assert root.is_dir() and not any(root.iterdir())
        assert "empty directory" in output and "cannot remove it from its parent" in output
        assert "The node root was removed" not in output
    finally:
        parent.chmod(mode)


def test_uninstall_accepts_exculpata_compose_names(node, monkeypatch, capsys):
    root, _paths = node
    project, _namespace = _project(root)
    env_path = root / "compose.env"
    renamed = env_path.read_text().replace(f"{LEGACY_ENV_PREFIX}COMPOSE_PROJECT", "EXCULPATA_COMPOSE_PROJECT")
    renamed = renamed.replace(f"{LEGACY_ENV_PREFIX}IMAGE_NAMESPACE", "EXCULPATA_IMAGE_NAMESPACE")
    env_path.write_text(renamed)
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--dry-run", "--plain"])
    assert installer.main() == 0
    output = capsys.readouterr().out
    assert project in output and "Dry run: nothing was removed" in output
    assert not _removed(_calls())


def test_uninstall_refuses_banned_root_before_removing_any_docker_resource(node, monkeypatch, capsys):
    root, _ = node
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: root))
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--delete-data", "--yes", "--plain"])
    assert installer.main() == 1
    assert not _removed(_calls())
    assert _calls() == []
    assert root.is_dir()


def test_uninstall_refuses_external_home_storage_before_any_docker_call(node, monkeypatch, capsys, tmp_path):
    root, _paths = node
    external = tmp_path / "external-home"
    external.mkdir()
    (external / f".{LEGACY_STEM}-managed-storage.json").write_text("synthetic marker")
    installation = json.loads((root / "installation.json").read_text())
    installation["storage_root"] = str(external)
    (root / "installation.json").write_text(json.dumps(installation))
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: external))
    monkeypatch.setattr(sys, "argv", [
        "install", "uninstall", "--root", str(root), "--delete-data", "--yes", "--plain",
    ])
    assert installer.main() == 1
    assert _calls() == []
    assert root.is_dir() and (external / f".{LEGACY_STEM}-managed-storage.json").is_file()


def test_uninstall_keep_data_still_removes_containers_when_the_root_is_home(node, monkeypatch, capsys):
    root, _paths = node
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: root))
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--yes", "--plain"])
    assert installer.main() == 0
    assert ["rm", "-f", "--", "a" * 64] in _calls()
    assert root.is_dir()


def test_uninstall_refuses_a_project_that_does_not_match_the_root(node, monkeypatch, capsys):
    root, _paths = node
    env = (root / "compose.env").read_text().replace(
        installer._dotenv(root / "compose.env")[f"{LEGACY_ENV_PREFIX}COMPOSE_PROJECT"], "exculpata-0123456789",
    )
    (root / "compose.env").write_text(env)
    monkeypatch.setattr(sys, "argv", ["install", "uninstall", "--root", str(root), "--yes", "--delete-data"])
    assert installer.main() == 1
    assert "does not match this root" in capsys.readouterr().out
    assert root.is_dir()
    assert not Path(os.environ["SYNTHETIC_DOCKER_STATE"], "calls").read_text().strip()
