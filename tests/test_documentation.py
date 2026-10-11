from __future__ import annotations

from pathlib import Path


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
    assert workflow.count("EXPECTED_INTEGRATION_SHA: ${{ github.sha }}") == 5
    assert workflow.count('test "$(git rev-parse HEAD)" = "$EXPECTED_INTEGRATION_SHA"') == 5
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
                   "draft PR", "synthetic data only", "Never include real case material"):
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
