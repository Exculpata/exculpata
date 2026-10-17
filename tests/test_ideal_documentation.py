"""Keep the short review contract navigable without claiming semantic enforcement."""
from __future__ import annotations

import ast
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/IDEAL.md"
LINK = re.compile(r"\[[^\]]+\]\(([^\s)]+)\)")


def document() -> str:
    assert DOCUMENT.is_file(), "Add docs/IDEAL.md before linking the ideal contract."
    return DOCUMENT.read_text(encoding="utf-8")


def local_target(link: str) -> Path:
    url = urlsplit(link)
    assert not url.scheme and not url.netloc, f"Use a repository source link: {link}"
    target = (DOCUMENT.parent / unquote(url.path)).resolve()
    assert target.is_relative_to(ROOT), f"Link leaves the repository: {link}"
    assert target.is_file(), f"Missing IDEAL.md link target: {link}"
    return target


def test_ideal_remains_a_short_architecture_security_and_integrity_contract():
    text = document()
    assert 60 <= len(text.splitlines()) <= 100
    assert len(text.split()) <= 1100, "Keep the ideal contract concise."
    for source in ("ARCHITECTURE.md", "SECURITY_MODEL.md", "PRODUCT_NORTH_STAR.md"):
        assert source in LINK.findall(text), f"Link the authoritative {source}."
    for section in ("Architecture boundaries", "Local first", "Evidence integrity"):
        assert section in text
    assert "maintainer review and approval before merge" in text
    assert "QUALITY_GATES.md#review-policy" in LINK.findall(text)
    assert "PUBLIC_ALPHA.md#prevent-disclosure-before-publishing" in LINK.findall(text)


def test_every_ideal_rule_has_real_checks_or_justified_review_only_status():
    text = document()
    rules = re.findall(r"^\*\*([ALE]\d+) — ", text, re.MULTILINE)
    rows = re.findall(r"^\| ([ALE]\d+) \| (.+) \|$", text, re.MULTILINE)
    assert rules and {rule[0] for rule in rules} == {"A", "L", "E"}
    assert len(rules) == len(set(rules)), "Rule identifiers must be unique."
    assert sorted(rule for rule, _ in rows) == sorted(rules), "Pair each rule with one enforcer row."
    for rule, enforcement in rows:
        checks = [local_target(link) for link in LINK.findall(enforcement)
                  if urlsplit(link).path.endswith(".py")]
        review_only = "review only" in enforcement.lower()
        assert checks or review_only, f"{rule} needs an executable check or review-only reason."
        if review_only:
            assert re.search(r"review only(?:: .+| because .+)", enforcement, re.IGNORECASE), rule
        for check in checks:
            assert check.name.startswith("test_"), f"Link an executable test, not just code: {check}"
            tree = ast.parse(check.read_text(encoding="utf-8"))
            assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name.startswith("test_") for node in ast.walk(tree)), check


def test_ideal_links_resolve_including_markdown_fragments():
    links = LINK.findall(document())
    assert links, "The ideal contract must link its sources and enforcers."
    for link in links:
        target = local_target(link)
        fragment = unquote(urlsplit(link).fragment)
        if fragment:
            assert target.suffix == ".md", f"Use stable Markdown headings: {link}"
            headings = re.findall(r"^#{1,6} (.+)$", target.read_text(encoding="utf-8"), re.MULTILINE)
            slugs = {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                     for heading in headings}
            assert fragment in slugs, f"Missing IDEAL.md heading target: {link}"


@pytest.mark.parametrize("broken", ["missing-ideal-source.md", "../../outside-ideal.md"])
def test_ideal_link_validation_rejects_missing_or_escaping_targets(broken):
    with pytest.raises(AssertionError):
        local_target(broken)
