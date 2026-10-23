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
source receipt must identify one reviewed diagnostic by its identifier and exact
source-line SHA-256 anchor, include a semantic reason and validation, and fail on
changed flagged lines or extra diagnostics while allowing line relocation;
it is never a file exclusion or automatic acceptance of unknown syntax. After
required validation, request one final review. Consolidate precision-only
suggestions into one follow-up issue; correctness, security and required
acceptance failures still block readiness. This rule does not waive publication
guards or change either analyzer's task-specific acceptance requirements.

## Bounded fail-closed checker contract

Hand-written checkers recognize specific input forms; they do not prove arbitrary
Python, JavaScript, shell, document or network behavior safe. For each supported
boundary, preserve a synthetic supported control and an unmodeled-input probe in
`tests/test_checker_fail_closed_contract.py`. Each adapter accepts the pytest
monkeypatch fixture and a boolean selecting the unsupported input, invokes the
real checker, and returns its diagnostics as strings. The shared test requires
an empty result for the control and an explicit unsupported diagnostic naming a
supported helper for the probe. Unexpected exceptions, an empty result for the
probe, and loss of the control all fail the test. Substitute external processes
only; do not mock the checker into returning the desired verdict.

This test currently covers two available interfaces:

- **Fixture writes:** `tests.fixture_write_policy.check_fixture_writes` parses
  Python source, checks exact AST node types/fields against `_AST_FIELDS`, then
  follows its bounded local provenance rules. The probe uses valid Python
  `except*` (`ast.TryStar`), absent from that reviewed grammar. Ordinary `try` /
  `except` around a direct `with fixture_write(workspace) as connection:` SQL
  write is the supported control. Use `tests.fixture_write.fixture_write` with
  explicit named workspace/connection handles; comments do not license unknown
  AST forms. This row does not prove local-class, reflective or delayed-callback
  resolution. The stricter fixture-specific tests, inventory and standards
  remain owned by #315; #333 is not expanded by this shared harness.
- **Publication media:** `scripts/publication-check.py` routes recognized media
  suffixes through `_scan_media()`. Its external input is successful ffprobe
  `format_tags:stream_tags` JSON object output, within the existing size limit.
  A JSON array is not a modeled metadata response and must fail, with the helper
  named in the diagnostic. Object output still goes through the complete byte
  scanner; this is a root-shape contract, not a nested metadata schema or a
  guarantee that every possible media format is inspectable. Use the existing
  `scan_tree()` / `scan_history()` entry points and `_scan_media()` inspection
  path; no digest, comment or extension-based escape is introduced. All existing
  raw-content, metadata, history and installed pre-push checks remain required.

Two integrations remain pending; they are not skipped tests or passing coverage:

- **Rename source policy (#316):** main's `legacy_name_check.py` scans literal
  text with compatibility exceptions; `doc_identifier_check.py` grounds selected
  documented identifiers using a bounded string evaluator. Neither substitutes
  for #316's `rename_contract_check.py` and `rename_javascript_policy.py`.
  Integrate their actual public checker interfaces after landing, with a
  registered `case_intelligence.legacy_names.name_value` control and a selected
  unsupported construction. The inspected pending Python policy models bounded
  immutable values and rejects unknown AST forms and dynamic execution; its
  helper-return and mutable-state restrictions must not be replaced by arbitrary
  class resolution. Reinspect the final grammar before selecting the probes.
- **Network source policy (#320):** main's
  `test_reference_network_boundary.py` checks literal Compose layout, not Python
  outbound-call provenance. The pending `scripts/network-contract.py` inventories
  recognized imports, aliases, client references and host literals. Its resolver
  returns an empty result for unmodeled expressions; it is explicitly a review
  inventory, not a Python sandbox. A blanket unsupported-AST rejection would be
  a materially different design. Define a bounded unsupported networking form
  and a supported `case_intelligence.service_endpoints.open_service_request`
  control with that owner before integrating an adapter. Runtime network
  isolation and egress tests remain separate requirements.

Keep #334 in draft until those integrations and their scope decisions are
resolved, required full validation is green and fresh review is complete. Do not
import unmerged branches, add blanket exemptions, weaken existing tests or claim
that this finite probe set establishes universal checker soundness.

## Cursor Cloud development environment

Python 3.12, ffmpeg, ImageMagick, poppler, English and Spanish Tesseract, the Docker CLI with Compose v2, and Gitleaks 8.30.1 are installed for contributor checks. PostgreSQL 17 with pgvector is installed as a disposable loopback database. The environment start command brings that cluster up; it is not left running by install.

Refresh the virtualenv with `python3.12 scripts/bootstrap-dev.py`. `make bootstrap` also runs `make hooks`, which fails while Cursor owns `core.hooksPath`. Before pushing, run `.venv/bin/python scripts/publication-check.py`. That scan uses `git rev-list --all`, so a full clone can report historical findings that are only reachable from other remote branches.

`make test-postgres` and `make check-fast` need `CASE_REVIEW_POSTGRES_TEST_DSN` set to the synthetic value in `.github/workflows/quality-gates.yml`. On this smaller VM also export `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0` and `CASE_INTELLIGENCE_TEST_MODE=1`, as that workflow does. `docker compose config` validates the Compose files without a running Docker engine.

For a loopback UI check:

```console
CASE_INTELLIGENCE_AUTH_MODE=preview .venv/bin/exculpata-workbench --host 127.0.0.1
```

Sign in with a synthetic preview identity and create a synthetic matter. Keep this mode on loopback.

## Synthetic fixture writes

Use `tests.fixture_write.fixture_write(workspace)` for raw writes to a shared
workspace connection. Manual media queue/claim/import/finish sequences belong in
one `fixture_write(workspace, media=True)` scope. Keep worker waits, HTTP calls and
projection outside that scope. The media scope serializes the public methods;
it does not turn their individual commits into one atomic transaction.

The fixture inventory checks tracked Python files under test directories,
the root `scripts` and `benchmarks` directories, and
`services/transcription/compatibility`. It scans these roots without executing
imports. Review imports and file-based loaders when adding support elsewhere;
include fixture support in the inventory before using it. Production modules
retain their own write boundaries and are not fixture setup merely because a test
imports them. A passing inventory is not proof about arbitrary Python reflection.
Stage new fixture sources before running the inventory. Use explicit named
handles and the supported syntax described in
[development guidance](docs/DEVELOPMENT.md#synthetic-fixture-writes).
Run `python -m pytest -q tests/test_fixture_write_policy.py tests/test_fixture_inventory.py tests/test_fixture_inventory_coverage.py tests/test_fixture_reflective_deny.py`.

Fixture code must not access `__call__` on a builtin, pass starred positional
arguments to builtin `getattr`, or use non-literal or callable keys with builtin
dictionary `fromkeys`. These forms fail closed with `fixture_write()` guidance.
Use direct builtin calls, explicit `getattr` arguments, literal keys, or an
explicit synchronous loop for dynamic data. The fixture-specific denial contract
is `tests/test_fixture_reflective_deny.py`; shared checker validation should run
that module rather than duplicate its cases.

Do not hide setup in `exec` or `eval`, including aliases and compiled payloads.
References to these builtins are rejected before they can enter opaque callable
arguments, defaults or containers; ordinary local callables remain supported.
Async, generator and lambda results must not carry locally defined class
constructors, instances or instance-derived attributes, including bound methods.
The checker rejects these results without resolving constructors or members.
Rewrite fixture setup with `tests.fixture_write.fixture_write` and explicit named handles,
retaining equivalent runtime assertions. Optional precision work is tracked in
[#333](https://github.com/Exculpata/exculpata/issues/333); strict restrictions remain
until a reviewed replacement preserves their denial tests and fixture gates.
Dynamic execution is a structural violation; `# fixture-write:` cannot waive it.
Negative regression probes retain actual runtime effects and check rejection of
the same source through `tests.fixture_probe` with fixed named inputs. Installer
and workflow probes use fixed repository-source entry points in
`tests.runtime_probes`, with no caller-provided executable source or namespace.
The inventory pins these two reviewed harnesses and suppresses only their dynamic
execution finding; all other fixture rules still apply. Changes to a harness
require review of its fixed inputs, retained runtime assertions and updated pin.
