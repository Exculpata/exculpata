# Coding standards

Build toward a small defense team dropping in discovery and getting cited
understanding without choosing an engine, configuring anything, or letting
material leave the building. Read the [product north star](docs/PRODUCT_NORTH_STAR.md).
GitHub issues and milestones are the work queue. Verify the checkout, branch and
upstream PR state before acting; respect the selected scope and existing holds.

Treat this repository and every GitHub interaction as public. Before any push,
run the installed pre-push publication check; never bypass it. Separately inspect
PR text, comments, attachments and release assets, including metadata. Hosted CI
cannot prevent the initial disclosure. Follow [publication rules](docs/PUBLIC_ALPHA.md)
and the single [review policy](docs/QUALITY_GATES.md#review-policy).

Use synthetic fixtures only. Never add real case material, private identities,
hostnames, addresses, credentials, certificates, database files, runtime state,
or deployment overlays. Reproduce downstream defects with content-free descriptions
and synthetic inputs; never copy private history, logs or configuration.

Model changes require an exact revision, license record, local/offline readiness
test and representative evaluation. Schema or storage changes require backup
and a clean restore drill with evidence. Before changing deployment behavior,
read [architecture](docs/ARCHITECTURE.md), [security](docs/SECURITY_MODEL.md) and the
relevant runbook; keep services private and preserve the trusted-proxy boundary.

Keep changes focused. Behavioral changes need a synthetic regression, relevant
docs and a clear user outcome. Run applicable validation and the publication
sanitizer before committing. Keep deployment overlays external and release tags
immutable; corrections get new commits and releases. See [Contributing](CONTRIBUTING.md)
and [development guidance](docs/DEVELOPMENT.md).

## Restricted static analyzers

The rename-compatibility and import-boundary analyzers enforce documented syntax
contracts. Unmodeled syntax must produce a blocking diagnostic naming the
supported helper or boundary API; never treat an unknown node as safe or keep
adding per-construct inference to accommodate a caller. Rename callers use
`case_intelligence.legacy_names.name_value` and the registered name contract.
Migrate callers to supported forms while preserving behavior. Each analyzer needs
a synthetic regression for a reported failure and a meta-test showing an unknown
AST node fails closed, including when no known origin has been detected.

Keep literal checks, ambiguity and resource-limit failures active. Any permitted
source receipt must identify one reviewed diagnostic in exact source, include a
semantic reason and validation, and fail on changed source or extra diagnostics;
it is never a file exclusion or automatic acceptance of unknown syntax. After
required validation, request one final review. Consolidate precision-only
suggestions into one follow-up issue; correctness, security and required
acceptance failures still block readiness. This rule does not waive publication
guards or change either analyzer's task-specific acceptance requirements.

## Cursor Cloud development environment

Python 3.12, ffmpeg, ImageMagick, poppler, English and Spanish Tesseract, the Docker CLI with Compose v2, and Gitleaks 8.30.1 are installed for contributor checks. PostgreSQL 17 with pgvector is installed as a disposable loopback database. The environment start command brings that cluster up; it is not left running by install.

Refresh the virtualenv with `python3.12 scripts/bootstrap-dev.py`. `make bootstrap` also runs `make hooks`, which fails while Cursor owns `core.hooksPath`. Before pushing, run `.venv/bin/python scripts/publication-check.py`. That scan uses `git rev-list --all`, so a full clone can report historical findings that are only reachable from other remote branches.

`make test-postgres` and `make check-fast` need `CASE_REVIEW_POSTGRES_TEST_DSN` set to the synthetic value in `.github/workflows/quality-gates.yml`. On this smaller VM also export `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0` and `CASE_INTELLIGENCE_TEST_MODE=1`, as that workflow does. `docker compose config` validates the Compose files without a running Docker engine.

For a loopback UI check:

```console
CASE_INTELLIGENCE_AUTH_MODE=preview .venv/bin/exculpata-workbench --host 127.0.0.1
```

Sign in with a synthetic preview identity and create a synthetic matter. Keep this mode on loopback.
