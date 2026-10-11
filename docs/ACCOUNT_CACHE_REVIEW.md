# Account cache review

This records the bounded R6 investigation from
[independent review issue #109](https://github.com/Exculpata/exculpata/issues/109).
The reported overlayfs timestamp-test failure remains unverified on that
filesystem. It is not evidence that revocation passed or failed on a supported
Linux installation.

## Authority and reproduction

The [account repository](LOCAL_ACCOUNT_LIFECYCLE.md) writer locks, rereads,
validates and atomically replaces the account file. Password, enabled-state and
role changes rotate the account revision. The read-only cache key includes the
directory and file identities, file size, nanosecond modification/change time,
owner, mode and link count. Readers reopen and validate the protected path on
every access. No cache or storage-format change is made by this investigation.

The existing `test_same_size_edit_with_restored_mtime_reloads_cached_snapshot`
test performs an in-place edit and restores the original modification time. Its
explicit change-time assertion establishes the filesystem condition needed to
test that particular invalidation mechanism. A failure of that assertion must
be distinguished from a subsequent stale-snapshot assertion. Operators must
continue using the repository writer rather than editing account JSON by hand.
The test and its assertion remain intact.

The new `tests/test_local_account_cache_authority.py` matrix exercises the real
writer with rapid replacements and no reader access between disabling/re-enabling
or demoting/promoting. Password replacements and both round trips preserve the
final file size. The matrix uses same-process and separate-process writers,
two already-running readers and a restarted reader. Each running reader has a
different cookie, and read-only session resolution verifies each denial before
any reader can persist revocation in the shared database. It checks rotated
revisions, old-password denial, fresh sign-in and non-revival of old sessions.
The assertions do not require particular inode or timestamp changes.

## Evidence and remaining work

At baseline `a33d93b5302fc0f726f361ae2e3341883a4bdd77`, the 57 existing local
account lifecycle tests passed on Darwin arm64 with APFS. With the new matrix,
the cache-authority, lifecycle and rollback suites passed together: 80 tests.
Tests imported this checkout through `PYTHONPATH=src`. No stale authority was
observed in this local lane, and it does not justify changing the cache or
weakening the existing test.

Run the same focused command on the final candidate using each relevant Linux
storage layout, recording the filesystem type and exact failing assertion:

```console
PYTHONPATH=src python -m pytest -q tests/test_local_account_cache_authority.py tests/test_local_account_lifecycle.py tests/test_local_account_rollback.py
```

The reported overlayfs/tmpfs comparison, supported installed-filesystem
validation and exact-candidate hosted Linux results are separate evidence.
Local APFS results do not close those remaining lanes. If a real writer causes
a retained reader to accept superseded credentials or authority, preserve that
reproduction and correct the cache while retaining bounded secure opens,
immutable snapshots and writer locking.
