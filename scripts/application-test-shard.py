"""Print one of two deterministic, file-level pytest argument lists."""

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    print("\n".join(files))


if __name__ == "__main__":
    main()
