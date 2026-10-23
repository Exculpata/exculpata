#!/usr/bin/env python3
"""Regenerate the frozen Review acceptance pack's derived integrity pins."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.review_acceptance.integrity import (  # noqa: E402
    PackIntegrityError,
    canonical_sha256,
    file_sha256,
    referenced_fixture_files,
    referenced_test_files,
    referenced_test_nodes,
    safe_repo_path,
    test_node_sha256,
    verify_pack_integrity,
)


def regenerate(root: Path, payload: dict) -> dict:
    if payload.get("schema_version") != 2:
        raise PackIntegrityError("review acceptance pack schema is unsupported")
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise PackIntegrityError("acceptance integrity algorithm must be sha256")
    updated = copy.deepcopy(payload)
    nodes = referenced_test_nodes(updated)
    files = referenced_test_files(updated)
    fixtures = referenced_fixture_files(updated)
    updated["case_fingerprint"] = canonical_sha256(updated["cases"])
    pins = updated["integrity"]
    pins["test_node_sha256"] = {node: test_node_sha256(root, node) for node in nodes}
    pins["test_file_sha256"] = {path: file_sha256(root, path) for path in files}
    pins["fixture_file_sha256"] = {path: file_sha256(root, path) for path in fixtures}
    pins["content_fingerprint"] = canonical_sha256({
        "manifest": {key: value for key, value in updated.items() if key != "integrity"},
        **{key: pins[key] for key in (
            "algorithm", "test_node_sha256", "test_file_sha256", "fixture_file_sha256"
        )},
    })
    verify_pack_integrity(root, updated)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report stale pins without writing")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository containing pack inputs")
    parser.add_argument("--pack", default="benchmarks/review-acceptance-v1.json",
                        help="manifest path relative to --root")
    args = parser.parse_args()
    try:
        manifest = safe_repo_path(args.root, args.pack)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise PackIntegrityError("acceptance manifest must be an object")
        updated = regenerate(args.root, payload)
        if updated == payload:
            print("Acceptance pins are current.")
            return 0
        if args.check:
            print("Acceptance pins are stale; run scripts/regenerate-acceptance-pins.py to regenerate.")
            return 1
        manifest.write_text(json.dumps(updated, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print("Acceptance pins regenerated.")
        return 0
    except (PackIntegrityError, OSError, ValueError) as exc:
        print(f"Cannot regenerate acceptance pins: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
