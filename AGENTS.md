# RecordBench contributor guide

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
