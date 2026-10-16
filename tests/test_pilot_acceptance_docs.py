"""Pilot-acceptance links resolve, and deletion docs follow the code."""

from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
CONTROLS = (
    "## Matter isolation",
    "## Identity and authorization",
    "## Runtime network egress",
    "## Hostile inputs and parser limits",
    "## Prompt injection from discovery text",
    "## Audit trail",
    "## Backup and restore integrity",
    "## Retention and deletion",
    "## Coverage limits",
    "### Email attachments",
    "### Entity recall",
    "### 6,000-character synthesis cap",
    "## Failure paths",
    "## Phased pilot plan",
)


def _targets(document: Path) -> list[Path]:
    found = []
    for match in LINK.finditer(document.read_text(encoding="utf-8")):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            raise AssertionError(f"external link in {document.name}: {target}")
        path = target.split("#", 1)[0].strip()
        if not path:
            continue
        found.append((document.parent / path).resolve())
    return found


def test_pilot_acceptance_covers_every_control_and_links_resolve() -> None:
    document = ROOT / "docs" / "PILOT_ACCEPTANCE.md"
    text = document.read_text(encoding="utf-8")
    assert "not a security certification" in text
    assert "ABA Model Rule 7.1" in text
    assert "No evidence yet" in text
    for heading in CONTROLS:
        assert heading in text
    missing = [str(path.relative_to(ROOT)) for path in _targets(document) if not path.exists()]
    assert missing == []


def test_readme_matches_explicit_deletion_and_links_the_pilot_guide() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/PILOT_ACCEPTANCE.md" in readme
    assert "scheduled expiry can" not in readme.casefold()
    assert "permanently remove that workspace" not in readme.casefold()
    assert "does not remove a workspace" in readme
    assert "Dates never authorize deletion." in readme
    assert "automatic purge is disabled" in readme
    assert "CASE_INTELLIGENCE_GENERATOR_BACKEND" in readme
    assert "Open WebUI" in readme
    guide = ROOT / "docs" / "PILOT_ACCEPTANCE.md"
    assert guide.is_file()


def test_deletion_docs_do_not_describe_current_automatic_purge() -> None:
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    layout = (ROOT / "docs" / "WORKSPACE_LAYOUT.md").read_text(encoding="utf-8")
    recovery = (ROOT / "docs" / "EXPIRED_MATTER_RECOVERY.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs" / "REVIEW_CORRECTION_PLAN.md").read_text(encoding="utf-8")
    assert "maintenance does not purge a matter" in architecture
    assert "deletion is due" not in layout
    assert "does not start a grace period" in layout
    assert "purge_due_matters" in recovery
    assert "does not purge them on maintenance" in plan
    assert "The current application does not." in plan
