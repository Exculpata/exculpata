"""Freeze refactor-sensitive interfaces; update snapshots only after review.

From the repository root, deliberately regenerate both route configurations or
the store API snapshot with:
    .venv/bin/python -m tests.test_api_snapshots --update routes
    .venv/bin/python -m tests.test_api_snapshots --update store
"""


from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import inspect
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore

SNAPSHOTS = Path(__file__).parent / "snapshots"
ROUTE_SNAPSHOTS = (
    (False, "workbench_routes.json"),
    (True, "workbench_routes_acceptance.json"),
)


def _route_snapshot(runtime: Path, *, acceptance_diagnostics: bool = False) -> str:
    # Keep the synthetic runtime independent of configured services and storage.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CASE_INTELLIGENCE_")
    }
    # Enable only the route-registration flag under test, never operator settings.
    if acceptance_diagnostics:
        environment["CASE_INTELLIGENCE_ACCEPTANCE_DIAGNOSTICS"] = "1"
    with patch.dict(os.environ, environment, clear=True):
        app = create_workbench_app(
            runtime, generator=UnavailableGenerator(), auth_mode="test"
        )
        with TestClient(app):
            # Sort only each method set: FastAPI matches the route list in order.
            # Mounts (including /static) have no methods and use an empty list.
            rows = [
                (sorted(getattr(route, "methods", None) or ()), route.path)
                for route in app.routes
            ]
    return _serialize(rows)


def _store_snapshot() -> str:
    # getmembers includes inherited methods and sorts names.
    return _serialize([
        (name, _signature(method))
        for name, method in inspect.getmembers(WorkspaceStore, inspect.isroutine)
        if not name.startswith("_")
    ])


def _signature(method) -> str:
    signature = inspect.signature(method)
    rendered = str(signature)
    for parameter in signature.parameters.values():
        if inspect.isfunction(parameter.default):
            # queue_answer_job has a lambda default. Its address changes across
            # processes; retain its function name and every other signature detail.
            default = parameter.default
            rendered = rendered.replace(repr(default), f"<function {default.__qualname__}>")
    return rendered


def _serialize(rows: list[tuple]) -> str:
    # One complete entry per line makes additions, removals and order reviewable.
    return "[\n" + ",\n".join("  " + json.dumps(row) for row in rows) + "\n]\n"


def _assert_snapshot(actual: str, filename: str, target: str) -> None:
    path = SNAPSHOTS / filename
    guidance = (
        f"{filename} changed. Review the interface diff; if intentional, regenerate "
        f"from the repository root with: "
        f".venv/bin/python -m tests.test_api_snapshots --update {target}. "
        "Commit the reviewed snapshot with the change."
    )
    assert path.exists(), guidance
    assert actual == path.read_text(encoding="utf-8"), guidance


@pytest.mark.parametrize(
    ("acceptance_diagnostics", "filename"),
    ROUTE_SNAPSHOTS,
    ids=("default", "acceptance-diagnostics"),
)
def test_workbench_route_snapshot(
    tmp_path: Path, acceptance_diagnostics: bool, filename: str
) -> None:
    _assert_snapshot(
        _route_snapshot(tmp_path / "runtime", acceptance_diagnostics=acceptance_diagnostics),
        filename,
        "routes",
    )


def test_workspace_store_api_snapshot() -> None:
    _assert_snapshot(_store_snapshot(), "workspace_store_api.json", "store")


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", choices=("routes", "store", "all"), required=True)
    args = parser.parse_args()
    SNAPSHOTS.mkdir(exist_ok=True)
    if args.update in {"routes", "all"}:
        for acceptance_diagnostics, filename in ROUTE_SNAPSHOTS:
            with TemporaryDirectory(prefix=f"{LEGACY_STEM}-route-snapshot-") as directory:
                snapshot = _route_snapshot(
                    Path(directory) / "runtime",
                    acceptance_diagnostics=acceptance_diagnostics,
                )
            (SNAPSHOTS / filename).write_text(snapshot, encoding="utf-8")
    if args.update in {"store", "all"}:
        (SNAPSHOTS / "workspace_store_api.json").write_text(
            _store_snapshot(), encoding="utf-8"
        )


if __name__ == "__main__":
    _main()
