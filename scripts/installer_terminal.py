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
from typing import Sequence


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


def format_elapsed(seconds: float) -> str:
    """Whole-second duration. Hours appear only after 60 minutes."""
    whole = max(0, int(seconds))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def progress_event_text(text: str, width: int) -> str:
    """One progress row clipped so a narrow terminal does not wrap it."""
    return clip_cells(f"  [ NOTE ] {text}", max(0, width - 1))


def cell_width(value: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in value
    )


class DockerActivity:
    """Summarize BuildKit and Compose lines without inventing a total or a percentage."""

    def __init__(self, targets: Sequence[str] = ()) -> None:
        self.targets = tuple({target: None for target in targets})
        self.built: list[str] = []
        self.service: str | None = None
        self.step_current: int | None = None
        self.step_total: int | None = None
        self.instruction: str | None = None
        self.build_current: int | None = None
        self.build_total: int | None = None
        self.note: str | None = None

    def observe(self, line: str) -> str | None:
        if len(line) > 2000:
            return None
        before = self.summary()
        self._consume(terminal_text(line).strip())
        after = self.summary()
        return after if after != before else None

    def summary(self) -> str:
        parts: list[str] = []
        if self.targets:
            parts.append(f"images built {len(self.built)} of {len(self.targets)}")
        elif self.built:
            parts.append(f"images built {len(self.built)}")
        if self.build_current is not None and self.build_total is not None:
            parts.append(f"build steps {self.build_current} of {self.build_total}")
        if self.step_current is not None and self.step_total is not None:
            label = f"{self.service} " if self.service else ""
            parts.append(f"{label}step {self.step_current} of {self.step_total}")
        if self.instruction:
            parts.append(self.instruction)
        elif self.note:
            parts.append(self.note)
        return "; ".join(parts) if parts else "waiting for Docker output"

    def _consume(self, line: str) -> None:
        if not line:
            return
        prefixed = re.match(r"^[A-Za-z0-9_.-]+ \| (.*)$", line)
        if prefixed:
            line = prefixed.group(1).strip()
        # Byte-progress redraws report a moving percentage. That is not a step total.
        if "%" in line and not line.startswith("#"):
            return
        building = re.match(r"^\[\+\] Building \S+ \((\d+)/(\d+)\)", line)
        if building:
            self.build_current = int(building.group(1))
            self.build_total = int(building.group(2))
            return
        image = re.match(r"^Image\s+(\S+)\s+(Building|Built|Pulling|Pulled)$", line)
        legacy = None if image else re.match(r"^(\S+)\s+(Built|Pulled)$", line)
        chosen = image or legacy
        if chosen:
            token, state = chosen.group(1), chosen.group(2)
            if legacy and self.targets and self._known_service(token) is None:
                return
            if legacy and not self.targets and "/" not in token and ":" not in token:
                return
            service = self._known_service(token)
            if state in {"Built", "Pulled"} and service and service not in self.built:
                self.built.append(service)
            elif state in {"Building", "Pulling"}:
                self.note = f"{state.lower()} {service or token}"
                self.instruction = None
            return
        step = re.match(r"^#\d+ \[([^\]]+)\] (.+)$", line)
        if step:
            bracket, detail = step.group(1), step.group(2)
            fraction = re.search(r"(\d+)/(\d+)", bracket)
            self.service = self._known_service(bracket.split()[0])
            if fraction:
                self.step_current = int(fraction.group(1))
                self.step_total = int(fraction.group(2))
            else:
                self.step_current = self.step_total = None
            self.instruction = clip_cells(detail, 120)
            self.note = None
            return
        pulling = re.match(r"^(\S+): Pulling from (\S+)$", line)
        if pulling:
            self.note = f"pulling {pulling.group(2)}"
            self.instruction = None
            return
        layer = re.match(
            r"^([0-9a-f]{4,64}): (Pulling fs layer|Waiting|Download complete|Verifying Checksum|Extracting|Pull complete|Already exists)$",
            line,
        )
        if layer:
            self.note = f"layer {layer.group(1)}: {layer.group(2)}"
            self.instruction = None
            return
        # Compose aligns these rows with more than one space.
        resource = re.match(
            r"^(Container|Network|Volume) (\S+)\s+(Creating|Created|Starting|Started|Running|Restarting|Stopping|Stopped|Removing|Removed|Healthy|Waiting)$",
            line,
        )
        if resource:
            self.note = f"{resource.group(1)} {resource.group(2)} {resource.group(3)}"
            self.instruction = None

    def _known_service(self, token: str) -> str | None:
        leaf = token.rsplit("/", 1)[-1].split(":", 1)[0]
        if self.targets:
            if leaf in self.targets:
                return leaf
            if token in self.targets:
                return token
            return None
        if leaf and leaf not in {"internal", "Image"} and not leaf[0].isdigit():
            return leaf
        return None


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
        self.typed(f"  Exculpata installer // {mode.upper()} // BUILD {version}")
        self.line(self.paint("  +-- This account runs this command. --+", C.muted))

    def progress_event(self, text: str) -> None:
        """Print one progress event. Animated consoles redraw inside progress()."""
        if self.quiet or self.animation:
            return
        visible = progress_event_text(text, terminal_width())
        marker = "  [ NOTE ] "
        if self.color and visible.startswith(marker):
            visible = "  " + self.paint("[ NOTE ]", C.cyan + C.bold) + visible[len(marker) - 1:]
        self.line(visible)

    @contextmanager
    def progress(self, title: str, status: dict[str, str] | None = None):
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
                    if status is None:
                        text = title
                    else:
                        elapsed = terminal_text(status.get("elapsed") or "")
                        detail = terminal_text(status.get("detail") or title)
                        text = f"{elapsed} {detail}".strip()
                    frame = clip_cells(f"  [{indicator} WORK] {text}", terminal_width() - 1)
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
