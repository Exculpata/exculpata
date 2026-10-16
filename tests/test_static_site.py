"""Contracts for the independently hosted, resource-local landing page."""
from html.parser import HTMLParser
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.feed((SITE / "index.html").read_text())

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def local_file(reference, parent=SITE):
    parsed = urlsplit(reference)
    assert not parsed.scheme and not parsed.netloc, reference
    path = (parent / parsed.path).resolve()
    assert path.is_relative_to(SITE), reference
    assert path.is_file(), reference
    return path


def test_page_resources_are_local_and_passive():
    for tag, attrs in Page().elements:
        assert tag not in {"script", "iframe", "object", "embed", "form", "base"}
        assert not any(key.startswith("on") for key in attrs)
        assert "style" not in attrs and "srcset" not in attrs
        if "src" in attrs:
            local_file(attrs["src"])
        if tag == "link":
            assert attrs["rel"] in {"stylesheet", "icon"}
            local_file(attrs["href"])
    css = (SITE / "styles.css").read_text()
    assert "@import" not in css
    resources = re.findall(r'url\([\"\']?([^\"\')]+)', css)
    assert resources
    for reference in resources:
        local_file(reference)
    for svg in (SITE / "assets").glob("*.svg"):
        for element in ET.parse(svg).iter():
            assert element.tag.rsplit("}", 1)[-1] not in {"script", "image", "use", "foreignObject"}
            assert not any(key.rsplit("}", 1)[-1].startswith("on") for key in element.attrib)


def test_navigation_targets_and_accessible_structure():
    elements = Page().elements
    ids = [attrs["id"] for _, attrs in elements if "id" in attrs]
    assert len(ids) == len(set(ids))
    assert sum(tag == "h1" for tag, _ in elements) == 1
    assert sum(tag == "main" for tag, _ in elements) == 1
    assert ("html", {"lang": "en"}) in elements
    external_targets = set()
    for tag, attrs in elements:
        if tag == "img":
            assert attrs.get("alt")
            assert int(attrs["width"]) > 0 and int(attrs["height"]) > 0
        if tag != "a":
            continue
        href = attrs["href"]
        assert "target" not in attrs  # Ordinary navigation, no surprise new windows.
        if href.startswith("#"):
            assert href[1:] in ids
        else:
            external_targets.add(href)
    assert external_targets == {
        "https://github.com/Exculpata/exculpata",
        "https://github.com/Exculpata/exculpata#documentation",
        "https://github.com/Exculpata/exculpata/blob/main/CONTRIBUTING.md",
        "https://github.com/Exculpata/exculpata/blob/main/LICENSE",
    }
    assert any(tag == "a" and attrs.get("class") == "skip-link" and attrs["href"] == "#main"
               for tag, attrs in elements)
    assert any(tag == "main" and attrs.get("tabindex") == "-1" for tag, attrs in elements)


def test_original_brand_assets_and_font_licenses_are_preserved():
    sources = {
        "assets/exculpata-wordmark.svg": "docs/assets/brand/exculpata-wordmark.svg",
        "assets/exculpata-icon.svg": "docs/assets/brand/exculpata-icon.svg",
        **{f"assets/fonts/{name}": f"src/case_intelligence/static/fonts/{name}" for name in (
            "Geist-Variable.woff2", "JetBrainsMono-Variable.woff2",
            "OFL-Geist.txt", "OFL-JetBrainsMono.txt",
        )},
    }
    for output, source in sources.items():
        assert (SITE / output).read_bytes() == (ROOT / source).read_bytes(), output
    media = {
        "assets/exculpata-launch-16x9.mp4": "docs/assets/exculpata-launch-16x9.mp4",
        "assets/exculpata-launch-9x16.mp4": "docs/assets/exculpata-launch-9x16.mp4",
    }
    posters = {
        "assets/exculpata-launch-16x9-poster.jpg",
        "assets/exculpata-launch-9x16-poster.jpg",
    }
    for output, source in media.items():
        assert (SITE / output).read_bytes() == (ROOT / source).read_bytes(), output
    expected = {
        "index.html", "styles.css", "_headers", ".assetsignore", "README.md",
        *sources, *media, *posters,
    }
    assert {str(path.relative_to(SITE)) for path in SITE.rglob("*") if path.is_file()} == expected


def test_launch_video_is_self_hosted_without_autoplay():
    videos = [attrs for tag, attrs in Page().elements if tag == "video"]
    expected = [
        ("./assets/exculpata-launch-16x9.mp4", "./assets/exculpata-launch-16x9-poster.jpg", 1920, 1080),
        ("./assets/exculpata-launch-9x16.mp4", "./assets/exculpata-launch-9x16-poster.jpg", 1080, 1920),
    ]
    assert len(videos) == len(expected)
    for attrs, (src, poster, width, height) in zip(videos, expected):
        assert attrs["src"] == src and attrs["poster"] == poster
        assert attrs["preload"] == "metadata"
        assert "controls" in attrs and "playsinline" in attrs and "autoplay" not in attrs
        assert int(attrs["width"]) == width and int(attrs["height"]) == height
        local_file(src)
        poster_file = local_file(poster)
        data = poster_file.read_bytes()
        assert data.startswith(b"\xff\xd8\xff") and len(data) < 400_000
    css = (SITE / "styles.css").read_text()
    assert ".launch-narrow { display: none; }" in css
    assert ".launch-wide { display: none; }" in css
    assert ".launch-narrow { display: block; }" in css
    assert "autoplay" not in (SITE / "index.html").read_text()


def test_static_policy_blocks_scripts_and_connections():
    policy = next(attrs["content"] for tag, attrs in Page().elements
                  if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
    directives = dict(part.strip().split(" ", 1) for part in policy.split(";") if part.strip())
    assert directives == {
        "default-src": "'none'", "style-src": "'self'", "img-src": "'self'",
        "font-src": "'self'", "media-src": "'self'", "base-uri": "'none'",
        "form-action": "'none'",
    }
    headers = (SITE / "_headers").read_text()
    assert headers.startswith("/*\n")
    assert f"Content-Security-Policy: {policy}; frame-ancestors 'none'" in headers
    assert "Referrer-Policy: no-referrer" in headers
    assert "X-Content-Type-Options: nosniff" in headers
    rules = {}
    path = None
    for line in headers.splitlines():
        if not line.strip():
            continue
        if line.startswith("  "):
            name, value = line.split(":", 1)
            if name.strip() == "Content-Type":
                rules[path] = value.strip()
        else:
            path = line.strip()
    assert rules == {
        "/*.html": "text/html; charset=utf-8",
        "/*.css": "text/css; charset=utf-8",
        "/assets/*.mp4": "video/mp4",
        "/assets/*.jpg": "image/jpeg",
        "/assets/*.svg": "image/svg+xml",
        "/assets/fonts/*.woff2": "font/woff2",
        "/assets/fonts/*.txt": "text/plain; charset=utf-8",
    }
    assert (SITE / ".assetsignore").read_text().split() == ["README.md"]


def test_workers_static_assets_config_has_no_account_binding():
    config = tomllib.loads((ROOT / "wrangler.toml").read_text())
    assert config["name"] == "exculpata-site"
    assert config["main"] == "site-worker/range.js"
    assert config["compatibility_date"] == "2026-10-16"
    assert config["assets"] == {
        "directory": "./site",
        "binding": "ASSETS",
        "run_worker_first": ["/assets/*.mp4"],
    }
    assert "account_id" not in config
    published = (ROOT / "wrangler.toml").read_text() + (ROOT / "site-worker" / "range.js").read_text()
    assert "workers.dev" not in published and "account_id" not in published
    assert "env.ASSETS.fetch" in (ROOT / "site-worker" / "range.js").read_text()
    deploy = (SITE / "README.md").read_text()
    assert "npx wrangler versions upload --preview-alias <name>" in deploy
    assert "npx wrangler versions deploy" in deploy
