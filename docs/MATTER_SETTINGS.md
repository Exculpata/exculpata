# Matter settings and naming

Current contract for the matter name, updated September 5, 2026.

The matter owner or an application administrator can open **Settings → Rename
matter**, edit the name, and select **Save matter name**. All team members see
the saved name when they next navigate or refresh. An administrator can rename
without joining the team; the action is attributed to that administrator.
Ordinary members can read settings but cannot rename.

Names use the creation rules: 1–140 characters after Unicode NFC normalization
and trimming, with unsupported control/format characters rejected. Invalid
submitted text remains in the form for correction. The form is escaped normally
and fits a narrow viewport. Changing a name does not change the matter ID, URL,
owner, memberships, source files or versions, citations, conversations, saved
notes, Reports, processing jobs, or review period. New export requests use the
current name; already downloaded work product is not rewritten.

## Authorization and concurrent edits

`POST /matters/{slug}/rename` requires an authenticated owner/administrator,
valid CSRF, `name`, and `expected_name` from the displayed form. Nonmembers
receive 404; ordinary members receive 403. An already validated administrator
may use the existing explicit store-level override. Inside one SQLite immediate
transaction, the store rechecks an active principal and active matter, plus
active owner membership when not using administrator authority.

The store compares the current name with `expected_name` before saving. If it
has changed, the response is 409 with the current name, the proposed name still
in the input, and an instruction to review before submitting again. This is a
current-name comparison, not a full edit-history revision: changing a name away
and back to the displayed value does not create a conflict. Unrelated case
activity does not invalidate the form. Saving the same name makes no mutation
or additional successful rename audit event.

The name change and the content-free `matter.rename` audit commit atomically.
The event records actor, session/request, matter and owner/administrator role,
without recording old or new names. Audit failure rolls back the rename.
The operation uses existing schema; no source registry or search-index rewrite
is needed. Frozen, failed, inactive and deleted matters cannot be renamed.

[Export and exit](CASE_LIFECYCLE.md) requires a frozen closure, a verified full
preservation archive, a minimum seven-day cancellation window and fresh final
confirmation. Beginning closure and renaming share the immediate transaction
boundary. An old-name confirmation after a rename is rejected, and a closing
matter must be reopened before it can be renamed. Check-ins never authorize
deletion.

## Verification and recovery

`tests/test_matter_rename.py` covers atomic rollback, attribution, permissions,
invalid names, separate-connection stale edits, lifecycle denial, source
identity, and the rename/close race. The race was reproduced before the
transaction change. `tests/test_matter_management.py` covers the surrounding
settings, administration, export, and deletion behavior.

`scripts/browser-accept-matter-rename.py` starts only an ephemeral loopback
application with synthetic proxy identities. It uses separate Chrome sessions
for owner/admin edits, checks the ordinary-member view and mobile layout,
uploads a source, saves a source-supported note, reopens its citation after
rename, downloads a renamed matter bundle, rejects old-name closure, and
confirms deletion using the new name. Its synthetic runtime is temporary;
only requested screenshots and the content-free result are retained.

Validation on September 5: the full application suite passed **749 tests** with
**9 environment-dependent skips**; the focused rename/management suite passed
30 tests. All **5 Chrome workflow checks** passed, including desktop conflict
recovery, mobile layout, export, and deliberate synthetic closure. Publication
scan, compilation, and diff whitespace checks passed.

Run with explicit Chrome and driver paths:

```sh
PYTHONPATH=src python scripts/browser-accept-matter-rename.py \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver \
  --output /path/to/synthetic-results
```

The original rename change required no persistent-schema change. Subsequent
lifecycle migration 0040 has a separate [backup and rollback contract](CASE_LIFECYCLE.md#migration-0040-and-recovery);
older code must not open an upgraded store. No automatic deployment is part of
these changes.
