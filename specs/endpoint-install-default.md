# Install an endpoint-based node without choosing bundled models

## Kickoff

An operator running the ordinary installer should be able to configure an answer
endpoint or continue directly to intake, review, local OCR, and word search.
These workflows use no bundled models or model staging. Asking the operator to
choose a GPU tier and trying to contact a bundled retrieval worker that was never
started make the installed capabilities misleading.

Change the installer prompts, generated Compose settings and doctor, the
installation and model guides, and synthetic regressions. Verify that fresh
interactive and unattended nodes using the default start without model services,
stage no models and have no suggested hardware tier. Check the installed Compose
configuration with and without a saved worker setting. Exercise resume with an
existing model selection and an explicit legacy GPU profile.

The default installation should work on a host with no GPU. Keep explicit bundle
profiles while qualifying their replacements. Saved installations still need
their private worker and GPU configuration for recovery. Removing those services
also requires verifying the retrieval index migration and the media API contracts.
[API migration tracker](https://github.com/Exculpata/exculpata/issues/558)
