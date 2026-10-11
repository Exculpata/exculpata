# Substep 20b: Linux CPU node for contributors (B-lite)

Status: **Done** in the binding [cruise](../EXIT_ALPHA_CRUISE.md); the
[timed clean-node journey passed](../LINUX_CPU_NODE_ACCEPTANCE_2026-09-19.md),
with all six Quality jobs green. Slice 20 remains Done. This is the node
companion to the code-only `make bootstrap/check` path, not another product feature.

## User outcome

A stranger starts with a fresh dedicated Ubuntu 24.04 x86-64 host, follows the
documented service-account and Docker setup, and uses the existing `./install`
with CPU / models `none`, loopback HTTPS and local accounts. `doctor` passes.
The installer-created first administrator signs in through a trusted browser,
creates one synthetic practice matter and opens an uploaded synthetic source
(or records an explicitly documented incomplete source step).

## Bounded change

- Put one ordered CPU happy path before the advanced INSTALL reference.
- Put first-admin sign-in and one practice matter before the wider FIRST_RUN
  team/adoption journey.
- Split README Quick start into running a node and contributing code. Keep
  CONTRIBUTING's code-only Make path and link the node path for UI work.
- List portable package capabilities; only Ubuntu 24.04 x86-64 is in acceptance.
- Change preflight/doctor remedies only for friction reproduced on the clean
  host, with a synthetic regression for any behavioral change.
- Include the approved clean-host asset fix: shared workspace CSS/JavaScript
  URLs retain the browser's HTTPS origin and port across the internal HTTP
  gateway hop. Proxy-header trust and authentication remain unchanged.

## Acceptance

Time the fresh-host route once without preloaded node dependencies or runtime
state. Retain a dated, content-free receipt under `docs/` identifying the public
OS image/checksum, resources, exact candidate, commands, timing, actual doctor
exit status and real browser outcome. No seeded preview administrator, lowered
storage reserve, disabled scanner or ignored certificate errors may stand in
for the normal installed product. Record failures and retries honestly.

Quality gates must pass on the delivered candidate. Mark the cruise Done only
with the successful stranger-path receipt and Quality evidence. Review all
published material and run the installed pre-push guard before upload.

## Holds

Use `./install`; do not create another installer or merge/rebase #87 wholesale.
GPU, transcription, OIDC/Kerberos, cross-distribution installation, Mac/#34,
visual/slice 21 and later-19 remain outside this substep. No schema, model or
storage change is selected. Follow the
[review policy](../QUALITY_GATES.md#review-policy).
