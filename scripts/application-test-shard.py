"""Select an application shard or verify the full collection against both shards."""

import argparse
from collections import Counter
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
SERIAL_TESTS = (
    "tests/test_discovery_review_index.py::test_rebuild_step_row_budget[10000]",
    "tests/test_discovery_review_index_lifecycle.py::"
    "test_single_heavily_coded_item_stays_pending_with_complete_values_on_resume",
)


def application_files(root: Path) -> list[str]:
    return sorted({
        path.relative_to(root).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in (root / "tests").rglob(pattern)
        if path.is_file()
    })


def verify_collections(all_nodes: list[str], shards: list[list[str]]) -> None:
    expected = Counter(all_nodes)
    selected = Counter(node for shard in shards for node in shard)
    serial = set(SERIAL_TESTS)
    if not serial <= expected.keys():
        raise ValueError(f"serial tests absent from the full collection: {sorted(serial - expected.keys())}")
    if serial & selected.keys():
        raise ValueError(f"serial tests selected in a shard: {sorted(serial & selected.keys())}")
    for counts in (expected, selected):
        duplicates = sorted(node for node, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate test IDs: {duplicates}")
    missing = sorted(expected.keys() - serial - selected.keys())
    unexpected = sorted(selected.keys() - expected.keys())
    if missing or unexpected:
        raise ValueError(f"missing test IDs: {missing}; unexpected test IDs: {unexpected}")
    if not any(node.startswith("tests/test_discovery_review_index.py::test_rebuild_step_row_budget[")
               for node in selected):
        raise ValueError("no other row-budget parameter remains in the shards")


def check_inventory() -> None:
    deadline = time.monotonic() + 600
    env = {**os.environ, "PYTEST_ADDOPTS": "", "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT)))}

    def collect(argument: str) -> list[str]:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "--color=no", "-q", argument],
            cwd=ROOT, env=env, capture_output=True, text=True,
            timeout=max(0, deadline - time.monotonic()),
        )
        if result.returncode:
            raise ValueError(f"pytest collection failed for {argument}\n{result.stdout}{result.stderr}")
        return [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]

    all_nodes = collect("tests")
    shards = []
    with tempfile.TemporaryDirectory() as directory:
        for index in (0, 1):
            argument_file = Path(directory) / f"shard-{index}.txt"
            with argument_file.open("w", encoding="utf-8") as output:
                subprocess.run([sys.executable, str(Path(__file__).resolve()), str(index)],
                               stdout=output, check=True, timeout=max(0, deadline - time.monotonic()))
            shards.append(collect("@" + str(argument_file)))
    verify_collections(all_nodes, shards)
    print(f"Verified {len(all_nodes)} application tests: shards {len(shards[0])} and {len(shards[1])}, "
          f"plus {len(SERIAL_TESTS)} serial tests.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("shard", nargs="?", type=int, choices=(0, 1))
    mode.add_argument("--check", action="store_true", help="verify application test coverage across both shards")
    args = parser.parse_args()
    if args.check:
        try:
            check_inventory()
        except subprocess.TimeoutExpired:
            parser.exit(1, "Application shard collection exceeded its ten-minute deadline.\n")
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            parser.exit(1, f"Application shard collection failed: {exc}\n")
        return
    files = application_files(ROOT)[args.shard::2]
    if not files or any("\n" in path or "\r" in path for path in files):
        parser.error("shard requires nonempty test paths representable in a pytest argument file")
    print("\n".join([*(f"--deselect={node}" for node in SERIAL_TESTS), *files]))


if __name__ == "__main__":
    main()
