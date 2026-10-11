import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from case_intelligence.branding import PRODUCT_NAME, PRODUCT_TAGLINE
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.static_assets import asset_url
from case_intelligence.workbench import create_workbench_app


TEMPLATES = Path(__file__).parents[1] / "src/case_intelligence/templates"


def test_exculpata_identity_preserves_health_contract_and_brands_login_and_mark(tmp_path) -> None:
    assert PRODUCT_NAME == "Exculpata"
    assert PRODUCT_TAGLINE == "Review the record. Build the work."

    with TestClient(
        create_workbench_app(
            tmp_path / "runtime",
            generator=UnavailableGenerator(),
            auth_mode="preview",
        )
    ) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["product"] == "RecordBench"

        login = client.get("/auth/login")
        assert login.status_code == 200
        assert f"Sign in · {PRODUCT_NAME}" in login.text
        assert PRODUCT_TAGLINE in login.text
        assert 'class="login-brand-mark"' in login.text
        assert login.text.count(asset_url("favicon.svg")) == 2
        for name in ("favicon.ico", "favicon-16.png", "favicon-32.png", "apple-touch-icon.png"):
            assert asset_url(name) in login.text
        assert "Choose a preview identity" in login.text
        assert "Temporary evaluation access" in login.text
        assert "Choosing a synthetic identity is not authentication" in login.text
        assert "loopback preview" not in login.text
        assert "Case Intelligence" not in login.text

        mark = client.get("/static/favicon.svg")
        assert mark.status_code == 200
        root = ElementTree.fromstring(mark.content)
        assert root.tag.endswith("svg")
        assert PRODUCT_NAME in mark.text
        assert "#0B0D10" in mark.text
        assert "#22D3EE" in mark.text
        assert "#071a3c" not in mark.text.lower()
        assert "#ed4b2f" not in mark.text.lower()

        icon = client.get("/favicon.ico")
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/")
        assert icon.content.startswith(b"\x00\x00\x01\x00")
        for name in ("favicon-16.png", "favicon-32.png", "apple-touch-icon.png"):
            image = client.get(f"/static/{name}")
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/png"

    for path in TEMPLATES.glob("workbench_*.html"):
        text = path.read_text(encoding="utf-8")
        if "{% block title %}" in text:
            title = text.split("{% block title %}", 1)[1].split("{% endblock %}", 1)[0]
            assert "product_name" in title, path.name

    workbench_base = (TEMPLATES / "workbench_base.html").read_text(encoding="utf-8")
    assert workbench_base.count("{{ asset_url('favicon.svg') }}") == 2
    for name in ("favicon.ico", "favicon-16.png", "favicon-32.png", "apple-touch-icon.png"):
        assert workbench_base.count(f"{{{{ asset_url('{name}') }}}}") == 1


def test_application_favicon_matches_brand_icon() -> None:
    root = Path(__file__).parents[1]
    brand = (root / "docs/assets/brand/exculpata-icon.svg").read_text(encoding="utf-8")
    favicon = (root / "src/case_intelligence/static/favicon.svg").read_text(encoding="utf-8")
    assert brand == favicon
    assert 'fill="#22D3EE"' in brand
    assert 'fill="#0B0D10"' in brand


def test_readme_hero_uses_exculpata_wordmark() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert "<picture>" in readme
    assert 'media="(prefers-color-scheme: dark)"' in readme
    assert 'media="(prefers-color-scheme: light)"' in readme
    assert "docs/assets/brand/wordmark-cyan-transparent-lighttext.png" in readme
    assert "docs/assets/brand/wordmark-cyan-transparent-darktext.png" in readme
    assert "docs/assets/brand/wordmark-cyan-dark.png" in readme
    assert "your discovery, cited." in readme
    assert "local-first by default." in readme
    assert "docs/assets/exculpata-banner.svg" not in readme


def test_export_branding_preserves_machine_readable_identifiers():
    from case_intelligence.work_product_exports import export_answer, export_full_review
    from tests.test_work_product_exports import STAMP, _records, _review_records

    matter, conversation, question, answer = _records()
    markdown = export_answer(matter, conversation, answer, question, "markdown", exported_at=STAMP)
    assert "Exported from Exculpata" in markdown.body.decode()
    assert "Exculpata answer" in markdown.body.decode()
    docx = export_answer(matter, conversation, answer, question, "docx", exported_at=STAMP)
    with zipfile.ZipFile(io.BytesIO(docx.body)) as archive:
        assert b"Exculpata" in archive.read("docProps/core.xml")
        assert b"Exculpata answer" in archive.read("word/document.xml")

    criterion, version, run, decision, metrics = _review_records(matter)
    args = (matter, criterion, version, run, (decision,), metrics)
    review = export_full_review(*args, "markdown", exported_at=STAMP)
    assert "Exculpata labels do not replace" in review.body.decode()
    payload = json.loads(export_full_review(*args, "json", exported_at=STAMP).body)
    assert payload["product"] == "RecordBench"
    assert payload["schema"] == "recordbench-source-check-v1"
    csv = export_full_review(*args, "csv", exported_at=STAMP).body.decode("utf-8-sig")
    assert "RecordBench label" in csv.splitlines()[0]
