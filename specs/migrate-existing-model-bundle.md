# Move a saved node from bundled models to APIs

## Kickoff

A team that already installed a review or transcription bundle should be able
to opt into endpoint-only operation in place. Keep its matter and runtime mounts,
source data, transcript revisions and installed credentials. A rerun or failed
cutover should leave an identifiable recovery path. Old model files remain in
place until the operator decides how to manage them.

Change the installer update path and operating guide, with synthetic regression
tests for the normal update command. Verify old bundle services stop, the
replacement omits them, the saved configuration selects no bundle on subsequent
resume, and updated source and reviewer records remain accessible. Exercise a
failed replacement and the existing rollback flow. Require unfinished private
media jobs to be resolved before disabling their service.

This migration follows qualification of the replacement APIs and is explicitly
selected for one node. [Migration tracker](https://github.com/Exculpata/exculpata/issues/558)
