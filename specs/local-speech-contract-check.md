# Verify speech APIs on a local synthetic installation

## Kickoff

An operator should be able to exercise all four speech API contracts from an
installed application, without model weights or access to a matter. The ASR
doctor already ships a synthetic WAV. Its response validation alone does not
exercise the other services, TLS trust, or the configured relay.

Add a bounded synthetic HTTPS provider in `tests/`, a disposable local setup
under `scripts/`, and an operator walkthrough under `docs/`. Use the
application's real diagnostics, production relay and reviewed private address
pins. Keep certificates temporary and exercise a failed connection as a control.

Acceptance requires four successful, independent diagnostic results on the
packaged fixture and a visible failure when the relay is unavailable. The
fixture should reject unrelated input, use fixed responses, and never retain
request bodies. The job and matter grant regressions separately exercise
case-data dispatch. [Migration tracker](https://github.com/Exculpata/exculpata/issues/558)
