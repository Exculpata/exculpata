# Speech enrichment APIs

The media worker can use three optional services before importing a machine transcript. Configure each HTTPS origin, model, allowed hosts, relay pins or remote confirmation, and optional credential file with its own environment prefix. In matter setup, the owner or administrator reviews the capability, origin and model and approves each destination separately. A synthetic contract check can run on an empty installation. Case dispatch also requires current matter access for the person who queued the job.

| Capability | Environment prefix | POST path | Case input |
| --- | --- | --- | --- |
| Word alignment | `CASE_INTELLIGENCE_ALIGNMENT_` | `/v1/audio/alignments` | Recording, source language, source text and time ranges |
| English translation | `CASE_INTELLIGENCE_TRANSLATION_` | `/v1/translations` | Source language, target language and segment text |
| Speaker labeling | `CASE_INTELLIGENCE_DIARIZATION_` | `/v1/audio/speaker-labels` | Recording and segment time ranges |

Each service returns one JSON object with a `segments` array. Return exactly one row per input segment, in request order, with the same opaque `id`. Durations and times are in seconds relative to the original recording. The application owns the link between each opaque ID and its source version. A role configured with a credential receives an HTTP Bearer token for that role.

## Alignment

Accept multipart form fields `model`, `language`, `segments` (a serialized JSON array) and `file`. The binary file is the original recording and its multipart filename is generic. Each segment has `id`, `text`, `start` and `end`. An example result for the source text “Prueba uno.” follows.

```json
{"segments":[{"id":"segment-1","words":[{"word":"Prueba","start":0.12,"end":0.5},{"word":"uno.","start":0.55,"end":1.2}]}]}
```

Within each segment, words must appear in source order, account for the original text including punctuation (ignoring whitespace), span at least one rounded millisecond and stay inside the original range without overlap. The first and last aligned word refine the segment citation boundaries. The application retains the original source text and segment identity. The application stores citations at segment precision. When the retained private processor already reports successful forced word alignment, the worker uses those segment boundaries and omits the external alignment request.

## Translation

Accept an `application/json` request. The application translates into English and skips transcripts already identified as English. `und` denotes an undetermined source language.

```json
{"model":"synthetic-translator","source_language":"es","target_language":"en","segments":[{"id":"segment-1","text":"Prueba uno."}]}
```

Return a nonempty translated string of at most 20,000 characters per segment. The application stores the translation alongside the source-language passage.

```json
{"segments":[{"id":"segment-1","text":"Test one."}]}
```

## Speaker labels

Accept multipart form fields `model`, `segments` (a serialized JSON array) and `file`. Each segment contains only `id`, `start` and `end`. Return anonymous cluster labels of the form `SPEAKER_` followed by one to five decimal digits. Use the same cluster for segments attributed to the same voice in this recording. Reviewers can listen and assign names in the matter workspace.

```json
{"segments":[{"id":"segment-1","speaker":"SPEAKER_01"}]}
```

## Partial results and retry

The checklist in matter setup controls which destinations may receive material. The worker checks approval again immediately before every external request. Each request's segment JSON and each JSON reply are limited to 8 MiB. A missing grant, invalid response, or unavailable configured service produces a bounded processing notice and leaves that capability's annotation unapplied. Other authorized services can finish, and the original transcript stays available for review. After import, retry recovers the stored transcript with its existing reviewer edits and citation IDs. A retained private transcription service also supports these enrichment APIs during migration.


## Installed contract checks

After saving settings and recreating the app and relay, run the installer
`doctor` command. It reports the saved model, HTTPS origin and data sent for
each configured capability, then checks the running app with synthetic input.
To repeat one check inside the app, use
`python -m case_intelligence.media_endpoint_doctor alignment`,
`translation` or `speaker_labeling` as the positional capability. The command
prints one JSON status. It reads the packaged diagnostic recording for the audio
checks and uses a fixed Spanish sentence for translation. The result describes
API compatibility. Approve case processing separately in matter setup.

Run the [local four-service contract check](LOCAL_SPEECH_APIS.md) to exercise
the installed diagnostics over HTTPS, including the behavior during a relay outage.
