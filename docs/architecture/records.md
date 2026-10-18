# Cited records v1

Status: proposed interchange and acceptance specification. No production caller,
storage format, migration, authorization mechanism or model is added. This builds
on [core evolution](../CORE_EVOLUTION.md), [Ideal Exculpata](../IDEAL.md) and the
[current architecture](../ARCHITECTURE.md). It does not claim those runtime paths
already emit this format. The separate core/components draft and report appendices
were unavailable; no requirement or implemented behavior is attributed to them.

The purpose is to help defense teams uncover evidence and buried connections that
can exculpate, challenge allegations or change understanding, while preserving
contrary evidence and uncertainty. This enables stronger honest investigation;
it does not promise a legal outcome or manufacture a conclusion of innocence.

## The invariant comes before capacity

A useful core can accept manually captured, cited assertions and deterministically
compile them without a model. Optional producers can examine more material or
propose richer connections. **Every producer uses the same source, revision,
uncertainty and human-review contract.** A capability tier never grants authority
to accept a record, change an earlier decision or relax evidence validation.

This is a low-compute architectural goal, not a new hardware qualification. See
[measured compatibility](../COMPATIBILITY.md) for actual tested configurations.
The acceptance example needs ordinary Python and existing dependencies; it makes
no GPU, service, database, model download or network call.

Over a two-to-five-year horizon, evidence identity, provenance, matter isolation,
permissions, reviewed revisions and portable results remain stable boundaries.
Models, planners, analytical tools, indexes and execution infrastructure remain
replaceable. The contract assumes no provider, context-window size, search UI or
single-server topology. Readers explicitly negotiate a supported schema version;
unknown versions are retained opaquely or refused, never silently downgraded into
accepted v1 records. An extension requires its own reviewed version and adapter.

An event is an assertion about an occurrence. A statement is an attributed account.
An entity mention is a particular surface occurrence, not a resolved identity.
`accepted` means a human accepted this revision for use as a cited record. It does
not establish truth, credibility, legal significance or that a paraphrase follows
from its quotation. Conflicting accepted accounts can coexist. Exact quotation
checks prove traceability to supplied text, not semantic entailment, authenticity
of the original, extraction accuracy or completeness of recognition.

## Envelope, identity and review

The machine contract is [cited-record.schema.json](../../schemas/cited-record.schema.json),
JSON Schema 2020-12. Unknown properties and schema versions fail closed. All four
types carry `schema_version`, `record_id`, `matter_id`, `record_type`, `revision`,
`previous_revision`, `base_accepted_revision`, `created_at`, `producer`, `status`,
`review`, `citations`, `relations` and type-specific `payload`.
V1 uses `schema_version: "1.0"`; revision numbers belong to records, not the schema.
Schema validation with format checking is the shape gate. Conformance also needs
the contextual source, scope and revision checks demonstrated by the acceptance
helper; passing JSON Schema alone cannot authorize or establish a valid acceptance.

- `record_id` is an opaque stable identity assigned at first capture, never a hash
  of the current wording. `(record_id, revision)` identifies an immutable snapshot.
  Matter and type cannot change across that identity. Separate assertions are not
  deduplicated merely because their wording or source passage matches.
- Revisions start at 1 and append contiguously. `previous_revision` names the prior
  snapshot; `base_accepted_revision` names the accepted revision observed when this
  snapshot was made, or null. A stale base cannot replace reviewed work. Every
  status transition, citation correction and edit is a new snapshot. Old snapshots
  are retained; a duplicate revision key with different bytes is an error.
- Every new or edited assertion starts `proposed`, with `review: null`. A review
  appends an `accepted` or `rejected` snapshot with identical payload, producer,
  citations and relations to the preceding proposal. It requires a human
  `reviewer_id`, exact `reviewed_revision`, UTC `reviewed_at` and nonempty `reason`.
  Editing during review requires a new proposal first. Creation and
  review clocks describe records, never event time. Review cannot predate creation.
  Append order is authoritative if wall clocks tie; clocks cannot move backward
  within a record. A model's confidence is not review.
- A new proposal or rejection does not erase the latest accepted snapshot.
  Compilation selects the latest accepted revision and, if newer, the latest
  proposed revision. Both remain visibly labeled. Rejected revisions are omitted.
  Accepting a replacement explicitly advances the accepted revision. Withdrawal of
  already accepted records requires a future explicit contract; rejection of a
  pending proposal must not accidentally serve as withdrawal.
- `relations` preserve explicit `conflicts_with` references to exact record
  revisions in the same matter, with a rationale. They do not implement contradiction
  discovery or choose a winner. A later revision does not silently retarget a link.
  The projection labels each target's review status and whether that revision is
  current or historical/superseded; a rejected target is not rendered as evidence.
  Fresh proposals and review decisions resolve these exact targets against the
  supplied history before append; missing, foreign-matter and non-assertion targets
  fail at that boundary, not only during later chronology compilation.

Existing runtime states (`needs_review`, `confirmed`, `disputed`, `dismissed`)
are not interchangeable with this proposal's review decisions. Runtime adoption
needs an explicit, reviewed adapter; this specification performs no migration.

An append checker can detect changes against the supplied prior snapshot; a bare
schema or self-contained JSON file cannot enforce durable immutability. Any future
runtime adoption must enforce append/CAS and access checks in the application's
existing authorized transaction, with audit, export, purge, backup and clean restore
acceptance. This PR only round-trips the synthetic contract, not a database restore.
When reading historical snapshots, the helper checks their structure and revision
chain, then resolves the selected records against current supplied support. It
does not re-accept historical decisions. A correction can therefore restore use
without requiring withdrawn historical representations to remain available.
History remains subject to the application's authorized retention lifecycle.
Fresh coverage appends also require their frozen scope and validate its receipt,
target resolution and partition before returning any new acceptance snapshot.
The fresh append entrypoint cannot disable contextual validation; only the private
history-reconstruction helper defers it while loading a historical chain.

## Citations and support validity

Every evidentiary record, including a proposal or rejection, requires at least one
citation. An uncited working note is outside this format. Citations contain stable
`source_id`, immutable `source_version_id`, `quote` and `content_sha256`, plus a
`locator` with `representation_id`, a positive page number **or** a bounded media
time span, character start/end and `excerpt_sha256`. Time spans are integer
milliseconds and half-open; pages are one-based. Character offsets are half-open
Unicode code-point positions in the identified text unit, never byte offsets.

V1 is deliberately a subset of the existing
[`SourceLocator` contract](../../src/case_intelligence/contracts.py). The acceptance
helper invokes that validator; it does not parse human-readable locator strings.
One citation addresses one page or one supplied media text unit. Multi-page support
uses multiple citations. General image/line/Bates locator extensions are deferred.

Resolution must bind matter, source identity, source version, representation,
locator bounds, full unit content digest and exact quote slice together. The
excerpt hash alone cannot detect changed surrounding context. A representation
pin is required because OCR or transcript text can change without changing original
bytes. IDs and hashes are declared provenance, not runtime attestation. Original
source bytes and extraction provenance remain the application's responsibility.
Installation-local support tokens are not portable record identities.
Each citation declares the material fields it `supports`; all required content,
attribution and time fields must have bindings. A binding is a reviewable support
claim, not a semantic proof. Mention surfaces must equal their exact quoted slice.

There is no fallback to a current version, same-name source, matching quote on a
different page, or another matter. Review state and present support validity are
separate: an accepted historical revision stays accepted if a source later becomes
unavailable or stale, but cannot be presented as presently verified. The synthetic
compiler fails closed on unavailable, stale or mismatched support; future runtime
views may preserve it with an explicit unavailable-support label. Reverification
or corrected support produces a new revision, not a mutation of the old decision.

## Type-specific meaning

| Type | Required payload | Interpretation |
| --- | --- | --- |
| `event` | `description`, `when` | An asserted occurrence; dates belong to the assertion, not its capture time. |
| `statement` | `content`, `speaker` | Attributed words/account; speaker may explicitly be unknown. This does not resolve the speaker to a person. |
| `entity_mention` | `surface`, `entity_kind`, `identity` | Exactly one cited surface occurrence. `identity` is `unresolved` in v1; reconciliation is a separate human operation. |
| `coverage` | Frozen scope, processed/unprocessed/failed portions with reasons, execution receipt | A processing declaration; never an evidentiary assertion about absent events. |

`when` preserves a source/reviewer expression and either an explicit day, a bounded
inclusive date range, or unresolved bounds with a reason. Both bounds are null for
unresolved time. V1 deliberately compiles at day precision: it never supplies a
missing timezone, year, time of day or midpoint. A finer timestamp can remain in
the expression; intra-day ordering needs a future explicit precision contract.

## Coverage boundary

The approved narrow exception permits an empty `citations` array **only** for
coverage of failed, unreadable targets. Every target still resolves to its exact
source/version in the same matter; every portion is failed with a nonempty reason.
The record retains a frozen scope and a failed execution receipt. It cannot claim
processed material, use an invented quote, representation or text digest, or carry
an uncited event, statement or entity mention. All three evidentiary types require
quotations at every status and tier. The projection explicitly labels quote-free
coverage as a processing receipt, not evidence.

Coverage freezes a declared population, stage and source versions. Each known
range must be partitioned into disjoint processed/unprocessed/failed portions;
each portion names its reason. Unknown extent remains explicit and cannot acquire
a percentage. Omitted sources and unsealed inventories are visible gaps. A complete
partition only accounts for the declared population, not undiscovered sources.
Processing a range means performing the named stage, not understanding every fact.
Successful extraction with no events is not evidence that no event occurred.
The companion [scope schema](../../schemas/cited-record-scope.schema.json) freezes
the operation, inventory completeness, source/version/representation/content basis
and known unit ranges. Coverage partitions reference its exact digest. Ranges are
half-open, including pages; unknown extent uses null bounds and has no percentage.
Known physical extent survives failed extraction: a twelve-page unreadable source
can declare page `[1,13)` with null representation and content basis. Its catalog
must independently supply that same physical extent; failed extraction cannot
turn a known denominator into unknown extent. Physical inventory and extracted
text coverage are different claims. These catalog declarations are synthetic
inputs here, not a new source-discovery or storage subsystem.

Every coverage record includes an `execution_receipt` with stable receipt ID,
stage, UTC start/finish and completed/partial/failed outcome. The stage matches the
scope, the clocks precede record creation, and the outcome agrees with its portions
and inventory completeness. A receipt declares what was attempted; it is not an
attestation of execution. The fixture's receipts are hand-built synthetic values.
The scope digest uses UTF-8 compact JSON with sorted object keys, unchanged array
order and no non-finite numbers, as implemented by the test helper's `digest`.
Quotes must lie within processed portions. They demonstrate access to those cited
units, not completion of the whole operation; the processing claim remains a receipt.

## Deterministic chronology acceptance

The hand-built [synthetic fixture](../../tests/fixtures/cited_records.json) and
[test-only compiler](../../tests/cited_records_support.py) are an executable example,
not a new product timeline or store. No runtime module imports them.

1. Validate each record's shape and revision chain, selected records' exact support,
   and the frozen coverage partition. Reject cross-matter or unresolved references.
   Every selected event citation must fall within a processed portion of the same
   declared chronology scope; an unrelated complete scope cannot hide its gaps.
2. Select current accepted snapshots plus pending proposed alternatives. Keep
   competing accounts and their exact relation targets; exclude rejections.
3. Order dated records by `(earliest, latest, record_id, revision)`, then unresolved
   records by identity/revision. Render date ranges unchanged. This is a stable
   **presentation order**, not a claim of temporal precedence: overlaps, same-day
   entries and unresolved dates are explicitly not ordered by evidence.
4. Every event line carries record/revision identity, status, dates/uncertainty,
   all source/version/representation locators and exact quotes. Preserve coverage
   and gaps alongside the chronology, even if no events were selected.
   Escape text control characters so source text cannot forge an uncited line.

The checked expected output proves shuffled input gives identical output and never
creates a date from capture time. It also proves rejection exclusion and accepted
history surviving a later proposal. Tests cover broken quotes, changed context,
missing pins, invalid spans, stale/unavailable support, cross-matter references,
uncited acceptance, invalid review, duplicate/rewritten revisions and coverage
omissions. Fixture expectations are reviewed files; tests never rewrite them.

## Capability contract

`producer` records `kind` (`manual`, `deterministic`, `model`), `name`, `version`,
`capability`, declared `tier` and `model`. Manual and deterministic producers use
`model: null`; a model producer must name its model and immutable revision. The
producer's software version and model revision are separate. These are declarations,
not proof of execution. V1 newly captured records require known provenance; a
future import adapter must disclose unknown historical provenance rather than
fabricate a model pin. Tiers describe evaluated tasks, not hardware brands or
guaranteed truth. The same valid records can move between tiers without conversion.
Capability (operation), capacity (budget), qualification (tested producer
combination) and policy (permitted use) are independent. A small installation can
inspect existing records without reproducing the analysis that proposed them.
There is no implicit remote fallback or outbound authority in this contract.

| Declared tier | Bounded synthetic evaluation |
| --- | --- |
| `baseline` | Manual capture or deterministic processing: all four record types, append/review decisions, exact citations, chronology and honest gaps with no model. |
| `assisted` | Baseline contract plus proposed extraction from fixed passages: report per-type precision/recall, exact span accuracy, attribution errors, date uncertainty and abstention on unsupported cases. |
| `extended` | Same contract plus declared bounded cross-source tasks: preserve conflicting accounts, multi-source support and incomplete-input behavior; report false links, missed conflicts and recall separately. |

Each claimed capability needs a versioned synthetic suite and fixed input digest,
producer/model/runtime revisions, prompts/configuration where applicable, attempted
and completed scope, resource/time budgets and observed peak memory/latency. Report
counts and denominators, errors, abstentions and failures per task; quality thresholds
must be declared before a run, never inferred from a tier name. Citation/review/
revision/isolation contract violations have zero tolerance at **every** tier and
cannot be averaged away by quality scores. All machine output starts proposed.
The fixture passes only baseline contract conformance; it is not an extraction,
model-quality, hardware-capacity or deployment qualification result.

Higher capacity may widen a declared processing budget, test richer capabilities
or lower latency. It must retain omissions and refusals and never auto-promote past
human review. CPU-only manual operation is sufficient for the contract. Optional
future work includes runtime adapters, concurrent persistence, richer time models,
entity reconciliation and withdrawal; none is a prerequisite for v1 conformance.

## Seams for analysis and research

The four v1 records are inputs to richer work, not a ceiling on product behavior.
The wider invariant is that every result has an inspectable basis appropriate to
its kind; it is not that every result is quoted verbatim. The existing
[investigation](../EVIDENCE_DRIVEN_INVESTIGATION.md) and
[synthesis](../HIERARCHICAL_SYNTHESIS.md) boundaries are relevant foundations.
Future derived analyses, calculations and hypotheses need typed input revision
references, methods, assumptions, uncertainty and independent verification state.
Their lineage must remain distinct from direct source support: a quotation can
support an input assertion without proving the computed result or theory. Derived
outputs must not masquerade as v1 source-backed event records. Versioned work
products can assemble evidence and derived claims while retaining those distinctions.
An accepted input being revised, re-extracted or becoming unavailable makes
dependent analysis potentially stale; it must not silently change its premises.
For example, a clock calibration of +6 minutes with uncertainty of one minute can
normalize a recorded 21:20 to a conditional 21:13–21:15 interval. The source clock
reading and calibration remain cited inputs; the interval is a calculation with
assumptions, not a quotation or an automatically established contradiction.

Permitted external research needs separately labeled public/reference sources,
retrieval dates and provenance, distinct from case evidence. A research capability
does not itself authorize network access, transmit private matter data or change
source-role policy. These future contracts require separate design and tests;
this PR adds no research tools, calculations, model calls or new record types.

### Future stress test: many entities, transactions and allegations

Consider millions of records, 50 candidate people/items/organizations, ten banking
records and allegations organized by indictment count. A useful system must be
able to produce layered analysis of their relationships, not only retrieve similar
passages. This is a design stress test, **not** scale demonstrated by this fixture.

A future typed query plan would coordinate scoped structured joins and aggregates,
relationship traversal, targeted retrieval and analytical tools. Checked intermediate
results would be reusable only with exact input revisions, methods, assumptions,
budgets and coverage receipts. Entity resolution must retain ambiguous matches;
same names or addresses alone do not establish identity. Transaction reconciliation
must distinguish recorded transfers from inferred control, benefit or relationships.

Count-by-count analysis should preserve supporting and opposing evidence, missing
links and alternative explanations. Proximity or a transaction edge does not
establish guilt or the elements of an allegation. Layered output can offer an
executive overview, entity/transaction views, count analyses and drill-down to
individual sources, assumptions and processing gaps. Acceptance of an analysis
does not authorize filing, sending or any other consequential action.
Each entity/record/count cell must distinguish a supported connection, an unresolved
possible connection, no match within a completed defined scope, and not evaluated.
Blank cells or zero counts must not imply no relationship. Bank joins need
identity- and time-qualified roles, duplicate/reversal handling and explicit
currency treatment. Indictment allegations remain distinct from evidence and from
researched legal elements. A graph path is an inspectable lead, not proof of
involvement or culpability.

Capability evaluations would separately measure join/reconciliation correctness,
ambiguous-identity abstention, unsupported relationship rates, opposing-evidence
retention, calculation verification and provenance completeness. Capacity trials
would measure actually processed scope, resumability and resource limits on the
declared corpus/topology. More compute may enable deeper justified exploration,
additional modalities and independent cross-checks; it cannot turn a partial run
into a completeness claim. The four v1 record types are enabling inputs, not this
analytical engine, and `statement` must not be overloaded with arbitrary analysis.

### A staged path with useful increments

These are proposed follow-ups requiring their own scoped implementation approval;
this specification authorizes no further runtime work.

1. **Cited records (this PR):** schemas and an executable manual chronology prove
   immutable review decisions, traceability and honest gaps.
2. **Next smallest vertical slice:** one synthetic transaction between two ambiguous
   named parties, linked to one alleged count. Preserve observed amount/date/account
   assertions separately from proposed identity and relationship links. Test a
   same-name distractor, a reversal and an unresolved party, with evidence on both
   sides and no automatic inference of control or culpability. Integrate through
   existing matter/review/lifecycle boundaries before broadening the dataset.
3. **Bounded analytical plans:** compose that slice into typed joins, calculations
   and targeted retrieval with checkpointed, reusable results. Test cancellation,
   budget exhaustion, changed inputs and identical semantics at different capacities.
4. **Layered work products:** compile checked outputs into overview, relationship
   and count-specific views with revision-level drill-down. Test preservation of
   opposing evidence, assumptions, unresolved cells and coverage in every export.

Each increment should deliver a usable capability and measured acceptance evidence.
The flagship 50-entity/million-record scenario guides boundaries; it does not delay
useful small installations until a distributed analytical system exists.
