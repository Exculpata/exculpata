# Returned synthetic Linux validation

The returned validation package reports successful native Linux validation of
the two workflow repairs. Its patch is byte-for-byte identical to the four
production-file changes in local candidate
`e8b6039acc2d32662b4c3c61cd8282f306c4348d` over public baseline
`d43e142769b5d57a901fb58439bf841ed954b8b4`.

Linux tested an uncommitted patch on that baseline. The local Git bundle was
unavailable there; neither the complete local candidate commit nor its 43 new
tests are claimed as Linux-validated. The Linux agent authored a separate
25-case suite and browser journey. These are returned results, not Linux runs
performed by the coding task. Attachment integrity and production-file identity
were independently checked when the feedback returned.

## Verified artifact identity

All nine artifact checksums in the returned `validation-manifest.json` match
their files. The returned patch SHA-256 is
`0f929aa2f9a6691b6197be4e2689acd4b32d419f1b010227d5bb5638013fff17`.
The four production files also match the local candidate directly:

| File under `src/case_intelligence/` | SHA-256 |
| --- | --- |
| `workbench.py` | `bb022b914fdb29d73737020b80db8d7c7ab533bfa5af82a533b48d008d17095c` |
| `report_materials.py` | `08844911a86fc1288b50e0dd46764a921b09505ce3a6918238115cc83b7c73a0` |
| `report_review_basis.py` | `fa741feebd50e4706c9f15c1ec66d5e507e66fa688de4b29440f8504a62c294e` |
| `templates/workbench_research.html` | `9b9dc787959746518db526a9ba11c370754ef530d1bec2afcc4ae893544f6e5d` |

The supplied report, bounded receipts and synthetic test sources remain in the
original feedback package. This document records their relevant results without
importing a second overlapping test suite, raw logs or runtime state.

## Reported Linux gates

Environment: Ubuntu 24.04.5 LTS, x86_64, Python 3.12.3, checksum-pinned Chrome
for Testing and driver 153.0.8010.36. Each checkout used its own contributor
bootstrap. Native Linux PDF extraction and FFmpeg ran without the macOS fixture
accommodations. FFmpeg Flite generated synthetic speech with measured segment
durations. Transcription and generation transport were controlled; speech
recognition and learned-model quality were not tested.

| Check | Patched baseline | Unchanged baseline |
| --- | --- | --- |
| `make bootstrap` | Exit 0 | Exit 0 |
| `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0 make check` | Exit 0: 2,945 application tests passed, 9 skipped; 194 transcription tests passed; compilation, Compose and publication checks passed | Exit 2: 2,944 application tests passed, 1 failed, 9 skipped; stopped before transcription |
| Separate `make test-transcription`, with the same test reserve override | Included above | Exit 0: 194 passed |
| Independently authored Linux workflow tests | 25 passed | 14 failed, 5 passed, 6 skipped |
| Existing pinned-browser runner | Both journeys passed, 23 checks | Both journeys passed, 23 checks |
| New synthetic browser journey | Repaired workflows passed | Both expected workflow rejections reproduced |

The 25 new tests ran separately after full-suite collection; they are not
included in the 2,945 application-test total. Their node-level receipt agrees
with the totals above. Six baseline skips concern the absent new saved-capture
adapter and are not baseline successes.

The one unrelated baseline failure was
`tests/test_local_account_lifecycle.py::test_same_size_edit_with_restored_mtime_reloads_cached_snapshot`.
Its filesystem change timestamps remained equal during the fixture setup, so
the required precondition failed before the cache assertion. The report records
the same isolated failure on baseline and success on the candidate, whose test
and local-account implementation are byte-identical. This supports a timing
sensitivity in the fixture; it establishes neither a production cache defect
nor a repair by this patch. The failed baseline gate remains recorded as failed.

## Workflow evidence and limits

The baseline producer again saved three candidate passages and five selected
passages, then failed checkpoint verification. The repaired first pass saved
five candidates from three sources, with five selected, newly admitted and
analyzed passages. A duplicate-only follow-up saved three candidates and zero
selected/new/analyzed passages. Overlap, bounded admission, interruption and
resume checks passed. Invalid historical checkpoints remained rejected without
rewriting their counters. Empty/unavailable retrieval and source-change cases
were covered by the existing suite, not all repeated as new neighbor fixtures.

PDF-only synchronous and queued saved work compiled on both revisions.
Transcript-only and mixed saved work failed conversation compilation on the
baseline and passed on the candidate. Separate database readback, supported-note
compilation, original support links, and Word/Markdown exports were exercised.
The new browser journey also checked stale-conversation rejection and recovery
in a new conversation while retaining prior work.

The candidate rejected stale saved-answer capture, independently cited stale
qualifications, forged support metadata and unauthorized capture. Affected Report
exports returned HTTP 400 after transcript editing. Existing safe legacy
digest-bound compatibility remained covered by the full suite.

The returned browser assertions verify original support panes and exports
containing both source names. They do not independently assert the exact
recording seek time or every immutable exported field. The stronger local
source-position and exact-text checks remain separate evidence. Likewise, the
returned largest-payload test checks serialization and the material adapter;
the local test additionally exercises verification, persistence and queued
compilation. Neither is a maximum-payload browser stress test.

The five-source/eight-unit quality fixture passed extraction, source-packet
delivery, persistence, coverage accounting, separate human decisions and stale
review rejection. Its classifier follows fixture labels, so its four retained
relevant sources and excluded control do not measure learned recall. Discovery
produced seven suggestions/mentions with five labels; source navigation, manual
merge and Undo passed. These are fixture-specific counts, not adjudicated entity
counts or a measurement of reviewer effort.

## Disposition

No additional production correction is supported by the returned checks. Keep
the existing repairs and strict validation unchanged. Actual-model relevance,
semantic usefulness, independent-user access and broader operational acceptance
remain outside these deterministic results. Follow the quality evaluation
handoff for the outstanding model work.

Linux validation does not grant public upload, hosted review, merge or deployment
authorization. The complete review candidate still follows the
[review policy](QUALITY_GATES.md#review-policy). Instructions recommending those
actions inside the attachment are recommendations, not user authorization.

The coding task changed documentation only when incorporating this receipt.
Its unadapted macOS application rerun returned 2,835 passed, 127 failed, 26 fixture
errors and 9 skipped, with the same failure/error nodes as the prior native
candidate run. This remains the documented platform limitation, not a green
local application gate. Transcription tests, compilation, Compose validation,
publication content checks and documentation-link checks passed.

During subsequent preparation for public review, an independent local review
found that reusing the Report resolver also imposed its full-unit size limit on
Note capture. A separate correction retains the existing bounded Note preview
after full-source verification while keeping Report limits strict. The hashes
and Linux results above describe the originally returned patch, not that later
correction. Validate the current PR head and its new long-source regression
separately before accepting the complete candidate.
