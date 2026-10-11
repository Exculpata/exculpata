# Recording checks before transcription

Current contract, September 5, 2026. This is the source of truth for the narrow
recording-check workflow. It adds no transcription model, translation mode,
visual analysis, or recording-review report builder.

After upload admission and any configured scan, the durable media worker checks
the recording before first submission to the transcription processor. A likely-speech
result proceeds to transcription. Other results retain the recording and stop
automatic submission:

| Result | Staff action and behavior |
| --- | --- |
| Video without audio | Playback only; no transcription is offered or submitted. Browser conversion can prepare a video-only compatibility copy. |
| No speech detected | Listen, then explicitly transcribe or check again. This is not a finding that nobody spoke. |
| Uncertain | Listen, explicitly transcribe, or retry the check. Includes unavailable capability, partial coverage and bounded quality concerns. |
| Check failed | Retry inspection. A failed inspection cannot be used to authorize transcription. |
| Likely speech found | Continue automatic transcription of the complete original recording. The saved check remains visible beside the transcript. |

Media without searchable text remains excluded from search/answers and complete
review's eligible text population. Readiness explains playback/review availability
without calling a video without audio a failed import. Existing review coverage
must still disclose excluded sources. Playback, source removal and owner closure
remain independent of a held transcription decision.

## Detector and bounds

`media_preflight.py` uses offline FFmpeg decoding and pinned
`webrtcvad-wheels==2.0.14`, mode 3, 16 kHz mono signed 16-bit PCM, 30 ms frames.
A later wheel such as 2.0.14.post1 reports a different `__version__` and is refused;
the speech check stays uncertain until the pin and policy revision change together.
The [upstream API](https://github.com/roman-nekrasov/py-webrtcvad-wheels)
classifies voiced/unvoiced frames; it does not establish language, intelligibility,
speaker identity or evidentiary truth. The component's MIT/WebRTC BSD notices
remain in the dependency distribution; see `THIRD_PARTY_NOTICES.md`.

The policy revision is `webrtcvad-2.0.14-mode3-30ms-v1`. Change it when detector,
threshold, decoding or decision semantics change. No model weights or runtime
network access are needed. Missing or mismatched detector capability produces an
uncertain result; it does not silently fall through to the transcription service.

- Decode at most the first 600 seconds, with a 60-second decoding/analysis wall
  limit, bounded frame buffering, and one decoder thread. The existing technical
  probe has its separate 30-second timeout. Longer/incomplete checks always
  remain uncertain, even if the inspected prefix contains speech or silence.
  Only the first audio track is inspected; multiple-track recordings remain
  uncertain with an explicit incomplete-coverage message.
- First/last likely-speech offsets require at least ten consecutive voiced
  frames (300 ms). Automatic readiness additionally needs at least twenty voiced
  frames (600 ms), complete coverage, and no measured quality flags.
- Quiet means frame RMS below 33 PCM units (approximately -60 dBFS). Quiet totals
  and leading/trailing quiet durations describe audio level, not absence of
  communication. Whole-record RMS below 328 with nonzero energy flags low volume;
  more than 1% of decoded samples at absolute value 32700 or above flags clipping.
  Low-volume audio stays uncertain rather than being labeled no speech.
- Language is always **not assessed**. This includes multilingual recordings;
  no language or translation decision is inferred from VAD or a filename.

FFmpeg resampling retains the original recording timeline, including leading
gaps; offsets are not relative to a trimmed speech-only file. The source bytes
and transcription input are unchanged. Findings are frame-level estimates, not
transcript citations. Browser seek uses the saved original-time millisecond
offset; later transcript citations retain the processor's original-time contract.

## Durable state, authorization and recovery

The inspection lives in the existing media job's `quality_json.preflight`, bound
by that job's matter, document, source version, SHA-256 and byte size. It includes
policy revision, result/coverage/quality, offsets, a fresh inspection identity and
an explicit-continue flag. Source digest is verified before inspection/reuse and
after inspection; a changed source cannot reuse a decision, even when byte size
and filesystem modification time were preserved.

A held job uses the existing **cancelled** queue state to mean no transcription
is scheduled. The staff projection distinguishes `needs_review` and
`playback_only`; it never reports a transcript success for an inspection. The
source projection uses matching non-searchable states. No SQLite schema change
or new independent processing queue is introduced. Held checks occupy no worker
and do not prevent quiet-state backup or deliberate matter closure.

`POST /matters/{slug}/sources/{token}/recording-check` requires ordinary matter
authorization and CSRF, plus `action=continue|retry` and the current inspection
identity. A conditional transition admits one decision, so duplicate/stale
requests return an understandable 409. The inspection identity is a concurrency
token, not an authorization grant. Stale, missing or inapplicable decisions are
rejected before digest reads. The complete source-locked verification/transition
runs with a separate two-worker capacity budget. Duplicate decisions for a matter
are rejected before worker admission; excess concurrent matters receive a clear
503/Retry-After response without queued worker work. Ordinary staff requests keep
their own worker capacity. Membership and inspection state are checked again at
the atomic transition without holding the shared database lock during hashing.
The job admission and content-free success audit commit in one control-database
transaction. Audit failure leaves the job held and returns a retryable error.
The decision route does not write the source registry: the admitted worker saves
its processing projection before submitting any bytes. A registry-write failure
there fails processing without submitting the recording.
Retry makes the old decision inapplicable immediately via queue state and keeps
the last held inspection as non-authorizing history until a fresh check replaces
it; source/version or policy changes require a fresh check. Decision audit events
contain only action/state, without source content or audio findings.

The source projection is saved before the held queue transition. An interrupted
inspection remains running and returns to the existing queue on restart. The
next worker rechecks safely. A completed hold survives restart/restore. Existing
already-submitted jobs resume their processor job; they are not retrospectively
intercepted. Recovered attempts with an unknown submission boundary reconcile a
matching processor job before inspection can hold them, including the legacy
submit-before-local-ID crash window. The match must preserve owner, digest and
size; normal completion/failure removes processor scratch. If reconciliation is
unavailable, processing remains retryable rather than declaring a completed hold.
A known, unsubmitted hold can be checked again without processor availability. Finished transcript jobs retain their original inspection. Matter
closure removes the job, inspection, processing copy and derived state through
the existing purge boundary.

## Synthetic evidence and known limits

`tests/test_media_preflight.py` covers video-only admission/conversion/playback,
silence, corrupt inspection, delayed/interior/trailing silence, low-level speech,
noise, clipping, absent capability, partial/multiple-track checks, original-byte preservation,
explicit and duplicate decisions, repeated upload admission, source tampering,
cross-matter denial, restart, interrupted work, stopped-state backup/restore,
non-searchability, original-time transcript citations/SRT, reserved inspection
metadata and closure cleanup. Compact upload polling treats every held check as
terminal work. Playback-only sources have a separate upload count and create no
unresolved Activity, attention badge or Matter Home resolve item, including
alongside searchable text. Actionable catalog filtering precedes pagination.
Upload status adds `playback_only_count`; these terminal items are neither
`ready_count` (searchable) nor `attention_count` (action needed).
Only recordings awaiting a decision create actionable review work. Pending
decisions remain visible in global Activity after
switching matters; initial and refreshed recording panels offer Review recording
with an attention marker, without claiming transcript readiness. It is separate from frozen acceptance packs.

The committed speech fixture is synthesized from invented text; its generation
command is in `tests/fixtures/media-preflight/README.md`. On one development
CPU, the 5.5-second fixture was inspected in 0.23 seconds, and the bounded first
600 seconds of a 601-second silence fixture in 0.89 seconds. The latter stayed
uncertain. These are reproducible smoke measurements, not a deployment capacity
benchmark or an accuracy comparison.

Measured fixture outcomes: ordinary generated speech and speech following
30 seconds of silence were ready; digital silence was no-speech; deterministic
white noise, clipped audio and speech reduced by a factor of 1000 were uncertain.
A 440 Hz tone can be classified as likely speech: VAD can mistake music or noise
for speech and can miss quiet/brief speech. This slice makes no validated
intelligibility, language, forensic-integrity or exhaustive-review claim.

`scripts/browser-accept-media-preflight.py` exercises real selection/confirmation,
upload completion, video-only playback, failed-check retry, saved no-speech review,
recording-panel status and Activity after switching matters, mobile
explicit continuation, original-byte submission and original-time seek/reload.
It uses synthetic recordings and a deterministic transcription processor; it
does not measure ASR accuracy. Supply Chrome/ChromeDriver and an output directory.

## Rollback

The existing database schema remains compatible. Before activation, retain the
normal verified whole-boundary backup. Run
`scripts/verify-media-preflight-rollback.py --previous-source /path/to/previous-checkout`
to check the intended previous reader. Against the preceding reviewed code,
held no-speech and video-only records remained readable/playable, non-searchable
and unsubmitted. The old UI cannot offer the new continue/recheck controls or
admit new video-only sources; restore the new code to regain those actions.
Do not manually requeue held rows or delete inspection metadata during rollback.
If a different prior reader fails this proof, restore its matching whole boundary
instead of assuming code-only rollback is safe. Feature activation is a separate
deployment action.
