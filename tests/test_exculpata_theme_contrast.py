"""WCAG AA contrast for the self-hosted Exculpata theme tokens."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "exculpata_theme_contrast",
    Path(__file__).resolve().parents[1] / "scripts/exculpata_theme_contrast.py",
)
_CONTRAST = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_CONTRAST)
CHECKS = _CONTRAST.CHECKS
THEME_TOKENS = _CONTRAST.THEME_TOKENS
adjusted_tokens = _CONTRAST.adjusted_tokens
contrast_failures = _CONTRAST.contrast_failures
parse_theme_tokens = _CONTRAST.parse_theme_tokens

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "src/case_intelligence/static/fonts"
STATIC = ROOT / "src/case_intelligence/static"


def test_theme_token_contrast_matches_the_approved_palette():
    css = THEME_TOKENS.read_text(encoding="utf-8")
    parsed = parse_theme_tokens(css)
    assert contrast_failures(parsed) == []
    assert parsed == adjusted_tokens()
    for theme, values in parsed.items():
        for foreground, background, _minimum in CHECKS:
            assert foreground in values and background in values, (theme, foreground, background)


def test_theme_fonts_are_self_hosted_with_licenses():
    css = THEME_TOKENS.read_text(encoding="utf-8")
    assert "http://" not in css and "https://" not in css
    for name in (
        "Geist-Variable.woff2",
        "JetBrainsMono-Variable.woff2",
        "JetBrainsMono-Variable-Italic.woff2",
    ):
        assert f'url("./fonts/{name}")' in css
        font = FONTS / name
        assert font.is_file() and font.stat().st_size > 1000
    for license_name in ("OFL-Geist.txt", "OFL-JetBrainsMono.txt"):
        text = (FONTS / license_name).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE" in text
    styles = (STATIC / "case-intelligence.css").read_text(encoding="utf-8")
    base = (ROOT / "src/case_intelligence/templates/workbench_base.html").read_text(encoding="utf-8")
    login = (ROOT / "src/case_intelligence/templates/workbench_login.html").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in styles and "fonts.googleapis.com" not in css
    assert base.index("theme-tokens.css") < base.index("case-intelligence.css")
    assert login.index("theme-tokens.css") < login.index("case-intelligence.css")
    assert 'data-theme="{{ theme|default(\'dark\') }}"' in base
    assert 'data-theme="dark"' in login
    assert 'value="dark"' in base and 'value="light"' in base and 'value="dusk"' not in base
    assert "var(--x-font-sans)" in styles and "var(--font-mono)" in styles
    # The previous gray fails 4.5:1 on the light canvas. Placeholders use muted text.
    assert "#8993a4" not in styles
    assert "textarea::placeholder,\n.matter-search-form input::placeholder {\n  color: var(--ink-faint);" in styles
    # Source-library hints lost their Dusk-only color. The browser gray is 4.22:1 on dark canvas.
    assert (
        ".source-library-filters input::placeholder,\n"
        ".source-bulk-toolbar input::placeholder {\n"
        "  color: var(--ink-faint);\n"
        "  opacity: 1;\n"
        "}"
    ) in styles
    dependencies = (ROOT / "docs/DEPENDENCIES.md").read_text(encoding="utf-8")
    assert "Geist" in dependencies and "JetBrains Mono" in dependencies
    assert "SIL Open Font License" in dependencies


def test_all_accents_keep_semantic_colors_and_disabled_controls():
    tokens = parse_theme_tokens(THEME_TOKENS.read_text(encoding="utf-8"))
    assert len(tokens) == 6
    for mode in ("dark", "light"):
        for name in ("warning", "error", "success", "warning-bg", "error-bg", "success-bg"):
            assert len({tokens[f"{mode}-{accent}"][name] for accent in ("cyan", "green", "amber")}) == 1
    styles = (STATIC / "case-intelligence.css").read_text()
    assert ".button-danger:disabled, .button-danger.disabled { border-color: var(--x-error); }" in styles
    assert ".button-secondary:disabled, .button-secondary.disabled { border-color: var(--x-border-strong); }" in styles
    for path in STATIC.glob("*.css"):
        assert 'data-theme="dusk"' not in path.read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", styles)


def test_generated_css_is_reproducible():
    assert THEME_TOKENS.read_text() == _CONTRAST.render_tokens()


def _declaration(css, selector, property_name):
    """Read the last ordinary rule for a focused component selector."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    values = [value.strip() for selectors, block in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
              if selector in [part.strip() for part in selectors.split(",")]
              for name, value in re.findall(r"([a-z-]+)\s*:\s*([^;]+);", block)
              if name == property_name]
    assert values, (selector, property_name)
    return values[-1]


def _token_value(palette, declaration):
    match = re.fullmatch(r"var\(--x-([a-z-]+)\)", declaration)
    assert match, f"Component color must follow its palette: {declaration}"
    return palette[match[1]]


def test_topbar_focus_and_citation_hover_follow_every_palette():
    layout = (STATIC / "workspace-layout.css").read_text()
    focus = _declaration(layout, ".topbar :focus-visible", "outline-color")
    citation_text = _declaration(layout, ".citation-ref:hover", "color")
    assert focus == "var(--x-focus-ring)"
    assert citation_text == "var(--x-accent-contrast)"
    for palette in parse_theme_tokens(THEME_TOKENS.read_text()).values():
        # Both topbar canvas and the raised picker can touch the focus ring.
        for surface in ("panel", "panel-raised"):
            assert _CONTRAST.cr(_token_value(palette, focus), palette[surface]) >= 3
        assert _CONTRAST.cr(_token_value(palette, citation_text), palette["accent"]) >= 4.5


def test_graph_text_links_and_keyboard_target_follow_every_palette():
    graph = (STATIC / "evidence-graph.css").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", graph)
    # A mode-only override would let the light graph retain an unrelated accent.
    assert "data-theme" not in graph
    prefix = ".evidence-graph-page "
    text = _declaration(graph, prefix + ".evidence-graph text", "fill")
    subtitle = _declaration(graph, prefix + ".evidence-graph .graph-subtitle", "fill")
    link = _declaration(graph, prefix + ".evidence-graph .graph-account-link", "fill")
    focus = _declaration(graph, prefix + ".evidence-graph a:focus rect", "stroke")
    assert link == focus == "var(--x-accent)"
    historical = _declaration(graph, prefix + ".graph-role-historical rect", "stroke")
    assert historical == "var(--x-warning)"
    for palette in parse_theme_tokens(THEME_TOKENS.read_text()).values():
        for node in (".graph-center rect", ".graph-record rect", ".graph-role rect", ".graph-role-historical rect"):
            surface = _token_value(palette, _declaration(graph, prefix + node, "fill"))
            for foreground in (text, subtitle, link):
                assert _CONTRAST.cr(_token_value(palette, foreground), surface) >= 4.5
            assert _CONTRAST.cr(_token_value(palette, focus), surface) >= 3
