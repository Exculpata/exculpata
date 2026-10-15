"""Check Exculpata theme tokens against WCAG AA.

The seed palette and nudge match the approved contrast.py. A token that falls
below its minimum is a failure. Text pairs need 4.5:1. UI and focus pairs need
3:1.
"""
from __future__ import annotations

import argparse
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
    ("accent", "panel-raised", 4.5),
    ("accent-hover", "bg", 4.5),
    ("accent-hover", "panel", 4.5),
    ("accent-hover", "panel-raised", 4.5),
    ("accent-active", "bg", 4.5),
    ("accent-active", "panel", 4.5),
    ("accent-active", "panel-raised", 4.5),
    ("success", "bg", 4.5),
    ("warning", "bg", 4.5),
    ("error", "bg", 4.5),
    ("success", "panel", 4.5),
    ("warning", "panel", 4.5),
    ("error", "panel", 4.5),
    ("warning", "warning-bg", 4.5),
    ("error", "error-bg", 4.5),
    ("success", "success-bg", 4.5),
    ("text-muted", "warning-bg", 4.5),
    ("text-muted", "error-bg", 4.5),
    ("text-muted", "success-bg", 4.5),
    ("accent-contrast", "accent", 4.5),
    ("accent-contrast", "accent-hover", 4.5),
    ("accent-contrast", "accent-active", 4.5),
    ("text", "selection-bg", 4.5),
    ("border-strong", "bg", 3),
    ("border-strong", "panel", 3),
    ("border-strong", "panel-raised", 3),
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
    accents = {
        "dark": {
            "cyan": ("#22D3EE", "#67E8F9", "#06B6D4", "#0E4A57"),
            "green": ("#39FF88", "#7AFFB0", "#20D96D", "#124D2A"),
            "amber": ("#FFB020", "#FFCA65", "#E99A0B", "#573B0E"),
        },
        "light": {
            "cyan": ("#097488", "#086A7C", "#065A69", "#BDEBF3"),
            "green": ("#0E8A4A", "#0C793F", "#096533", "#C6EBD4"),
            "amber": ("#A15C00", "#8C5000", "#754300", "#F6DFB5"),
        },
    }
    tokens = {
        f"{mode}-{accent}": {**values, "accent": palette[0], "accent-hover": palette[1],
                              "accent-active": palette[2], "focus-ring": palette[0],
                              "selection-bg": palette[3]}
        for mode, values in tokens.items() for accent, palette in accents[mode].items()
    }
    # Semantic colors and their surfaces are invariant under accent changes.
    for name, values in tokens.items():
        dark = name.startswith("dark-")
        values.update({"shadow": "#000000", "warning-bg": "#30281A" if dark else "#FFF2D9",
                       "error-bg": "#321D20" if dark else "#FDECE9",
                       "success-bg": "#162D21" if dark else "#E3F3E9"})
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


def token_block(mode, values, selector):
    lines = [f"{selector} {{", *(f"  --x-{name}: {value};" for name, value in values.items()),
             f"  color-scheme: {mode};", "}"]
    return "\n".join(lines)


def render_tokens():
    """Generate all six palettes with an explicit Dark/Cyan default."""
    current = THEME_TOKENS.read_text(encoding="utf-8")
    prefix = current[:current.index("/* Default")]
    blocks = [prefix + "/* Default is Dark/Cyan; mode and accent are independent. */"]
    for name, values in adjusted_tokens().items():
        mode, accent = name.split("-")
        selector = f'[data-theme="{mode}"][data-accent="{accent}"]'
        if accent == "cyan":
            selector = (":root, " if mode == "dark" else "") + f'[data-theme="{mode}"], ' + selector
        blocks.append(token_block(mode, values, selector))
    blocks.append("::selection { background: var(--x-selection-bg); color: var(--x-text); }")
    blocks.append(":focus-visible { outline: var(--x-focus-ring-width) solid var(--x-focus-ring); outline-offset: var(--x-focus-ring-offset); }")
    return "\n".join(blocks) + "\n"


def parse_theme_tokens(css: str) -> dict[str, dict[str, str]]:
    result = {}
    for match in re.finditer(r'\[data-theme="(dark|light)"\]\[data-accent="(cyan|green|amber)"\] \{(.*?)\n\}', css, re.DOTALL):
        result[f"{match[1]}-{match[2]}"] = {
            name: value.upper() for name, value in re.findall(r"--x-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", match[3])
        }
    if len(result) != 6:
        raise ValueError("theme-tokens.css must contain all six mode/accent palettes")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the checked token stylesheet")
    args = parser.parse_args()
    if args.write:
        THEME_TOKENS.write_text(render_tokens(), encoding="utf-8")
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
    print(f"6 palettes, {len(CHECKS) * 6} contrast checks; fails: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
