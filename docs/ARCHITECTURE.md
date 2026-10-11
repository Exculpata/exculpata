# Architecture

This document describes current behavior. The [product north star](PRODUCT_NORTH_STAR.md)
and [core evolution plan](CORE_EVOLUTION.md) describe the direction for extending
it through focused modules while preserving shared authorization, provenance,
transaction, and lifecycle contracts.

```text
staff browser
    │ HTTPS
    ▼
gateway ── optional Kerberos proxy
    │
    ▼
Exculpata app ── PostgreSQL + pgvector (derived retrieval index)
    │            ├─ ClamAV (stream scan)
    │            ├─ embedding + reranker worker (offline GPU)
    │            ├─ vLLM generator (offline GPU)
    │            └─ transcription API → durable ephemeral queue
    │                                      │
    │                                      ▼
    │                               WhisperX worker
    ▼
managed matter storage + SQLite control record
```

The app, not the model, owns authorization, matter isolation, audit attribution,
citations, exports, and deletion. PostgreSQL is a rebuildable projection of
matter source units. Candidate retrieval combines lexical and vector lanes,
then a cross-encoder reranks a bounded set. The generator sees only the selected
evidence; a separate deterministic verifier checks claims and citations.

The SQLite control store combines `NotebookStoreMixin` and `ReportsStoreMixin`
in `WorkspaceStore`. These mixins add no state, connection, lock or transaction
boundary: their unchanged operations use the owning store's existing helpers
and shared resources. Report methods keep the same public names and signatures;
record types and Report limits remain available from `workspace_store` for
existing callers. Authorization, text validation, review records and shared
export helpers stay in `WorkspaceStore`.

The existing `iter_review_decisions_for_report` exception also stays unchanged:
it opens a dedicated read-only SQLite connection to stream one stable snapshot
without retaining the shared lock while callers rank decisions. Its cursor and
connection still close in `finally` blocks; callers exhaust or close the
iterator before Report writes. The module move adds no storage or schema.
## Application context

`create_workbench_app` creates one frozen `AppContext` from its existing local
objects and exposes it as `app.state.app_context`. The Reports handlers read
these bindings through the context; their route registration, signatures,
dependency decorators, authorization, transactions and response leases stay the
same. Construction occurs at the end of the factory, after every callback has
been defined and before requests can run. No route moves to another module.

The context contains the workbench, identity service and template renderer;
authentication, matter authorization, both CSRF checks, audit and safe return
path helpers; the base template context, presentation finalizer and download
renderer; active-matter and bundle response-lease dependencies plus their lookup
and transfer helpers; the recording decision limit, capacity limiter and pending
set; and Report labels, citation links, edit recovery, saved-work choices,
compilation rendering and material insertion helpers. The three self-contained
helpers `present_value`, `auth_context` and `requested_path` live in
`app_context.py`; their behavior is unchanged.

Frozen bindings are shallow: services, the renderer, label mapping, limiter and
pending set retain their original identities and mutability. In particular,
recording admission shares the same capacity and pending set with its existing
handler. All other route groups continue using factory locals. Specialized
readiness, assistant, source, workflow and export projections remain local,
as do the Report callback implementations and decorator dependencies. This
limits the initial context adoption to Reports without duplicating services or
altering when dependencies execute.

## Intake and source processing

Loose-file selection first uses a matter-authorized, CSRF-protected metadata
preflight. It returns one content-minimized row per selected file without
copying bytes, reserving capacity, or creating an upload session; filename
type is explicitly provisional, while detected type, readability, source
version, content duplicates, and scan results remain pending. Explicit staff
confirmation records the complete selection in the control database before
sending only the eligible subset through the retained upload route,
which revalidates metadata and remains authoritative for staging, signature
checks, configured ClamAV policy, and failure recovery. [Selection receipts](INTAKE_RECEIPTS.md)
retain explicitly browser-reported selection reasons separately from server
filename checks, byte receipt and source availability,
use idempotent metadata/session bindings and exact source versions, and provide
coherent paginated views and complete exports. Their tables stay in the existing
temporary-matter deletion and backup boundary. Atomic matter/creator/workspace
receipt, selected-row and metadata-byte limits admit writes before storage grows;
only owners can cancel empty pending uploads and discard their receipts. Received
source data and saved partial bytes prevent discard.
Each new resumable upload item identifies a separate [received occurrence](UPLOAD_OCCURRENCES.md),
even with repeated paths or bytes. The existing source-registry key retains
same-item retry identity across finalization and restart, including copy fallback.

[Source folder navigation](SOURCE_FOLDER_NAVIGATION.md) uses the existing
matter-scoped source catalog and organization joins. A literal separator-bound
relative-path prefix restricts source rows; bounded SQL grouping pages immediate
children and descendant counts under the same filters. It adds no stored state
and preserves exact source links and existing source-set memberships.

[Identical-byte comparison](EXACT_BYTE_MATCHES.md) uses a derived digest/size
lookup updated transactionally with source catalog projections. It joins the
current admitted source version and size, and normal registry reconciliation
rebuilds it. The existing catalog row and registry JSON shapes stay compatible
with previous readers. SQL materializes at most 100 display rows and aggregates
the matter's current digest/size groups once before joining their counts. Indexed
source identities avoid scanning the byte-row intermediate for every display row;
folder and matching-only filters stay database-native.
Comparison URLs use existing matter-bound source action tokens, never hashes.

[Email extraction](EMAIL_ATTACHMENT_COVERAGE.md) traverses the parent body tree
without entering attached messages or multipart attachments. All non-report
`message/*` children are boundaries even without filenames or attachment disposition. Header sections
inventory attachment names/types, including explicit empty filename markers as
unnamed attachments, and state that attachment contents were not
processed or searched. All parsed parts still count toward the MIME limit,
including descendants of attachment boundaries; malformed MIME containers fail
with ordinary retry/remove recovery. Lazily parsed MIME classification headers
reject duplicate singleton fields and are checked for their own defects before fallback types can mix attached text
into parent evidence. Body decoding rechecks MIME defects so
reported transfer-encoding defects cannot leave best-effort text searchable. The existing catalog supplies a
matter-scoped email count for readiness and query-time coverage without reading
source files. Email does not block otherwise-ready queries, but `partial_query`
also identifies incomplete attachment coverage with zero excluded source files.
The ordinary answer coverage snapshot flows into UI and work-product exports.
Investigation results combine source/attachment coverage with the focused-search
caution, retaining both in standalone and complete-bundle exports. After final
evidence validation, investigations refresh coverage from the source catalog to
include sources admitted during their live retrieval passes. Queued and synchronous
answers refresh their coverage after generation for the same live-index reason.
The saved counts describe current availability, not an exhaustive searched-source
ledger. The first retrieval records a streaming fingerprint of matter-scoped
catalog IDs, versions, content-basis digests, source states, media types, processing
jobs and orphan pending-upload checkpoints. Uploading/processing sources count
as excluded and retain a plain recovery notice alongside any recording playback
guidance. Investigations keep the first boundary across every pass. Selected
source-set identity and membership are included, atomically captured with the
frozen search membership before retrieval; other sets do not affect that scope
checksum and foreign sets are refused. A
changed boundary during retrieval or generation sets partial coverage with a
rerun notice retained by exports, including equal-count source swaps. Final
coverage and ordinary answer scope refresh under the same source mutation guard
as citation validation and durable saving, with the workspace control lock held
from the coverage read through saving to include control-only upload changes.
Worker completion metadata retains the
retrieval boundary until that save, and storage strips transient research
completion metadata before storing a finished result. One optional
fingerprint field in existing research checkpoint JSON allows recovery to repeat
a bounded search plan when source state changed or an older checkpoint lacks the
boundary. The stale checkpoint and progress reset atomically without changing job
identity or overriding cancellation. No schema migration or source-byte read is
needed; staff status and completed/exported results omit the fingerprint. Focused-answer wording describes
current availability and actual cited support without claiming every available
source was searched. Structured
report metadata is recognized in the second child of `multipart/report` when its
`message/*` subtype matches the container’s `report-type`, including feedback and extension
reports. Explicit attachment disposition or filenames still identify attached
report data; returned messages and out-of-context parts remain boundaries.
Related MIME containers select their body root and inventory other resources;
surrounding Content-ID comments and folding whitespace normalize before root
matching. Missing, ambiguous or explicitly empty root references fail rather than
choosing another text part.
Coverage links use the complete source list in server and live browser rendering,
so combined processing/email coverage can reach both affected and ready sources.
No schema or source registry shape changes; older passages and indexes are not
silently rewritten or assigned a new citation basis.

Accepted documents then enter extraction. PDFs retain native text; expanded
bounded CPU OCR also selects pages using image-placement evidence so long
stamps cannot hide scanned bodies. [PDF extraction coverage](PDF_OCR_COVERAGE.md)
distinguishes searchable text from complete page reading and retains skipped,
failed, timed-out and capped outcomes. Media enters the durable media queue,
which performs a bounded offline recording check before processor submission.
An existing cancelled queue state plus source/version-bound inspection metadata
holds playback-only or review-needed recordings without claiming transcript
success; see [recording-check contracts](MEDIA_PREFLIGHT.md). The WhisperX worker performs transcription, alignment, optional
anonymous speaker clustering, and export generation; the app projects
transcript segments back into the matter index and generates an automatic
source summary.

An extracted document can be marked ready before indexing and registered-source
staging cleanup finish. Matter search readiness therefore uses the durable job
state as well as the source catalog. Integration tests must wait for that same
matter readiness before expecting search results, while retaining source and
result assertions. A paused-cleanup regression proves that extraction alone
does not permit matter search or closure, and that search becomes available
after the job finishes. This contract does not delay ordinary source viewing.

Model workers have no published ports and run on an internal Docker network.
The transcription worker has no network namespace at runtime. The app may need
egress only for a configured OIDC provider. Companion-service URLs require an
exact operator allowlist.

Matter storage is configurable and may be a dedicated host-mounted NAS path.
Exculpata remains a temporary review workspace: exports leave the system,
matters can be closed and purged, and the independent Transcript Studio queue
has a short expiry.

[Export readiness](EXPORT_READINESS.md) and complete-matter download share one
bounded preparation helper, including source verification, saved-work limits,
Report formatting and ZIP packaging. Explicit inspection discards the prepared
artifact and returns an uncached result with its inspection time. It collects
Report validation problems from the same bounded snapshot and links to their
normal review pages; a blocked Report means later bundle checks remain incomplete.
Inspection retains the export response lease through response delivery and
rechecks membership before returning any result. Failed-close inspection keeps
the existing owner/administrator boundary and frozen catalog rules without
reopening quarantined source state. Failed-close Report problems have no edit URL
and render as plain details with Close matter recovery guidance, because ordinary
Report routes require an active matter. Download always prepares anew; a check creates
no stored snapshot, archive, export receipt or authority for a future download.

## Selected review and export improvements

[Investigation budgets](INVESTIGATION_BUDGETS.md) unify retrieval and generation
limits with saved accounting. [Report review basis](REPORT_REVIEW_BASIS.md)
converts saved results atomically with validated citations and separate human
review. [Exact-search grammar](EXACT_SEARCH.md) supplies the shared parser for the
separate [exact-result service and staff UI](EXACT_SEARCH_RESULTS.md); it does
not turn ranked answer retrieval into complete exact search. [Export readiness](EXPORT_READINESS.md)
uses the same bounded preparation as the eventual download. The
[selected acceptance record](SELECTED_ACCEPTANCE_2026-09-09.md) identifies the
combined candidate and deferred feature stacks.

[Local account lifecycle](LOCAL_ACCOUNT_LIFECYCLE.md) separates read-only live
account snapshots from operator-authorized mutations of the same private JSON
file. A shared repository serializes writers and preserves at least one enabled
administrator. Version 2 account revisions bind local sessions to current
credentials, enabled state and roles without changing the SQLite session schema
or granting the web application write access to its secrets mount. The optional
[browser account profile](LOCAL_ACCOUNT_BROWSER.md) relocates the single account
file into a dedicated owner-only writable directory. People uses the same
repository with fresh administrator authorization and stale-edit protection
inside its writer lock; no second account store is created.


## Full extracted-text review

[Full-text review](FULL_TEXT_REVIEW.md) freezes source/extraction identity and
resource admission in SQLite before queueing work. Unit streaming creates
canonical range checkpoints with compact locators, bounded model packets and
frozen output-token limits. Durable per-actor/matter/instance reservations,
pre-dispatch call charges, UTF-8 ledger accounting and bounded coverage counters
keep large and interrupted runs explicit. Source decisions remain compatible with
selected-passage review; a separate [single-run adapter](FULL_TEXT_SYNTHESIS.md)
now feeds their saved findings to the shared hierarchy. Missing extraction,
empty/zero-unit sources, failed ranges and unfinished inventories
remain coverage gaps, independent of relevance-recognition quality.

## Hierarchical investigation synthesis

[Hierarchical synthesis](HIERARCHICAL_SYNTHESIS.md) consumes persisted investigation
findings with original support through issue and matter stages. It preserves
versioned checkpoints, charged requests, omissions, and source validation. Its
investigation input remains bounded. The [single-run full-text adapter](FULL_TEXT_SYNTHESIS.md)
uses the same engine through `full_text_synthesis.py` and
`full_text_synthesis_repository.py`: a narrow streamed snapshot repository shares
the authorized SQLite transaction, while the service validates original support
and produces the canonical input receipt. Routes only compose these interfaces.
Both adapters retain the existing research-job lifecycle and versioned JSON.

[Manual entities](ENTITY_WORKSPACE.md) use a narrow entity service and repository
over the existing authorized SQLite unit of work. Original passage validation
and final save remain under the source mutation guard; names and aliases never
merge stable matter-scoped identities.

[Evidence connections](GRAPH_EXPLORATION.md) project existing explicit entity
roles, events/assertions and source accounts into a bounded read-only neighborhood.
The graph service shares the authorized entity/assertion unit of work and batches
original-reference validation. Graph traversal retains saved identity revisions;
historical roles and source accounts remain explicit. No graph database or new
persistent authority is added. Native visual retrieval follows its separate
qualification contract and is not implied by OCR or transcript navigation.

[Case notes continuity](product-slices/continuity-a-case-notes.md) adds a bounded
read-only projection of the same saved identities and events/assertions to the
existing notebook. `matter_knowledge_repository.py` reads on the authorized
assertion connection; `matter_knowledge.py` holds the source guard and batches
exact original-reference validation without loading correction histories.
Independent pages and per-stance omissions preserve competing accounts. The
page keeps human review separate from source availability and rechecks access
after rendering. No schema, model call, persisted projection or context selection
is introduced. Existing note exports and operational access/activity writes
retain their contracts.
