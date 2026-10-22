"""Apply an OCR output-file limit in a fresh process, then replace it with OCR.

Applying limits here keeps the threaded application out of ``preexec_fn`` and
leaves its own process limits unchanged. Exec preserves the parent's timeout
and kill target; it does not leave a second, unsupervised recognition child.
"""

from __future__ import annotations

import os
import resource
import signal
import sys

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
# Isolated helper: python -I -S cannot import the package. The prefix is a
# wire format shared with the parent and stays stable across the rename.
_OCR_LIMIT_RECEIPT_PREFIX = "recordbench-ocr-limit"


def main(arguments: list[str]) -> int:
    try:
        maximum = int(arguments[0])
        command = arguments[1:]
        if not 0 < maximum <= MAX_OUTPUT_BYTES or not command or not os.path.isabs(command[0]):
            return 125
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        effective = min(value for value in (maximum, soft, hard) if value != resource.RLIM_INFINITY)
        resource.setrlimit(resource.RLIMIT_FSIZE, (effective, effective))
        # A default SIGXFSZ can send source-bearing process memory to an external
        # core collector, even when RLIMIT_CORE is zero. Inherit SIG_IGN through
        # exec so an oversized write fails with EFBIG without that signal path.
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # Report the limit actually installed in this process, including a
        # tighter inherited limit. This pipe write is not subject to RLIMIT_FSIZE.
        receipt = f"{_OCR_LIMIT_RECEIPT_PREFIX}:{effective}\n".encode("ascii")
        if os.write(sys.stdout.fileno(), receipt) != len(receipt):
            return 125
        os.execv(command[0], command)
    except (IndexError, OSError, ValueError):
        # No unbounded fallback if the operating system cannot apply the limit.
        return 125
    return 125


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
