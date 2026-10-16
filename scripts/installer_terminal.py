"""Small stdlib-only boot console; no terminal effects in plain output."""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field


BRAND = {
    "cyan": "22D3EE", "green": "39FF88", "amber": "FFB020",
    "black": "0B0D10", "white": "EDEAE2", "muted": "787E86",
}


def nearest_xterm(hex_color: str) -> int:
    rgb = tuple(bytes.fromhex(hex_color))
    # Slots 0–15 are theme-configurable ANSI colors, not fixed xterm RGBs.
    levels = (0, 95, 135, 175, 215, 255)
    palette = [(r, g, b) for r in levels for g in levels for b in levels]
    palette += [(value,) * 3 for value in range(8, 239, 10)]
    return 16 + min(range(240), key=lambda index: sum((a - b) ** 2 for a, b in zip(rgb, palette[index])))


class C:
    cyan, green, amber, black, white, muted = (
        f"\033[38;5;{nearest_xterm(BRAND[name])}m" for name in BRAND
    )
    dim = muted
    bold = "\033[1m"
    reset = "\033[0m"


# Seven-column cells keep every stroke aligned at an 80-column terminal width.
_WORDMARK_GLYPHS = {
    "E": (" _____ ", "| ____|", "|  _|  ", "| |___ ", "|_____|"),
    "X": ("__   __", "\\ \\ / /", " \\ V / ", " / . \\ ", "/_/ \\_\\"),
    "C": (" _____ ", "/ ____|", "| |    ", "| |___ ", "\\_____|"),
    "U": (" _   _ ", "| | | |", "| | | |", "| |_| |", " \\___/ "),
    "L": (" _     ", "| |    ", "| |    ", "| |___ ", "|_____|"),
    "P": (" ____  ", "|  _ \\ ", "| |_) |", "|  __/ ", "|_|    "),
    "A": ("   _   ", "  / \\  ", " / _ \\ ", "/ ___ \\", "/_/ \\_\\"),
    "T": (" _____ ", "|_   _|", "  | |  ", "  | |  ", "  |_|  "),
}
WORDMARK = "\n".join(
    "  " + " ".join(_WORDMARK_GLYPHS[letter][row] for letter in "EXCULPATA")
    for row in range(5)
)


# Terminal escape sequences from child processes must never control the terminal.
_ESCAPE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")


def terminal_text(value: str) -> str:
    value = _ESCAPE.sub("", value)
    return "".join(char if char == "\t" or char.isprintable() else " " for char in value)


def terminal_width() -> int:
    try:
        return max(1, os.get_terminal_size(sys.stdout.fileno()).columns)
    except (OSError, ValueError, AttributeError):
        return max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)


def clip_cells(value: str, width: int) -> str:
    result = []
    remaining = max(0, width)
    for char in terminal_text(value).replace("\t", " "):
        cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if cells > remaining:
            break
        result.append(char)
        remaining -= cells
    return "".join(result)


@dataclass
class Console:
    color: bool
    quiet: bool = False
    animation: bool = False
    truecolor: bool = field(default_factory=lambda: os.getenv("COLORTERM", "").lower() in {"truecolor", "24bit"})
    _typing_left: float = field(default=0.45, init=False)

    def __post_init__(self) -> None:
        # Enforce this at the console boundary, including direct/tooling callers.
        self.color = (self.color and sys.stdout.isatty()
                      and "NO_COLOR" not in os.environ and "CI" not in os.environ
                      and os.getenv("TERM", "").lower() != "dumb")
        self.animation = self.animation and self.color and not self.quiet

    def paint(self, value: str, color: str) -> str:
        if not self.color:
            return value
        if self.truecolor:
            for name, hex_color in BRAND.items():
                rgb = ";".join(str(part) for part in bytes.fromhex(hex_color))
                color = color.replace(getattr(C, name), f"\033[38;2;{rgb}m")
        return f"{color}{value}{C.reset}"

    def line(self, value: str = "", *, force: bool = False) -> None:
        if force or not self.quiet:
            print(value, flush=True)

    def typed(self, value: str) -> None:
        if not self.animation or self._typing_left <= 0:
            self.line(self.paint(value, C.cyan + C.bold))
            return
        # Chunked typing, one shared budget across banner and every phase.
        # Charge real elapsed time as well as requested sleep, so slow terminals
        # exhaust the budget rather than accumulating delays throughout a run.
        for offset in range(0, len(value), 4):
            started = time.monotonic()
            print(self.paint(value[offset:offset + 4], C.cyan + C.bold), end="", flush=True)
            delay = min(0.008, max(0, self._typing_left))
            if delay:
                time.sleep(delay)
            self._typing_left -= max(delay, time.monotonic() - started)
        print(flush=True)

    def phase(self, number: int, title: str, detail: str) -> None:
        self.line()
        self.typed(f"  PHASE {number:02d} / {title}")
        self.line(f"             {self.paint(detail, C.muted)}")

    def status(self, marker: str, value: str, *, force: bool = False) -> None:
        color = C.green if marker == "OK" else C.amber if marker in {"WARN", "BLOCK"} else C.cyan
        self.line(f"  {self.paint(f'[{marker:^6}]', color + C.bold)} {value}", force=force)

    def ok(self, value: str) -> None:
        self.status("OK", value)

    def note(self, value: str, *, force: bool = False) -> None:
        self.status("NOTE", value, force=force)

    def warn(self, value: str, *, force: bool = False) -> None:
        self.status("WARN", value, force=force)

    def banner(self, mode: str, version: str = "0.1.0-beta.1") -> None:
        width = terminal_width()
        mark = WORDMARK if width >= 73 else clip_cells("  EXCULPATA" if width >= 11 else "EXCULPATA", width)
        self.line(self.paint("\n" + mark, C.cyan + C.bold))
        self.typed(f"  EVIDENCE NODE PROVISIONER // {mode.upper()} // BUILD {version}")
        self.line(self.paint("  +-- LOCAL MODELS. PRIVATE RECORD. OPERATOR CONTROL. --+", C.muted))

    @contextmanager
    def progress(self, title: str):
        title = terminal_text(title).replace("\t", " ")
        self.note(title)
        stop = threading.Event()
        worker = None
        redrawn = False
        if self.animation:
            def redraw():
                nonlocal redrawn
                index = 0
                while not stop.wait(0.1):
                    # Leave the final cell unused to avoid terminal auto-wrap.
                    indicator = "|/-\\"[index % 4]
                    frame = clip_cells(f"  [{indicator} WORK] {title}", terminal_width() - 1)
                    print("\r" + self.paint(frame, C.cyan) + "\033[K", end="", flush=True)
                    redrawn = True
                    index += 1
            worker = threading.Thread(target=redraw, daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join()
                if redrawn:
                    # Clear the transient row; the next event owns its newline.
                    print("\r\033[2K", end="", flush=True)
