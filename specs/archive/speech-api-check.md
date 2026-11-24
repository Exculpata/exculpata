# Check a speech recognition API from the installed application

An operator moving to a speech API needs to know whether the configured model
accepts a recording and returns timestamps that Exculpata could use for source
citations. The existing transcription service reports readiness for its own
bundled jobs. That signal says nothing about an operator's upstream API.
[API migration tracker](https://github.com/Exculpata/exculpata/issues/558)

## Approach

Give speech recognition a separate HTTPS origin, model and relay destination.
During a diagnostic, the app submits its packaged, synthesized recording to the
configured model using the multipart transcription API. Validate the segment
times and text. Report a short operator-facing result and discard the reply.
The input is fixed so the check can run on an empty installation without access
to a matter, upload or job. Service credentials belong to the app.

## Compatibility and verification

Exercise the same diagnostic an operator runs, including the installed fixture,
invalid replies, unavailable services and the unconfigured case. Check the
existing relay's destination policy and the limits applied to the new role.
Existing media jobs continue to use the private transcription service.

Connecting case recordings requires a distinct dispatch path with revocable
approval for the matter, capability, origin and model. That path also needs
source-version checks, interruption recovery and stable citations for reviewed
segments. Alignment, speaker labeling and translation each need their own
contract and authorization before the bundled services can be retired.
