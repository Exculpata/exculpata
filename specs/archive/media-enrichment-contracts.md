# Word timing, English translation and anonymous speaker labels

## Kickoff

When teams grant an operator's speech service access to a matter, the media worker can refine segment citations with aligned words, translate non-English passages, and group segments by speaker. Each service receives only the input for its own job. Source recordings, source text, timestamps and reviewer decisions remain under application control. [Migration tracker](https://github.com/Exculpata/exculpata/issues/558)

Expected changes include the media coordinator, API adapters and response checks, matter permission registry and setup labels, protocol documentation and synthetic tests. The installed relay configuration and synthetic fixture server are coordinated in related changes.

Acceptance requires a separate current grant for every dispatch, matching the current matter, capability, destination and model. Synthetic contracts exercise successful responses, cross-matter denial, revocation, failed services, altered segment identities and out-of-range times. A partially available installation preserves the original transcript. Replaying an imported transcript uses its stored segments and edits.

## Problem and approach

The managed transcription API supplies timed segments. Separate services can refine their timing or provide translations and speaker clusters. Reusing one broad recording permission would allow unrelated destinations to receive evidence. Regenerating segment IDs or replacing the source text would undermine citation and review continuity.

Check each optional service immediately before sending a request through its configured HTTPS relay. Send opaque segment IDs; keep matter, document and operator identifiers local. Validate the complete reply before applying any change. Translate only non-English passages, in segment order. Derive anonymous speaker clusters without assigning real-world identities. Tighten segment boundaries using ordered word spans that account for the source text. Preserve the original segments on an invalid response or missing approval and record the capability status.

## Risks and proof

Bound response sizes, preserve the app's original segment ordering and prevent an alignment from expanding or moving citations to a different passage. Verify the recording's source identity before audio leaves the app. Use only a packaged synthetic recording and generated segment text for contract diagnostics. Exercise disabled, denied and rejected services independently and verify that each service receives only its documented fields.
