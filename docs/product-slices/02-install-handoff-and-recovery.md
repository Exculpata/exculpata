# 02: Finish installation at a usable first login

Status: implemented on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#57](https://github.com/Exculpata/exculpata/pull/57) resumable
handoff). See [installation handoff](../FIRST_RUN.md). Depends on 01.
Target-host interrupted resume remains operator adoption evidence in that
guide, not a remaining product-code gap in this brief.

## Finding and outcome

Installation includes administrator initialization and launch acceptance, but
the adoption journey spans terminal work, certificates, model staging, and a
browser. The operator needs a clear handoff and a reliable way to continue
after interruption.

## Small implementation

Extend the existing staged installer rather than replacing it. Save
content-minimized phase receipts and distinguish prepared, running, login
reachable, and selected capabilities ready. Finish with the exact configured
browser URL, the selected authentication mode, the administrator username for
local mode, and any incomplete capabilities. Never display passwords or tokens.

On rerun, validate completed phases and present the next failed/incomplete step.
Do not recreate accounts, rotate secrets, restage unchanged models, or reset
storage merely because the process stopped. Explain local certificate trust in
the selected deployment context without instructing users to disable browser
security. Keep operator-managed LAN/TLS work explicit.

## Code and acceptance

Start with `scripts/recordbench_install.py::_configure`, `_provision`, launch
acceptance and doctor paths, `docs/INSTALL.md`, `docs/AUTHENTICATION.md`, and
installer tests. Share phase meanings with other launchers.

Interrupt after configuration, administrator creation, and model staging; resume
on a clean target host and prove source state and account identity remain intact.
Check a failed model service is visibly unavailable while successful CPU
capabilities remain described accurately. Verify a real browser login separately
from HTTP health. Version any new receipt format and define older-node recovery.
