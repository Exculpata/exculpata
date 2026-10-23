# Contributing

Help a small defense team drop in discovery and get cited understanding, without
choosing an engine, configuring anything, or letting material leave the building.
See the [product north star](docs/PRODUCT_NORTH_STAR.md).

## Start small

[GitHub issues](https://github.com/Exculpata/exculpata/issues) are the
work queue. Pick an unassigned
[`good first issue`](https://github.com/Exculpata/exculpata/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22),
read its scope, and comment that you would like to take it on. Ask for clarification
there if needed. Keep PRs small and focused; a draft PR is fine for early feedback.

## Set up and check

Clone your fork, create a branch, and configure your own public GitHub username
and no-reply Git email. Use Python 3.12 and the
[development prerequisites](docs/DEVELOPMENT.md#set-up-a-development-checkout).

```console
make bootstrap
.venv/bin/exculpata --help
.venv/bin/exculpata-workbench --help
make check-fast
```

`make bootstrap` installs development dependencies and the pre-push publication
hook. Run `make check-fast` before pushing; see
[local validation](docs/QUALITY_GATES.md#local-validation) for its PostgreSQL,
Docker Compose and secret-scanner prerequisites. No GPU or model download is needed.

Use the read-only [weekly velocity report](docs/VELOCITY_REPORT.md) to inspect
PR timing, review activity, CI duration and merge queue drops.

## Protect private material

Use synthetic data only. Never include real case material in code, fixtures,
issues, PRs, screenshots, logs, or attachments. Keep credentials and private
deployment details outside this repo. Run the publication check before pushing
and separately inspect anything you post through GitHub. Follow the
[publication requirements](docs/PUBLIC_ALPHA.md#prevent-disclosure-before-publishing).

Describe what changed and how you tested it. Behavioral changes need a synthetic
regression and relevant documentation. See the
[review policy](docs/QUALITY_GATES.md#review-policy),
[security model](docs/SECURITY_MODEL.md), [model rules](docs/MODELS.md), and
[backup and restore rules](docs/STORAGE_AND_BACKUP.md) for deeper guidance.

## License and CLA

Exculpata is licensed under [Apache-2.0](LICENSE). Outside contributors must
sign the [Contributor License Agreement](CLA.md) once before their first pull
request is merged. The CLA Assistant GitHub app will prompt you on your pull
request; signing takes a single click with your GitHub account. You keep your
copyright. The Exculpata name and logo are not covered by Apache-2.0; see
[TRADEMARKS.md](TRADEMARKS.md).
