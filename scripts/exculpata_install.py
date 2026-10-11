#!/usr/bin/env python3
"""Exculpata node installer with an automation-safe retro terminal UI."""
from __future__ import annotations

import argparse
import csv
import fnmatch
import getpass
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import pwd
import re
import runpy
import secrets
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from case_intelligence.compat_env import env as _env, required_env as _required_env
sys.path.pop(0)
VERSION = "0.1.0-beta.1"
STORAGE_MARKERS = (".recordbench-managed-storage.json", ".exculpata-managed-storage.json")


class NamingError(RuntimeError):
    """An existing node cannot change naming generations implicitly or explicitly."""


def _legacy_account() -> bool:
    try:
        pwd.getpwnam("recordbench")
        return True
    except KeyError:
        return False


def _detected_names(root: Path, storage_root: Path | None = None) -> str | None:
    generations = set()
    installation = root / "installation.json"
    if installation.is_symlink():
        raise NamingError("Saved installation metadata cannot be a symbolic link.")
    if installation.exists():
        try:
            record = json.loads(installation.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NamingError("Saved installation metadata is invalid.") from exc
        if not isinstance(record, dict):
            raise NamingError("Saved installation metadata is invalid.")
        if "names" in record:
            if record["names"] not in ("recordbench", "exculpata"):
                raise NamingError("Saved installation naming generation is invalid.")
            generations.add(record["names"])
    for names in ("recordbench", "exculpata"):
        path = root / "config" / f"{names}.env"
        if path.exists() or path.is_symlink():
            generations.add(names)
    storage = storage_root or root / "matter-storage"
    if (root / "compose.env").is_symlink():
        raise NamingError("Saved Compose configuration cannot be a symbolic link.")
    if (root / "compose.env").is_file():
        saved = _dotenv(root / "compose.env")
        storage = Path(_env("RECORDBENCH_STORAGE_ROOT", str(storage), environ=saved))
    for names in ("recordbench", "exculpata"):
        for directory in (root, storage):
            marker = directory / f".{names}-managed-storage.json"
            if marker.exists() or marker.is_symlink():
                generations.add(names)
    if len(generations) > 1:
        raise NamingError("Conflicting naming markers or installation metadata; restore the node's original marker/configuration set. No migration was performed.")
    if generations:
        return generations.pop()
    # P2 may already use EXCULPATA environment keys; those are not a migration.
    if (root / "installation.json").exists() or (root / "compose.env").exists() or _legacy_account():
        return "recordbench"
    return None


def _node_names(root: Path) -> str:
    return _detected_names(root) or "exculpata"


def _select_names(args: argparse.Namespace, root: Path) -> str:
    detected = _detected_names(root, args.storage_root)
    requested = args.names
    if detected and requested and requested != detected:
        raise NamingError(f"This node uses {detected} names. --names {requested} would require a migration, which is not implemented. Existing paths, database, images and backups were left untouched.")
    args.names = detected or requested or "exculpata"
    return args.names


def _default_root() -> Path:
    legacy = Path("/srv/recordbench")
    return legacy if _detected_names(legacy) == "recordbench" else Path("/srv/exculpata")


def _app_env(root: Path, *, names: str | None = None) -> Path:
    return root / "config" / f"{names or _node_names(root)}.env"


def _write_env_values(path: Path, values: Mapping[str, object], heading: str) -> None:
    if path.exists():
        for key, value in values.items():
            _replace_env(path, key, str(value), add_missing_aliases=True)
    else:
        _private_write(path, _env_text(values, heading))


def _compose_environment(command: Sequence[str], environment: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Saved node coordinates must beat unrelated shell naming overrides."""
    if list(command[:2]) != ["docker", "compose"] or "--env-file" not in command:
        return dict(environment) if environment is not None else None
    saved = _dotenv(Path(command[command.index("--env-file") + 1]))
    result = dict(os.environ if environment is None else environment)
    for key in tuple(result):
        if key.startswith(("RECORDBENCH_", "EXCULPATA_")) or key in {"COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"}:
            result.pop(key)
    # Compose defaults changed for new checkouts; older nodes may omit these.
    for suffix in ("COMPOSE_PROJECT", "IMAGE_NAMESPACE"):
        result[f"EXCULPATA_{suffix}"] = _env(f"RECORDBENCH_{suffix}", "recordbench", environ=saved)
    return result


RELEASE_IGNORED_PATTERNS = ("__pycache__", "*.pyc", ".pytest_cache", "*.egg-info", "build")
RELEASE_DIRECTORIES = (
    "benchmarks",
    "config",
    "deploy",
    "docs",
    "migrations",
    "requirements",
    "schemas",
    "scripts",
    "services",
    "src",
)
RELEASE_FILES = (
    ".dockerignore",
    ".env.example",
    "AGENTS.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "compose.kerberos.yaml",
    "compose.local-accounts.yaml",
    "compose.yaml",
    "install",
    "pyproject.toml",
)


class C:
    green = "\033[38;5;46m"
    cyan = "\033[38;5;51m"
    magenta = "\033[38;5;201m"
    amber = "\033[38;5;220m"
    dim = "\033[2m"
    bold = "\033[1m"
    reset = "\033[0m"


@dataclass
class Console:
    color: bool
    quiet: bool = False

    def paint(self, value: str, color: str) -> str:
        return f"{color}{value}{C.reset}" if self.color else value

    def line(self, value: str = "") -> None:
        if not self.quiet:
            print(value, flush=True)

    def phase(self, number: int, title: str, detail: str) -> None:
        marker = self.paint(f"PHASE {number:02d}", C.magenta + C.bold)
        self.line(f"\n{marker}  {self.paint(title, C.cyan + C.bold)}")
        self.line(f"          {self.paint(detail, C.dim)}")

    def ok(self, value: str) -> None:
        self.line(f"  {self.paint('[OK]', C.green + C.bold)} {value}")

    def note(self, value: str) -> None:
        self.line(f"  {self.paint('[::]', C.cyan)} {value}")

    def warn(self, value: str) -> None:
        self.line(f"  {self.paint('[!!]', C.amber + C.bold)} {value}")

    def banner(self, mode: str) -> None:
        art = "EXCULPATA"
        self.line(self.paint(art, C.green + C.bold))
        self.line(
            self.paint(
                f"  EVIDENCE NODE PROVISIONER // {mode.upper()} // BUILD {VERSION}",
                C.cyan,
            )
        )
        self.line(self.paint("  LOCAL MODELS. PRIVATE RECORD. OPERATOR CONTROL.", C.dim))


@dataclass(frozen=True, slots=True)
class GpuDevice:
    index: str
    name: str
    total_mib: int
    free_mib: int
    compute_capability: float | None = None


@dataclass(frozen=True, slots=True)
class GpuPlan:
    layout: str
    generator_gpus: tuple[str, ...]
    transcription_gpu: str
    retrieval_device: str
    retrieval_gpu: str

    def environment(self) -> dict[str, str]:
        return {
            "RECORDBENCH_GPU_LAYOUT": self.layout,
            "RECORDBENCH_GENERATOR_GPU": ",".join(self.generator_gpus),
            "RECORDBENCH_GENERATOR_TENSOR_PARALLEL": str(
                max(len(self.generator_gpus), 1)
            ),
            "RECORDBENCH_TRANSCRIPTION_GPU": self.transcription_gpu,
            "RECORDBENCH_RETRIEVAL_DEVICE": self.retrieval_device,
            "RECORDBENCH_RETRIEVAL_GPU": self.retrieval_gpu,
        }


@dataclass(frozen=True, slots=True)
class ReviewModelPlan:
    profile: str
    model_id: str
    revision: str
    served_name: str
    dtype: str
    gpu_utilization: float
    max_sequences: int
    generation_concurrency: int
    transcription_min_free_vram_mib: int

    def environment(self) -> dict[str, str]:
        return {
            "RECORDBENCH_GENERATOR_MODEL_ID": self.model_id,
            "RECORDBENCH_GENERATOR_REVISION": self.revision,
            "RECORDBENCH_GENERATOR_SERVED_NAME": self.served_name,
            "RECORDBENCH_GENERATOR_DTYPE": self.dtype,
            "RECORDBENCH_GENERATOR_GPU_UTILIZATION": (
                f"{self.gpu_utilization:.2f}"
            ),
            "RECORDBENCH_GENERATOR_SEQUENCES": str(self.max_sequences),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./install",
        description="Provision or inspect a private Exculpata node.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("install", "preflight", "doctor", "update", "backup", "restore"),
        default="install",
    )
    parser.add_argument("--names", choices=("recordbench", "exculpata"), help="naming generation for a fresh node; existing nodes cannot be renamed")
    parser.add_argument("--root", type=Path, help="exact installation state directory")
    parser.add_argument("--storage-root", type=Path, help="dedicated local or mounted-NAS matter storage directory")
    parser.add_argument("--auth", choices=("local", "oidc", "kerberos"), default=None)
    parser.add_argument("--enable-account-management", action="store_true", help="enable browser account changes for an explicit --auth local installation")
    parser.add_argument("--models", choices=("none", "review", "transcription", "all"), default=None)
    parser.add_argument(
        "--gpu-layout",
        choices=("auto", "shared", "split"),
        default="auto",
        help="automatically pack or deliberately separate GPU workloads",
    )
    parser.add_argument(
        "--generator-gpus",
        help="comma-separated visible GPU indices for vLLM tensor parallelism",
    )
    parser.add_argument("--transcription-gpu", help="visible GPU index for WhisperX")
    parser.add_argument("--retrieval-gpu", help="visible GPU index for learned retrieval")
    parser.add_argument(
        "--retrieval-device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
    )
    parser.add_argument(
        "--review-model-profile",
        choices=("auto", "portable", "quality"),
        default="auto",
        help="choose a pinned generator tier or let preflight select by topology",
    )
    parser.add_argument("--generator-gpu-utilization", type=float)
    parser.add_argument("--transcription-min-free-vram-mib", type=int)
    parser.add_argument("--server-name")
    parser.add_argument("--bind-address")
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--admin-username")
    parser.add_argument("--admin-display-name")
    parser.add_argument("--oidc-issuer")
    parser.add_argument("--oidc-client-id")
    parser.add_argument("--oidc-client-secret-file", type=Path)
    parser.add_argument("--oidc-allowed-groups")
    parser.add_argument("--oidc-admin-groups")
    parser.add_argument("--kerberos-realm")
    parser.add_argument("--kerberos-allowed-groups")
    parser.add_argument("--kerberos-admin-groups")
    parser.add_argument("--kerberos-keytab", type=Path)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--hf-token-stdin", action="store_true")
    parser.add_argument("--accept-model-terms", action="store_true",
                        help="acknowledge selected model and tokenizer resource terms")
    parser.add_argument(
        "--transcription-languages",
        default="en",
        help="comma-separated staged alignment languages: en and/or es",
    )
    parser.add_argument(
        "--enable-diarization",
        action="store_true",
        help="stage and enable speaker diarization (ungated Nemotron by default)",
    )
    parser.add_argument("--diarization-backend", choices=("nemotron", "community-1"), default="nemotron")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit a structured preflight result (preflight command only)")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-backup", action="store_true", help="allow an update without a configured recovery snapshot")
    parser.add_argument("--repository", type=Path, help="encrypted restic repository for backup initialization")
    parser.add_argument("--recovery-key-output", type=Path, help="separate owner-only recovery credential path")
    parser.add_argument("--retention", default="14d")
    parser.add_argument("--allow-local-backup", action="store_true")
    parser.add_argument("--restore-target", type=Path)
    parser.add_argument("--snapshot", default="latest")
    parser.add_argument("--schedule-backups", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def _ask(prompt: str, default: str, *, non_interactive: bool) -> str:
    if non_interactive:
        return default
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def _choose(prompt: str, choices: Sequence[str], default: str, *, non_interactive: bool) -> str:
    if non_interactive:
        return default
    rendered = "/".join(f"[{value}]" if value == default else value for value in choices)
    while True:
        value = input(f"{prompt} ({rendered}): ").strip().casefold() or default
        if value in choices:
            return value
        print(f"Choose one of: {', '.join(choices)}")


IDENTITY_FIELDS = {
    "local": (
        ("admin_username", "Initial administrator username", "recordbench.admin"),
        ("admin_display_name", "Administrator display name", "RecordBench Administrator"),
    ),
    "oidc": (
        ("oidc_issuer", "OIDC issuer URL", "https://identity.example.test/realms/case-team"),
        ("oidc_client_id", "OIDC client ID", "recordbench"),
        ("oidc_allowed_groups", "OIDC allowed groups", "RecordBench_Users"),
        ("oidc_admin_groups", "OIDC administrator groups", "RecordBench_Administrators"),
    ),
    "kerberos": (
        ("kerberos_realm", "Kerberos realm", "EXAMPLE.TEST"),
        ("kerberos_allowed_groups", "Allowed Kerberos group", "recordbench_users@example.test"),
        ("kerberos_admin_groups", "Administrator Kerberos group", "recordbench_administrators@example.test"),
    ),
}


def _collect_identity_choices(args: argparse.Namespace) -> None:
    """Collect non-secret choices once so preflight checks the actual installation."""
    if args.auth is None:
        args.auth = _choose("Identity mode", ("local", "oidc", "kerberos"), "local",
                            non_interactive=args.non_interactive)
    for name, prompt, default in IDENTITY_FIELDS[args.auth]:
        if getattr(args, name) is None:
            setattr(args, name, _ask(prompt, default, non_interactive=args.non_interactive))
    if args.auth == "local":
        args.admin_username = args.admin_username.strip().casefold()
        args.admin_display_name = args.admin_display_name.strip()
    if args.auth == "kerberos":
        args.kerberos_realm = args.kerberos_realm.upper()
        if args.kerberos_keytab is None and not args.non_interactive and not args.dry_run:
            args.kerberos_keytab = Path(_ask("Path to the exported HTTP service keytab", "", non_interactive=False))


def _run(
    console: Console,
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_value: str | None = None,
    check: bool = True,
    capture: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    safe = " ".join(command)
    console.note(f"exec :: {safe}")
    if dry_run:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(
        command,
        cwd=PROJECT,
        env=_compose_environment(command, env),
        input=input_value,
        text=True,
        check=check,
        capture_output=capture,
    )


def _require(command: str) -> str:
    value = shutil.which(command)
    if value is None:
        raise RuntimeError(f"required command is unavailable: {command}")
    return value


def _private_write(path: Path, value: str, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, value.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _credential_source_available(source: Path | None, *, minimum_bytes: int = 1,
                                 maximum_bytes: int = 1024 * 1024, private: bool = False) -> bool:
    """Check credential-file metadata without opening or retaining its contents."""
    if source is None:
        return False
    try:
        source = source.expanduser()
        metadata = source.lstat()
        return (stat.S_ISREG(metadata.st_mode) and minimum_bytes <= metadata.st_size <= maximum_bytes
                and (not private or metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o600)
                and os.access(source, os.R_OK))
    except OSError:
        return False


def _credential_source_content(source: Path | None, *, private: bool = False) -> bytes | None:
    """Read a bounded source without following its final link or reporting secrets."""
    if not _credential_source_available(source, private=private):
        return None
    try:
        descriptor = os.open(source.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024
                    or private and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600)):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(1024 * 1024 + 1)
            after = os.fstat(descriptor)
            if len(content) > 1024 * 1024 or _read_identity(before) != _read_identity(after):
                return None
            return content
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return None


def _oidc_secret_source_valid(source: Path | None, *, private: bool = False) -> bool:
    content = _credential_source_content(source, private=private)
    if content is None:
        return False
    try:
        # Match OidcSettings.from_env: UTF-8, trailing CR/LF only, then
        # character bounds and rejection of embedded CR/LF/NUL.
        secret = content.decode("utf-8").rstrip("\r\n")
        return 16 <= len(secret) <= 4096 and not any(value in secret for value in ("\x00", "\r", "\n"))
    except UnicodeDecodeError:
        return False


def _kerberos_proxy_source_valid(source: Path) -> bool:
    content = _credential_source_content(source, private=True)
    if content is None:
        return False
    try:
        return re.fullmatch(r"[A-Za-z0-9_-]{32,256}", content.decode("ascii").rstrip("\r\n")) is not None
    except UnicodeDecodeError:
        return False


def _read_identity(metadata: os.stat_result) -> tuple:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _account_format() -> dict:
    """Load this reviewed launcher's stdlib validator without runtime imports."""
    return runpy.run_path(str(PROJECT / "src/case_intelligence/local_account_format.py"))


def _saved_account_status(path: Path, *, managed: bool) -> str:
    """Validate saved account bytes and private traversal without runtime imports."""
    if not path.is_absolute() or path.name != "local-accounts.json" or ".." in path.parts:
        return "invalid"
    directory = None
    try:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for component in (*path.parent.parts[1:], None):
            metadata = os.fstat(directory)
            mode = stat.S_IMODE(metadata.st_mode)
            final = component is None
            trusted_sticky = not final and metadata.st_uid == 0 and mode == 0o1777
            if (metadata.st_uid not in ({os.geteuid()} if final else {0, os.geteuid()})
                    or (mode & 0o022 and not trusted_sticky)
                    or (final and managed and mode != 0o700)):
                return "invalid"
            if not final:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
        if managed:
            allowed = re.compile(r"^(?:local-accounts\.json|\.local-accounts\.json\.lock|\.local-accounts\.json-[0-9a-f]{32}\.tmp)$")
            for name in os.listdir(directory):
                if not allowed.fullmatch(name):
                    return "invalid"
                entry = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    metadata = os.fstat(entry)
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600):
                        return "invalid"
                finally:
                    os.close(entry)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return "missing"
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 1024 * 1024):
                return "invalid"
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024 or _read_identity(before) != _read_identity(os.fstat(descriptor)):
                return "invalid"
            parser = _account_format()["parse_accounts"]
            version, _accounts = parser(json.loads(content.decode("utf-8")))
            return "valid" if not managed or version == 2 else "invalid"
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError, ValueError):
        return "invalid"
    finally:
        if directory is not None:
            os.close(directory)


def _private_copy(source: Path, target: Path) -> None:
    metadata = source.lstat()
    if source.is_symlink() or not source.is_file() or metadata.st_size > 1024 * 1024:
        raise RuntimeError("source credential file is unavailable or unsafe")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        content = os.read(descriptor, 1024 * 1024 + 1)
    finally:
        os.close(descriptor)
    if not content or len(content) > 1024 * 1024:
        raise RuntimeError("source credential file is empty or too large")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(output, content)
        os.fsync(output)
    finally:
        os.close(output)


def _secret(path: Path, size: int = 48) -> str:
    if path.exists():
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or (metadata.st_mode & 0o777) != 0o600
            or metadata.st_uid != os.geteuid()
            or not 16 <= metadata.st_size <= 16_384
        ):
            raise RuntimeError(f"existing secret file is unsafe: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value or any(character in value for character in ("\x00", "\r", "\n")):
            raise RuntimeError(f"existing secret file is invalid: {path}")
        return value
    value = secrets.token_urlsafe(size)
    _private_write(path, value + "\n")
    return value


def _env_text(values: Mapping[str, object], heading: str, *, aliases: bool = False) -> str:
    if aliases:
        expanded = {}
        for key, value in values.items():
            if key.startswith(("EXCULPATA_", "RECORDBENCH_")):
                suffix = key.removeprefix("RECORDBENCH_").removeprefix("EXCULPATA_")
                new, old = f"EXCULPATA_{suffix}", f"RECORDBENCH_{suffix}"
                preferred = values.get(new)
                expanded[new] = values.get(old, "") if preferred in (None, "") else preferred
            else:
                expanded[key] = value
        values = expanded
    product = "Exculpata" if heading.startswith("Exculpata") else "RecordBench"
    lines = [f"# {heading}", f"# Generated by the {product} node provisioner."]
    for key, value in values.items():
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None:
            raise RuntimeError("generated environment key is invalid")
        encoded = str(value)
        if any(character in encoded for character in ("\x00", "\r", "\n")):
            raise RuntimeError(f"generated environment value is invalid: {key}")
        lines.append(f"{key}={json.dumps(encoded)}")
    return "\n".join(lines) + "\n"


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, encoded = line.partition("=")
        if (
            not separator
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None
            or key in values
        ):
            raise RuntimeError(f"invalid node environment line {number}")
        encoded = encoded.strip()
        if encoded.startswith('"'):
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid node environment line {number}") from exc
            if not isinstance(value, str):
                raise RuntimeError(f"invalid node environment line {number}")
        else:
            value = encoded
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise RuntimeError(f"invalid node environment line {number}")
        values[key] = value
    return values


def _valid_host(value: str) -> str:
    candidate = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", candidate):
        raise RuntimeError("server name is invalid")
    return candidate


def _release_sources() -> tuple[Path, ...]:
    sources: list[Path] = []
    for name in (*RELEASE_DIRECTORIES, *RELEASE_FILES):
        source = PROJECT / name
        if not source.exists() or source.is_symlink():
            raise RuntimeError(f"release source is missing or unsafe: {name}")
        sources.append(source)
    return tuple(sources)


def _release_digest() -> str:
    digest = hashlib.sha256()
    for source in _release_sources():
        paths = [source] if source.is_file() else sorted(source.rglob("*"))
        for path in paths:
            relative_path = path.relative_to(PROJECT)
            if any(fnmatch.fnmatchcase(part, pattern)
                   for part in relative_path.parts
                   for pattern in RELEASE_IGNORED_PATTERNS):
                continue
            if path.is_symlink():
                raise RuntimeError(f"release source contains a symbolic link: {path}")
            if not path.is_file():
                continue
            relative = relative_path.as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            digest.update(b"\0")
    return digest.hexdigest()


def _copy_release_entry(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            symlinks=False,
            ignore=shutil.ignore_patterns(*RELEASE_IGNORED_PATTERNS),
        )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _stage_release(console: Console, root: Path, *, dry_run: bool) -> tuple[str, Path]:
    digest = _release_digest()
    release_id = f"{VERSION}-{digest[:12]}"
    destination = root / "releases" / release_id
    if dry_run:
        console.ok(f"release capsule :: {release_id}")
        return release_id, destination
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(releases, 0o700)
    if destination.exists():
        manifest = destination / "RELEASE_MANIFEST.json"
        if destination.is_symlink() or not manifest.is_file():
            raise RuntimeError("existing release capsule is unsafe")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("release_id") != release_id or payload.get("source_sha256") != digest:
            raise RuntimeError("existing release capsule failed integrity validation")
        console.ok(f"release capsule :: {release_id} (already staged)")
        return release_id, destination
    staging = Path(tempfile_name(releases, ".staging-release-"))
    staging.mkdir(mode=0o700)
    try:
        for source in _release_sources():
            _copy_release_entry(source, staging / source.name)
        manifest = {
            "format_version": 1,
            "version": VERSION,
            "release_id": release_id,
            "source_sha256": digest,
        }
        (staging / "RELEASE_MANIFEST.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    console.ok(f"release capsule :: {release_id}")
    return release_id, destination


def tempfile_name(parent: Path, prefix: str) -> str:
    for _ in range(100):
        candidate = parent / f"{prefix}{secrets.token_hex(8)}"
        if not candidate.exists():
            return str(candidate)
    raise RuntimeError("could not allocate a release staging directory")


def _installed_release(root: Path) -> tuple[dict[str, object], Path]:
    path = root / "installation.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("RecordBench node is not installed")
    try:
        installation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("RecordBench installation record is invalid") from exc
    if (not isinstance(installation, dict)
            or installation.get("auth") not in ("local", "oidc", "kerberos")
            or installation.get("models", "none") not in ("none", "review", "transcription", "all")
            or not isinstance(installation.get("profiles", []), list)
            or not all(isinstance(profile, str) for profile in installation.get("profiles", []))):
        raise RuntimeError("RecordBench installation record is invalid")
    configured = installation.get("release_path")
    release = Path(configured) if isinstance(configured, str) else PROJECT
    if not release.is_absolute() or not (release / "compose.yaml").is_file():
        raise RuntimeError("installed release capsule is unavailable")
    return installation, release


def _compose(
    root: Path,
    profiles: Iterable[str],
    *,
    release: Path | None = None,
    auth: str | None = None,
    local_accounts: bool | None = None,
) -> list[str]:
    if local_accounts is None:
        try:
            record, _ = _installed_release(root)
            local_accounts = bool(record.get("local_account_management", False))
        except RuntimeError:
            local_accounts = False
    if release is None:
        try:
            installation, release = _installed_release(root)
            auth = auth or str(installation.get("auth", ""))
        except RuntimeError:
            release = PROJECT
    command = [
        "docker",
        "compose",
        "--env-file",
        str(root / "compose.env"),
        "-f",
        str(release / "compose.yaml"),
    ]
    if auth == "kerberos":
        command.extend(("-f", str(release / "compose.kerberos.yaml")))
    if local_accounts:
        command.extend(("-f", str(release / "compose.local-accounts.yaml")))
    for profile in profiles:
        command.extend(("--profile", profile))
    return command


def _required_gpu_count(models: str) -> int:
    if models == "none":
        return 0
    if models in {"review", "transcription"}:
        return 1
    if models == "all":
        return 1
    raise RuntimeError("model profile is invalid")


def _parse_gpu_inventory(value: str) -> tuple[GpuDevice, ...]:
    devices: list[GpuDevice] = []
    for row in csv.reader(value.splitlines()):
        if not row:
            continue
        if len(row) not in {4, 5}:
            raise RuntimeError("nvidia-smi returned an unexpected GPU inventory")
        try:
            compute_capability = (
                float(row[4].strip()) if len(row) == 5 else None
            )
            device = GpuDevice(
                index=row[0].strip(),
                name=row[1].strip(),
                total_mib=int(row[2].strip()),
                free_mib=int(row[3].strip()),
                compute_capability=compute_capability,
            )
        except ValueError as exc:
            raise RuntimeError("nvidia-smi returned an invalid GPU inventory") from exc
        if (
            not device.index
            or device.total_mib <= 0
            or device.free_mib < 0
            or (
                device.compute_capability is not None
                and device.compute_capability <= 0
            )
        ):
            raise RuntimeError("nvidia-smi returned an invalid GPU inventory")
        devices.append(device)
    if len({device.index for device in devices}) != len(devices):
        raise RuntimeError("nvidia-smi returned duplicate GPU indices")
    return tuple(devices)


def _gpu_selection(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    selected = tuple(item.strip() for item in value.split(",") if item.strip())
    if not selected or len(set(selected)) != len(selected):
        raise RuntimeError("GPU selection contains an empty or duplicate index")
    return selected


def _gpu_plan(
    models: str,
    devices: tuple[GpuDevice, ...],
    *,
    layout: str = "auto",
    generator_gpus: str | None = None,
    transcription_gpu: str | None = None,
    retrieval_device: str = "auto",
    retrieval_gpu: str | None = None,
) -> GpuPlan:
    required = _required_gpu_count(models)
    if layout not in {"auto", "shared", "split"}:
        raise RuntimeError("GPU layout is invalid")
    if retrieval_device not in {"auto", "cpu", "cuda"}:
        raise RuntimeError("retrieval device is invalid")
    eligible_devices = tuple(
        device
        for device in devices
        if device.compute_capability is None or device.compute_capability >= 7.5
    )
    if required and len(eligible_devices) < required:
        raise RuntimeError(
            f"the {models} profile requires at least {required} visible NVIDIA "
            "GPU(s) with compute capability 7.5 or newer"
        )
    ranked_devices = tuple(
        sorted(
            eligible_devices,
            key=lambda device: (
                -device.free_mib,
                -device.total_mib,
                (
                    (0, int(device.index))
                    if device.index.isdigit()
                    else (1, device.index)
                ),
            ),
        )
    )
    indices = tuple(device.index for device in ranked_devices)
    available = set(indices)
    if not indices:
        indices = ("0",)
    selected_generator = _gpu_selection(generator_gpus)
    if not selected_generator:
        selected_generator = (indices[0],)
    generator_set = set(selected_generator)
    if transcription_gpu:
        selected_transcription = transcription_gpu
    elif models == "all" and layout != "shared":
        selected_transcription = next(
            (index for index in indices if index not in generator_set),
            selected_generator[0],
        )
    elif models == "all":
        selected_transcription = selected_generator[0]
    else:
        selected_transcription = indices[0]

    if (
        layout == "split"
        and models == "all"
        and selected_transcription in generator_set
    ):
        raise RuntimeError(
            "the split full profile requires distinct generator and transcription GPUs"
        )
    if (
        layout == "shared"
        and models == "all"
        and selected_transcription not in generator_set
    ):
        raise RuntimeError(
            "the shared full profile requires transcription on a generator GPU"
        )

    selected_retrieval_device = retrieval_device
    occupied = set(selected_generator) if models in {"review", "all"} else set()
    if models in {"transcription", "all"}:
        occupied.add(selected_transcription)
    remaining = tuple(index for index in indices if index not in occupied)
    if selected_retrieval_device == "auto":
        selected_retrieval_device = (
            "cuda"
            if retrieval_gpu
            or (models in {"review", "all"} and remaining and layout != "shared")
            else "cpu"
        )
    if retrieval_gpu and selected_retrieval_device == "cpu":
        raise RuntimeError("--retrieval-gpu requires --retrieval-device cuda or auto")
    if retrieval_gpu:
        selected_retrieval = retrieval_gpu
    elif selected_retrieval_device == "cuda":
        selected_retrieval = remaining[0] if remaining else selected_generator[0]
    else:
        selected_retrieval = indices[0]

    if (
        layout == "split"
        and selected_retrieval_device == "cuda"
        and selected_retrieval in occupied
    ):
        raise RuntimeError(
            "the split profile requires a distinct retrieval GPU or CPU retrieval"
        )

    used = {*selected_generator}
    if models in {"transcription", "all"}:
        used.add(selected_transcription)
    if models in {"review", "all"} and selected_retrieval_device == "cuda":
        used.add(selected_retrieval)
    if devices and not used.issubset(available):
        missing = ",".join(sorted(used - available))
        raise RuntimeError(f"GPU selection is not visible to nvidia-smi: {missing}")

    effective_layout = layout
    if layout == "auto":
        separated_transcription = bool(
            models == "all" and selected_transcription not in generator_set
        )
        separated_retrieval = bool(
            models in {"review", "all"}
            and selected_retrieval_device == "cuda"
            and selected_retrieval not in generator_set
        )
        effective_layout = (
            "split" if separated_transcription or separated_retrieval else "shared"
        )
    return GpuPlan(
        layout=effective_layout,
        generator_gpus=selected_generator,
        transcription_gpu=selected_transcription,
        retrieval_device=selected_retrieval_device,
        retrieval_gpu=selected_retrieval,
    )


def _review_model_plan(
    models: str,
    devices: tuple[GpuDevice, ...],
    gpu_plan: GpuPlan,
    *,
    requested_profile: str = "auto",
    requested_dtype: str | None = None,
    gpu_utilization: float | None = None,
    transcription_min_free_vram_mib: int | None = None,
) -> ReviewModelPlan:
    if requested_profile not in {"auto", "portable", "quality"}:
        raise RuntimeError("review model profile is invalid")
    device_map = {device.index: device for device in devices}
    generator_devices = [
        device_map[index]
        for index in gpu_plan.generator_gpus
        if index in device_map
    ]
    minimum_total_mib = min(
        (device.total_mib for device in generator_devices), default=0
    )
    tensor_parallel_size = max(len(generator_devices), 1)
    shared_with_transcription = bool(
        models == "all"
        and gpu_plan.transcription_gpu in set(gpu_plan.generator_gpus)
    )
    profile = requested_profile
    if profile == "auto":
        profile = "quality" if minimum_total_mib >= 40_000 else "portable"

    definitions = {
        "portable": (
            "Qwen/Qwen3.5-4B",
            "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a",
            "recordbench-qwen35-4b",
        ),
        "quality": (
            "Qwen/Qwen3.5-9B",
            "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
            "recordbench-qwen35-9b",
        ),
    }
    model_id, revision, served_name = definitions[profile]
    if models in {"review", "all"} and devices:
        if profile == "portable":
            if tensor_parallel_size == 1:
                threshold = 20_000 if shared_with_transcription else 16_000
            else:
                threshold = 16_000 if shared_with_transcription else 12_000
        elif tensor_parallel_size == 1:
            threshold = 40_000 if shared_with_transcription else 32_000
        elif tensor_parallel_size == 2:
            threshold = 28_000 if shared_with_transcription else 20_000
        else:
            threshold = 24_000 if shared_with_transcription else 16_000
        if minimum_total_mib < threshold:
            raise RuntimeError(
                f"the {profile} review profile needs at least {threshold} MiB "
                "on each selected generator GPU for this topology"
            )

    utilization = gpu_utilization
    if utilization is None:
        if profile == "portable":
            utilization = 0.50 if shared_with_transcription else 0.60
        else:
            utilization = 0.50 if shared_with_transcription else 0.72
    if not 0.20 <= utilization <= 0.95:
        raise RuntimeError("generator GPU utilization must be between 0.20 and 0.95")
    minimum_free = transcription_min_free_vram_mib
    if minimum_free is None:
        minimum_free = 7_000 if shared_with_transcription else 12_000
    if minimum_free < 1_000:
        raise RuntimeError("transcription minimum free VRAM must be at least 1000 MiB")
    if models in {"review", "all"}:
        for device in generator_devices:
            required_free = int(device.total_mib * utilization) + 1_024
            if device.free_mib < required_free:
                raise RuntimeError(
                    f"generator GPU {device.index} currently has "
                    f"{device.free_mib} MiB free; this plan reserves "
                    f"approximately {required_free} MiB. Stop competing "
                    "workloads or select another device."
                )
    if models in {"transcription", "all"} and devices:
        transcription_device = device_map.get(gpu_plan.transcription_gpu)
        if transcription_device is not None:
            required_free = minimum_free + 1_024
            if gpu_plan.transcription_gpu in set(gpu_plan.generator_gpus):
                required_free += int(
                    transcription_device.total_mib * utilization
                )
            if transcription_device.free_mib < required_free:
                raise RuntimeError(
                    f"transcription GPU {transcription_device.index} currently "
                    f"has {transcription_device.free_mib} MiB free; this plan "
                    f"needs approximately {required_free} MiB before startup. "
                    "Stop competing workloads or select another device."
                )
    max_sequences = (
        4 if profile == "portable" or shared_with_transcription else 8
    )
    generation_concurrency = (
        2 if profile == "portable" or shared_with_transcription else 4
    )
    dtype = (
        "bfloat16"
        if generator_devices
        and all(
            device.compute_capability is not None
            and device.compute_capability >= 8.0
            for device in generator_devices
        )
        else "half"
    )
    if requested_dtype is not None and models in {"review", "all"}:
        if requested_dtype not in {"half", "bfloat16"}:
            raise RuntimeError("saved generator dtype must be half or bfloat16")
        if requested_dtype == "bfloat16" and dtype != "bfloat16":
            raise RuntimeError(
                "saved bfloat16 generator dtype requires compute capability "
                "8.0 or newer on every selected generator GPU; restore compatible "
                "hardware or explicitly reconfigure the saved runtime precision"
            )
        dtype = requested_dtype
    return ReviewModelPlan(
        profile=profile,
        model_id=model_id,
        revision=revision,
        served_name=served_name,
        dtype=dtype,
        gpu_utilization=utilization,
        max_sequences=max_sequences,
        generation_concurrency=generation_concurrency,
        transcription_min_free_vram_mib=minimum_free,
    )


def _resolve_gpu_plans(
    args: argparse.Namespace,
    models: str,
    devices: tuple[GpuDevice, ...],
) -> tuple[GpuPlan, ReviewModelPlan]:
    explicit_topology = bool(
        args.gpu_layout != "auto"
        or args.generator_gpus
        or args.transcription_gpu
        or args.retrieval_gpu
        or args.retrieval_device != "auto"
    )
    layouts = [args.gpu_layout]
    if not explicit_topology and models == "all" and len(devices) > 1:
        layouts.append("shared")
    if args.review_model_profile == "auto" and models in {"review", "all"}:
        profiles = ("quality", "portable")
    else:
        profiles = (
            "portable"
            if args.review_model_profile == "auto"
            else args.review_model_profile,
        )
    failures: list[str] = []
    for profile in profiles:
        for layout in dict.fromkeys(layouts):
            try:
                gpu_plan = _gpu_plan(
                    models,
                    devices,
                    layout=layout,
                    generator_gpus=args.generator_gpus,
                    transcription_gpu=args.transcription_gpu,
                    retrieval_device=args.retrieval_device,
                    retrieval_gpu=args.retrieval_gpu,
                )
                review_model = _review_model_plan(
                    models,
                    devices,
                    gpu_plan,
                    requested_profile=profile,
                    requested_dtype=getattr(args, "generator_dtype", None),
                    gpu_utilization=args.generator_gpu_utilization,
                    transcription_min_free_vram_mib=(
                        args.transcription_min_free_vram_mib
                    ),
                )
                return gpu_plan, review_model
            except RuntimeError as exc:
                failures.append(str(exc))
                if explicit_topology and args.review_model_profile != "auto":
                    raise
    detail = failures[-1] if failures else "no compatible GPU lane was found"
    raise RuntimeError(f"automatic GPU planning failed: {detail}")


def _transcription_languages(value: str) -> tuple[str, ...]:
    languages = tuple(
        sorted({item.strip().casefold() for item in value.split(",") if item.strip()})
    )
    if not languages or not set(languages).issubset({"en", "es"}):
        raise RuntimeError("transcription languages must select en and/or es")
    return languages


def _model_stage_groups(
    models: str,
    *,
    transcription_languages: tuple[str, ...],
    diarization: bool,
) -> tuple[str, ...]:
    _required_gpu_count(models)
    groups: list[str] = []
    if models in {"review", "all"}:
        groups.append("review")
    if models in {"transcription", "all"}:
        groups.append("transcription-asr")
        groups.extend(
            f"transcription-alignment-{language}"
            for language in transcription_languages
        )
        if diarization:
            groups.append("transcription-diarization")
    elif diarization:
        raise RuntimeError("diarization requires a transcription capability profile")
    return tuple(groups)


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    state: str
    observed: str
    required_capability: str
    blocking: bool
    remedy: str


@dataclass(frozen=True)
class PreflightResult:
    checks: tuple[PreflightCheck, ...]
    devices: tuple[GpuDevice, ...] = ()

    @property
    def ready(self) -> bool:
        return not any(check.blocking and check.state != "pass" for check in self.checks)

    def payload(self) -> dict[str, object]:
        return {"schema_version": 1, "ready": self.ready,
                "checks": [asdict(check) for check in self.checks]}


def _probe(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # Diagnostic commands never print arbitrary runtime output (which may contain
    # operator topology) and cannot hang an unattended first-run check forever.
    return subprocess.run(command, env=_compose_environment(command), capture_output=True, text=True, timeout=20, check=False)


def _storage_ancestors_safe(ancestor: Path) -> bool:
    """Require a private creation point and a non-replaceable parent chain."""
    uid = os.geteuid()
    # The portable no-follow descriptor walk opens directories read-only, so
    # check its read/search requirements before reporting a usable path.
    if not os.access(ancestor, os.R_OK | os.X_OK):
        return False
    creation = ancestor.stat()
    if not stat.S_ISDIR(creation.st_mode) or creation.st_uid != uid or creation.st_mode & 0o022:
        return False
    for parent in ancestor.parents:
        if not os.access(parent, os.R_OK | os.X_OK):
            return False
        metadata = parent.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, uid}:
            return False
        # A trusted sticky system directory protects each owned child from
        # replacement, e.g. an existing private test directory beneath /tmp.
        # It is never itself accepted as the writable creation point above.
        if metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX:
            return False
    return True


def _storage_path_status(path: Path) -> tuple[Path, Path, bool]:
    """Check the entered and canonical path without issuing runtime commands."""
    entered_path = path.expanduser()
    if not _storage_path_text_valid(entered_path):
        raise RuntimeError("storage paths cannot contain control characters")
    lexical_path = Path(os.path.abspath(entered_path))
    safe = entered_path.is_absolute() and not any(
        part.is_symlink()
        for part in (entered_path, *entered_path.parents, lexical_path, *lexical_path.parents)
    )
    path = entered_path.resolve(strict=False)
    # HOME may be inherited through sudo; also consult the effective account.
    import pwd
    effective_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=False)
    safe = safe and path not in {Path("/"), Path.home().resolve(strict=False), effective_home}
    ancestor = path
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    safe = safe and _storage_ancestors_safe(ancestor)
    if path.exists():
        safe = safe and path.is_dir() and path.stat().st_uid == os.geteuid()
    return path, ancestor, safe


def _existing_storage_path(path: Path) -> Path:
    try:
        canonical, ancestor, safe = _storage_path_status(path)
        if safe and canonical.is_dir() and os.access(ancestor, os.W_OK | os.X_OK):
            return canonical
    except (OSError, RuntimeError, KeyError):
        pass
    raise RuntimeError("saved storage path or protected ancestry is unavailable; restore safe ownership and access before resuming")


def _identity_options_valid(args: argparse.Namespace) -> bool:
    """Mirror non-secret identity.py settings without importing runtime dependencies."""
    values = {name: getattr(args, name) if getattr(args, name) is not None else default
              for name, _prompt, default in IDENTITY_FIELDS[args.auth]}
    if any(any(character in value for character in ("\x00", "\r", "\n"))
           for value in values.values()):
        return False
    groups = []
    for suffix, limit in (("allowed_groups", 100), ("admin_groups", 50)):
        selected = {value.strip() for value in values[f"{args.auth}_{suffix}"].split(",") if value.strip()}
        if args.auth == "kerberos":
            selected = {value.casefold() for value in selected}
            valid = all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@\\ -]{0,254}", value) for value in selected)
        else:
            valid = all(len(value) <= 256 and not any(ord(character) < 32 for character in value)
                        for value in selected)
        if len(selected) > limit or not valid:
            return False
        groups.append(selected)
    if args.auth == "kerberos":
        realm = values["kerberos_realm"].strip().upper()
        principals = []
        for name, limit in (("kerberos_allowed_principals", 200), ("kerberos_admin_principals", 50)):
            selected = {value.strip().casefold() for value in getattr(args, name, "").split(",") if value.strip()}
            if len(selected) > limit:
                return False
            for principal in selected:
                if len(principal) > 255 or principal.count("@") != 1:
                    return False
                login, principal_realm = principal.rsplit("@", 1)
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", login) is None or principal_realm.upper() != realm:
                    return False
            principals.append(selected)
        return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", realm)
                    and "." in realm and (any(groups) or any(principals)))
    client = values["oidc_client_id"].strip()
    if not client or len(client) > 512 or any(ord(character) < 32 for character in client):
        return False
    for value, origin_only in ((values["oidc_issuer"], False),
                               (f"https://{(args.server_name or 'recordbench.example.test').strip().casefold()}:{args.https_port}", True)):
        candidate = value.strip().rstrip("/")
        if not candidate or len(candidate) > 1024:
            return False
        try:
            parsed = urllib.parse.urlsplit(candidate)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or (origin_only and parsed.path not in {"", "/"})):
                return False
            hostname = parsed.hostname.casefold()
            if hostname == "localhost":
                return False
            try:
                if ipaddress.ip_address(hostname).is_loopback:
                    return False
            except ValueError:
                pass
        except ValueError:
            return False
    return True


def _saved_oidc_ca_valid(root: Path, value: str) -> bool:
    if not value:
        return True
    path = Path(value)
    # The standard app graph mounts this canonical credential directory. Do not
    # mistake a host path or a traversal outside that mount for a runtime file.
    if (path.parent != Path('/run/recordbench-secrets')
            or path.name in {'', '.', '..'} or '..' in path.parts):
        return False
    source = root / 'secrets' / path.name
    try:
        if source.lstat().st_mode & 0o022:
            return False
        content = _credential_source_content(source)
        if not content:
            return False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            context.load_verify_locations(cadata=content.decode('ascii'))
        except (UnicodeError, ssl.SSLError):
            context.load_verify_locations(cadata=content)
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def _saved_provider_options_valid(args: argparse.Namespace) -> bool:
    """Validate retained provider choices, never substitute new CLI defaults."""
    try:
        environment = _dotenv(_app_env(args.root))
        compose = _dotenv(args.root / "compose.env")
        options = argparse.Namespace(**vars(args))
        for name, _prompt, _default in IDENTITY_FIELDS[args.auth]:
            setattr(options, name, environment.get("CASE_INTELLIGENCE_" + name.upper(), ""))
        if args.auth == "kerberos":
            for name in ("kerberos_allowed_principals", "kerberos_admin_principals"):
                setattr(options, name, environment.get("CASE_INTELLIGENCE_" + name.upper(), ""))
        if not _identity_options_valid(options):
            return False
        if args.auth == "kerberos":
            return (environment.get("CASE_INTELLIGENCE_KERBEROS_PROXY_SECRET_FILE", "").strip() == "/run/recordbench-secrets/kerberos-proxy-secret"
                    and _env("RECORDBENCH_KERBEROS_PRINCIPAL", environ=compose) == f"HTTP/{args.server_name}@{options.kerberos_realm.strip().upper()}")
        origin = environment.get("CASE_INTELLIGENCE_EXTERNAL_ORIGIN", "").strip().rstrip("/")
        origins = {f"https://{args.server_name}:{args.https_port}"}
        if args.https_port == 443:
            origins.add(f"https://{args.server_name}")
        if (origin not in origins or environment.get("CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE", "").strip()
                != "/run/recordbench-secrets/oidc-client-secret"):
            return False
        if not _saved_oidc_ca_valid(args.root, environment.get('CASE_INTELLIGENCE_OIDC_CA_FILE', '').strip()):
            return False
        scopes = set(environment.get("CASE_INTELLIGENCE_OIDC_SCOPES", "openid profile email").split())
        if ("openid" not in scopes or len(scopes) > 12
                or any(re.fullmatch(r"[A-Za-z0-9:._/-]{1,80}", value) is None for value in scopes)):
            return False
        for name, default in (("GROUPS", "groups"), ("DISPLAY_NAME", "name"), ("LOGIN_NAME", "preferred_username")):
            claim = environment.get(f"CASE_INTELLIGENCE_OIDC_{name}_CLAIM", default).strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", claim) is None:
                return False
        return environment.get("CASE_INTELLIGENCE_OIDC_TOKEN_AUTH_METHOD", "client_secret_post").strip() in {"client_secret_basic", "client_secret_post"}
    except (OSError, RuntimeError, ValueError):
        return False


def _matter_storage_layout_valid(path: Path) -> bool:
    """Inspect initialization names and existing marker metadata without probes or writes."""
    markers = [path / name for name in STORAGE_MARKERS if (path / name).exists() or (path / name).is_symlink()]
    if len(markers) > 1:
        return False
    marker = markers[0] if markers else path / STORAGE_MARKERS[1]
    names = "recordbench" if marker.name == STORAGE_MARKERS[0] else "exculpata"
    owned = tuple(path / name for name in ("matters", ".matter-purging", "ingestion-staging"))
    if not marker.exists() and not marker.is_symlink():
        return not any(entry.exists() or entry.is_symlink() for entry in owned)
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            return False
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("format_version") != 1
                or payload.get("product") != ("RecordBench" if names == "recordbench" else "Exculpata")
                or not isinstance(payload.get("storage_id"), str)
                or not payload["storage_id"].startswith(f"{names}-storage-")):
            return False
        device = path.stat().st_dev
        return all(not entry.is_symlink() and entry.is_dir() and entry.stat().st_dev == device
                   and os.access(entry, os.R_OK | os.W_OK | os.X_OK) for entry in owned)
    except (OSError, ValueError):
        return False


def _storage_path_text_valid(path: Path) -> bool:
    return not any(ord(character) < 32 or ord(character) == 127 for character in str(path))


def _absolute_tls_path(path: Path | None) -> Path | None:
    # Retain invalid text for the blocking preflight check, and preserve lexical
    # symlinks so making a path absolute cannot bypass their rejection.
    if path is None or not _storage_path_text_valid(path):
        return path
    return path.expanduser().absolute()


def _storage_roots_compatible(node: Path, storage: Path) -> bool:
    """Keep managed sources out of installer-owned state and control paths."""
    if not _storage_path_text_valid(node) or not _storage_path_text_valid(storage):
        return False
    try:
        node = node.expanduser().resolve(strict=False)
        storage = storage.expanduser().resolve(strict=False)
        if node.is_relative_to(storage):
            return False
        controls = [path for name, path in _paths(node).items() if name != "storage"]
        controls.extend(node / name for name in ("compose.env", "installation.json", "releases", "accounts"))
        return not any(storage.is_relative_to(control) for control in controls)
    except (OSError, ValueError):
        return False


def _service_home_ready() -> bool:
    """Inspect the HOME used by Compose without creating client state."""
    try:
        value = os.environ.get("HOME", "")
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            return False
        home = Path(value)
        metadata = home.lstat()
        return (home.is_absolute() and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and stat.S_IMODE(metadata.st_mode) == 0o700
                and os.access(home, os.R_OK | os.W_OK | os.X_OK)
                and not any(parent.is_symlink() for parent in home.parents)
                and _storage_ancestors_safe(home))
    except (OSError, ValueError):
        return False


def _collect_preflight(models: str, args: argparse.Namespace | None = None, *,
                       model_args: argparse.Namespace | None = None,
                       needs_model_staging: bool = True,
                       defer_gpu_free_check: bool = False) -> PreflightResult:
    checks: list[PreflightCheck] = []
    devices: tuple[GpuDevice, ...] = ()

    def add(name: str, passed: bool, observed: str, capability: str, remedy: str,
            *, blocking: bool = True) -> None:
        checks.append(PreflightCheck(name, "pass" if passed else "fail", observed,
                                     capability, blocking, "" if passed else remedy))

    supported = platform.system() == "Linux" and platform.machine().lower() in {"x86_64", "amd64"}
    add("host", supported, "Linux x86-64" if supported else "Unsupported operating system or architecture",
        "Run the Linux application node", "Use a dedicated x86-64 Linux host; this launcher does not provision other platforms.")
    if not supported:
        return PreflightResult(tuple(checks))
    add("ownership", os.geteuid() != 0 and os.getegid() != 0,
        "Non-root account and primary group" if os.geteuid() != 0 and os.getegid() != 0 else "Root account or primary group",
        "Own application state without running containers as root",
        "Run as a dedicated non-root service account with a non-root primary group. Have an administrator assign only the dedicated node and storage directories to it.")
    home_ready = _service_home_ready()
    names = getattr(args or model_args, "names", None) or "exculpata"
    add("service-home", home_ready,
        "Owner-only HOME is accessible" if home_ready else "HOME is missing, unsafe, or inaccessible",
        "Let Compose and build tools create service-account client state",
        f"Have an administrator prepare /var/lib/{names}-home for a new service account, or repair the existing account HOME without moving it. Require mode 0700 and a root/service-owned parent chain without symlinks or replaceable shared directories, set the account home, and start a new service-account session with that HOME. See docs/INSTALL.md#service-account-home.")
    docker = shutil.which("docker") is not None
    add("docker", docker, "Docker CLI available" if docker else "Docker CLI missing",
        "Build and run application containers", "Install Docker Engine and the Compose v2 plugin using the Docker Linux installation instructions in docs/INSTALL.md.")
    for name, command, capability, remedy in (
        ("docker-access", ["docker", "info", "--format", "{{.ServerVersion}}"], "Access the container engine",
         "Start Docker and arrange approved engine access for the service account. Without systemd PID 1, have the host administrator manage dockerd directly; see docs/INSTALL.md#atypical-docker-hosts. Do not make the Docker socket world-writable; engine access is privileged."),
        ("compose", ["docker", "compose", "version", "--short"], "Run the application service definition",
         "Install the Docker Compose v2 plugin, then rerun this command as the service account."),
    ):
        if not docker:
            checks.append(PreflightCheck(name, "unknown", "Docker CLI is required to check this", capability, True, remedy))
            continue
        try:
            passed = _probe(command).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            passed = False
        add(name, passed, "Available" if passed else "Unavailable or diagnostic timed out", capability, remedy)
    if docker:
        try:
            driver_probe = _probe(["docker", "info", "--format", "{{.Driver}}"])
            driver = driver_probe.stdout.strip() if driver_probe.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            driver = ""
        add("docker-storage-driver", bool(driver) and driver != "vfs",
            "VFS copies image layers" if driver == "vfs" else
            ("Non-VFS driver reported" if driver else "Storage driver unavailable"),
            "Preserve disk headroom during image builds",
            "Inspect docker info on the engine host. On nested hosts prefer fuse-overlayfs over vfs when supported; budget build space above the 100 GiB runtime reserve. See docs/INSTALL.md#atypical-docker-hosts.", blocking=False)
    openssl = shutil.which("openssl") is not None
    add("openssl", openssl, "OpenSSL available" if openssl else "OpenSSL missing",
        "Prepare HTTPS", "Install the distribution OpenSSL package; retain HTTPS and secure cookies.")

    options = model_args if model_args is not None else args
    if options is not None:
        try:
            languages = _transcription_languages(options.transcription_languages)
            _model_stage_groups(models, transcription_languages=languages,
                                diarization=bool(options.enable_diarization))
            model_options_ok = True
        except RuntimeError:
            model_options_ok = False
        add("model-options", model_options_ok,
            "Selected model options are compatible" if model_options_ok else "Selected model options conflict or use an unsupported language",
            "Stage the selected AI capabilities",
            "Choose --transcription-languages en, es, or en,es. Enable diarization only with --models transcription or all.")
        if (needs_model_staging and options.non_interactive
                and models in {"transcription", "all"}
                and not (options.enable_diarization and options.diarization_backend == "community-1")):
            add("resource-terms", bool(options.accept_model_terms),
                "Model/resource terms acknowledged" if options.accept_model_terms else "Model/resource terms acknowledgement missing",
                "Acquire the selected transcription models and tokenizer data",
                "Review config/models.json and docs/MODEL_ACQUISITION.md, then pass --accept-model-terms. Nemotron needs no hub account or token.")
        if (needs_model_staging and options.non_interactive
                and options.enable_diarization and options.diarization_backend == "community-1"
                and models in {"transcription", "all"}):
            add("model-terms", bool(options.accept_model_terms),
                "Diarization terms acknowledged" if options.accept_model_terms else "Diarization terms acknowledgement missing",
                "Stage the selected gated speaker module",
                "Accept the selected model's terms, then pass --accept-model-terms.")
            add("model-token-input", bool(options.hf_token_stdin),
                "Token input selected; no token read" if options.hf_token_stdin else "One-time token input not selected",
                "Authorize one-time gated model staging without retaining credentials",
                "Pass --hf-token-stdin and supply a read-only token through controlled standard input when staging runs.")
    if args is not None:
        existing_resume = False
        if args.resume:
            try:
                _installed_release(args.root)
                existing_resume = True
            except RuntimeError:
                pass
        account_directory = (getattr(args, "account_root", args.root / "accounts")
                             if args.enable_account_management else args.root / "secrets")
        account_status = "missing"
        if existing_resume and args.auth in {None, "local"}:
            account_status = _saved_account_status(account_directory / "local-accounts.json",
                                                   managed=args.enable_account_management)
            if account_status == "missing":
                try:
                    _assert_unfinished_account_bootstrap(args.root)
                except RuntimeError:
                    account_status = "recovery-required"
            if account_status != "missing":
                valid = account_status == "valid"
                add("saved-node", valid,
                    "Saved local account store is valid" if valid else "Saved local account store is unsafe or invalid",
                    "Resume using the existing attributed account store",
                    "Restore the saved canonical account file with owner-only mode 0600, safe ownership and ancestry, and valid account JSON. Browser management requires a dedicated mode-0700 account directory and version 2; migrate version 1 explicitly with a backup before enabling it.")
        needs_local_account = args.auth in {None, "local"} and (
            not existing_resume or account_status == "missing")
        if needs_local_account:
            # Share account-admin's NFC and Unicode control policy; names are
            # reported only as valid/invalid, with no runtime dependency.
            login = (args.admin_username if args.admin_username is not None else
                     "" if existing_resume else "recordbench.admin").strip().casefold()
            display = (args.admin_display_name if args.admin_display_name is not None else
                       "" if existing_resume else "RecordBench Administrator").strip()
            try:
                _account_format()["normalized_account"](login, display)
                identity_ok = True
            except RuntimeError:
                identity_ok = False
            add("admin-identity", identity_ok,
                "Initial administrator identity is valid" if identity_ok else "Initial administrator identity is invalid",
                "Create the initial local administrator account",
                "Choose --admin-username with 3-128 letters, numbers, dots, dashes, underscores or @, starting with a letter or number. Choose a nonempty --admin-display-name of at most 160 NFC-normalized characters without Unicode control or formatting characters.")
        if args.non_interactive and not args.dry_run and needs_local_account:
            add("admin-password-input", bool(args.password_stdin),
                "Password input selected; no password read" if args.password_stdin else "Initial administrator password input not selected",
                "Create the initial local administrator account",
                "Pass --password-stdin and supply the initial administrator password through controlled standard input when installation runs.")
        if existing_resume and args.auth in {"oidc", "kerberos"}:
            valid = _saved_provider_options_valid(args)
            add("saved-provider-options", valid,
                "Saved provider settings are valid" if valid else "Saved provider settings are invalid or disagree with canonical credentials",
                "Resume with the retained identity provider boundary",
                "Repair the retained provider settings in the installed application environment file and matching Compose identity. Keep canonical secret-file references, a readable valid CA certificate in the secrets mount when configured, valid provider options and the configured external origin; replacement command-line values do not change saved settings.")
            if args.auth == "oidc":
                valid = _oidc_secret_source_valid(args.root / "secrets/oidc-client-secret", private=True)
                add("oidc-secret-input", valid,
                    "Saved OIDC secret is valid; contents not reported" if valid else "Saved OIDC secret is missing, unsafe or invalid",
                    "Start the configured OIDC provider",
                    "Restore secrets/oidc-client-secret as an owned mode-0600 regular file. UTF-8 content must contain 16–4096 characters after trailing CR/LF removal, without embedded CR/LF or NUL.")
            else:
                valid = _credential_source_available(args.root / "secrets/recordbench.keytab", private=True)
                add("kerberos-keytab-input", valid,
                    "Saved keytab metadata is valid" if valid else "Saved keytab is missing or unsafe",
                    "Start the configured Kerberos gateway",
                    "Restore secrets/recordbench.keytab as a nonempty owned mode-0600 regular file of at most 1 MiB. The proxy still validates its HTTP service principal at startup.")
                valid = _kerberos_proxy_source_valid(args.root / "secrets/kerberos-proxy-secret")
                add("kerberos-proxy-secret", valid,
                    "Saved proxy secret is valid; contents not reported" if valid else "Saved proxy secret is missing, unsafe or invalid",
                    "Preserve the gateway's trusted-proxy identity boundary",
                    "Restore secrets/kerberos-proxy-secret as an owned mode-0600 regular file containing 32–256 ASCII letters, digits, underscores or dashes, with optional trailing CR/LF.")
        if not existing_resume:
            if args.auth in {"oidc", "kerberos"}:
                valid = _identity_options_valid(args)
                add("identity-options", valid,
                    "Identity provider settings are valid" if valid else "Identity provider settings are invalid",
                    "Configure an identity provider accepted by the runtime",
                    "Use an HTTPS issuer and non-loopback server name for OIDC, a nonempty client ID of at most 512 characters, and group names of at most 256 characters. Kerberos requires a dotted realm and at least one valid admission group. Limit allowed/admin groups to 100/50; omit control characters. See docs/INSTALL.md.")
            for auth, source, name, option in (
                ("oidc", args.oidc_client_secret_file, "oidc-secret-input", "--oidc-client-secret-file"),
                ("kerberos", args.kerberos_keytab, "kerberos-keytab-input", "--kerberos-keytab"),
            ):
                if args.auth == auth and (source is not None or (args.non_interactive and not args.dry_run)):
                    available = (_oidc_secret_source_valid(source) if auth == "oidc"
                                 else _credential_source_available(source))
                    observation = ("OIDC secret source is valid; contents not reported" if auth == "oidc"
                                   else "Credential source metadata is available; contents not read")
                    requirement = ("UTF-8 with 16–4096 characters after stripping trailing CR/LF, without embedded CR/LF or NUL"
                                   if auth == "oidc" else "nonempty")
                    add(name, available,
                        observation if available else "Required credential source is missing, unsafe or invalid",
                        "Configure the selected identity provider",
                        f"Supply {option} as a readable regular file of at most 1 MiB without a symbolic link. It must be {requirement}.")
        if args.auth == "kerberos" and (existing_resume or not args.dry_run):
            host_join = Path("/var/lib/sss/pipes").is_dir() and Path("/etc/krb5.conf").is_file()
            add("kerberos-host", host_join,
                "Host SSSD and Kerberos configuration paths are present" if host_join else "Host SSSD or Kerberos configuration is missing",
                "Connect the Kerberos gateway to the host identity service",
                "Complete the host SSSD/NSS join and Kerberos configuration before installation. Presence checks do not prove a working identity exchange.")
        storage_root = args.storage_root or args.root / "matter-storage"
        compatible = _storage_roots_compatible(args.root, storage_root)
        add("storage-separation", compatible,
            "Managed storage is separate from node control paths" if compatible else "Managed storage overlaps node control paths or uses an invalid path",
            "Keep source storage separate from installation configuration",
            "Use the default matter-storage directory, a separate dedicated directory, or a custom child outside config, secrets, runtime, transcription, state, models, tls, accounts, releases, compose.env and installation.json. Matter storage cannot equal or contain the node root.")
        for name, path in (("node-storage", args.root), ("matter-storage", storage_root)):
            try:
                path, ancestor, safe = _storage_path_status(path)
                writable = safe and os.access(ancestor, os.W_OK | os.X_OK)
                if name == "node-storage" and safe and path.is_dir():
                    empty_or_resume = existing_resume or not any(path.iterdir())
                    add("node-empty", empty_or_resume,
                        "Existing root can be prepared" if empty_or_resume else "Installation root already contains files",
                        "Prepare a dedicated installation root",
                        "Choose a new empty node directory. Use --resume only when this directory belongs to the RecordBench node being resumed.")
                add(name, writable, "Directory access checks pass (no write attempted)" if writable else "Unsafe path, ownership, or directory access",
                    "Create private application state" if name == "node-storage" else "Store and process admitted sources",
                    "Choose a dedicated absolute directory without symlinks or control characters, owned by the service account. Use an existing service-owned creation directory without group or other write access, beneath root-owned or service-owned protected parents. The service account needs read and search access to every ancestor; do not use a home directory or shared export root.")
                if name == "matter-storage" and writable:
                    layout_ok = _matter_storage_layout_valid(path)
                    add("matter-storage-layout", layout_ok,
                        "Managed storage initialization metadata is compatible" if layout_ok else "Managed storage initialization names conflict or existing metadata is invalid",
                        "Initialize or reopen the managed source boundary",
                        "Choose an empty dedicated matter storage root, or an intact initialized RecordBench storage root. Without a valid marker, matters, .matter-purging and ingestion-staging must not exist; do not delete existing data to bypass this check.")
                free = shutil.disk_usage(ancestor).free / (1024 ** 3) if safe else None
                add(name + "-reserve", free is not None and free > 100,
                    f"{free:.1f} GiB free" if free is not None else "Capacity could not be checked safely",
                    "Admit sources above the configured 100 GiB storage reserve",
                    "Provide more than 100 GiB free on this filesystem, plus capacity for images, models and matter data. See profile targets in docs/INSTALL.md.")
                target = {"none": 150, "review": 200, "transcription": 300, "all": 300}[models]
                if name == "node-storage":
                    add("capacity-target", free is not None and free >= target,
                        f"Alpha evaluation target: {target} GiB before matter data",
                        "Leave headroom for images and selected models",
                        "Plan additional SSD capacity for the selected profile. These evaluation targets are not validated minimums.", blocking=False)
            except (OSError, RuntimeError, KeyError, ValueError):
                add(name, False, "Directory metadata unavailable", "Inspect storage before installation",
                    "Ask the storage administrator to restore directory access, then rerun preflight.")
        try:
            _valid_host(args.server_name or "recordbench.example.test")
            server_name_ok = True
        except RuntimeError:
            server_name_ok = False
        add("server-name", server_name_ok,
            "Server name is valid" if server_name_ok else "Server name is invalid",
            "Configure the HTTPS gateway identity",
            "Choose --server-name using letters, digits, dots and hyphens, starting and ending with a letter or digit.")
        bind = args.bind_address if args.bind_address is not None else "127.0.0.1"
        try:
            ipaddress.ip_address(bind)
            bind_ok = "%" not in bind and not any(ord(character) < 32 for character in bind)
        except ValueError:
            bind_ok = False
        add("bind-address", bind_ok,
            "Bind address is an IP literal" if bind_ok else "Bind address is invalid",
            "Bind the private HTTPS gateway",
            "Use --bind-address with an IPv4 or unbracketed, unscoped IPv6 literal, without a hostname, port or control characters. Set the port separately with --https-port.")
        args.tls_cert = _absolute_tls_path(args.tls_cert)
        args.tls_key = _absolute_tls_path(args.tls_key)
        pair = bool(args.tls_cert) == bool(args.tls_key)
        supplied = bool(args.tls_cert and args.tls_key)
        readable = supplied and all(_storage_path_text_valid(path) and path.is_file()
                                    and not path.is_symlink() and os.access(path, os.R_OK)
                                    for path in (args.tls_cert, args.tls_key))
        tls_ok = pair and (readable if supplied else bind in {"127.0.0.1", "::1"})
        add("tls", tls_ok, "Readable supplied TLS pair; trust must be verified" if readable else
            "Loopback smoke certificate will be generated during installation" if tls_ok else "TLS choice incomplete or files unavailable",
            "Reach the private HTTPS gateway with secure sessions",
            "Supply both --tls-cert and --tls-key as readable regular files with no control characters in their paths. LAN binding requires an organization-trusted pair; use the default loopback bind for local evaluation.")
        add("https-port", 1 <= args.https_port <= 65535, "Valid port" if 1 <= args.https_port <= 65535 else "Invalid port",
            "Bind the HTTPS gateway", "Choose --https-port between 1 and 65535.")

    if not _required_gpu_count(models):
        try:
            _resolve_gpu_plans(options or _parser().parse_args(["--models", models]), models, ())
            gpu_options_ok = True
        except RuntimeError:
            gpu_options_ok = False
        add("gpu-options", gpu_options_ok,
            "GPU option syntax is valid for CPU evaluation" if gpu_options_ok else "GPU option values or selections conflict",
            "Configure the selected CPU evaluation profile",
            "Remove GPU overrides when using --models none, or choose values accepted by the GPU/model options. CPU evaluation does not require a GPU.")
        checks.append(PreflightCheck("gpu", "pass", "GPU optional for CPU evaluation",
            "Intake, extraction, OCR, word search, source review and exports; no generated answers or transcription",
            False, ""))
    else:
        try:
            if shutil.which("nvidia-smi") is None:
                raise RuntimeError("NVIDIA driver diagnostic unavailable")
            inventory = _probe(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free,compute_cap", "--format=csv,noheader,nounits"])
            if inventory.returncode:
                raise RuntimeError("NVIDIA driver diagnostic failed")
            devices = _parse_gpu_inventory(inventory.stdout)
            if not devices or any(device.compute_capability is None for device in devices):
                raise RuntimeError("GPU inventory or compute capability unavailable")
            plan_args = options or _parser().parse_args(["--models", models])
            # An update has not stopped its current models yet. Check physical
            # compatibility here, retaining the real inventory in the result.
            # Actual free-memory admission remains mandatory after its own
            # runtime stops; this internal option is never a CLI bypass.
            planning_devices = (tuple(replace(device, free_mib=device.total_mib) for device in devices)
                                if defer_gpu_free_check else devices)
            _resolve_gpu_plans(plan_args, models, planning_devices)
            add("gpu", True,
                "Saved GPU plan fits physical capacity; actual free-memory admission is deferred until this node stops"
                if defer_gpu_free_check else "Selected GPU and model plan fits current reported hardware",
                "Run the selected local AI tasks", "")
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            add("gpu", False, "Selected GPU/model plan unavailable, incompatible, or short of free memory",
                "Generate cited answers" if models == "review" else "Transcribe recordings" if models == "transcription" else "Generate cited answers and transcribe recordings",
                "Install the NVIDIA driver and Container Toolkit; provide compute capability 7.5 or newer and sufficient free VRAM for the selected model/GPU options. Saved bfloat16 precision requires 8.0 or newer on every generator GPU. Stop competing GPU work or choose a smaller model. Use --models none for CPU evaluation.")
        # Check the engine configuration without pulling an image or launching a
        # container. Actual offline model/container readiness is a later gate.
        try:
            runtime_probe = _probe(["docker", "info", "--format", "{{json .Runtimes}}"] ) if docker else None
            runtimes = json.loads(runtime_probe.stdout) if runtime_probe and runtime_probe.returncode == 0 else None
            registered = isinstance(runtimes, dict) and "nvidia" in runtimes
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            registered = False
        add("gpu-runtime", bool(registered), "NVIDIA runtime registered" if registered else "NVIDIA runtime registration not confirmed",
            "Make NVIDIA devices available to model containers",
            "Install and configure NVIDIA Container Toolkit for Docker using docs/INSTALL.md, then restart Docker through the approved operator procedure.")
    return PreflightResult(tuple(checks), devices)


def _render_preflight(console: Console, result: PreflightResult) -> None:
    console.phase(1, "HOST HANDSHAKE", "Read-only prerequisites; no state created or services started")
    for check in result.checks:
        marker = "OK" if check.state == "pass" else "BLOCK" if check.blocking else "NOTE"
        console.line(f"  [{marker}] {check.name}: {check.observed}")
        console.line(f"          Enables: {check.required_capability}")
        if check.remedy:
            console.line(f"          Next: {check.remedy}")
    console.line("  Prerequisites pass; installation and acceptance are still required." if result.ready else
                 "  Resolve blocking items and rerun ./install preflight with the same options.")


def _preflight(console: Console, *, models: str, dry_run: bool,
               args: argparse.Namespace | None = None,
               model_args: argparse.Namespace | None = None,
               needs_model_staging: bool = True,
               defer_gpu_free_check: bool = False) -> tuple[GpuDevice, ...]:
    result = _collect_preflight(models, args, model_args=model_args,
                                needs_model_staging=needs_model_staging,
                                defer_gpu_free_check=defer_gpu_free_check)
    _render_preflight(console, result)
    if not result.ready:
        raise RuntimeError("installation prerequisites are incomplete; see the checklist above")
    return result.devices


def _paths(root: Path, storage_root: Path | None = None) -> dict[str, Path]:
    return {
        "config": root / "config",
        "secrets": root / "secrets",
        "accounts": root / "accounts",
        "runtime": root / "runtime",
        "storage": storage_root or root / "matter-storage",
        "transcription": root / "transcription",
        "state": root / "state",
        "models": root / "models",
        "tls": root / "tls",
    }


def _mkdir_private(component: str, *, dir_fd: int) -> None:
    """Create relative to a held parent with owner access under any caller umask.

    Umask is process-global: change it only in an isolated child, before mkdir.
    The child inherits one already-validated directory descriptor and does not
    traverse an attacker-supplied path or repair permissions through a pathname.
    """
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import os,sys\nos.umask(0)\n"
         "try: os.mkdir(sys.argv[1], 0o700, dir_fd=int(sys.argv[2]))\n"
         "except FileExistsError: sys.exit(17)\n"
         "except OSError: sys.exit(1)\n",
         component, str(dir_fd)],
        pass_fds=(dir_fd,), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode == 17:
        raise FileExistsError("storage component was created concurrently")
    if result.returncode:
        raise OSError("storage component could not be created privately")


def _create_private_directory(path: Path) -> None:
    """Walk held, non-symlink directory descriptors and create each component privately.

    No path-based chmod can follow a replacement symlink. Existing ancestors are
    revalidated at use, and only the selected leaf is tightened to owner-only.
    """
    uid = os.geteuid()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            parent = os.fstat(descriptor)
            if parent.st_uid not in {0, uid} or (
                parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX
            ):
                raise RuntimeError("storage parent is replaceable by another account")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if parent.st_uid != uid or parent.st_mode & 0o022:
                    raise RuntimeError("storage creation directory must be service-owned and protected")
                # The child preserves owner access even when umask masks 0700.
                try:
                    _mkdir_private(component, dir_fd=descriptor)
                except FileExistsError:
                    pass  # Revalidate a concurrently created entry below.
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if metadata.st_uid not in {0, uid} or (
                metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX
            ):
                raise RuntimeError("storage directory is replaceable by another account")
            if index == len(components) - 1:
                if metadata.st_uid != uid or metadata.st_mode & 0o022:
                    raise RuntimeError("selected storage must be service-owned and protected")
                os.fchmod(descriptor, 0o700)
    except OSError as exc:
        raise RuntimeError("storage directory changed or could not be prepared safely") from exc
    finally:
        os.close(descriptor)


def _prepare_directories(
    console: Console,
    root: Path,
    *,
    storage_root: Path | None,
    resume: bool,
    dry_run: bool,
) -> dict[str, Path]:
    console.phase(2, "CLAIM NODE STORAGE", "Creating narrow, owner-controlled state boundaries")
    if not root.is_absolute() or root == Path("/"):
        raise RuntimeError("--root must be an exact absolute directory other than /")
    if root.is_symlink():
        raise RuntimeError("installation root cannot be a symbolic link")
    if root.exists() and any(root.iterdir()) and not resume:
        raise RuntimeError("installation root is not empty; use --resume only for this RecordBench node")
    selected_storage = storage_root.expanduser() if storage_root else None
    for selected in (root, selected_storage):
        if selected is not None and any(part.is_symlink() for part in (selected, *selected.parents)):
            raise RuntimeError("storage paths cannot contain symbolic links")
    selected_storage = selected_storage.resolve(strict=False) if selected_storage else None
    if selected_storage is not None and (
        not selected_storage.is_absolute() or selected_storage == Path("/")
    ):
        raise RuntimeError("--storage-root must be an exact absolute directory other than /")
    if selected_storage is not None and selected_storage.exists() and selected_storage.is_symlink():
        raise RuntimeError("matter storage root cannot be a symbolic link")
    paths = _paths(root, selected_storage)
    if not dry_run:
        _create_private_directory(root)
        for value in paths.values():
            _create_private_directory(value)
    for name, value in paths.items():
        console.ok(f"{name:13s} -> {value}")
    return paths


def _password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        value = sys.stdin.readline(4097).rstrip("\r\n")
    elif args.non_interactive:
        raise RuntimeError("local non-interactive installation requires --password-stdin")
    else:
        value = getpass.getpass("Initial administrator password: ")
        if value != getpass.getpass("Confirm administrator password: "):
            raise RuntimeError("administrator passwords did not match")
    if not 14 <= len(value) <= 1024 or "\x00" in value:
        raise RuntimeError("administrator password must contain 14 to 1,024 characters without NUL")
    return value


def _assert_unfinished_account_bootstrap(root: Path) -> None:
    # Even a stale or damaged seal is evidence of prior provisioning, never
    # authorization to replace a missing attributed account store.
    marker = _provision_marker(root)
    if marker.exists() or marker.is_symlink():
        raise RuntimeError("saved local account store is missing after provisioning; restore the canonical account backup before resuming")


@contextmanager
def _bootstrap_password_input(args: argparse.Namespace, root: Path, auth: str):
    account_root = (getattr(args, 'account_root', root / 'accounts')
                    if args.enable_account_management else root / 'secrets')
    values = []
    try:
        if not args.dry_run and auth == 'local' and not (account_root / 'local-accounts.json').exists():
            _assert_unfinished_account_bootstrap(root)
            values.append(_password(args))
        yield values
    finally:
        values.clear()


def _hf_token(args: argparse.Namespace) -> str:
    if args.hf_token_stdin:
        value = sys.stdin.readline(4097).rstrip("\r\n")
    elif args.non_interactive:
        raise RuntimeError("transcription staging requires --hf-token-stdin")
    else:
        value = getpass.getpass("Read-only Hugging Face token (used once, never retained): ")
    if not value or len(value) > 4096:
        raise RuntimeError("Hugging Face token is missing or invalid")
    return value


def _configure(
    console: Console,
    args: argparse.Namespace,
    root: Path,
    paths: Mapping[str, Path],
    release_id: str,
    release_path: Path,
    gpu_devices: tuple[GpuDevice, ...],
) -> tuple[str, str, str, str | None, str | None]:
    console.phase(4, "IDENTITY + FRONT DOOR", "Sealing sessions, service credentials, and HTTPS coordinates")
    names = _select_names(args, root)
    product = "Exculpata" if names == "exculpata" else "RecordBench"
    auth = args.auth
    if auth not in IDENTITY_FIELDS:
        raise RuntimeError("identity choices must be collected and checked before configuration")
    models = args.models or "none"
    transcription_languages = _transcription_languages(
        args.transcription_languages
    )
    _model_stage_groups(
        models,
        transcription_languages=transcription_languages,
        diarization=bool(args.enable_diarization),
    )
    gpu_plan, review_model = _resolve_gpu_plans(args, models, gpu_devices)
    args.review_model_profile = review_model.profile
    server_name = _valid_host(
        args.server_name
        or _ask("RecordBench hostname", "recordbench.example.test", non_interactive=args.non_interactive)
    )
    bind = args.bind_address or _ask(
        "HTTPS bind address", "127.0.0.1", non_interactive=args.non_interactive
    )
    if not 1 <= args.https_port <= 65535:
        raise RuntimeError("HTTPS port is invalid")
    profile_values = []
    if models in {"all", "review"}:
        profile_values.append("ai")
    if models in {"all", "transcription"}:
        profile_values.append("transcription")

    postgres_password = "dry-run-secret" if args.dry_run else _secret(paths["secrets"] / "postgres-password")
    if not args.dry_run:
        _secret(paths["secrets"] / "transcription-token")
        dsn = f"host=postgres port=5432 dbname=recordbench user=recordbench password={postgres_password}\n"
        dsn_path = paths["secrets"] / "postgres-dsn"
        if not dsn_path.exists():
            _private_write(dsn_path, dsn)

    tls_cert = _absolute_tls_path(args.tls_cert)
    tls_key = _absolute_tls_path(args.tls_key)
    if bool(tls_cert) != bool(tls_key):
        raise RuntimeError("--tls-cert and --tls-key must be supplied together")
    if tls_cert is None:
        if bind not in {"127.0.0.1", "::1"}:
            raise RuntimeError("a LAN bind requires an organization-trusted --tls-cert and --tls-key")
        tls_cert, tls_key = paths["tls"] / "tls.crt", paths["tls"] / "tls.key"
        if not tls_cert.exists() and not args.dry_run:
            _run(
                console,
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
                    "-days", "825", "-subj", f"/CN={server_name}",
                    "-addext", f"subjectAltName=DNS:{server_name},IP:127.0.0.1",
                    "-keyout", str(tls_key), "-out", str(tls_cert),
                ],
            )
            os.chmod(tls_key, 0o600)
        console.warn("Generated a loopback smoke certificate; replace it before staff access")
    for value in (tls_cert, tls_key):
        if not args.dry_run and (value is None or not value.is_file() or value.is_symlink()):
            raise RuntimeError("TLS material is unavailable or unsafe")

    compose_values = {
        "RECORDBENCH_COMPOSE_PROJECT": f"{names}-{hashlib_short(root)}",
        "RECORDBENCH_IMAGE_NAMESPACE": f"{names}-{hashlib_short(root)}",
        "RECORDBENCH_RELEASE_ID": release_id,
        "RECORDBENCH_UID": os.getuid(),
        "RECORDBENCH_GID": os.getgid(),
        "RECORDBENCH_CONFIG_ROOT": paths["config"],
        "RECORDBENCH_SECRETS_ROOT": paths["secrets"],
        "RECORDBENCH_RUNTIME_ROOT": paths["runtime"],
        "RECORDBENCH_STORAGE_ROOT": paths["storage"],
        "RECORDBENCH_TRANSCRIPTION_ROOT": paths["transcription"],
        "RECORDBENCH_STATE_ROOT": paths["state"],
        "RECORDBENCH_MODEL_ROOT": paths["models"],
        "RECORDBENCH_BIND_ADDRESS": bind,
        "RECORDBENCH_HTTPS_PORT": args.https_port,
        "RECORDBENCH_SERVER_NAME": server_name,
        "RECORDBENCH_TLS_CERT": tls_cert,
        "RECORDBENCH_TLS_KEY": tls_key,
        "RECORDBENCH_GATEWAY_UPSTREAM": "app:8786",
        "COMPOSE_PROFILES": ",".join(profile_values),
        **gpu_plan.environment(),
        **review_model.environment(),
    }
    if names == "exculpata":
        compose_values["EXCULPATA_APP_ENV_FILE"] = "exculpata.env"
        compose_values["EXCULPATA_STORAGE_NAMES"] = "exculpata"
    if (root / "compose.env").is_file():
        saved = _dotenv(root / "compose.env")
        for key in ("RECORDBENCH_COMPOSE_PROJECT", "RECORDBENCH_IMAGE_NAMESPACE"):
            compose_values[key] = _env(key, "recordbench", environ=saved)
    app_values: dict[str, object] = {
        "CASE_INTELLIGENCE_AUTH_MODE": auth,
        "CASE_INTELLIGENCE_SECURE_COOKIE": 1,
        "CASE_INTELLIGENCE_GENERATOR_BACKEND": "openai" if models in {"all", "review"} else "",
        "CASE_INTELLIGENCE_GENERATOR_URL": "http://generator:8000",
        "CASE_INTELLIGENCE_GENERATOR_MODEL": review_model.served_name,
        "CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING": 1,
        "CASE_INTELLIGENCE_GENERATOR_TIMEOUT": 180,
        "CASE_INTELLIGENCE_GENERATION_CONCURRENCY": (
            review_model.generation_concurrency
        ),
        "CASE_INTELLIGENCE_TRANSCRIPTION_URL": "http://transcription-api:8510" if models in {"all", "transcription"} else "",
        "CASE_INTELLIGENCE_BACKGROUND_INGESTION": 1,
        "CASE_INTELLIGENCE_INGESTION_WORKERS": 2,
        "CASE_INTELLIGENCE_ANSWER_WORKERS": 2,
        "CASE_INTELLIGENCE_RESEARCH_WORKERS": 1,
        "CASE_INTELLIGENCE_FULL_REVIEW_WORKERS": 1,
        "CASE_INTELLIGENCE_FULL_REVIEW_SOURCE_CONCURRENCY": 2,
        "CASE_INTELLIGENCE_OCR_MODE": "expanded",
        "CASE_INTELLIGENCE_OCR_LANGUAGE": "eng",
        "CASE_INTELLIGENCE_MAX_OCR_PAGES": 500,
        "CASE_INTELLIGENCE_MALWARE_SCAN_MODE": "extended",
        "CASE_INTELLIGENCE_MAINTENANCE_ENABLED": 1,
        "CASE_INTELLIGENCE_MAINTENANCE_INTERVAL_SECONDS": 900,
        "CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY": 1,
        "CASE_INTELLIGENCE_MATTER_QUOTA_GIB": 250,
        "CASE_INTELLIGENCE_UPLOAD_COLLECTION_GIB": 100,
        "CASE_INTELLIGENCE_MEDIA_FILE_GIB": 100,
        "CASE_INTELLIGENCE_DOCUMENT_FILE_MIB": 512,
        "CASE_INTELLIGENCE_STORAGE_RESERVE_GIB": 100,
    }
    admin_username = admin_display = None
    if auth == "local":
        if args.enable_account_management:
            compose_values["RECORDBENCH_LOCAL_ACCOUNT_ROOT"] = paths["accounts"]
            app_values["CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE"] = "/var/lib/recordbench-accounts/local-accounts.json"
            app_values["CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT"] = "/var/lib/recordbench-accounts"
        else:
            app_values["CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE"] = "/run/recordbench-secrets/local-accounts.json"
        admin_username, admin_display = _account_format()["normalized_account"](
            args.admin_username, args.admin_display_name)
    elif auth == "oidc":
        app_values.update(
            {
                "CASE_INTELLIGENCE_EXTERNAL_ORIGIN": f"https://{server_name}" + (f":{args.https_port}" if args.https_port != 443 else ""),
                "CASE_INTELLIGENCE_OIDC_ISSUER": args.oidc_issuer,
                "CASE_INTELLIGENCE_OIDC_CLIENT_ID": args.oidc_client_id,
                "CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE": "/run/recordbench-secrets/oidc-client-secret",
                "CASE_INTELLIGENCE_OIDC_ALLOWED_GROUPS": args.oidc_allowed_groups,
                "CASE_INTELLIGENCE_OIDC_ADMIN_GROUPS": args.oidc_admin_groups,
                "CASE_INTELLIGENCE_OIDC_SCOPES": "openid profile email",
            }
        )
        if not args.dry_run and not (paths["secrets"] / "oidc-client-secret").exists():
            if args.oidc_client_secret_file is not None:
                _private_copy(
                    args.oidc_client_secret_file.expanduser(),
                    paths["secrets"] / "oidc-client-secret",
                )
            elif args.non_interactive:
                raise RuntimeError(
                    "non-interactive OIDC installation requires "
                    "--oidc-client-secret-file"
                )
            else:
                _private_write(
                    paths["secrets"] / "oidc-client-secret",
                    getpass.getpass("OIDC client secret: ") + "\n",
                )
    else:
        realm = args.kerberos_realm.strip().upper()
        compose_values["RECORDBENCH_KERBEROS_PRINCIPAL"] = f"HTTP/{server_name}@{realm}"
        app_values.update(
            {
                "CASE_INTELLIGENCE_KERBEROS_REALM": realm,
                "CASE_INTELLIGENCE_KERBEROS_ALLOWED_GROUPS": args.kerberos_allowed_groups,
                "CASE_INTELLIGENCE_KERBEROS_ADMIN_GROUPS": args.kerberos_admin_groups,
                "CASE_INTELLIGENCE_KERBEROS_PROXY_SECRET_FILE": "/run/recordbench-secrets/kerberos-proxy-secret",
            }
        )
        if not args.dry_run:
            _secret(paths["secrets"] / "kerberos-proxy-secret")
            target_keytab = paths["secrets"] / "recordbench.keytab"
            if not target_keytab.exists():
                source_keytab = args.kerberos_keytab
                if source_keytab is None:
                    raise RuntimeError("Kerberos installation requires --kerberos-keytab")
                _private_copy(source_keytab.expanduser(), target_keytab)
            if not Path("/var/lib/sss/pipes").is_dir() or not Path("/etc/krb5.conf").is_file():
                raise RuntimeError("Kerberos mode requires a working host SSSD/NSS join and /etc/krb5.conf")
        console.ok("Kerberos keytab and host NSS bridge selected")

    transcription_values = {
        "TRANSCRIPTION_V2_PIPELINE": "mock",
        "TRANSCRIPTION_V2_WORKER_ID": "recordbench-transcription-worker-1",
        "TRANSCRIPTION_V2_WORKER_POLL_SECONDS": 1,
        "TRANSCRIPTION_V2_LANGUAGE_CODES": (
            "auto," + ",".join(transcription_languages)
        ),
        "TRANSCRIPTION_V2_MODEL_MANIFEST": "/models/huggingface/hub/approved-model-manifest.json",
        "TRANSCRIPTION_V2_DIARIZATION_BACKEND": args.diarization_backend,
        "TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH": (
            ("/models/huggingface/hub/models--nvidia--Nemotron-3-Diarization/"
             "snapshots/0f087031414a6616bda8228f447d915a70a25720"
             if args.diarization_backend == "nemotron" else
            "/models/huggingface/hub/models--pyannote--speaker-diarization-community-1/"
            "snapshots/3533c8cf8e369892e6b79ff1bf80f7b0286a54ee/config.yaml")
            if args.enable_diarization
            else ""
        ),
        "TRANSCRIPTION_V2_ALLOW_DEGRADED_DIARIZATION": (
            0 if args.enable_diarization else 1
        ),
        "TRANSCRIPTION_V2_MIN_FREE_VRAM_MB": (
            review_model.transcription_min_free_vram_mib
        ),
        "TRANSCRIPTION_V2_GPU_LOCK_PATH": "/tmp/recordbench-transcription-gpu.lock",
        "TRANSCRIPTION_V2_RETENTION_HOURS": 24,
        "TRANSCRIPTION_V2_MAX_RETENTION_HOURS": 24,
        "TRANSCRIPTION_V2_MAX_ACTIVE_JOB_HOURS": 48,
        "TRANSCRIPTION_V2_MAX_UPLOAD_BYTES": 5368709120,
        "TRANSCRIPTION_V2_MAX_BATCH_UPLOAD_BYTES": 21474836480,
        "TRANSCRIPTION_V2_MAX_FILES": 100,
        "TRANSCRIPTION_V2_MAX_RETAINED_JOBS_PER_OWNER": 100,
        "TRANSCRIPTION_V2_MAX_RETAINED_BYTES_PER_OWNER": 214748364800,
        "TRANSCRIPTION_V2_MAX_RETAINED_JOBS_GLOBAL": 400,
        "TRANSCRIPTION_V2_MAX_RETAINED_BYTES_GLOBAL": 858993459200,
        "TRANSCRIPTION_V2_MIN_FREE_DISK_BYTES": 107374182400,
        "TRANSCRIPTION_V2_MAX_MEDIA_DURATION_SECONDS": 43200,
        "TRANSCRIPTION_V2_DELIVERY_TOKEN_SECONDS": 600,
        "HF_HUB_OFFLINE": 1,
        "TRANSFORMERS_OFFLINE": 1,
        "HF_DATASETS_OFFLINE": 1,
        "PYANNOTE_METRICS_ENABLED": 0,
    }
    if not args.dry_run:
        compose_path = root / "compose.env"
        if compose_path.exists():
            for key, value in compose_values.items():
                _replace_env(compose_path, key, str(value), add_missing_aliases=True)
        else:
            _private_write(compose_path, _env_text(compose_values, f"{product} Compose node", aliases=names == "exculpata"))
        _write_env_values(_app_env(root, names=names), app_values, f"{product} application")
        _write_env_values(paths["config"] / "transcription.env", transcription_values, f"{product} transcription")
        _private_write(
            root / "installation.json",
            json.dumps(
                {
                    "format_version": 1,
                    **({"names": names} if names == "exculpata" else {}),
                    "version": VERSION,
                    "release_id": release_id,
                    "release_path": str(release_path),
                    "node_id": hashlib_short(root),
                    "storage_root": str(paths["storage"]),
                    "auth": auth,
                    "local_account_management": bool(args.enable_account_management),
                    "initial_administrator": admin_username if auth == "local" else None,
                    "initial_administrator_display_name": admin_display if auth == "local" else None,
                    "tls_source": "operator" if args.tls_cert is not None else "loopback-smoke",
                    "models": models,
                    "transcription_languages": list(transcription_languages),
                    "transcription_diarization": bool(args.enable_diarization),
                    "diarization_backend": args.diarization_backend,
                    "gpu_layout": gpu_plan.layout,
                    "gpu_topology": {
                        "visible_devices": len(gpu_devices),
                        "generator": list(gpu_plan.generator_gpus),
                        "transcription": gpu_plan.transcription_gpu,
                        "retrieval_device": gpu_plan.retrieval_device,
                        "retrieval_gpu": (
                            gpu_plan.retrieval_gpu
                            if gpu_plan.retrieval_device == "cuda"
                            else None
                        ),
                    },
                    "review_model_profile": review_model.profile,
                    "server_name": server_name,
                    "profiles": profile_values,
                },
                indent=2,
            )
            + "\n",
            replace=(root / "installation.json").exists(),
        )
    console.ok(f"identity matrix :: {auth}")
    console.ok(f"model payload   :: {models}")
    console.ok(
        "GPU topology    :: "
        f"{gpu_plan.layout}; generator={','.join(gpu_plan.generator_gpus)}; "
        f"transcription={gpu_plan.transcription_gpu}; "
        f"retrieval={gpu_plan.retrieval_device}"
        + (
            f":{gpu_plan.retrieval_gpu}"
            if gpu_plan.retrieval_device == "cuda"
            else ""
        )
        + f"; visible={len(gpu_devices)}"
    )
    assigned = set(gpu_plan.generator_gpus)
    if models in {"transcription", "all"}:
        assigned.add(gpu_plan.transcription_gpu)
    if models in {"review", "all"} and gpu_plan.retrieval_device == "cuda":
        assigned.add(gpu_plan.retrieval_gpu)
    if len(gpu_devices) > len(assigned):
        console.note(
            f"GPU reserve     :: {len(gpu_devices) - len(assigned)} visible "
            "device(s) intentionally unassigned"
        )
    if models in {"review", "all"}:
        console.ok(
            f"review model    :: {review_model.profile} ({review_model.model_id})"
        )
    if not args.dry_run:
        _install_phase(root, "configuration", "complete")
    console.ok(f"front door      :: https://{server_name}:{args.https_port}")
    return auth, models, server_name, admin_username, admin_display


def hashlib_short(path: Path) -> str:
    import hashlib

    return hashlib.sha256(str(path).encode()).hexdigest()[:10]


def _replace_env(path: Path, key: str, value: str, *, add_missing_aliases: bool = False) -> None:
    original = path.read_bytes().decode("utf-8")
    saved = _dotenv(path)
    keys = {key}
    aliased = key.startswith(("RECORDBENCH_", "EXCULPATA_"))
    if aliased:
        suffix = key.removeprefix("RECORDBENCH_").removeprefix("EXCULPATA_")
        keys = {f"EXCULPATA_{suffix}", f"RECORDBENCH_{suffix}"}
    output = []
    for line in original.splitlines(keepends=True):
        current = line.strip().partition("=")[0]
        if current in keys and saved[current] != value:
            ending = line[len(line.rstrip("\r\n")):]
            output.append(f"{current}={json.dumps(value)}{ending}")
        else:
            output.append(line)
    # Updates keep existing aliases. Reconfiguration may introduce a setting,
    # but only when neither alias exists, using the node's current naming style.
    if (not aliased or add_missing_aliases) and keys.isdisjoint(saved):
        if aliased:
            prefix = "EXCULPATA_" if any(name.startswith("EXCULPATA_") for name in saved) else "RECORDBENCH_"
            key = prefix + suffix
        if original and not original.endswith(("\r", "\n")):
            output.append("\n")
        output.append(f"{key}={json.dumps(value)}\n")
    updated = "".join(output)
    if updated != original:
        _private_write(path, updated, replace=True)


def _atomic_private_write(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    _private_write(temporary, value)
    os.replace(temporary, path)


def _provision_marker(root: Path) -> Path:
    return root / "state" / "provisioned.json"


INSTALL_PHASES = ("configuration", "runtime", "storage", "administrator", "models", "prepared", "running", "login_reachable", "basic_review", "selected_capabilities")


def _install_progress(root: Path) -> dict[str, object]:
    installation, _ = _installed_release(root)
    path = root / "state" / "install-progress.json"
    empty = {"format_version": 1, "release_id": installation.get("release_id"), "phases": {}}
    if not path.exists():
        return empty
    metadata = path.lstat()
    if path.is_symlink() or not path.is_file() or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o777 != 0o600 or metadata.st_size > 16_384:
        raise RuntimeError("Installation progress receipt is unsafe; keep the node stopped and inspect its owner-only state directory")
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("format_version") != 1 or not isinstance(value.get("phases"), dict)
            or any(key not in INSTALL_PHASES or not isinstance(state, str) or state not in {"checking", "complete", "incomplete", "not-selected"} for key, state in value["phases"].items())):
        raise RuntimeError("Installation progress receipt format is unsupported; use the matching reviewed installer")
    return value if value.get("release_id") == installation.get("release_id") else empty


def _install_phase(root: Path, phase: str, state: str) -> None:
    if phase not in INSTALL_PHASES or state not in {"checking", "complete", "incomplete", "not-selected"}:
        raise RuntimeError("Installation progress state is invalid")
    progress = _install_progress(root)
    if progress["phases"].get(phase) == state:
        return
    progress["phases"][phase] = state
    progress["observed_unix"] = int(time.time())
    _atomic_private_write(root / "state" / "install-progress.json", json.dumps(progress, indent=2) + "\n")


def _record_prepared_phases(root: Path, installation: Mapping[str, object]) -> None:
    if not _provisioning_complete(root, installation):
        return
    for phase in INSTALL_PHASES[:6]:
        not_selected = (phase == 'administrator' and installation.get('auth') != 'local'
                        or phase == 'models' and installation.get('models') == 'none')
        _install_phase(root, phase, 'not-selected' if not_selected else 'complete')


def _handoff(console: Console, root: Path, *, dry_run: bool = False) -> None:
    if dry_run:
        console.note("PLAN COMPLETE: no node was prepared, launched or signed in")
        return
    installation, _ = _installed_release(root)
    progress = _install_progress(root)["phases"]
    env = _dotenv(root / "compose.env")
    host = _valid_host(str(installation["server_name"]))
    port = int(_env("RECORDBENCH_HTTPS_PORT", "8443", environ=env))
    console.line("\n  INSTALLATION HANDOFF")
    console.ok(f"Open in your browser: https://{host}:{port}/auth/login")
    console.note(f"Sign-in mode: {installation['auth']}")
    username = installation.get("initial_administrator")
    if installation.get("auth") == "local" and isinstance(username, str) and re.fullmatch(r"[a-z0-9][a-z0-9._@-]{2,127}", username):
        console.note(f"Initial administrator username: {username}")
    elif installation.get("auth") == "local":
        console.note("Initial administrator username: not recorded by this older installer; use your issued local account")
    for phase in ("prepared", "running", "login_reachable", "basic_review", "selected_capabilities"):
        console.note(f"{phase.replace('_', ' ')}: {progress.get(phase, 'not verified')}")
    console.note("Browser sign-in: not verified by the installer; sign in and follow Team setup")
    if installation.get("tls_source") == "loopback-smoke":
        console.warn("Loopback smoke certificate: arrange a trusted certificate and matching hostname before team access; do not bypass browser certificate protection")
    else:
        console.note("Use the configured hostname and confirm its certificate is trusted on each team device")
    incomplete = next((name for name in INSTALL_PHASES if progress.get(name) not in {"complete", "not-selected"}), None)
    if incomplete:
        console.warn(f"Next unverified step: {incomplete.replace('_', ' ')}")
        console.note(f"Continue with: ./install install --root {shlex.quote(str(root))} --resume")
    console.note("No password or token is included in this handoff")


def _gateway_probe_address(environment: Mapping[str, str]) -> str:
    address = ipaddress.ip_address(_env("RECORDBENCH_BIND_ADDRESS", "127.0.0.1", environ=environment))
    if address.is_unspecified:
        return "127.0.0.1" if address.version == 4 else "::1"
    return str(address)


def _login_reachable(root: Path) -> bool:
    installation, _ = _installed_release(root)
    environment = _dotenv(root / "compose.env")
    port = int(_env("RECORDBENCH_HTTPS_PORT", "8443", environ=environment))
    address = _gateway_probe_address(environment)
    host = _valid_host(str(installation["server_name"]))
    # Bounded probes of the configured bind never follow redirects. They do not
    # establish browser certificate trust, sign-in, or remote reachability.
    def probe(route: str):
        connection = http.client.HTTPSConnection(address, port, timeout=5, context=ssl._create_unverified_context())
        try:
            connection.request("GET", route, headers={"Host": host})
            response = connection.getresponse()
            return response.status, response.getheader("WWW-Authenticate", ""), response.read(262_144)
        finally:
            connection.close()
    try:
        status, challenge, body = probe("/auth/login")
        if installation.get("auth") == "kerberos":
            health_status, health_challenge, _ = probe("/health")
            return status == health_status == 401 and "Negotiate" in challenge and "Negotiate" in health_challenge
        return status == 200 and (b"Exculpata" in body or b"RecordBench" in body)
    except (OSError, ValueError, http.client.HTTPException):
        return False


def _provisioning_complete(root: Path, installation: Mapping[str, object]) -> bool:
    marker = _provision_marker(root)
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("release_id") == installation.get("release_id")
        and payload.get("auth") == installation.get("auth")
        and payload.get("models") == installation.get("models")
    )


def _seal_provisioning(root: Path, installation: Mapping[str, object]) -> None:
    payload = {
        "format_version": 1,
        "release_id": installation.get("release_id"),
        "auth": installation.get("auth"),
        "models": installation.get("models"),
        "completed_unix": int(time.time()),
    }
    _atomic_private_write(
        _provision_marker(root), json.dumps(payload, indent=2) + "\n"
    )


def _provision(
    console: Console,
    args: argparse.Namespace,
    root: Path,
    auth: str,
    models: str,
    admin_username: str | None,
    admin_display: str | None,
    *, model_cache_verified: bool = False, password_input: list[str] | None = None,
) -> None:
    if password_input is None:
        account_root = (getattr(args, 'account_root', root / 'accounts')
                        if args.enable_account_management else root / 'secrets')
        if auth == 'local' and not (account_root / 'local-accounts.json').exists() and (not admin_username or not admin_display):
            raise RuntimeError('unfinished local installation requires --admin-username and --admin-display-name when resumed')
        with _bootstrap_password_input(args, root, auth) as values:
            return _provision(console, args, root, auth, models, admin_username, admin_display,
                              model_cache_verified=model_cache_verified, password_input=values)
    account_root = (getattr(args, 'account_root', root / 'accounts')
                    if args.enable_account_management else root / 'secrets')
    if auth == 'local' and not (account_root / 'local-accounts.json').exists():
        _assert_unfinished_account_bootstrap(root)
    profiles = ["tools"]
    if not args.dry_run:
        for phase in INSTALL_PHASES[1:]:
            _install_phase(root, phase, "incomplete")
    if models in {"all", "review"}:
        profiles.append("ai")
    if models in {"all", "transcription"}:
        profiles.append("transcription")
    compose = _compose(root, profiles, auth=auth, local_accounts=args.enable_account_management)

    console.phase(5, "FORGE RUNTIME", "Building isolated OCR, review, retrieval, and transcription images")
    if not args.dry_run:
        _install_phase(root, "runtime", "checking")
    targets = _build_targets(auth, models)
    _run(console, [*compose, "build", *targets], dry_run=args.dry_run)
    if not args.dry_run:
        _install_phase(root, "runtime", "complete")
    console.ok("Runtime images forged")

    console.phase(6, "SEAL CONTROL PLANE", "Initializing managed storage and attributed access")
    if args.dry_run:
        storage_root = args.storage_root or root / "matter-storage"
    else:
        installation, _release = _installed_release(root)
        configured_storage = installation.get("storage_root")
        if not isinstance(configured_storage, str):
            raise RuntimeError("installed matter storage path is invalid")
        storage_root = Path(configured_storage)
    markers = tuple(storage_root / name for name in STORAGE_MARKERS)
    if not args.dry_run:
        _install_phase(root, "storage", "checking")
    if not any(marker.exists() or marker.is_symlink() for marker in markers):
        _run(
            console,
            [*compose, "run", "--rm", "--no-deps", "account-admin", "storage", "init", "--root", "/var/lib/recordbench/matter-storage"],
            dry_run=args.dry_run,
        )
    if not args.dry_run:
        # The storage tool validates the existing ownership marker without reset.
        _run(console, [*compose, "run", "--rm", "--no-deps", "account-admin", "storage", "status", "--root", "/var/lib/recordbench/matter-storage"], capture=True)
        _install_phase(root, "storage", "complete")
    account_directory = (getattr(args, "account_root", root / "accounts")
                         if args.enable_account_management else root / "secrets")
    account_container_file = "/var/lib/recordbench-accounts/local-accounts.json" if args.enable_account_management else "/run/recordbench-secrets/local-accounts.json"
    if not args.dry_run:
        _install_phase(root, "administrator", "checking" if auth == "local" else "not-selected")
    if auth == "local" and not (account_directory / "local-accounts.json").exists():
        if not admin_username or not admin_display:
            raise RuntimeError(
                "unfinished local installation requires --admin-username and "
                "--admin-display-name when resumed"
            )
        if not args.dry_run and not password_input:
            raise RuntimeError('Initial account state changed; rerun preflight before initialization')
        password = "dry-run-password-placeholder" if args.dry_run else password_input.pop()
        try:
            _run(
                console,
                [*compose, "run", "--rm", "--no-deps", "-T", "account-admin", "accounts", "init", "--file", account_container_file, "--username", str(admin_username), "--display-name", str(admin_display), "--password-stdin"],
                input_value=password + "\n",
                dry_run=args.dry_run,
            )
        finally:
            password = ""
    if not args.dry_run and auth == "local":
        _run(console, [*compose, "run", "--rm", "--no-deps", "account-admin", "accounts", "list", "--file", account_container_file], capture=True)
        _install_phase(root, "administrator", "complete")
    password_input.clear()
    console.ok("Managed storage boundary and identity control plane sealed")

    if models != "none":
        if not args.dry_run:
            _install_phase(root, "models", "checking")
        console.phase(7, "OPEN MODEL VAULT", "Acquiring exact revisions, hashing artifacts, then cutting network access")
        languages = _transcription_languages(args.transcription_languages)
        groups = ",".join(
            _model_stage_groups(
                models,
                transcription_languages=languages,
                diarization=bool(args.enable_diarization),
            )
        )
        verification = _run(console, [*compose, "run", "--rm", "--no-deps", "-T", "model-stager", "verify",
            "--groups", groups, "--review-profile", args.review_model_profile,
            "--diarization-backend", args.diarization_backend], check=False, capture=True, dry_run=args.dry_run)
        # A dry-run may reuse the launcher's read-only receipt verification.
        # Real provisioning always rechecks through the selected runtime image.
        verified = model_cache_verified if args.dry_run else verification.returncode == 0
        if verified:
            console.ok("Existing model selection verified offline; staging and token entry skipped")
        else:
            token = None
            _run(console, [*compose, "run", "--rm", "--no-deps", "-T", "model-stager", "plan",
                "--groups", groups, "--review-profile", args.review_model_profile,
                "--diarization-backend", args.diarization_backend], dry_run=args.dry_run)
            if models in {"all", "transcription"} and not args.accept_model_terms:
                if args.non_interactive:
                    raise RuntimeError("transcription acquisition requires --accept-model-terms")
                console.warn("Review the displayed model/resource sources. punkt_tab data terms require operator review; the NLTK code license does not cover them automatically.")
                if not args.dry_run and input("Type ACCEPT after reviewing model and resource terms: ").strip() != "ACCEPT":
                    raise RuntimeError("model/resource terms were not acknowledged")
                args.accept_model_terms = True
            if (models in {"all", "transcription"} and args.enable_diarization
                    and args.diarization_backend == "community-1"):
                if not args.accept_model_terms:
                    if args.non_interactive:
                        raise RuntimeError("transcription staging requires --accept-model-terms")
                    console.warn("Community-1 is gated and CC-BY-4.0; accept its upstream terms before continuing")
                    if input("Type ACCEPT after reviewing the linked model terms: ").strip() != "ACCEPT":
                        raise RuntimeError("model terms were not accepted")
                token = "dry-run-token-placeholder" if args.dry_run else _hf_token(args)
            command = [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "model-stager",
                "stage",
                "--groups",
                groups,
                "--review-profile",
                args.review_model_profile,
                "--diarization-backend", args.diarization_backend,
            ]
            if args.accept_model_terms:
                command.append("--accept-model-terms")
            if token is not None:
                command.append("--token-stdin")
            try:
                _run(console, command, input_value=(token + "\n") if token else None, dry_run=args.dry_run)
            finally:
                token = None
        if models in {"all", "transcription"} and not args.dry_run:
            _replace_env(root / "config" / "transcription.env", "TRANSCRIPTION_V2_PIPELINE", "whisperx")
        console.ok("Model vault sealed; runtime services retain no hub token")
        if not args.dry_run:
            _install_phase(root, "models", "complete")
    elif not args.dry_run:
        _install_phase(root, "models", "not-selected")

    if not args.dry_run:
        installation, _release = _installed_release(root)
        _seal_provisioning(root, installation)
        _install_phase(root, "prepared", "complete")

    if args.prepare_only:
        console.phase(8, "NODE ARMED", "Preparation complete; services intentionally remain stopped")
        console.ok(f"Resume with: ./install install --root {shlex.quote(str(root))} --resume")
        return
    console.phase(8, "IGNITE NODE", "Launching the private service mesh and waiting for health consensus")
    active = []
    if models in {"all", "review"}:
        active.append("ai")
    if models in {"all", "transcription"}:
        active.append("transcription")
    runtime_compose = _compose(root, active, local_accounts=args.enable_account_management)
    if not args.dry_run:
        _install_phase(root, "running", "checking")
    try:
        _run(console, [*runtime_compose, "up", "-d", "--remove-orphans"], dry_run=args.dry_run)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Service startup failed. " + _STARTUP_REMEDY) from exc
    if not args.dry_run:
        _wait_health(console, root)
    console.ok("RecordBench node is online")


def _selected_capabilities_ready(payload: Mapping[str, object], models: str) -> bool:
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    if models in {"review", "all"} and not (
        capabilities.get("answering") == "ready"
        and capabilities.get("search") == "word + meaning"
    ):
        return False
    if models in {"transcription", "all"} and not str(
        capabilities.get("transcription", "")
    ).startswith("local WhisperX"):
        return False
    return True


_STARTUP_REMEDY = (
    "Inspect clamav-updater logs and fresh daily.* signatures, current free space on node/matter "
    "filesystems (builds can erode the 100 GiB reserve), and Docker bridge connectivity. "
    "See docs/INSTALL.md#startup-troubleshooting; correct the cause before resume or doctor."
)


def _installed_auth_mode(root: Path, installation: Mapping[str, object]) -> str:
    """Compare installation intent with saved configuration without echoing values."""
    expected = installation.get("auth")
    path = _app_env(root)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("saved authentication mode is unavailable")
    saved = _dotenv(path).get("CASE_INTELLIGENCE_AUTH_MODE")
    if not isinstance(expected, str) or expected not in {"local", "oidc", "kerberos"} or saved != expected:
        raise RuntimeError("saved authentication mode disagrees with application configuration")
    return str(expected)


# The proof never leaves the app container, enters argv, or appears in output.
# A direct HTTP connection avoids proxy environment variables and redirects.
_AUTH_MODE_PROBE = """import hashlib, hmac, http.client, json
try:
    with open('/var/lib/recordbench/runtime/identity-session.key', 'rb') as stream:
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError()
    proof = hmac.new(key, b'recordbench/auth-mode-diagnostic/v1', hashlib.sha256).hexdigest()
    connection = http.client.HTTPConnection('127.0.0.1', 8786, timeout=5)
    try:
        connection.request('GET', '/internal/auth-mode', headers={'X-RecordBench-Auth-Diagnostic': proof})
        response = connection.getresponse()
        content = response.read(4097)
        if response.status != 200 or len(content) > 4096:
            raise ValueError()
        payload = json.loads(content)
        if not isinstance(payload, dict) or set(payload) != {'auth_mode'}:
            raise ValueError()
        if payload['auth_mode'] not in ('local', 'oidc', 'kerberos', 'preview', 'test'):
            raise ValueError()
    finally:
        connection.close()
    print(json.dumps(payload))
except Exception:
    raise SystemExit(1)
"""


def _running_auth_mode(root: Path, installation: Mapping[str, object]) -> str | None:
    """Read the live IdentityService; process environment is not effective state."""
    command = [*_compose(root, installation.get("profiles", [])),
               "exec", "-T", "app", "python", "-c", _AUTH_MODE_PROBE]
    try:
        result = _probe(command)
        if result.returncode != 0 or len(result.stdout) > 4096:
            return None
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or set(payload) != {"auth_mode"}:
            return None
        mode = payload["auth_mode"]
        return mode if mode in ("local", "oidc", "kerberos", "preview", "test") else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _wait_health(console: Console, root: Path) -> None:
    settings, _ = _installed_release(root)
    expected_auth = _installed_auth_mode(root, settings)
    env = _dotenv(root / "compose.env")
    host = _valid_host(str(settings["server_name"]))
    port = int(_env("RECORDBENCH_HTTPS_PORT", "8443", environ=env))
    address = _gateway_probe_address(env)
    authority = f"[{address}]" if ":" in address else address
    url = f"https://{authority}:{port}/health"
    deadline = time.monotonic() + 900
    next_report = time.monotonic() + 15
    last = "services are starting"
    for phase in ("running", "login_reachable", "basic_review", "selected_capabilities"):
        _install_phase(root, phase, "checking")
    while time.monotonic() < deadline:
        payload = None
        try:
            if settings.get("auth") == "kerberos":
                compose = _compose(root, settings.get("profiles", []))
                probe = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8786/health',timeout=5).read(262144).decode())"
                result = _run(console, [*compose, "exec", "-T", "app", "python", "-c", probe], capture=True)
                payload = json.loads(result.stdout)
            else:
                request = urllib.request.Request(url, headers={"Host": host})
                with urllib.request.urlopen(request, timeout=5, context=ssl._create_unverified_context()) as response:
                    payload = json.loads(response.read(262_144))
        except (OSError, ValueError, subprocess.CalledProcessError, http.client.HTTPException) as exc:
            last = exc.__class__.__name__
        if isinstance(payload, dict) and payload.get("product") == "RecordBench":
            effective_auth = _running_auth_mode(root, settings)
            if effective_auth is not None and effective_auth != expected_auth:
                raise RuntimeError(
                    f"running authentication mode {effective_auth} disagrees with installed mode {expected_auth}; "
                    "restore the intended authentication configuration and recreate the app before retrying doctor"
                )
            storage, capabilities = payload.get("storage", {}), payload.get("capabilities", {})
            base_ready = (payload.get("status") in {"ok", "degraded"} and isinstance(storage, dict)
                          and storage.get("status") == "ready" and isinstance(capabilities, dict)
                          and capabilities.get("source_review") == "ready"
                          and capabilities.get("malware_scan") in {"ready", "not required"})
            login_reachable = _login_reachable(root)
            login_ready = login_reachable and effective_auth == expected_auth
            selected_ready = base_ready and _selected_capabilities_ready(payload, str(settings.get("models", "none")))
            _install_phase(root, "running", "complete")
            _install_phase(root, "basic_review", "complete" if base_ready else "incomplete")
            _install_phase(root, "login_reachable", "complete" if login_ready else "incomplete")
            _install_phase(root, "selected_capabilities", "complete" if selected_ready else "incomplete")
            if selected_ready and login_ready:
                console.ok(f"Application health, selected capabilities and sign-in endpoint passed; authentication mode :: {expected_auth}")
                if payload.get("status") == "degraded":
                    console.warn("Basic review is available; an unselected optional capability remains unavailable")
                return
            last = "selected capabilities are incomplete" if base_ready else "basic review needs attention"
            if isinstance(storage, dict) and storage.get("reserve_satisfied") is False:
                last += "; storage reserve is unsatisfied: restore free space above the configured reserve; image builds may have consumed it"
            if isinstance(capabilities, dict) and capabilities.get("malware_scan") not in {"ready", "not required"}:
                last += "; malware scanning is unavailable: inspect clamav-updater logs and fresh daily.* signatures"
            if effective_auth is None:
                last += "; running authentication mode is unavailable: verify the app supports protected authentication diagnostics and recreate it with the intended configuration"
            if not login_reachable:
                last += "; sign-in endpoint is not reachable"
        if time.monotonic() >= next_report:
            console.note(f"health consensus pending :: {last}")
            next_report = time.monotonic() + 15
        time.sleep(3)
    raise RuntimeError(f"node did not reach health before timeout ({last}). {_STARTUP_REMEDY}")


def _doctor(console: Console, args: argparse.Namespace, root: Path) -> None:
    console.phase(1, "NODE DIAGNOSTIC", "Checking configuration, permissions, Compose graph, and live health")
    console.ok(f"naming generation :: {_node_names(root)}")
    required = (root / "compose.env", root / "installation.json", _app_env(root))
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"required node file is unavailable: {path}")
        console.ok(f"control file :: {path.name}")
    config, _ = _installed_release(root)
    expected_auth = _installed_auth_mode(root, config)
    console.ok(f"saved authentication mode :: {expected_auth}")
    profiles = list(config.get("profiles", []))
    compose = _compose(root, profiles)
    _run(console, [*compose, "config", "--quiet"], dry_run=args.dry_run)
    _run(console, [*compose, "ps"], dry_run=args.dry_run)
    if not args.dry_run:
        _wait_health(console, root)
    else:
        console.note("Dry run: running authentication mode and live health are not verified")
    if not args.dry_run:
        installation, _ = _installed_release(root)
        _record_prepared_phases(root, installation)
    console.ok("Node diagnostic complete")


def _restore_model_options(
    args: argparse.Namespace,
    installation: Mapping[str, object],
    compose_env: Mapping[str, str],
    transcription_env: Mapping[str, str],
) -> None:
    """Validate the saved deployment's choices, never substitute an automatic plan."""
    languages = installation.get("transcription_languages", ["en"])
    topology = installation.get("gpu_topology", {})
    if (not isinstance(languages, list)
            or not all(isinstance(value, str) for value in languages)
            or not isinstance(topology, dict)):
        raise RuntimeError("installed model metadata is invalid")
    generator = topology.get("generator", [])
    if not isinstance(generator, list) or not all(isinstance(value, str) for value in generator):
        raise RuntimeError("installed GPU metadata is invalid")
    args.transcription_languages = ",".join(languages)
    args.enable_diarization = bool(installation.get("transcription_diarization", False))
    args.diarization_backend = str(installation.get("diarization_backend", "community-1"))
    if (args.diarization_backend not in {"community-1", "nemotron"}
            or (args.enable_diarization and transcription_env.get(
                "TRANSCRIPTION_V2_DIARIZATION_BACKEND", "community-1") != args.diarization_backend)):
        raise RuntimeError("installed diarization selections disagree")
    args.review_model_profile = str(installation.get("review_model_profile", "portable"))
    args.gpu_layout = _env("RECORDBENCH_GPU_LAYOUT", str(installation.get("gpu_layout", "shared")), environ=compose_env)
    args.generator_gpus = _env("RECORDBENCH_GENERATOR_GPU", ",".join(generator), environ=compose_env) or None
    # Match Compose's default when an older environment omits this setting.
    args.generator_dtype = _env("RECORDBENCH_GENERATOR_DTYPE", environ=compose_env) or "bfloat16"
    args.transcription_gpu = _env("RECORDBENCH_TRANSCRIPTION_GPU", topology.get("transcription"), environ=compose_env)
    args.retrieval_device = _env("RECORDBENCH_RETRIEVAL_DEVICE", str(topology.get("retrieval_device", "cpu")), environ=compose_env)
    args.retrieval_gpu = (_env("RECORDBENCH_RETRIEVAL_GPU", topology.get("retrieval_gpu"), environ=compose_env)
                          if args.retrieval_device == "cuda" else None)
    models = str(installation.get("models", "none"))
    if (args.review_model_profile not in {"portable", "quality"}
            or (models in {"review", "all"} and not args.generator_gpus)
            or (models in {"transcription", "all"} and not args.transcription_gpu)
            or (args.retrieval_device == "cuda" and not args.retrieval_gpu)):
        raise RuntimeError("installed model/GPU selections are incomplete")
    try:
        utilization = _env("RECORDBENCH_GENERATOR_GPU_UTILIZATION", environ=compose_env)
        args.generator_gpu_utilization = float(utilization) if utilization is not None else None
        minimum_free = transcription_env.get("TRANSCRIPTION_V2_MIN_FREE_VRAM_MB")
        args.transcription_min_free_vram_mib = int(minimum_free) if minimum_free is not None else None
    except (TypeError, ValueError) as exc:
        raise RuntimeError("installed GPU capacity settings are invalid") from exc


def _saved_node_arguments(args: argparse.Namespace, root: Path) -> tuple[dict[str, object], Path]:
    """Restore actual saved coordinates and check all mounts before any Compose call."""
    canonical = _existing_storage_path(args.root if args.root is not None else root)
    if canonical != root:
        raise RuntimeError("saved storage root does not match the selected node")
    # Validate controlled children before reading environment files through them.
    for name, directory in _paths(root).items():
        if name not in {"accounts", "storage"}:
            _existing_storage_path(directory)
    installation, release = _installed_release(root)
    if not isinstance(installation, dict):
        raise RuntimeError("installed node metadata is invalid")
    auth, models = installation.get("auth"), installation.get("models")
    profiles = installation.get("profiles")
    account_management = installation.get("local_account_management", False)
    if (auth not in {"local", "oidc", "kerberos"}
            or models not in {"none", "review", "transcription", "all"}
            or not isinstance(profiles, list) or any(profile not in {"ai", "transcription"} for profile in profiles)
            or not isinstance(account_management, bool) or account_management and auth != "local"):
        raise RuntimeError("installed node metadata is invalid")
    for path in (root / "compose.env", _app_env(root), root / "config/transcription.env"):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("saved node configuration is unavailable or unsafe")
    compose_env = _dotenv(root / "compose.env")
    app_env = _dotenv(_app_env(root))
    transcription_env = _dotenv(root / "config/transcription.env")
    storage = installation.get("storage_root")
    if not isinstance(storage, str) or _env("RECORDBENCH_STORAGE_ROOT", environ=compose_env) != storage:
        raise RuntimeError("saved storage coordinates disagree with the Compose mount")
    args.root = canonical
    args.storage_root = _existing_storage_path(Path(storage))
    args.resume = True
    args.auth, args.models = auth, models
    args.enable_account_management = account_management
    paths = _paths(root, args.storage_root)
    mount_keys = {"config": "CONFIG", "secrets": "SECRETS", "runtime": "RUNTIME",
                  "storage": "STORAGE", "transcription": "TRANSCRIPTION", "state": "STATE", "models": "MODEL"}
    if account_management:
        saved_accounts = _env("RECORDBENCH_LOCAL_ACCOUNT_ROOT", environ=compose_env)
        if not isinstance(saved_accounts, str):
            raise RuntimeError("saved account storage mount is unavailable")
        args.account_root = _existing_storage_path(Path(saved_accounts))
    for name, key in mount_keys.items():
        saved = _env(f"RECORDBENCH_{key}_ROOT", environ=compose_env)
        if not isinstance(saved, str) or _existing_storage_path(Path(saved)) != paths[name]:
            raise RuntimeError("saved storage mount disagrees with the installed node layout")
    if app_env.get("CASE_INTELLIGENCE_AUTH_MODE") != auth:
        raise RuntimeError("saved authentication mode disagrees with application configuration")
    if auth == "local":
        expected_file = ("/var/lib/recordbench-accounts/local-accounts.json" if account_management
                         else "/run/recordbench-secrets/local-accounts.json")
        expected_management = "/var/lib/recordbench-accounts" if account_management else ""
        if (app_env.get("CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE") != expected_file
                or app_env.get("CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT", "") != expected_management):
            raise RuntimeError("saved local-account paths disagree with the installed application configuration")
        account_root = args.account_root if account_management else root / "secrets"
        if not (account_root / "local-accounts.json").exists():
            _assert_unfinished_account_bootstrap(root)
    server_name = installation.get("server_name")
    if not isinstance(server_name, str) or _env("RECORDBENCH_SERVER_NAME", environ=compose_env) != server_name:
        raise RuntimeError("saved HTTPS hostname coordinates disagree")
    args.server_name = _valid_host(server_name)
    try:
        args.bind_address = _required_env("RECORDBENCH_BIND_ADDRESS", environ=compose_env)
        args.https_port = int(_required_env("RECORDBENCH_HTTPS_PORT", environ=compose_env))
        args.tls_cert = Path(_required_env("RECORDBENCH_TLS_CERT", environ=compose_env))
        args.tls_key = Path(_required_env("RECORDBENCH_TLS_KEY", environ=compose_env))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("saved HTTPS coordinates are invalid") from exc
    if not args.tls_cert.is_absolute() or not args.tls_key.is_absolute():
        raise RuntimeError("saved HTTPS certificate paths must be absolute")
    _restore_model_options(args, installation, compose_env, transcription_env)
    return installation, release


def _saved_models_verified(args: argparse.Namespace, root: Path, release: Path) -> bool:
    """Check cached bytes offline before deciding whether a resumed download needs input."""
    if args.models == "none":
        return True
    model_root = root / "models"
    receipt = model_root / ".recordbench-stage-receipt.json"
    if receipt.is_symlink() or not receipt.is_file():
        return False
    # Use this reviewed launcher's verifier (including the current receipt
    # format), while binding to the installed release's exact model catalog.
    tool = Path(__file__).with_name("stage-models.py")
    catalog = release / "config/models.json"
    if tool.is_symlink() or not tool.is_file() or catalog.is_symlink() or not catalog.is_file():
        return False
    groups = _model_stage_groups(args.models,
        transcription_languages=_transcription_languages(args.transcription_languages),
        diarization=bool(args.enable_diarization))
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(tool), "verify", "--catalog", str(catalog),
             "--model-root", str(model_root), "--groups", ",".join(groups),
             "--review-profile", args.review_model_profile,
            "--diarization-backend", args.diarization_backend],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _resume_node(console: Console, args: argparse.Namespace, root: Path) -> None:
    installation, _release = _saved_node_arguments(args, root)
    args.admin_username = args.admin_username or installation.get("initial_administrator")
    args.admin_display_name = args.admin_display_name or installation.get("initial_administrator_display_name")
    auth, models, profiles = args.auth, args.models, installation["profiles"]
    needs_model_staging = not _saved_models_verified(args, root, _release)
    if (needs_model_staging and args.non_interactive and args.enable_diarization
            and args.diarization_backend == "community-1"
            and not (args.accept_model_terms and args.hf_token_stdin)):
        raise RuntimeError("resumed model staging requires --accept-model-terms and --hf-token-stdin before runtime checks")
    prepared = _provisioning_complete(root, installation)
    recover_running_gpu = prepared and models != "none" and not args.prepare_only
    _preflight(console, models=models, dry_run=args.dry_run, args=args,
               needs_model_staging=needs_model_staging,
               defer_gpu_free_check=recover_running_gpu)
    with _bootstrap_password_input(args, root, auth) as password_input:
        console.phase(2, "REJOIN NODE", "Using sealed identity, storage, model, and release coordinates")
        compose = _compose(root, profiles)
        _run(console, [*compose, "config", "--quiet"], dry_run=args.dry_run)
        running: list[str] = []
        if recover_running_gpu:
            if args.dry_run:
                console.note("Dry run: observe this node's running services, stop them if a selected model is running, then recheck GPU capacity; actual free-memory admission is deferred")
            else:
                enabled = {line for line in _run(console, [*compose, "config", "--services"], capture=True).stdout.splitlines() if line}
                # Compose may emit a bare newline when no service is running.
                # Ignore empty records only; malformed names must still fail closed.
                observed = {line for line in _run(console, [*compose, "ps", "--status", "running", "--services", "--orphans=false"], capture=True).stdout.splitlines() if line}
                if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service) is None for service in enabled | observed):
                    raise RuntimeError("could not safely identify this node's running services")
                model_services = set()
                if models in {"review", "all"}:
                    model_services.add("generator")
                    if args.retrieval_device == "cuda":
                        model_services.add("retrieval")
                if models in {"transcription", "all"}:
                    model_services.add("transcription-worker")
                if enabled & observed & model_services:
                    running = sorted(enabled & observed)
        stop_attempted = False
        try:
            if running:
                console.note("Stopping this node's observed running services to release its model allocation")
                stop_attempted = True
                _run(console, [*compose, "stop", "--timeout", "120", *running])
            if recover_running_gpu and not args.dry_run:
                # Deferral never authorizes provisioning: even an empty running
                # model set must pass admission using actual free device memory.
                _preflight(console, models=models, dry_run=False, args=args,
                           needs_model_staging=needs_model_staging)
            if not args.dry_run:
                _install_phase(root, "configuration", "complete")
            console.ok(f"release capsule :: {installation.get('release_id', 'legacy')}")
            if not prepared:
                console.warn("Prior boot stopped before the provisioning seal; validating and continuing existing state")
            else:
                console.note("Prepared phase receipt found; revalidating storage, accounts and the offline model selection")
            _provision(console, args, root, auth, models, args.admin_username, args.admin_display_name,
                       model_cache_verified=not needs_model_staging, password_input=password_input)
        except Exception as resume_error:
            if not stop_attempted:
                raise
            try:
                _run(console, [*compose, "start", *running])
            except Exception as restore_error:
                raise RuntimeError("resume failed and restoration also failed; inspect this node's service status before retrying") from restore_error
            raise RuntimeError("resume failed; previously running services were restarted, but health is not confirmed") from resume_error


def _backup_tool(root: Path) -> Path:
    _installation, release = _installed_release(root)
    tool = release / "scripts" / f"{_node_names(root)}_backup.py"
    if tool.is_symlink() or not tool.is_file():
        raise RuntimeError("installed release does not contain the backup tool")
    return tool


def _systemd_quote(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise RuntimeError("systemd unit path is invalid")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _schedule_backup(console: Console, root: Path, *, dry_run: bool) -> None:
    installation, release = _installed_release(root)
    node_id = str(installation.get("node_id", ""))
    if re.fullmatch(r"[0-9a-f]{10}", node_id) is None:
        raise RuntimeError("node identity is invalid")
    user_units = Path.home() / ".config" / "systemd" / "user"
    names = _node_names(root)
    service_name = f"{names}-backup-{node_id}.service"
    timer_name = f"{names}-backup-{node_id}.timer"
    replacements = {
        "@NODE_ID@": node_id,
        "@RELEASE_ROOT@": str(release),
        "@PYTHON@": _systemd_quote(sys.executable),
        "@TOOL@": _systemd_quote(str(_backup_tool(root))),
        "@NODE_ROOT@": _systemd_quote(str(root)),
    }
    if not dry_run:
        user_units.mkdir(parents=True, exist_ok=True)
        for template_name, destination_name in (
            (f"{names}-backup.service.in", service_name),
            (f"{names}-backup.timer.in", timer_name),
        ):
            text = (release / "deploy" / "systemd" / template_name).read_text(
                encoding="utf-8"
            )
            for marker, value in replacements.items():
                text = text.replace(marker, value)
            if "@" in text:
                raise RuntimeError("backup timer template contains an unresolved marker")
            destination = user_units / destination_name
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        _run(console, ["systemctl", "--user", "daemon-reload"])
        _run(console, ["systemctl", "--user", "enable", "--now", timer_name])
        _atomic_private_write(
            root / "state" / "backup-unit.json",
            json.dumps(
                {
                    "format_version": 1,
                    "service": service_name,
                    "timer": timer_name,
                    "release_path": str(release),
                },
                indent=2,
            )
            + "\n",
        )
    console.ok(f"quiet-hour timer :: {timer_name}")


def _backup(console: Console, args: argparse.Namespace, root: Path) -> None:
    console.phase(1, "FREEZE THE RECORD", "Checking queues, snapshot boundaries, and encrypted recovery storage")
    tool = _backup_tool(root)
    config = root / "config" / "backup.json"
    if args.repository is not None or args.recovery_key_output is not None:
        if args.repository is None or args.recovery_key_output is None:
            raise RuntimeError("backup initialization needs both --repository and --recovery-key-output")
        command = [
            sys.executable,
            str(tool),
            "initialize",
            "--node-root",
            str(root),
            "--repository",
            str(args.repository.expanduser().resolve(strict=False)),
            "--recovery-key-output",
            str(args.recovery_key_output.expanduser().resolve(strict=False)),
            "--retention",
            args.retention,
        ]
        if args.allow_local_backup:
            command.append("--allow-local-repository")
        _run(console, command, dry_run=args.dry_run)
    elif not config.is_file():
        raise RuntimeError(
            "backup is not initialized; provide --repository and --recovery-key-output"
        )
    console.phase(2, "TRANSMIT VAULT SNAPSHOT", "Quiescing briefly, restarting service, then transferring encrypted blocks")
    command = [sys.executable, str(tool), "backup", "--node-root", str(root)]
    if args.dry_run:
        command.append("--dry-run")
    _run(console, command, dry_run=args.dry_run)
    console.ok("Backup sequence completed or safely deferred by active work")
    if args.schedule_backups:
        console.phase(3, "ARM QUIET-HOUR DAEMON", "Scheduling the daily snapshot and one automatic retry")
        _schedule_backup(console, root, dry_run=args.dry_run)


def _restore(console: Console, args: argparse.Namespace, root: Path) -> None:
    if args.restore_target is None:
        raise RuntimeError("restore requires --restore-target with a new exact directory")
    console.phase(1, "RECOVERY SIMULATION", "Restoring encrypted evidence into an isolated, never-live target")
    tool = _backup_tool(root)
    _run(
        console,
        [
            sys.executable,
            str(tool),
            "restore",
            "--node-root",
            str(root),
            "--target",
            str(args.restore_target.expanduser().resolve(strict=False)),
            "--snapshot",
            args.snapshot,
        ],
        dry_run=args.dry_run,
    )
    console.ok("Restore drill cryptographic and database checks passed")


def _build_targets(auth: str, models: str) -> list[str]:
    targets = ["app", "account-admin", "gateway", "clamav", "postgres"]
    if auth == "kerberos":
        targets.append("kerberos-proxy")
    if models in {"all", "review"}:
        targets.append("retrieval")
    if models in {"all", "transcription"}:
        targets.extend(("transcription-api", "transcription-worker"))
    return targets


def _update(console: Console, args: argparse.Namespace, root: Path) -> None:
    installation, old_release = _saved_node_arguments(args, root)
    auth, models, profiles = args.auth, args.models, installation["profiles"]
    _preflight(console, models=models, dry_run=args.dry_run, args=args,
               needs_model_staging=False, defer_gpu_free_check=True)
    old_compose = _compose(root, profiles, release=old_release, auth=auth)
    console.phase(2, "SNAPSHOT BEFORE MUTATION", "Requiring a recoverable checkpoint before swapping release capsules")
    backup_configured = (root / "config" / "backup.json").is_file()
    if backup_configured:
        _run(
            console,
            [sys.executable, str(_backup_tool(root)), "backup", "--node-root", str(root)],
            dry_run=args.dry_run,
        )
        console.ok("Pre-update encrypted snapshot completed or safely deferred")
        status_path = root / "state" / "backup-status.json"
        if not args.dry_run:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("state") != "succeeded":
                raise RuntimeError("update requires a newly succeeded backup, not a deferred snapshot")
    elif not args.no_backup:
        raise RuntimeError(
            "backup is not configured; initialize it first or explicitly accept risk with --no-backup"
        )
    else:
        console.warn("Operator explicitly accepted an update without a recovery snapshot")

    console.phase(3, "LOAD NEW RELEASE CAPSULE", "Hashing an allowlisted source tree and preserving the prior runtime intact")
    release_id, release = _stage_release(console, root, dry_run=args.dry_run)
    if release_id == installation.get("release_id"):
        console.ok("Node already runs this exact release capsule")
        if not args.dry_run:
            _doctor(console, args, root)
        return
    old_installation_text = (root / "installation.json").read_text(encoding="utf-8")
    old_compose_text = (root / "compose.env").read_text(encoding="utf-8")
    updated = dict(installation)
    updated.update(
        {
            "version": VERSION,
            "release_id": release_id,
            "release_path": str(release),
            "previous_release_id": installation.get("release_id", ""),
            "previous_release_path": str(old_release),
        }
    )
    if not args.dry_run:
        _replace_env(root / "compose.env", "RECORDBENCH_RELEASE_ID", release_id)
        _atomic_private_write(
            root / "installation.json", json.dumps(updated, indent=2) + "\n"
        )
    compose = _compose(root, profiles, release=release, auth=auth)
    console.phase(4, "FORGE REPLACEMENT RUNTIME", "Building versioned images without overwriting the rollback image set")
    try:
        _run(console, [*compose, "build", *_build_targets(auth, models)], dry_run=args.dry_run)
        _run(console, [*compose, "config", "--quiet"], dry_run=args.dry_run)
        console.phase(5, "ATOMIC NODE SWAP", "Starting the new capsule and waiting for application and storage consensus")
        if _required_gpu_count(models):
            console.note("Stopping this node to release its model allocation before checking actual free GPU memory")
            _run(console, [*old_compose, "stop", "--timeout", "120"], dry_run=args.dry_run)
            if args.dry_run:
                console.note("Dry run: actual free-memory admission is deferred until the node is stopped during update")
            else:
                # Never assume that all used VRAM belongs to this node: after
                # its scoped stop, competing allocations remain in this probe.
                _preflight(console, models=models, dry_run=False, model_args=args,
                           needs_model_staging=False)
        _run(console, [*compose, "up", "-d", "--remove-orphans"], dry_run=args.dry_run)
        if not args.dry_run:
            _wait_health(console, root)
            _seal_provisioning(root, updated)
            _record_prepared_phases(root, updated)
            if (root / "state" / "backup-unit.json").is_file():
                _schedule_backup(console, root, dry_run=False)
        console.ok(f"Update committed :: {release_id}")
    except Exception as update_error:
        console.warn("New capsule failed acceptance; rolling back configuration and versioned images")
        rollback_error: Exception | None = None
        if not args.dry_run:
            try:
                _atomic_private_write(root / "compose.env", old_compose_text)
                _atomic_private_write(root / "installation.json", old_installation_text)
                rollback = _compose(root, profiles, release=old_release, auth=auth)
                _run(console, [*rollback, "up", "-d", "--remove-orphans"])
                _wait_health(console, root)
                console.ok("Prior release restored and healthy")
            except Exception as exc:
                rollback_error = exc
                console.warn("Prior capsule did not recover; operator intervention is required")
        if rollback_error is not None:
            raise RuntimeError(
                f"update failed ({update_error}); rollback also failed ({rollback_error})"
            ) from update_error
        raise RuntimeError(f"update failed and rollback was attempted: {update_error}") from update_error


def main() -> int:
    args = _parser().parse_args()
    if args.enable_account_management and (args.command not in {"install", "preflight"} or args.auth != "local"):
        _parser().error("--enable-account-management requires install or preflight with explicit --auth local; existing nodes retain their saved account configuration")
    console = Console(
        color=sys.stdout.isatty() and not args.no_color and os.getenv("NO_COLOR") is None,
        quiet=args.quiet,
    )
    if args.json and args.command != "preflight":
        _parser().error("--json is only supported by the preflight command")
    if args.command == "preflight":
        args.root = (args.root or _default_root()).expanduser()
        try:
            _select_names(args, args.root)
            needs_model_staging = True
            if args.resume and (args.root / "installation.json").is_file():
                root = args.root.resolve(strict=False)
                installation, release = _saved_node_arguments(args, root)
                args.admin_username = args.admin_username or installation.get("initial_administrator")
                args.admin_display_name = args.admin_display_name or installation.get("initial_administrator_display_name")
                needs_model_staging = not _saved_models_verified(args, root, release)
            result = _collect_preflight(args.models or "none", args,
                                        needs_model_staging=needs_model_staging)
        except (OSError, RuntimeError, ValueError) as exc:
            result = PreflightResult((PreflightCheck(
                "saved-node", "fail", str(exc) if isinstance(exc, NamingError) else "Saved node configuration or storage coordinates are unavailable or unsafe",
                "Inspect the existing node using its canonical account, storage and model configuration", True,
                "Restore a valid installation record, saved configuration and accessible protected mounts before resuming. Preflight does not change the saved account mode or paths.",
            ),))
        if args.json:
            print(json.dumps(result.payload(), indent=2))
        else:
            _render_preflight(console, result)
        return 0 if result.ready else 1
    console.banner(args.command)
    try:
        if args.root is None:
            default_root = _default_root()
            args.root = Path(
                _ask(
                    "Installation state root",
                    str(default_root),
                    non_interactive=args.non_interactive,
                )
            )
        if not _storage_path_text_valid(args.root):
            raise RuntimeError("installation root cannot contain control characters")
        root = args.root.expanduser().resolve(strict=False)
        _select_names(args, root)
        if args.command == "doctor":
            _doctor(console, args, root)
            _handoff(console, root, dry_run=args.dry_run)
            return 0
        if args.command == "backup":
            _backup(console, args, root)
            console.line(console.paint("  >>> VAULT SNAPSHOT COMPLETE <<<", C.green + C.bold))
            return 0
        if args.command == "restore":
            _restore(console, args, root)
            console.line(console.paint("  >>> RECOVERY DRILL VERIFIED <<<", C.green + C.bold))
            return 0
        if args.command == "update":
            _update(console, args, root)
            _handoff(console, root, dry_run=args.dry_run)
            console.line(console.paint("  >>> NODE UPGRADE COMPLETE <<<", C.green + C.bold))
            return 0
        if args.resume and (root / "installation.json").is_file():
            _resume_node(console, args, root)
            _handoff(console, root, dry_run=args.dry_run)
            return 0
        if args.models is None:
            args.models = _choose(
                "Capability profile",
                ("none", "review", "transcription", "all"),
                "none",
                non_interactive=args.non_interactive,
            )
        models = args.models
        if args.storage_root is None and not args.non_interactive:
            args.storage_root = Path(
                _ask(
                    "Managed matter storage root",
                    str(root / "matter-storage"),
                    non_interactive=False,
                )
            )
        if args.bind_address is None and not args.non_interactive:
            args.bind_address = _ask("HTTPS bind address", "127.0.0.1", non_interactive=False)
        if not args.server_name:
            args.server_name = _ask(
                "RecordBench hostname", "recordbench.example.test", non_interactive=args.non_interactive
            )
        # Reject an invalid hostname before asking unrelated identity questions.
        _valid_host(args.server_name)
        _collect_identity_choices(args)
        args.root = args.root.expanduser()
        # Resolve storage, TLS and the complete hardware plan before creating
        # state. Dry-run discovery uses the same read-only prerequisite checks.
        gpu_devices = _preflight(console, models=models, dry_run=args.dry_run, args=args)
        with _bootstrap_password_input(args, root, args.auth) as password_input:
            paths = _prepare_directories(
                console,
                root,
                storage_root=args.storage_root,
                resume=args.resume,
                dry_run=args.dry_run,
            )
            console.phase(3, "LOAD RELEASE CAPSULE", "Hashing and staging only the deployable open-source tree")
            release_id, release_path = _stage_release(console, root, dry_run=args.dry_run)
            auth, models, _server_name, admin_user, admin_display = _configure(
                console, args, root, paths, release_id, release_path, gpu_devices
            )
            _provision(console, args, root, auth, models, admin_user, admin_display, password_input=password_input)
        console.line()
        _handoff(console, root, dry_run=args.dry_run)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        console.line()
        console.warn(f"BOOT SEQUENCE HALTED :: {exc}")
        if not args.dry_run and "root" in locals() and (root / "installation.json").is_file():
            try:
                _handoff(console, root)
            except (OSError, RuntimeError, KeyError, ValueError):
                console.warn("Handoff unavailable; inspect the retained node configuration before resuming")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
