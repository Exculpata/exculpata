#!/usr/bin/env python3
"""Check inline relative links in the two tester documents, offline."""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ("COMPATIBILITY.md", "docs/TRY_IT_ON_YOUR_HARDWARE.md")


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    result: set[str] = set()
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        result.add(f"{slug}-{count}" if count else slug)
        seen[slug] = count + 1
    return result


def check(root: Path = ROOT) -> list[str]:
    errors = []
    for document in DOCUMENTS:
        source = root / document
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{document}: unreadable document")
            continue
        # These bounded documents use inline links; code fences are not links.
        text = re.sub(r"^```.*?^```[^\n]*$", "", text, flags=re.M | re.S)
        for target in re.findall(r"\[[^\]\n]+\]\(([^)\s]+)\)", text):
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            path = (source.parent / unquote(url.path)).resolve() if url.path else source.resolve()
            if not path.is_relative_to(root.resolve()) or not path.exists():
                errors.append(f"{document}: missing or out-of-repository relative link")
            elif url.fragment and path.suffix == ".md":
                if unquote(url.fragment) not in anchors(path.read_text(encoding="utf-8")):
                    errors.append(f"{document}: missing Markdown anchor")
    return errors


if __name__ == "__main__":
    errors = check()
    for error in errors:
        print(error)
    if not errors:
        print("Tester documentation relative links: valid")
    raise SystemExit(bool(errors))
