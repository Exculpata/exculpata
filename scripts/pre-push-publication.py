#!/usr/bin/env python3
"""Scan every outgoing ref before Git transmits objects to a remote."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Hooks can inherit these; never let them redirect the isolated repository.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=environment,
    )


_CURRENT_GIT_PREFIX = "exculpata."
_LEGACY_GIT_PREFIX = "recordbench."


def _config(root: Path, *args: str) -> subprocess.CompletedProcess:
    return git(root, "config", *args, check=False)


def _prefer_current(root: Path, suffix: str, *options: str) -> subprocess.CompletedProcess:
    current = _config(root, *options, _CURRENT_GIT_PREFIX + suffix)
    legacy = _config(root, *options, _LEGACY_GIT_PREFIX + suffix)
    if current.returncode not in (0, 1) or legacy.returncode not in (0, 1):
        raise ValueError("Publication configuration could not be read.")
    if legacy.returncode == 0 and legacy.stdout.strip() and current.returncode != 0:
        print(
            "Deprecation notice: git config "
            f"{_LEGACY_GIT_PREFIX}{suffix} is deprecated; use {_CURRENT_GIT_PREFIX}{suffix}.",
            file=sys.stderr,
        )
        return legacy
    return current


def settings(root: Path) -> list[str]:
    required = _prefer_current(root, "requirePrivatePublicationCheck", "--bool", "--get")
    deny = _prefer_current(root, "publicationDenyFile", "--path", "--get")
    arguments: list[str] = []
    if required.returncode == 0 and required.stdout.strip() == "true" and not deny.stdout.strip():
        raise ValueError("This checkout requires a private publication deny file.")
    if deny.stdout.strip():
        path = Path(deny.stdout.strip())
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError("Publication deny file is unavailable or unsafe.")
        if path.stat().st_mode & 0o077:
            raise ValueError("Publication deny file must be owner-only.")
        arguments += ["--deny-file", str(path)]
    for suffix, flag, width in (
        ("publicCloneUrl", "--allow-public-clone-url", 1),
        ("publicGitIdentity", "--allow-public-git-identity", 2),
        ("publicBaselineIdentity", "--allow-public-baseline-git-identity", 3),
        ("reviewedMediaDigest", "--allow-reviewed-media-digest", 1),
        ("publicMergeCommit", "--allow-public-merge-commit", 1),
    ):
        result = _prefer_current(root, suffix, "--get-all")
        for value in result.stdout.splitlines():
            fields = value.split("\t")
            if len(fields) != width:
                raise ValueError("Invalid publication identity configuration.")
            arguments += [flag, *fields]
    return arguments


def main() -> int:
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip())
    arguments = settings(root)
    outgoing: list[tuple[str, str]] = []
    for line in sys.stdin:
        values = line.split()
        if len(values) != 4:
            raise ValueError("Malformed pre-push input.")
        _, oid, remote_ref, _ = values
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise ValueError("Invalid outgoing object.")
        if set(oid) == {"0"}:
            continue
        if not remote_ref.startswith(("refs/heads/", "refs/tags/")):
            raise ValueError("Only explicit branch and tag publication is supported.")
        outgoing.append((oid, remote_ref))
    if not outgoing:
        return 0
    scanner = Path(__file__).with_name("publication-check.py")
    if scanner.is_symlink() or not scanner.is_file():
        raise ValueError("Publication scanner is unavailable.")
    with tempfile.TemporaryDirectory(prefix="exculpata-push-check-") as directory:
        temporary = Path(directory)
        object_format = git(root, "rev-parse", "--show-object-format").stdout.strip()
        if object_format not in {"sha1", "sha256"}:
            raise ValueError("Unsupported source object format.")
        git(temporary, "init", "--quiet", "--template=", "--object-format=" + object_format)
        for oid, ref in outgoing:
            # Fetch only explicitly outgoing refs, never private neighboring histories.
            git(temporary, "fetch", "--quiet", "--no-tags", str(root), f"{oid}:{ref}")
        git(temporary, "checkout", "--quiet", "--detach", outgoing[0][0])
        reachable = set(git(temporary, "rev-list", "--all").stdout.splitlines())
        scoped_arguments = []
        index = 0
        while index < len(arguments):
            if arguments[index] == "--allow-public-baseline-git-identity":
                boundary = arguments[index + 1]
                kind = git(root, "cat-file", "-t", boundary, check=False)
                if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", boundary) or kind.returncode or kind.stdout.strip() != "commit":
                    raise ValueError("Invalid publication baseline boundary.")
                if boundary in reachable:
                    scoped_arguments.extend(arguments[index:index + 4])
                index += 4
            else:
                scoped_arguments.append(arguments[index])
                index += 1
        # The first scan covers the complete fetched history. Each remaining
        # outgoing tip also needs its own tree scan, including the naming gate
        # whose historical occurrences intentionally remain allowed.
        for index, oid in enumerate(dict.fromkeys(oid for oid, _ref in outgoing)):
            if index:
                git(temporary, "checkout", "--quiet", "--detach", oid)
            result = subprocess.run(
                [sys.executable, "-I", str(scanner), "--root", str(temporary),
                 *scoped_arguments, *(["--skip-history"] if index else [])],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "PYTHON"))},
            )
            if result.returncode:
                # Keep candidate filenames and matched values out of terminal recordings.
                print("Publication blocked: outgoing history failed inspection. Run the scanner locally for rule-only diagnostics.", file=sys.stderr)
                return 1
    print("Outgoing publication history passed inspection.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Publication blocked: pre-push inspection could not complete.", file=sys.stderr)
        raise SystemExit(1)
