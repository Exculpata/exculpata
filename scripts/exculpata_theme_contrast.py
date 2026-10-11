"""Check Exculpata theme tokens against WCAG AA.

The seed palette and nudge match the approved contrast.py. A token that falls
below its minimum is a failure. Text pairs need 4.5:1. UI and focus pairs need
3:1.
"""
from __future__ import annotations

import colorsys
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME_TOKENS = ROOT / "src/case_intelligence/static/theme-tokens.css"

CHECKS = [
    ("text", "bg", 4.5),
    ("text", "panel", 4.5),
    ("text", "panel-raised", 4.5),
    ("text-muted", "bg", 4.5),
    ("text-muted", "panel", 4.5),
    ("text-muted", "panel-raised", 4.5),
    ("accent", "bg", 4.5),
    ("accent", "panel", 4.5),
    ("accent-hover", "bg", 4.5),
    ("accent-active", "bg", 4.5),
    ("success", "bg", 4.5),
    ("warning", "bg", 4.5),
    ("error", "bg", 4.5),
    ("success", "panel", 4.5),
    ("warning", "panel", 4.5),
    ("error", "panel", 4.5),
    ("accent-contrast", "accent", 4.5),
    ("accent-contrast", "accent-hover", 4.5),
    ("accent-contrast", "accent-active", 4.5),
    ("text", "selection-bg", 4.5),
    ("border-strong", "bg", 3),
    ("border-strong", "panel", 3),
    ("focus-ring", "bg", 3),
    ("focus-ring", "panel", 3),
    ("focus-ring", "panel-raised", 3),
]


def lum(h: str) -> float:
    r, g, b = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]

    def f(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def cr(a: str, b: str) -> float:
    la, lb = sorted([lum(a), lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def nudge(h: str, bg: str, target: float) -> str:
    r, g, b = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    hue, lightness, saturation = colorsys.rgb_to_hls(r, g, b)
    up = lum(bg) < 0.18
    while cr(h, bg) < target and 0 < lightness < 1:
        lightness = min(1, lightness + 0.005) if up else max(0, lightness - 0.005)
        h = "#%02X%02X%02X" % tuple(
            round(c * 255) for c in colorsys.hls_to_rgb(hue, lightness, saturation)
        )
    return h


def adjusted_tokens() -> dict[str, dict[str, str]]:
    """Return the approved palette after the same contrast nudge as contrast.py."""
    tokens = {
        "dark": {
            "bg": "#0B0D10",
            "panel": "#14161A",
            "panel-raised": "#1C1F24",
            "text": "#EDEAE2",
            "text-muted": "#787E86",
            "border": "#2A2E35",
            "border-strong": "#5A616B",
            "accent": "#22D3EE",
            "accent-hover": "#67E8F9",
            "accent-active": "#06B6D4",
            "accent-contrast": "#0B0D10",
            "success": "#39FF88",
            "warning": "#FFB020",
            "error": "#FF6B6B",
            "focus-ring": "#22D3EE",
            "selection-bg": "#0E4A57",
        },
        "light": {
            "bg": "#F7F5F0",
            "panel": "#EDEAE2",
            "panel-raised": "#FFFFFF",
            "text": "#0B0D10",
            "text-muted": "#5A6069",
            "border": "#D6D2C8",
            "border-strong": "#8A8F96",
            "accent": "#097488",
            "accent-hover": "#086A7C",
            "accent-active": "#065A69",
            "accent-contrast": "#FFFFFF",
            "success": "#0E8A4A",
            "warning": "#A15C00",
            "error": "#C2281F",
            "focus-ring": "#097488",
            "selection-bg": "#BDEBF3",
        },
    }
    original = json.loads(json.dumps(tokens))
    for theme in tokens.values():
        for _ in range(4):
            for foreground, background, minimum in CHECKS:
                if cr(theme[foreground], theme[background]) < minimum:
                    if foreground == "accent-contrast":
                        theme[background] = nudge(theme[background], theme[foreground], minimum)
                    elif foreground == "text" and background == "selection-bg":
                        theme[background] = nudge(theme[background], theme[foreground], minimum)
                    else:
                        theme[foreground] = nudge(theme[foreground], theme[background], minimum)
    for theme_name, theme in tokens.items():
        for name, value in theme.items():
            if value != original[theme_name][name] and not value.startswith("#"):
                raise RuntimeError(f"adjusted token {theme_name} {name} is not a color")
    return tokens


def contrast_failures(tokens: dict[str, dict[str, str]]) -> list[str]:
    failures = []
    for theme_name, theme in tokens.items():
        for foreground, background, minimum in CHECKS:
            ratio = cr(theme[foreground], theme[background])
            if ratio < minimum:
                failures.append(
                    f"{theme_name} {foreground} {theme[foreground]} on "
                    f"{background} {theme[background]} is {ratio:.2f}:1, minimum {minimum}:1"
                )
    return failures


def parse_theme_tokens(css: str) -> dict[str, dict[str, str]]:
    """Read the dark and light --x-* color tokens from theme-tokens.css."""

    def colors(block: str) -> dict[str, str]:
        found = {
            match.group(1): match.group(2).upper()
            for match in re.finditer(r"--x-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", block)
        }
        return found

    dark_match = re.search(
        r":root, \[data-theme=\"dark\"\] \{(?P<body>.*?)\n\}",
        css,
        re.DOTALL,
    )
    light_match = re.search(
        r"\[data-theme=\"light\"\] \{(?P<body>.*?)\n\}",
        css,
        re.DOTALL,
    )
    prefers_match = re.search(
        r":root:not\(\[data-theme\]\) \{(?P<body>.*?)\n\s*\}",
        css,
        re.DOTALL,
    )
    if dark_match is None or light_match is None or prefers_match is None:
        raise ValueError("theme-tokens.css is missing a dark, light, or prefers-color-scheme block")
    light = colors(light_match.group("body"))
    prefers = colors(prefers_match.group("body"))
    if light != prefers:
        raise ValueError("light theme tokens differ from the prefers-color-scheme block")
    return {"dark": colors(dark_match.group("body")), "light": light}


def main() -> int:
    css = THEME_TOKENS.read_text(encoding="utf-8")
    parsed = parse_theme_tokens(css)
    expected = adjusted_tokens()
    failures = contrast_failures(parsed)
    drifted = [
        f"{theme} {name}: file {parsed[theme].get(name)} approved {value}"
        for theme, values in expected.items()
        for name, value in values.items()
        if parsed.get(theme, {}).get(name) != value
    ]
    if failures or drifted:
        print("\n".join([*failures, *drifted]))
        return 1
    print("fails: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
