# Retire model acquisition on fresh nodes

## Kickoff

Operators installing a new node should configure model APIs on their chosen
machines. A fresh install cannot stage or start the former review or speech
bundles. Operators resuming and updating existing nodes need their saved
configuration, source material, reviewer edits and model caches preserved.

Change the installer and its synthetic CLI tests, along with the install and
model configuration guides. Check fresh install, dry run and structured
preflight for each retired bundle. An invalid invocation must stop before
reading credentials or modifying the destination. Check that resume from a
real saved record still selects the recorded GPU services. Verify zero-endpoint
intake, OCR and search, and exercise ASR, alignment, translation and speaker
labels from a running app before landing this retirement.

A team that needs to reproduce the beta reference installation may use its
documented release. Keep the current recovery implementation for existing
installations. Preserve the original fallback on previously issued release
capsules. [API migration tracker](https://github.com/Exculpata/exculpata/issues/558)
