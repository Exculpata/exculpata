"""Render Markdown from a pytest log and an optional CI job status."""

from html import escape
from pathlib import Path
import re
import sys


NODE_SEPARATOR = ":" * 2
NODE = r"tests/[^\s]+\.py(?:::[^\r\n]+?)?"
OUTCOME = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|RERUN"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SHORT_FAILURE = re.compile(rf"^(?:FAILED|ERROR) ({NODE})(?: - .*)?$")
VERBOSE = re.compile(rf"^({NODE})(?:\s+({OUTCOME})\b.*)?$")
WORKER = re.compile(rf"^\[gw\d+\].*?\b({OUTCOME})\s+({NODE})(?:\s+\[\s*\d+%\])?$")
FRAME = re.compile(r'File "(?:.*[/\\])?(tests/[^"\r\n]+\.py)", line \d+, in (test_\w+)')
FINISHED = re.compile(
    r"^(?:=+\s*)?\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b.*\bin \d+(?:\.\d+)?s"
)


def code(value: str) -> str:
    return "`" + escape(value).replace("`", "&#96;") + "`"


def summarize(text: str, status: str) -> str:
    failures: dict[str, None] = {}
    frames: dict[str, None] = {}
    pending: dict[str, None] = {}
    last_reported = None
    finished = False
    timeout = False
    session = False
    for line in ANSI.sub("", text).splitlines():
        line = line.strip()
        finished = finished or bool(FINISHED.match(line))
        session = session or "test session starts" in line or line.startswith("collecting ")
        if re.match(r"^Timeout \([\d:.]+\)!", line):
            timeout = True
        if timeout and (frame := FRAME.search(line)):
            frames[f"{frame[1]}{NODE_SEPARATOR}{frame[2]}"] = None
        if failure := SHORT_FAILURE.fullmatch(line):
            failures[failure[1]] = None
        if worker := WORKER.fullmatch(line):
            outcome, node = worker.groups()
        elif verbose := VERBOSE.fullmatch(line):
            node, outcome = verbose.groups()
        else:
            continue
        last_reported = node
        if outcome:
            pending.pop(node, None)
            if outcome in {"FAILED", "ERROR"}:
                failures[node] = None
        elif NODE_SEPARATOR in node:
            pending.pop(node, None)
            pending[node] = None

    if not (failures or frames or last_reported or finished or session or status == "cancelled"):
        raise ValueError("unrecognized log")
    lines = ["### Application test summary", ""]
    if status == "cancelled" or not finished:
        lines += ["Cancelled or partial run.", ""]
        if pending:
            lines.append("Last started test: " + code(next(reversed(pending))))
        elif last_reported:
            lines.append("Last reported test (may have completed): " + code(last_reported))
        else:
            lines.append("No started test identified.")
        lines.append("")
    if failures:
        lines += ["Failed tests:", *["- " + code(node) for node in failures], ""]
    if frames:
        lines += ["Timed out in:", *["- " + code(node) for node in frames], ""]
    elif timeout:
        lines += ["Timeout reported; no test frame identified.", ""]
    if finished and not failures and not timeout:
        lines.append("No failed tests identified in the log.")
    return "\n".join(lines)


def main() -> None:
    status = "failure"
    try:
        if len(sys.argv) not in {2, 3}:
            raise ValueError("expected log path and optional job status")
        status = sys.argv[2] if len(sys.argv) == 3 else "failure"
        if status not in {"success", "failure", "cancelled"}:
            raise ValueError("unknown job status")
        summary = summarize(Path(sys.argv[1]).read_text(encoding="utf-8"), status)
    except Exception:
        prefix = "Application tests cancelled: " if status == "cancelled" else "Application tests: "
        summary = prefix + "could not parse the test log."
    print(summary)


if __name__ == "__main__":
    main()
