# Comparing generated answers with cited context

This R1a.2 correction packet adds an on-demand **Compare cited context** control
beside every citation in a saved focused answer's claims and limitation, in both
the conversation and the assistant. Each citation opens independently, so readers
can compare separate or conflicting sources without joining their text into a
single apparent account. A keyboard-operable disclosure loads the comparison;
a separate comparison page remains available without JavaScript.

The text is **cited context, not proof that a claim is supported**. Check the
claim's meaning, qualifications and attribution against each source. This is a
source-inspection aid, not semantic verification or real-model acceptance.
Known service-authored omission wording in the displayed generated limitation
uses the confidence presentation from PR #110, in both the comparison page and
its structured response. Source qualifications, custom text and generated claims
retain their wording. The saved payload and raw citation fields are still used
for source validation; this display change does not alter the returned source
excerpt, source version or full-unit digest.

## Exact source basis and bounds

A request identifies a stored message, passage and citation by matter and
conversation. It cannot supply replacement citation data. The app reads only that
message's bounded payload, authorizes current matter access, and resolves one
citation under the existing source mutation guard and workspace lock. It rechecks
live session/provider authority and matter access before reading and after
rendering. HTML also rechecks administrator authority used by its wider matter
navigation, even when the actor is a member of the requested matter. Losing that
role mid-request refuses the page; JSON remains available with current membership
because it contains no wider navigation. Existing audited administrator read-only
access is preserved.

The comparison holds the existing matter deletion lease until the outer HTTP
response finishes sending its body, covering both HTML and JSON. Scheduled purge
and deletion remain deferred while that response is in flight. Rendering failures,
send failures and cancellation release the lease through their cleanup paths.

Resolution reuses the saved-work validator: source identity/version, locator,
full-unit text digest and compatible legacy citation rules must match. In
particular, a transcript moment link alone does not authorize displaying edited
text as the context for an earlier answer. An edit after the displayed prefix
still invalidates the comparison. Missing, changed, unreadable or unverifiable
sources produce an explicit unavailable state without replacement text. Exceeding
the existing source-validation budget gives a separate boundedness notice.

Only after validating the complete unit does the response return its first
6,000 characters at most, preserving whitespace. The structured response retains
that exact source text. Inline browser display follows the `controls-to-spaces-v1`
presentation policy without changing the returned snapshot or its digest; valid
supplementary Unicode characters remain intact at the prefix boundary. The notice
identifies this as a prefix; it is not a relevance-selected excerpt. The full-source link opens the
current source viewer, which may change after comparison. Document links use the
validated searchable-unit position, preserving physical PDF page labels even
when preceding pages are blank or unreadable. Transcript links seek
to the validated segment's start time and identify its segment. Both source and
comparison return links retain the specific saved answer, even after newer
answers are added. Historical version and
full-unit digest remain inspectable. Each request retains existing streaming
validation limits (including time, characters, units and serialized record size).
There is no eager source resolution when rendering an answer. Reopening a
comparison requests fresh authority and validation; responses use `no-store`.

Saved answers continue to omit full source excerpts. No source bytes, source
version, digest, stored answer, schema, reviewer decision or storage contract
changes. No migration or backup/restore change is required. Closing a disclosure
clears its fetched text, cancels any request and prevents a late response from
repopulating it. Network failures leave a keyboard-reachable retry and page link.

## Validation and remaining work

`tests/test_answer_cited_context.py` covers served comparisons, historical basis,
source changes, authorization, escaping, bounds and unchanged saved payloads.
`tests/test_answer_cited_context_leases.py` covers deletion deferral through the
final response-body send and lease cleanup on rendering, send and cancellation
failures.
`tests/test_cited_context_confidence.py` checks current and historical limitation
presentation, custom/quoted text, unchanged raw evidence and unavailable sources.
`scripts/browser-accept-cited-context.py` exercises real browser rendering,
keyboard controls, independent citations, narrow widths, failure recovery and
transcript playback/return navigation with synthetic text and audio sources and
an injected deterministic generator and transcript processor. These tests do
not establish generator quality, transcript accuracy or installed-node readiness.

This packet covers focused generated-answer review, including historical answers
and answers produced with recorded context. It does not add comparison controls
to the separate investigation/full-text synthesis hierarchy or change portable
export formats. Those broader R1a inspection surfaces, real-model evaluation
(R1b) and integrated installation/GPU acceptance remain separate gates under
issue #109 and its correction plan. Follow the [review policy](QUALITY_GATES.md#review-policy) before merge. Existing source links
and export validation retain their contracts.

The overall R1a section stays open. To close the broader inspection work:

- Investigation and full-text synthesis claims must expose each original context
  separately, preserving frozen hierarchy input and source versions; changed or
  unavailable originals must fail closed in the served browser.
- Applicable portable exports must retain clearly attributed bounded context or
  an explicit unavailable state without rewriting historical citation basis or
  implying support. Validate Word/Markdown/JSON outputs, existing export budgets,
  authorization, and unchanged saved-answer storage.
- Exercise the combined confidence and comparison changes on the final integrated
  candidate, including the adversarial cases in the correction plan. Record
  Quality CI, source-review outcomes and remaining installed/model limitations.
