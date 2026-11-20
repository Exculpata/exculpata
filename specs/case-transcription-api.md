# Dispatch recordings to the operator's transcription API

## Kickoff

Reviewers can transcribe a recording through the configured speech service after
a matter owner or administrator approves sending the recording to that origin
and model. The application keeps the original, durable job, imported transcript
and timestamp citations. Existing installations can finish earlier private jobs.

The change covers the speech adapter, media coordinator, matter grant and media
job storage, setup disclosures, model guide and synthetic regressions. It extends
the [speech compatibility check](speech-api-check.md) and addresses the case
recording item in [issue 558](https://github.com/Exculpata/exculpata/issues/558).

Acceptance requires a real synthetic upload through the normal worker, an exact
recording and model at the permitted destination, and a timestamped transcript
in the matter. A missing or revoked grant, changed destination, changed source
or failed contract must prevent an unauthorized transfer or transcript import.
A retry after import must reuse the matter's reviewed work.

## Storage and compatibility

Keep the deployed private processor available for migration. A durable job
records which protocol accepted its dispatch so recovery can distinguish a
synchronous speech API request from a temporary private job. Retry verifies
the source and current authority again. A speech API may receive a repeated
request after interruption before import. Each source version can have only one
imported transcript.

Extend the stored capability constraint for the speech APIs while preserving
existing grants, including revocations and configuration identities. The setup
page offers a capability only when its adapter has a configured destination.
The installed doctor continues to read its packaged recording independently.
