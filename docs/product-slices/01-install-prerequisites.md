# 01: Make installation prerequisites actionable

Status: implemented on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#39](https://github.com/Exculpata/exculpata/pull/39) /
[#54](https://github.com/Exculpata/exculpata/pull/54)). See
[INSTALL.md](../INSTALL.md) preflight. No dependencies.

## Finding and outcome

`docs/INSTALL.md` asks the operator to choose a dedicated service account, storage,
authentication, TLS, models, and backup arrangements before installation. The
CLI exists, but that is still a demanding entry point. A new operator should
see what is missing, what it prevents, and the next concrete action.

## Small implementation

Give the existing preflight one shared structured result format: check name,
observed state, required capability, blocking/optional status, and a safe remedy.
Render a readable terminal checklist and a machine-readable result from that
same data. Explain CPU evaluation versus AI review in task language.

Cover supported host, Docker/Compose access, non-root ownership, free space,
storage writability, TLS choice, and selected model/GPU requirements. Detect an
unsupported host before creating state. Preserve loopback defaults and explicit
LAN configuration. Offer narrow prerequisite instructions; do not automatically
grant broad permissions or expose services to make checks pass.

## Code and acceptance

Start with `scripts/recordbench_install.py::_preflight`, the `install` entry
point, `docs/INSTALL.md`, and `tests/test_oss_installer.py`. Platform launchers
may consume the contract without changing its meaning.

Synthetic tests cover each missing prerequisite, optional GPU absence in CPU
mode, paths with spaces, and no-write diagnostic mode. Run a clean Linux CPU
installation attempt with a person following only the displayed instructions;
record where intervention remains necessary. This slice is prerequisite
guidance, not a claim of a supported one-click installer.

No persistent schema change. Read architecture, security, and installation
runbooks before changing deployment commands.
