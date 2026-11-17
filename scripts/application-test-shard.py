"""Print a file shard with the post-shard serial tests deselected."""

import argparse
from pathlib import Path


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shard", type=int, choices=(0, 1))
    args = parser.parse_args()
    files = application_files(ROOT)[args.shard::2]
    if not files or any("\n" in path or "\r" in path for path in files):
        parser.error("shard requires nonempty test paths representable in a pytest argument file")
    print("\n".join([*(f"--deselect={node}" for node in SERIAL_TESTS), *files]))


if __name__ == "__main__":
    main()
