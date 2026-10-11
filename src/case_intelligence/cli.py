"""Console entry points with compatibility notices for historical commands."""

from importlib import import_module
from pathlib import Path
import sys


_DEPRECATED = {
    "recordbench": "exculpata",
    "recordbench-workbench": "exculpata-workbench",
    "case-intelligence-workbench": "exculpata-workbench",
    "recordbench-retrieval-worker": "exculpata-retrieval-worker",
    "case-review-bench": "python -m case_intelligence.review_bench",
}


def _run(module: str):
    command = Path(sys.argv[0]).name
    # Windows console launchers include an executable suffix.
    if command.endswith(".exe"):
        command = command[:-4]
    replacement = _DEPRECATED.get(command)
    if replacement:
        print(f"Deprecation notice: {command} is deprecated; use {replacement}.",
              file=sys.stderr)
    return import_module(f"case_intelligence.{module}").main()


def admin():
    return _run("admin_cli")


def workbench():
    return _run("workbench")


def retrieval_worker():
    return _run("retrieval_worker")


def review_bench():
    return _run("review_bench")
