# People, places, and things

[Events and assertions](EVIDENCE_ASSERTIONS.md) connects explicit entity roles
to separately attributed source accounts and human review decisions.

[Entity discovery and reconciliation](ENTITY_DISCOVERY.md) adds the slice-17
workflow to this manual foundation. The sections below describe slice 16.

Slice 16 introduces manually reviewed matter entities. Open **People, places &
things** from matter navigation, create a person, place, or thing, and give it a
display name, review status, and optional aliases (one per line). Separate
creations always receive separate identities, even when every label matches.
An alias is a reviewer label, not permission to merge people automatically.
No extraction model or relationship inference is introduced.

## Review original passages

From a supporting-source pane choose **Attach to entity**, or from a document's
extracted section choose **Attach passage to entity**. The entity workspace shows
the selected original excerpt. Choose an existing identity or create a separate
one with that support. Search names and aliases to locate the intended identity;
results are paginated in groups of 50 without deduplicating names. Source-review
return links retain the originating path and query. Native links and labeled
form controls support keyboard navigation.

An entity detail page lists every saved mention and links each currently
resolvable mention to its original passage. Mentions use the existing passage
unit/support-token contract: source ID and version, source name, unit number,
chunk ID, locator, excerpt digest, token, and the original excerpt (up to the
existing 6,000-character notebook support limit). These are selected passages,
not an exhaustive occurrence index or new character-range annotations. Repeated
attachment of the same support token to the same identity is idempotent; another
source unit or another identity remains distinct.

If a source changes or becomes unavailable, the saved excerpt remains labeled
historical and its live-source link is disabled. New attachment and note import
must resolve current support before saving. Reviewers can remove a mistaken
mention and attach a current one; correction history retains the original added and removed support.
A removed mention is no longer current support.

## Existing case notes and provenance

Supported person/place notes offer **Import as entity**. Import preserves the
original note, references, creator/updater metadata and existing review state.
It copies all validated references and an immutable import snapshot, then links
the still-editable original as commentary. Note text never becomes evidence.
Import is idempotent by matter/note ID, never by name: repeating it returns the
same entity and does not overwrite subsequent human corrections. Notes without
support or with any unavailable/changed reference fail without partial import.
The current notebook has no `thing` type; things can be created manually.

The entity records manual creation versus notebook import. Imported snapshots
retain the note's manual/answer/citation/extraction origin; suggested machine
work stays suggested until a reviewer changes it. Subsequent entity revisions
retain their actor, timestamp, action and label/state snapshot, with explicit
mention additions and removals. Each original excerpt is stored in history only
when added or removed, rather than copied on every later edit. Older full
snapshots remain readable; new history records declare `history_format: 2`. Changes to a
linked note do not silently update the entity. Deleting a linked note removes
that live commentary link, but its explicit import snapshot remains part of the
entity until the entity or matter is deleted.

## Shared edits and authority

The entity service accepts explicit actor and matter authority. Its repository
uses the existing workspace connection/lock and an immediate SQLite transaction,
rechecking live membership and active lifecycle before reads or writes. An
integer revision is checked before editing, attaching/removing mentions or
deleting. Independent connections cannot both save from the same revision.
Availability checks group references by document and parse each referenced
document once, without repeatedly scanning the matter corpus.
Conflicts show the saved identity and preserve submitted form text for deliberate
comparison and resubmission. Lost access never returns the submitted draft.

For source-backed changes, the source store's mutation guard spans reference
resolution and the workspace transaction's commit. Source and workspace guards
use the existing source-then-workspace ordering. Routes contain no persistence
SQL. Authentication, CSRF, audit and download deletion leases remain application
responsibilities. The workspace is for active case-team members; existing
administrator final-bundle recovery retains its explicit authority path.

## Export, retention, and migration

The individual JSON export includes identity, mentions, current source
availability and correction history. Final matter bundles include a separate
`entities/NNN-entity.json` for every entity with its retained original support and
history. Bundle support is explicitly not revalidated, including after failed
closure; it must not be interpreted as a claim that original sources remain
available. Entity work is not yet consumed by automatic Report compilation.
Original source files remain outside the final bundle, as before.

Migration `0032_entity_workspace.sql` is mirrored in operator and package SQLite
migration directories. The workspace SQLite control store is authoritative on
both SQLite and PostgreSQL retrieval profiles, so no derived PostgreSQL entity
schema is introduced. Tables are additive: entities, mentions and history.
Entity deletion cascades to mentions/history while leaving originals and case
notes intact. Matter purge explicitly deletes entities, including all retained
snapshots; the existing content-free audit/closure receipt remains. Retention
expiry uses the same purge. Automatic discovery after processing (see
[entity discovery](ENTITY_DISCOVERY.md#automatic-discovery-after-processing))
only adds **Suggested** records; it never edits a reviewed identity.

Before updating, stop writers and retain a verified consistent backup of the
control database, session key and managed source storage under the existing
[backup contract](STORAGE_AND_BACKUP.md). A clean restore must preserve entity
identities, aliases, history, original notes, source versions and resolvable
mentions. Do not run a pre-0032 application against upgraded state: its purge
code does not own these tables. Rollback uses the matching pre-upgrade code and
its verified pre-upgrade backup restored into a clean target. Export post-upgrade
work first; a pre-upgrade backup does not contain it. Never move a release tag.

## Synthetic verification

The [September 12 receipt](ENTITY_WORKSPACE_VALIDATION_2026-09-12.json) records
local outcomes and limitations. The [review policy](QUALITY_GATES.md#review-policy)
applies separately before merge.

Run the focused service and HTTP regressions:

```console
python -m pytest -q tests/test_entity_workspace.py tests/test_matter_notebook.py tests/test_notebook_conflicts.py
python scripts/entity-storage-restore-drill.py
```

The drill creates an old note using public baseline
`e11ab25ccbbde7e864914b3bb7ba56603792b8b4`, backs it up, upgrades, creates and
edits an entity with mention support, restores the upgraded control database into
a clean target and checks all records/foreign keys. It separately restores the
pre-upgrade backup and verifies the note with the original baseline reader.
The HTTP regression additionally stops a synthetic app, backs up and restores
its complete runtime/source tree to a clean target, resolves the original
mention through the restored application, verifies the final bundle, and checks
matter purge removes entity tables. These are synthetic local receipts, not an
operator's installed-node recovery acceptance.

Run the standalone Chrome journey with explicit browser and matching driver:

```console
python scripts/browser-accept-entities.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-entity-acceptance
```

The output must be absent. The runner starts a disposable loopback application
with unavailable AI and synthetic originals, uses a test-only zero storage
reserve, and retains screenshots and a success/failure receipt. It exercises
keyboard creation, two original passages, same-name separation, stale-edit
recovery, mobile reflow and search-context return. Models, deployment, extraction
(slice 17), relationships (slice 18), memory and capacity expansion are outside
this slice. Contributor alpha is not confidential-casework readiness.
