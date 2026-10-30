# Case exit and purge

Run `make formal-case-exit-purge`, then `make formal-case-exit-purge-mutants`.
Use Java 21 on PATH and TLA2TOOLS_JAR pointing to the supplied TLA+ tools jar,
identified by SHA-256
`fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`.
The task labels it v1.8.0; its actual banner is TLC2 2026.10.28.014557,
revision 56d37a9. The runner verifies bytes, fails on missing tools, and keeps
all generated files in a disposable directory. Each mutant checks only its
named invariant and must produce that exact violation, not just a failing exit.

The model is synchronized with supplied base 0a06579. Each action/variable cites
its implementation. S1, S1b, S1c and S2-S6 are safety properties; L1 checks progress
under weak fairness of completion, reported failure or interruption recovery.
Fairness assumes an executing service or recovery process eventually reports an
outcome. Merely issuing a form does not promise a claim or automatic deletion.

Bounds: one durable matter, at most two exit identities, two modes, one current
form with absent/fresh/expired states, two possible purge identities (the second
only in the retry mutant), Boolean receipt/digest/input abstractions, and one
independent sequential ClosureStore run. Retries, reissues and restarts may loop
without increasing state. WindowEnds crosses the seven-day deadline; expiry
crosses the ten-minute form deadline. These are ordering abstractions, not equal
time units. Exact endpoints are checked by the Python traces. Cancellation plus
BeginExit resets the window and allocates a different bounded exit identity.

Durable repository actions each represent one transaction, including claim's
confirmation consumption, authority record and purging state. Startup form
invalidation and interrupted-purge recovery are separate actions/transactions.
Admission fields remember checks at claim time: subsequent expiry or restart
does not revoke an already consumed form or erase the durable purge identity.
Failed purge requires a new form; preserved export authority supports retry.
Receipt means trusted full-archive registration plus completed delivery in this
repository, not a ClosureStore receipt. CompleteDelivery abstracts the committed
result of a successfully begun, fully delivered download; transport and archive
verification themselves remain assumptions. Re-registration clears delivery/forms.

ClosureStore is a separate in-memory API, not a phase of durable claim_purge.
It is modeled under sequential calls, not as a thread-safe transactional store.
Stage indices 0..8 mean REQUESTED, EXPORTING, VERIFYING_EXPORTS, ARCHIVE_HANDOFF,
ARCHIVE_VERIFIED, DISABLING_ACCESS, REMOVING_INDEXES, PURGING_DERIVED_DATA,
COMPLETED; 9 is CANCELLED and -1 is no run. Only verify_archive supplies stage 4.
The history variable checks order independently from the transition table.

`tests/test_case_exit_purge_conformance.py` replays allowed and forbidden traces
for every safety property, both modes, exact time endpoints, real content fences,
synthetic persisted-session authority, cancellation/re-entry, restart and retry.
These are selected traces, not automated refinement proof. The model assumes
current owner/session authority, idle work and intact database transactions.
It does not prove provider authentication, cryptography, archive byte integrity,
concurrent ClosureStore calls, ingestion transitions, transport range accounting,
external storage/projection cleanup, physical erasure or backups. S6's trusted
receipt is checked through the real adapter in Python. No product code changes.

CI is intentionally deferred: although the local model runs under five minutes,
the supplied digest's banner differs from the stated release. A network-free
session cannot establish that the release download has identical bytes. Do not
add an unverified download job or relax the digest to obtain a green check.
