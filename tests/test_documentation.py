from __future__ import annotations

import ast
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).parents[1]
REQUIRED = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/INSTALL.md",
    "docs/ARCHITECTURE.md",
    "docs/AUTHENTICATION.md",
    "docs/CONFIGURATION.md",
    "docs/MODELS.md",
    "docs/OCR_AND_MEDIA.md",
    "docs/STORAGE_AND_BACKUP.md",
    "docs/SECURITY_MODEL.md",
    "docs/PUBLICATION_CHECKLIST.md",
    "services/transcription/README.md",
)


def test_required_operator_and_contributor_documents_exist() -> None:
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    assert missing == []


def test_readme_sets_an_honest_prerelease_boundary() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for marker in (
        "prerelease",
        "./install",
        "Local accounts",
        "OIDC",
        "Kerberos",
        "WhisperX",
        "ClamAV",
        "multi-stage retrieval",
        "No case data",
    ):
        assert marker.casefold() in text.casefold()
    assert "production ready" not in text.casefold()


def test_security_and_publication_docs_keep_release_gates_visible() -> None:
    security = (ROOT / "docs/SECURITY_MODEL.md").read_text(encoding="utf-8")
    publication = (ROOT / "docs/PUBLICATION_CHECKLIST.md").read_text(
        encoding="utf-8"
    )
    combined = security + "\n" + publication
    for marker in (
        "synthetic",
        "secret",
        "history",
        "private key",
        "license",
        "restore",
        "loopback",
    ):
        assert marker.casefold() in combined.casefold()


def test_storage_docs_do_not_present_backup_without_restore_as_success() -> None:
    text = (ROOT / "docs/STORAGE_AND_BACKUP.md").read_text(encoding="utf-8")
    assert "restore drill" in text.casefold()
    assert "never declare backup ready" in text.casefold()
    assert "model cache" in text.casefold()
    assert "excluded" in text.casefold()


def test_transcription_is_bundled_but_not_exposed_without_authentication() -> None:
    text = (ROOT / "services/transcription/README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    combined = text + "\n" + architecture
    assert "bundled" in combined.casefold()
    assert "not published" in combined.casefold()
    assert "token" in text.casefold()


def test_pull_request_jobs_test_the_merge_and_scan_the_exact_head_separately() -> None:
    workflow = (ROOT / ".github/workflows/quality-gates.yml").read_text(
        encoding="utf-8"
    )
    exact_ref = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
    assert workflow.count(exact_ref) == 1
    publication_job = workflow.split("\n  publication-scan:\n", 1)[1].split("\n  secret-scan:\n", 1)[0]
    application_job = workflow.split("\n  application:\n", 1)[1].split("\n  postgres-integration:\n", 1)[0]
    assert exact_ref in publication_job
    assert exact_ref not in application_job
    assert "needs: [change-scope]\n" in application_job
    deployment_job = workflow.split("\n  deployment-contract:\n", 1)[1].split("\n  synthetic-browser:\n", 1)[0]
    assert "needs: [change-scope, publication-scan]" in deployment_job
    assert 'test "$SCOPE_RESULT" = success && test "$PUBLICATION_RESULT" = success' in deployment_job
    assert "EXPECTED_PUBLICATION_SHA:" in publication_job
    assert "python scripts/check-publication-candidate.py" in publication_job
    assert "python -m pytest -q tests/test_documentation.py tests/test_quality_change_scope.py" in application_job
    assert workflow.count("EXPECTED_INTEGRATION_SHA: ${{ github.sha }}") == 6
    assert workflow.count('test "$(git rev-parse HEAD)" = "$EXPECTED_INTEGRATION_SHA"') == 6
    defense_eval_job = workflow.split("\n  defense-eval:\n", 1)[1].split("\n  transcription:\n", 1)[0]
    assert exact_ref not in defense_eval_job
    assert "EXPECTED_INTEGRATION_SHA: ${{ github.sha }}" in defense_eval_job
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_INTEGRATION_SHA"' in defense_eval_job
    assert "python scripts/check-publication-candidate.py" in workflow
    assert '--expected-head "$EXPECTED_PUBLICATION_SHA"' in workflow
    assert '--publication-ref "$PUBLICATION_REF"' in workflow
    assert "pull_request_target" not in workflow
    assert "fetch-depth: 0" in workflow


def test_review_policy_is_central_and_hosted_review_is_optional() -> None:
    policy = (ROOT / "docs/QUALITY_GATES.md").read_text()
    for marker in (
        "6 required CI checks",
        "conversations are resolved",
        "a maintainer performs the merge",
        "Hosted Codex review is **OPTIONAL**",
        "request it once on the final head",
        "authentication/authorization",
        "schema or\nmigrations",
        "exports",
        "model pinning",
        "publication or secret tooling",
        "`.github/workflows`",
        "`needs-hosted-review`",
        "no acceptance-comment ritual and no exact-SHA reconciliation",
    ):
        assert marker in policy
    for path in ("AGENTS.md", "CONTRIBUTING.md", "docs/PUBLIC_ALPHA.md"):
        text = (ROOT / path).read_text()
        assert "QUALITY_GATES.md#review-policy" in text
        assert "maintainer acceptance" not in text
        assert "code review is mandatory" not in text


def test_contributor_entry_points_stay_short_and_public_safe() -> None:
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    assert len(contributing.split()) <= 500
    for marker in ("make bootstrap", "make check-fast", "good first issue",
                   "draft PR", "synthetic data only", "Never include real case material",
                   "docs/VELOCITY_REPORT.md"):
        assert marker in contributing
    template = (ROOT / ".github/pull_request_template.md").read_text()
    assert len(template.splitlines()) <= 15
    assert "synthetic data only" in template
    assert "Needs hosted review?" in template
    assert "QUALITY_GATES.md#review-policy" in template
    cruise = (ROOT / "docs/EXIT_ALPHA_CRUISE.md").read_text()
    assert "> Retired:" in cruise
    assert "GitHub issues" in cruise
    assert "milestones" in cruise


def test_workflows_do_not_request_codex_reviews_automatically() -> None:
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        assert "@codex" not in path.read_text()


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
