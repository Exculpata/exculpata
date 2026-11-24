# Discovery review: document list, open item and configurable fields

## Kickoff

- **Goal, in my own words.** Turn the Production documents page from a bare
  count into a place to review discovery: a list of the matter's items, the
  selected item open beside it, and fast movement between items. What shows
  by default is data that a team can change, not code. The list leads with
  Bates or identifier, page count, who the item came from and other load-file
  metadata. Email fields appear only on email items. Low-value housekeeping
  values stay one step away instead of in the way.
- **User-visible outcome.** From Review, a reviewer opens Production
  documents, sees every item with its Bates range and custodian when a load
  file supplies them, presses `j`/`k` to move, and reads the document, its
  extracted text or its fields in the same view. They choose their own columns
  and fields, or set a starting view for the whole matter.
- **Expected files.** `modules/discovery_review/*`, the module contract and
  mount step, one field definition under `config/`, one migration and its
  mirror, the page template and item partial, the module stylesheet and
  script, route and layer snapshots, lifecycle and architecture docs, tests.
- **Acceptance check.** `tests/test_discovery_review_list.py` and
  `tests/test_discovery_review_mount.py` pass with the full application
  suite, and the page is exercised in a browser against a synthetic
  production (list, selection, keys, views, saved view).
- **Issue link.** None. The maintainer requested this directly. It consumes
  the load-file parsing direction in #279 and does not replace that intake.

## Problem

The page mounted by the first slice states how many documents a matter holds.
Reviewers need to skim a production: many items, their identifying metadata,
and the item itself, without leaving the list for each one.

## Root cause

Sources carry no production metadata. A stored source has a name, type, size,
page count and extracted text. Bates numbers, custodians and other load-file
fields exist only inside a load file, which nothing reads for review. The
existing Production summary reads one fixed three-column CSV per request.

## Approach

1. **One field definition.** `config/discovery_review_fields.json` lists every
   field, its group, the load-file headers that feed it, whether it applies
   only to email, the default columns and the default item fields. Routes,
   templates and the script never name a field. Changing a default, adding a
   field, or teaching a field a vendor's header is an edit to that file. The
   definition is validated as a whole and a malformed one stops startup.
2. **Load-file values as produced.** Delimited load files (`.csv`, `.tsv`)
   already stored in the matter are read with their headers and values
   unchanged. A row attaches to an item by exact path, by a unique file name,
   or by a unique Bates-named file. An ambiguous row attaches to nothing. Each
   item shows the load file, line and match basis, and the page states how many
   rows matched. Nothing is written and no Bates number is computed.
3. **Derived index in memory.** The list is built from the source store and
   rebuilt when a source changes. It is view state, holds no file content, is
   dropped when the matter is deleted, and never touches originals.
4. **List and open item.** Server-rendered pages of 100 rows with natural
   Bates ordering, one filter box and sortable headers. The selected item
   opens beside the list with Document (the existing in-app PDF and image
   route), Text (stored extracted text, labelled as extracted) and Fields.
   The page works without script; the script adds selection without reload,
   keyboard movement and a sticky view choice.
5. **Saved views.** A reviewer's columns and item fields are stored per user
   and matter in `workbench_discovery_view`. A matter may hold one shared
   starting view. Order of preference: own view, matter view, the definition.

6. **Team identifiers.** Most matters are plain file and folder uploads with
   no Bates. A team can number such items with its own prefix and sequence,
   in the listed order. Identifiers live in `workbench_discovery_identifier`,
   are unique within the matter, never collide with a produced Bates number,
   are shown as Team ID and never as Bates, and do not touch the file. They
   are work product: archived with the matter and deleted with it.

## What it could break

- **Deletion and archive guards.** A new `workbench_` table must be purged and
  listed in the archive coverage map. It is deleted by a named statement and
  excluded from archives as rebuildable view state (field keys only).
- **Module contract.** `RouteHook` gains `require_csrf`, `require_csrf_header`
  and `audit`, as first designed, so later modules add no mount-step lines.
- **Mis-mapping risk.** A load-file row attached to the wrong item would put
  wrong metadata beside evidence. Matching is exact or unique only, the basis
  is shown on the item, and unmatched and surplus rows are counted.
- **Memory and time on very large matters.** The index is rebuilt in memory
  when sources change and sorted on demand. That suits matters up to the low
  hundreds of thousands of items. A persisted index is the follow-up for the
  million-item target.
- **Email headers.** Stored `.eml` sources are read for header values at index
  build. The 256 KiB read boundary refuses incomplete headers explicitly; a
  malformed header is an absent value.

## Not in this change

Concordance DAT and Opticon OPT intake (#279), per-page Bates, family trees,
review codes, a server-side page renderer for TIFF and other formats, and a
persisted index. Load-file family range values are shown as produced.

## Test that proves it

`tests/test_discovery_review_list.py`: values shown exactly as produced and
in natural Bates order; no Bates invented for unlisted items; email fields on
email items only; every load-file header reachable; ambiguous rows attach to
nothing; a swapped definition changes defaults with no code change; saved
view precedence and reset; other matters and non-members see nothing;
originals keep their digests; deletion leaves no view row or cached index.

## Integrity repair contract (#500)

The [repair design](500-fix-design.md) tightens the existing guarantees:
accepted metadata remains complete, duplicate headings receive collision-free
display ordinals, and decimal sorting never changes displayed values. Oversized
email headers are explicitly refused rather than shown as partial metadata.
Oversized, malformed, invalidly encoded or unreadable candidate load files remain
in the load-file report and refuse identifier assignment because collision safety
cannot be established. Accepted values and original files remain unchanged.
Team IDs have storage-enforced ASCII-domain, case-insensitive uniqueness; full
supported produced intervals and unmatched load-file rows participate in collision
checks. Unsupported unequal ranges refuse assignment visibly. Persistent domain
triggers abort the invalid statement, leaving rollback to the transaction owner;
migration replay replaces old trigger definitions and refuses conflicting legacy rows.
Authorization and mutation share one explicit write transaction. A purged identity stays retired
for the process, including pending builds and later restored identities. Explicit
URL view, session preference and server default define navigation precedence;
keyboard pagination and no-script links preserve the selected view. Deliberate view
changes update pagination, sorting and filter navigation before those controls are used.
