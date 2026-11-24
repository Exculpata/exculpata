# Discovery review integrity repair

This is the implementation record for the approved repair of #500, with
regression baseline `e6644a225f75c2cedbf3e03a17bd0205e6a006e3`.
The round's kickoff was recorded in its PR body draft before implementation;
this file does not replace the original branch's first-commit kickoff.

## Goal and acceptance

Restore complete produced metadata, collision-safe Team IDs, current write
authority, purge-safe index retirement and persistent view navigation. Preserve
original files and produced Bates strings. Each of the eight defects needs a
behavioral failure on the baseline and a passing repair. Focused integration,
storage replay and rollback, archive, restore, browser and repository checks
provide the acceptance evidence. No main merge or dependent-draft update belongs
in this round. The originating request is direct; related PR: #500.

## Boundaries and risks

- The borrowed database owns BEGIN IMMEDIATE, current membership, mutation and
  commit or rollback. Repositories require authority and actors. Audit follows
  commit before response-time membership validation; audit is a separate write.
- Purge retires an opaque matter identity permanently for the process. Pending
  builds fail at publication, and later requests and LRU eviction cannot restore
  it. Restoring the same identity does not remove the retirement marker.
- Accepted load-file and email values remain complete. Existing parsing and byte
  safeguards refuse incomplete input explicitly. Duplicate labels reserve all
  original spellings; appended display ordinals do not shorten accepted headers.
- Migration 0047 enforces ASCII Team IDs and matter-scoped NOCASE uniqueness.
  Unsupported or conflicting legacy values stop upgrade with explicit rollback;
  nothing is renamed or deleted. Validation must also work for frozen matters.
- Produced range comparison covers all items and all parsed rows, including
  unmatched rows. Supported endpoints share a case-insensitive prefix and an
  equal-width ascending ASCII decimal suffix. Comparison never creates displayed
  Bates values. Unsupported unequal ranges refuse assignment. Source revision is
  checked under the source guard before the authorized database transaction.
- Natural and numeric sorting use comparison-only decimal keys without integer
  conversion. Nondecimal text remains sortable; genuinely missing values go last.
- A valid URL view wins over session preference and then the server default.
  Row selection, history, page edges and failure fallbacks retain that preference,
  even when an item temporarily displays an available fallback.

The changes operate at code and storage boundaries, without a new product mode,
evidence normalization, guard exception or dependency. All regression inputs are
synthetic. The 17 nested-pytest baseline failures are outside this repair; the
full-suite bar is evaluated after the runner inheritance fix or on an unaffected
lane. They are not skipped or patched here.
