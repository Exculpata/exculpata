"""WCAG AA contrast for the self-hosted Exculpata theme tokens."""
from __future__ import annotations

import importlib.util
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
    assert 'value="dark"' in base and 'value="light"' in base and 'value="dusk"' in base
    assert "var(--x-font-sans)" in styles and "var(--font-mono)" in styles
    # The previous gray fails 4.5:1 on the light canvas. Placeholders use muted text.
    assert "#8993a4" not in styles
    assert "textarea::placeholder,\n.matter-search-form input::placeholder {\n  color: var(--ink-faint);" in styles
    dependencies = (ROOT / "docs/DEPENDENCIES.md").read_text(encoding="utf-8")
    assert "Geist" in dependencies and "JetBrains Mono" in dependencies
    assert "SIL Open Font License" in dependencies
