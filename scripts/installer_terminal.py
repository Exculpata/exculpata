"""Small stdlib-only boot console; no terminal effects in plain output."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


BRAND = {
    "cyan": "22D3EE", "green": "39FF88", "amber": "FFB020",
    "black": "0B0D10", "white": "EDEAE2", "muted": "787E86",
}


def nearest_xterm(hex_color: str) -> int:
    rgb = tuple(bytes.fromhex(hex_color))
    basic = (0x000000, 0x800000, 0x008000, 0x808000, 0x000080, 0x800080,
             0x008080, 0xC0C0C0, 0x808080, 0xFF0000, 0x00FF00, 0xFFFF00,
             0x0000FF, 0xFF00FF, 0x00FFFF, 0xFFFFFF)
    palette = [tuple(value.to_bytes(3, "big")) for value in basic]
    levels = (0, 95, 135, 175, 215, 255)
    palette += [(r, g, b) for r in levels for g in levels for b in levels]
    palette += [(value,) * 3 for value in range(8, 239, 10)]
    return min(range(256), key=lambda index: sum((a - b) ** 2 for a, b in zip(rgb, palette[index])))


class C:
    cyan, green, amber, black, white, muted = (
        f"\033[38;5;{nearest_xterm(BRAND[name])}m" for name in BRAND
    )
    dim = muted
    bold = "\033[1m"
    reset = "\033[0m"


# Terminal escape sequences from child processes must never control the terminal.
_ESCAPE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")


def terminal_text(value: str) -> str:
    value = _ESCAPE.sub("", value)
    return "".join(char if char == "\t" or char.isprintable() else " " for char in value)


@dataclass
class Console:
    color: bool
    quiet: bool = False
    animation: bool = False
    truecolor: bool = field(default_factory=lambda: os.getenv("COLORTERM", "").lower() in {"truecolor", "24bit"})
    _typing_left: float = field(default=0.45, init=False)

    def __post_init__(self) -> None:
        # Enforce this at the console boundary, including direct/tooling callers.
        self.color = self.color and sys.stdout.isatty() and "NO_COLOR" not in os.environ and "CI" not in os.environ
        self.animation = self.animation and self.color and not self.quiet

    def paint(self, value: str, color: str) -> str:
        if not self.color:
            return value
        if self.truecolor:
            for name, hex_color in BRAND.items():
                rgb = ";".join(str(part) for part in bytes.fromhex(hex_color))
                color = color.replace(getattr(C, name), f"\033[38;2;{rgb}m")
        return f"{color}{value}{C.reset}"

    def line(self, value: str = "") -> None:
        if not self.quiet:
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

    def status(self, marker: str, value: str) -> None:
        color = C.green if marker == "OK" else C.amber if marker in {"WARN", "BLOCK"} else C.cyan
        self.line(f"  {self.paint(f'[{marker:^6}]', color + C.bold)} {value}")

    def ok(self, value: str) -> None:
        self.status("OK", value)

    def note(self, value: str) -> None:
        self.status("NOTE", value)

    def warn(self, value: str) -> None:
        self.status("WARN", value)

    def banner(self, mode: str, version: str = "0.1.0-beta.1") -> None:
        self.line(self.paint(r"""
   _______  _______ _   _ _     ____   _  _____  _
  | ____\ \/ / ____| | | | |   |  _ \ / \|_   _|/ \
  |  _|  \  / |   | | | | |   | |_) / _ \ | | / _ \
  | |___ /  \ |___| |_| | |___|  __/ ___ \| |/ ___ \
  |_____/_/\_\_____|\___/_____|_| /_/   \_\_/_/   \_\
""".rstrip(), C.cyan + C.bold))
        self.typed(f"  EVIDENCE NODE PROVISIONER // {mode.upper()} // BUILD {version}")
        self.line(self.paint("  +-- LOCAL MODELS. PRIVATE RECORD. OPERATOR CONTROL. --+", C.muted))

    @contextmanager
    def progress(self, title: str):
        self.note(title)
        stop = threading.Event()
        worker = None
        if self.animation:
            def redraw():
                index = 0
                while not stop.wait(0.1):
                    print(f"\r  {self.paint('[' + '|/-\\'[index % 4] + ' WORK]', C.cyan)} {title}", end="", flush=True)
                    index += 1
            worker = threading.Thread(target=redraw, daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join()
                # Always terminate the progress line, including exceptions.
                self.line("\r" + " " * (len(title) + 12) + "\r")
