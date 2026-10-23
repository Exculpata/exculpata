#!/usr/bin/env python3
"""Stage pinned model snapshots and write the offline transcription manifest."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


TRANSCRIPTION_SCHEMA = "transcription-v2-model-manifest-v1"
# Isolated verifier: python -I -S cannot import the package. Existing caches
# use this receipt filename.
_STAGE_RECEIPT_NAME = ".recordbench-stage-receipt.json"
STAGE_GROUPS = frozenset(
    {
        "review",
        "transcription-asr",
        "transcription-alignment-en",
        "transcription-alignment-es",
        "transcription-diarization",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _catalog(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("model catalog is invalid")
    return payload


def _token(args: argparse.Namespace) -> str | None:
    if not args.token_stdin:
        return None
    value = sys.stdin.readline(4097).rstrip("\r\n")
    if not value or len(value) > 4096 or any(character in value for character in "\x00"):
        raise RuntimeError("Hugging Face token input is invalid")
    return value


def _selected(
    value: Mapping[str, object],
    groups: frozenset[str],
    *,
    review_profile: str,
    diarization_backend: str = "community-1",
) -> bool:
    if value.get("group") not in groups:
        return False
    if value.get("group") == "review" and value.get("role") == "generator":
        return value.get("profile") == review_profile
    if value.get("group") == "transcription-diarization":
        return value.get("backend", "community-1") == diarization_backend
    return True


def _snapshot_files(snapshot: Path, cache: Path, *, bind_snapshot: bool = False) -> list[dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    cache_resolved = cache.resolve()
    for candidate in sorted(snapshot.rglob("*")):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(cache_resolved)
        except ValueError as exc:
            raise RuntimeError("staged model escaped the approved cache") from exc
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("staged model contains a non-regular artifact")
        logical = candidate.absolute().relative_to(cache.absolute()).as_posix()
        key = logical if bind_snapshot else relative.as_posix()
        files[key] = {
            "path": relative.as_posix(),
            **({"snapshot_path": logical} if bind_snapshot else {}),
            "size_bytes": metadata.st_size,
            "sha256": _sha256(resolved),
        }
    if not files:
        raise RuntimeError("staged model snapshot contains no files")
    return [files[key] for key in sorted(files)]


def _download_direct(item: Mapping[str, object], cache: Path) -> Path:
    target = cache / str(item["path"])
    if target.exists() and not target.is_symlink() and _sha256(target) == item["sha256"]:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".model-stage-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with urllib.request.urlopen(str(item["url"]), timeout=120) as response, temporary.open("wb") as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if _sha256(temporary) != item["sha256"]:
            raise RuntimeError(f"downloaded alignment artifact failed verification: {item['role']}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _stage_tokenizer(root: Path, resource: Mapping[str, object],
                     archive_path: Path | None = None) -> list[Path]:
    """Verify the immutable archive before bounded extraction into a fresh tree."""
    try:
        if archive_path is None:
            with urllib.request.urlopen(str(resource["url"]), timeout=120) as response:
                data = response.read(8 * 1024 * 1024 + 1)
        else:
            descriptor = os.open(archive_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise RuntimeError("tokenizer input must be a regular file")
                data = source.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != resource["sha256"]:
            raise RuntimeError("tokenizer archive failed verification")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if not members or len(members) > 256 or sum(m.file_size for m in members) > 16 * 1024 * 1024:
                raise RuntimeError("tokenizer archive exceeds extraction limits")
            names = set()
            for member in members:
                path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                        or not path.parts or path.parts[0] != "punkt_tab"
                        or member.filename in names or member.flag_bits & 1
                        or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                    raise RuntimeError("tokenizer archive contains an unsafe entry")
                names.add(member.filename)
            parent = root / "nltk_data" / "tokenizers"
            for directory in (root / "nltk_data", parent):
                if directory.is_symlink():
                    raise RuntimeError("tokenizer destination must not be a symlink")
                directory.mkdir(exist_ok=True)
            expected = {parent / m.filename for m in members if not m.is_dir()}
            for existing in (root / "nltk_data").rglob("*"):
                if existing.is_symlink() or (existing.is_file() and existing not in expected):
                    raise RuntimeError("existing tokenizer inventory differs; use a fresh staging root")
            target = parent / "punkt_tab"
            if target.is_symlink():
                raise RuntimeError("tokenizer destination must not be a symlink")
            with tempfile.TemporaryDirectory(prefix=".tokenizer-stage-", dir=parent) as temporary:
                stage = Path(temporary)
                for member in members:
                    destination = stage / member.filename
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, destination.open("xb") as output:
                            # The verified archive declares a bounded total size.
                            output.write(source.read())
                staged = stage / "punkt_tab"
                files = sorted(p.relative_to(staged) for p in staged.rglob("*") if p.is_file())
                if not files:
                    raise RuntimeError("tokenizer archive has no files")
                if target.exists():
                    existing = list(target.rglob("*"))
                    if (any(p.is_symlink() for p in existing)
                            or sorted(p.relative_to(target) for p in existing if p.is_file()) != files
                            or any(_sha256(target / p) != _sha256(staged / p) for p in files)):
                        raise RuntimeError("existing tokenizer differs from pinned resource; use a fresh staging root")
                else:
                    os.rename(staged, target)
            return [target / p for p in files]
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise RuntimeError("tokenizer staging failed") from exc


def _artifact(
    item: Mapping[str, object],
    files: Iterable[Mapping[str, object]],
    *,
    role: str | None = None,
) -> dict[str, object]:
    return {
        "role": role or item["role"],
        "model_id": item["model_id"],
        "revision": item["revision"],
        "license": item["license"],
        "files": list(files),
    }


def _protect_operator_inventory(cache: Path) -> None:
    """Reference acquisition must not reset a custom or unreadable inventory."""
    manifest = cache / "approved-model-manifest.json"
    if not manifest.exists() and not manifest.is_symlink():
        return
    try:
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
            raise ValueError
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != TRANSCRIPTION_SCHEMA:
            raise ValueError
    except (OSError, ValueError):
        raise RuntimeError(
            "Reference staging cannot replace this operator inventory. Use a new model root "
            "and keep the existing selection for rollback."
        ) from None


def _write_manifest(cache: Path, artifacts: list[dict[str, object]]) -> None:
    _protect_operator_inventory(cache)
    target = cache / "approved-model-manifest.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest-", dir=cache)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                {"schema_version": TRANSCRIPTION_SCHEMA, "artifacts": artifacts},
                output,
                indent=2,
                sort_keys=True,
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"[vault] transcription manifest sealed: {len(artifacts)} artifact sets")


def _receipt_artifact(root: Path, path: Path) -> tuple[str, dict[str, object]]:
    """Bind a runtime snapshot name, its link relationship, and the artifact bytes."""
    candidate = Path(os.path.abspath(path))
    if not candidate.is_relative_to(root) or candidate == root:
        raise RuntimeError("Staged model receipt contains an unsafe artifact")
    # Hugging Face snapshots use file symlinks into blobs. Directory symlinks
    # are not needed and would hide intermediate relationships from the receipt.
    for parent in candidate.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise RuntimeError("Staged model receipt contains a linked directory")
    link_target = os.readlink(candidate) if candidate.is_symlink() else None
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise RuntimeError("Staged model receipt contains an unsafe artifact")
    metadata = {"resolved_path": resolved.relative_to(root).as_posix(),
                "symlink_target": link_target,
                "size_bytes": resolved.stat().st_size, "sha256": _sha256(resolved)}
    # A link changed while hashing cannot receive a valid receipt for old bytes.
    if (candidate.resolve(strict=True) != resolved
            or (os.readlink(candidate) if candidate.is_symlink() else None) != link_target):
        raise RuntimeError("Staged model artifact changed while being verified")
    return candidate.relative_to(root).as_posix(), metadata


def _verify_snapshot_inventory(root: Path, names: Iterable[str]) -> None:
    """Require complete inventories for selected snapshots and tokenizer data."""
    recorded = set(names)
    directories: set[Path] = set()
    for name in recorded:
        candidate = root / name
        for parent in candidate.parents:
            if parent == root:
                break
            if parent.parent.name == "snapshots":
                directories.add(parent)
        if candidate.is_relative_to(root / "nltk_data"):
            directories.add(root / "nltk_data")

    def unavailable(error: OSError) -> None:
        raise RuntimeError("Staged model snapshot inventory is unavailable; resume staging") from error

    for directory in directories:
        expected = {name for name in recorded if (root / name).is_relative_to(directory)}
        observed: set[str] = set()
        for parent, children, files in os.walk(directory, followlinks=False, onerror=unavailable):
            base = Path(parent)
            if any((base / child).is_symlink() for child in children):
                raise RuntimeError("Staged model snapshot inventory contains a linked directory")
            observed.update((base / name).relative_to(root).as_posix() for name in files)
        if observed != expected:
            raise RuntimeError("Staged model snapshot inventory contains missing or unrecorded artifacts; resume staging")


def _stage_receipt(root: Path, catalog: Path, groups: frozenset[str], profile: str,
                   files: Iterable[Path], *, diarization_backend: str = "community-1") -> None:
    root = root.resolve(strict=True)
    inventory = {}
    for path in files:
        relative, metadata = _receipt_artifact(root, path)
        inventory[relative] = metadata
    if not inventory:
        raise RuntimeError("Staged model receipt has no artifacts")
    _verify_snapshot_inventory(root, inventory)
    payload = {"format_version": 2, "catalog_sha256": _sha256(catalog), "groups": sorted(groups),
               "review_profile": profile, "files": inventory}
    if "transcription-diarization" in groups:
        payload["diarization_backend"] = diarization_backend
    descriptor, temporary_name = tempfile.mkstemp(prefix=".stage-receipt-", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / _STAGE_RECEIPT_NAME)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_stage(root: Path, catalog: Path, groups: frozenset[str], profile: str,
                  *, diarization_backend: str = "community-1") -> None:
    """Verify the last complete exact selection without network or filesystem writes."""
    root = Path(root).resolve(strict=True)
    receipt = root / _STAGE_RECEIPT_NAME
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 32 * 1024 * 1024:
        raise RuntimeError("Model staging receipt is unavailable; resume staging")
    value = json.loads(receipt.read_text())
    if (not isinstance(value, dict) or value.get("format_version") != 2 or value.get("catalog_sha256") != _sha256(catalog)
            or value.get("groups") != sorted(groups) or value.get("review_profile") != profile
            or ("transcription-diarization" in groups
                and value.get("diarization_backend", "community-1") != diarization_backend)
            or not isinstance(value.get("files"), dict) or not value["files"]):
        raise RuntimeError("Model selection changed or staging is incomplete; resume staging")
    for name, metadata in value["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not isinstance(metadata, dict):
            raise RuntimeError("Model staging receipt contains an unsafe artifact")
        try:
            observed_name, observed = _receipt_artifact(root, root / relative)
            if observed_name != name or observed != metadata:
                raise RuntimeError("Staged model artifact relationship or bytes changed")
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("A staged model artifact is missing or changed; resume staging") from exc
    _verify_snapshot_inventory(root, value["files"])


def _acquisition_plan(payload: Mapping[str, object], groups: frozenset[str],
                      review_profile: str, diarization_backend: str) -> dict:
    artifacts = []
    for section in ("models", "direct_files"):
        for item in payload.get(section, []):
            if isinstance(item, dict) and _selected(item, groups, review_profile=review_profile,
                                                   diarization_backend=diarization_backend):
                artifacts.append({key: item[key] for key in
                    ("model_id", "revision", "license", "gated", "url", "sha256") if key in item})
    resources = {}
    if any(group.startswith("transcription-") for group in groups):
        resources["punkt_tab"] = payload["resources"]["punkt_tab"]
    return {"models": artifacts, "resources": resources,
            "terms_note": "Catalog license labels are upstream declarations, not legal clearance. "
                          "Review upstream terms, including tokenizer data, before acquisition."}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("stage", "verify", "plan"), default="stage")
    parser.add_argument("--catalog", type=Path, default=Path("config/models.json"))
    parser.add_argument("--model-root", type=Path, default=Path("/models"))
    parser.add_argument(
        "--groups",
        default="review,transcription-asr,transcription-alignment-en",
        help="comma-separated review and transcription module groups",
    )
    parser.add_argument("--token-stdin", action="store_true")
    parser.add_argument("--accept-model-terms", action="store_true",
                        help="acknowledge review of selected model and tokenizer resource terms")
    parser.add_argument("--punkt-archive", type=Path,
                        help="operator-supplied pinned punkt_tab ZIP; verified without downloading it")
    parser.add_argument("--diarization-backend", choices=("nemotron", "community-1"), default="nemotron")
    parser.add_argument(
        "--review-profile",
        choices=("portable", "quality"),
        default="portable",
    )
    args = parser.parse_args()
    groups = frozenset(value.strip() for value in args.groups.split(",") if value.strip())
    if not groups or not groups.issubset(STAGE_GROUPS):
        parser.error("--groups contains an unsupported model module")
    transcription_selected = any(
        value.startswith("transcription-") for value in groups
    )
    root = args.model_root.resolve()
    cache = root / "huggingface" / "hub"
    if args.command == "verify":
        if args.token_stdin:
            parser.error("offline verification does not accept a token")
        _verify_stage(root, args.catalog, groups, args.review_profile,
                      diarization_backend=args.diarization_backend)
        print("[vault] existing model selection verified offline; no model artifacts changed")
        return 0
    if args.command == "plan":
        if args.token_stdin:
            parser.error("acquisition planning does not accept a token")
        print(json.dumps(_acquisition_plan(_catalog(args.catalog), groups, args.review_profile,
                                          args.diarization_backend), indent=2))
        return 0
    _protect_operator_inventory(cache)
    payload = _catalog(args.catalog)
    if transcription_selected and not args.accept_model_terms:
        raise RuntimeError("review the acquisition plan, then pass --accept-model-terms for "
                           "the selected models and punkt_tab resource; no artifacts acquired")
    cache.mkdir(parents=True, exist_ok=True)
    token = _token(args)
    try:
        from huggingface_hub import snapshot_download
        transcription_artifacts: list[dict[str, object]] = []
        dependency_index = 0
        staged_files: list[Path] = []
        for raw in payload.get("models", []):
            if not isinstance(raw, dict) or not _selected(
                raw, groups, review_profile=args.review_profile,
                diarization_backend=args.diarization_backend,
            ):
                continue
            if raw.get("gated") and not token:
                raise RuntimeError(
                    f"{raw.get('model_id')} is gated; accept its terms and rerun with --token-stdin"
                )
            print(f"[vault] acquiring {raw['model_id']} @ {str(raw['revision'])[:12]}")
            snapshot_path = Path(
                snapshot_download(
                    repo_id=str(raw["model_id"]),
                    revision=str(raw["revision"]),
                    cache_dir=cache,
                    token=token or False,
                    **({"allow_patterns": raw["allow_patterns"]} if "allow_patterns" in raw else {}),
                    local_files_only=False,
                )
            )
            snapshot_files = [path for path in snapshot_path.rglob("*") if path.is_file() or path.is_symlink()]
            if not snapshot_files:
                raise RuntimeError("Staged model snapshot contains no artifacts")
            staged_files.extend(snapshot_files)
            if str(raw["group"]).startswith("transcription-"):
                role = str(raw["role"])
                if role == "diarization_dependency":
                    dependency_index += 1
                    role = f"diarization_dependency_{dependency_index}"
                transcription_artifacts.append(
                    _artifact(raw, _snapshot_files(snapshot_path, cache, bind_snapshot=role in {"asr", "diarization"}), role=role)
                )
        for raw in payload.get("direct_files", []):
            if not isinstance(raw, dict) or not _selected(
                raw, groups, review_profile=args.review_profile,
                diarization_backend=args.diarization_backend,
            ):
                continue
            print(f"[vault] acquiring {raw['model_id']} alignment artifact")
            target = _download_direct(raw, cache)
            staged_files.append(target)
            transcription_artifacts.append(
                _artifact(
                    raw,
                    [
                        {
                            "path": target.relative_to(cache).as_posix(),
                            "size_bytes": target.stat().st_size,
                            "sha256": _sha256(target),
                        }
                    ],
                )
            )
        if transcription_selected:
            print("[vault] acquiring verified tokenizer support: punkt_tab")
            staged_files.extend(_stage_tokenizer(root, payload["resources"]["punkt_tab"], args.punkt_archive))
            _write_manifest(cache, transcription_artifacts)
            staged_files.append(cache / "approved-model-manifest.json")
        _stage_receipt(root, args.catalog, groups, args.review_profile, staged_files,
                       diarization_backend=args.diarization_backend)
    finally:
        token = None
    print("[vault] pinned model staging complete; no access token retained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
