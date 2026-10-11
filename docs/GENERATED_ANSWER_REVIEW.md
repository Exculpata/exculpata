# Reviewing generated answers

Generated answers and synthesis are drafts for source review. Citation references,
word overlap, quoted-text checks and numeric/negation checks do not establish
that a sentence preserves the meaning of its source. A claim can reuse source
words while reversing a decision, swapping participants, combining unrelated
details, or dropping uncertainty.

New focused answers are introduced as generated work for source review. The Case
conversation, assistant panel, and synthesis view warn that automated checks do
not establish correctness. Known older answer introductions receive current
confidence language when displayed or exported; their saved payload, claim text,
source identifiers, and reviewer history are not rewritten. Word and Markdown
answer/conversation exports carry the notice, as do synthesis exports; portable
conversation and investigation JSON expose a review notice. Reports retain their
existing instruction to verify statements against original sources.
Saving a generated answer to a Report also normalizes its leading introduction
and includes the generated-text review notice in the new section and its exports.
The original saved message remains unchanged.

Portable conversation JSON updates both the structured introduction and the
matching leading introduction in the full generated message text. It does not
replace quoted occurrences inside claims or alter user-authored messages.
Historical investigation summaries and per-search findings receive the same
leading-introduction treatment in the research view, Word/Markdown/JSON exports,
complete bundles and newly copied Report sections. The structured exported answer
introduction is also normalized. Exact saved-result and source-ledger validation
runs against the original record before any succeeded-result view, export or
Report-copy presentation change. Invalid saved results remain stored unchanged;
the research page hides their derived text and offers a recovery notice. This
checks the saved ledger, not whether an old source is still current. Copies
to Reports carry the generated-text notice; the saved investigation, claims,
citations and source bytes remain unchanged. Research instructions and progress
describe citation/text checks, not verified meaning.

Written/spoken coverage notices describe which passages were cited or absent;
complete coverage does not establish that the generated claims are correct.
Known historical coverage notices receive the same wording in saved-answer and
research views, readable exports and portable JSON. This changes presentation
only: saved notices, coverage metrics and evidence remain unchanged, and custom
notices are preserved. Assistant readiness means sources can be used for an
answer, not that a future answer will preserve their meaning. New compiled Report
coverage labels answer-service calls as attempts, including unavailable or
rejected calls; older saved Report text remains historical content.

Actual generation rejections and partially retained answers describe automated
citation/text checks rather than source verification. Exact historical rejection
messages and service-authored omission notices receive current presentation only
when their saved fields identify that generated boilerplate. Source qualifications,
claims, custom text and human notes remain unchanged. New compiled Reports apply
the same guidance to generated coverage, gaps and otherwise empty generated
responses. Their input fingerprints retain the original selected text, so a
change during compilation still blocks saving even when both versions would
display identically. An in-progress compilation started before this fingerprint
update may require a retry; existing saved Reports are not rewritten.

Use each claim's citation to open the source and compare the wording, participant
roles, qualifications and surrounding context. Multiple citations do not establish
that their details belong to the same event. For transcripts, check the recording
and speaker attribution too. The recorded-context answer path retains its extra
original-span checks; those checks also do not establish general factual truth.

This is the first R1a mitigation in the [review correction plan](REVIEW_CORRECTION_PLAN.md),
tracked in [issue #109](https://github.com/Exculpata/exculpata/issues/109).
It does not change the generator, verifier acceptance algorithm, citation basis,
or storage schema. Direct inline excerpt comparison and real-model evaluation
remain separate sections with their own acceptance evidence. Saved research
records and human-authored text remain historical content; only known generated
leading introductions, coverage notices and surrounding review guidance receive
current presentation.

## Compact answer presentation

On the Ask page each statement reads first, followed by small numbered citation
links. Each distinct cited passage gets one number per answer, in reading order
(statements first, then a sourced limitation), and the number opens the same
passage as its full citation. Full citations are read aloud as "Source N", and a
transcript citation keeps its number, wrapping source label and icon in separate
columns so a long recording name never overflows a narrow screen. The full citations stay
below each statement, numbered to match, with **Save passage to case notes**,
per-citation **Save** and **Compare cited context** unchanged. Each answer's caveats
(source coverage when the answer was created, the generated-text caution and the
machine-transcript note) sit in one collapsed **About this answer** line whose
summary says to check each statement against its sources; the full wording is
kept in the page. Evidence selection, verification and saved citations are
unchanged. `tests/test_compact_answers.py` holds the synthetic regressions.

## Synthetic regression

`tests/test_generated_review_confidence.py` supplies four meaning-changing outputs
to the generator boundary and checks that any accepted result is presented for
review. A future verifier may reject them without breaking this contract. Faithful
paraphrase and fabricated-detail controls check continued usability and rejection.
HTTP rendering and Word/Markdown exports check historical answers without mutating
their stored payloads. These are deterministic software checks, not a measurement
of the installed model's error rate or GPU acceptance.

`tests/test_historical_research_confidence.py` recreates four historical
introductions on a saved investigation and checks its research view, all three
export formats, complete bundle and Report copy. A custom-introduction control
and quoted-text check prevent broad replacement. The original result JSON and
extracted source units must remain byte-for-byte/equality unchanged. Mismatched
raw introductions, summary claims and per-search findings are refused by exports
and Report copies and hidden on the research page, even when presentation would
otherwise make two inconsistent saved strings look identical.

`tests/test_modality_confidence_presentation.py` checks complete, partial and
unavailable coverage in new responses and historical views/exports, with unchanged
raw payloads, source bytes and ledgers. Exact-match and custom-notice controls
prevent rewriting quoted or reviewer-authored text.
`tests/test_generated_workflow_confidence.py` covers the ready assistant, served
readiness script, retrieved-passage abstention and unavailable compilation calls.
Additional rejection/omission regressions exercise the real generator checking
path, and `tests/test_compiled_modality_confidence.py` compiles saved conversation
and investigation coverage through the Report queue, views and exports. It checks
raw-input change detection, malformed saved text and preservation of custom,
human-authored and cited claim text.
