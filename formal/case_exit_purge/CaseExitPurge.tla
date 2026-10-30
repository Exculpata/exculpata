------------------------- MODULE CaseExitPurge -------------------------
EXTENDS Integers, Sequences, TLC
CONSTANTS MaxExits, Mutant
Modes == {"export_exit", "delete_without_export"}
(* All file references are relative to src/case_intelligence/.
   r fields: state: workspace_store.py matter_lifecycle;
   purge: case_lifecycle.py claim_purge;
   exit/mode: case_lifecycle.py begin_exit; elapsed: _ready's eligible_after;
   export/digest: register_export; receipt: case_downloads.py complete/receipt;
   token/tokenMode/tokenExit: issue_confirmation/discard_confirmations_on_startup.
   begunMode/cancelled/firstPurge are history of begin_exit/cancel_exit/claim_purge.
   admission records claim_purge's checked inputs at transaction commit, not a
   live form after consumption; it abstracts workbench_case_purge_authority.
   c fields: stage/path/verified: lifecycle.py ClosureStore request/advance/
   verify_archive/cancel; path is history, not an extra implementation guard. *)
VARIABLES r, c
vars == <<r, c>>
EmptyAdmission == [mode |-> "none", exit |-> 0, export |-> FALSE,
    digest |-> FALSE, receipt |-> FALSE, elapsed |-> FALSE, fresh |-> FALSE,
    bound |-> FALSE, ack |-> FALSE, name |-> FALSE]
Init ==
    /\ r = [state |-> "active", exit |-> 0, mode |-> "none",
        begunMode |-> "none", elapsed |-> FALSE, export |-> FALSE,
        digest |-> FALSE, receipt |-> FALSE, token |-> "none",
        tokenMode |-> "none", tokenExit |-> 0, purge |-> 0, firstPurge |-> 0,
        cancelled |-> {}, admission |-> EmptyAdmission]
    /\ c = [stage |-> -1, path |-> <<>>, verified |-> FALSE]
(* case_lifecycle.py begin_exit: one BEGIN IMMEDIATE transaction. *)
BeginExit(m) ==
    /\ r.state = "active" /\ r.exit < MaxExits
    /\ r' = [r EXCEPT !.state = "closing", !.exit = @ + 1,
        !.mode = m, !.begunMode = m, !.elapsed = FALSE,
        !.export = FALSE, !.digest = FALSE, !.receipt = FALSE,
        !.token = "none", !.admission = EmptyAdmission]
    /\ UNCHANGED c
(* case_lifecycle.py cancel_exit: invalidation and reopening commit together. *)
CancelExit ==
    /\ r.state = "closing"
    /\ r' = [r EXCEPT !.state = "active", !.cancelled = @ \cup {r.exit},
        !.export = FALSE, !.digest = FALSE, !.receipt = FALSE, !.token = "none"]
    /\ UNCHANGED c
(* case_lifecycle.py register_export: trusted service supplies validated digests. *)
RegisterExport ==
    /\ r.state = "closing" /\ r.mode = "export_exit"
    /\ r' = [r EXCEPT !.export = TRUE, !.digest = TRUE,
        !.receipt = FALSE, !.token = "none"] /\ UNCHANGED c
(* case_downloads.py complete: abstract successful full delivery receipt commit. *)
CompleteDelivery ==
    /\ r.state = "closing" /\ r.export /\ ~r.receipt
    /\ r' = [r EXCEPT !.receipt = TRUE] /\ UNCHANGED c
(* case_lifecycle.py _ready: environment crosses eligible_after (seven days). *)
WindowEnds ==
    /\ r.state = "closing" /\ ~r.elapsed
    /\ r' = [r EXCEPT !.elapsed = TRUE] /\ UNCHANGED c
(* case_lifecycle.py claim_purge: environment crosses nonce expires_at (10 min). *)
ExpireConfirmation ==
    /\ r.token = "fresh"
    /\ r' = [r EXCEPT !.token = "expired"] /\ UNCHANGED c
(* case_lifecycle.py _ready: shared issue/claim checks, including retry authority.
   A retry retains the original receipt in durable admission, not live delivery. *)
Ready(m) ==
    /\ (r.state \in {"closing", "purge_failed"} \/
                    (Mutant = "S4" /\ r.exit \in r.cancelled))
    /\ (m = r.mode \/ Mutant = "S1c")
    /\ (r.elapsed \/ Mutant = "S2")
    /\ (m # "export_exit" \/ Mutant = "S1" \/
        (r.export /\ r.digest /\ (r.receipt \/
            (r.state = "purge_failed" /\ r.admission.receipt))))
(* case_lifecycle.py issue_confirmation: replace the exit's sole form atomically. *)
IssueConfirmation(m) ==
    /\ Ready(m)
    /\ r' = [r EXCEPT !.token = "fresh", !.tokenMode = m, !.tokenExit = r.exit]
    /\ UNCHANGED c
(* case_lifecycle.py discard_confirmations_on_startup: separate from recovery. *)
Restart ==
    /\ r.token # "none"
    /\ r' = [r EXCEPT !.token = "none"] /\ UNCHANGED c
(* case_lifecycle.py claim_purge: consume form, persist authority and identity,
   and set confirmed/purging in ONE transaction. ack is no_backup_acknowledged;
   name is exact-name equality. Export acknowledgment/snapshot match are assumed. *)
ClaimPurge(m, ack, name) ==
    /\ Ready(m)
    /\ (m # "delete_without_export" \/ ack \/ Mutant = "S1b") /\ name
    /\ ((r.token = "fresh" /\ r.tokenExit = r.exit /\ r.tokenMode = m)
        \/ Mutant = "S3")
    /\ LET id == IF r.state = "purge_failed"
                 THEN IF Mutant = "S5" THEN 3 - r.purge ELSE r.purge ELSE 1
       IN r' = [r EXCEPT !.state = "purging", !.token = "none", !.purge = id,
        !.firstPurge = IF r.firstPurge = 0 THEN id ELSE @,
        !.admission = [mode |-> m, exit |-> r.exit, export |-> r.export,
          digest |-> r.digest, receipt |-> r.receipt \/
            (r.state = "purge_failed" /\ r.admission.receipt),
          elapsed |-> r.elapsed, fresh |-> r.token = "fresh",
          bound |-> r.tokenExit = r.exit /\ r.tokenMode = m,
          ack |-> ack, name |-> name]]
    /\ UNCHANGED c
(* workspace_store.py fail_matter_purge: one locked database update. *)
PurgeFail ==
    /\ r.state = "purging"
    /\ r' = [r EXCEPT !.state = "purge_failed"] /\ UNCHANGED c
(* workspace_store.py recover_interrupted_matter_purges: own startup transaction. *)
RecoverInterrupted == PurgeFail
(* workspace_store.py complete_matter_purge: control-store transaction, after
   validate_purge/purge_cleanup. External destructive phases are not atomic here. *)
PurgeComplete ==
    /\ r.state = "purging"
    /\ r' = [r EXCEPT !.state = "deleted"] /\ UNCHANGED c
(* lifecycle.py ClosureStore.request: sequential in-memory API, separate matter. *)
RequestClosure ==
    /\ c.stage = -1
    /\ c' = [c EXCEPT !.stage = 0, !.path = <<0>>] /\ UNCHANGED r
(* lifecycle.py ClosureStore.advance and _ALLOWED_CLOSURE, indices in README. *)
AdvanceClosure ==
    /\ (c.stage \in {0, 1, 2, 4, 5, 6, 7} \/ (Mutant = "S6" /\ c.stage = 3))
    /\ c' = [c EXCEPT !.stage = @ + 1, !.path = Append(@, c.stage + 1)]
    /\ UNCHANGED r
(* lifecycle.py ClosureStore.verify_archive: only trusted, bound, timely receipts. *)
VerifyArchive ==
    /\ c.stage = 3
    /\ c' = [c EXCEPT !.stage = 4, !.path = Append(@, 4), !.verified = TRUE]
    /\ UNCHANGED r
(* lifecycle.py ClosureStore.cancel: only before archive verification. *)
CancelClosure ==
    /\ c.stage \in 0..3
    /\ c' = [c EXCEPT !.stage = 9, !.path = Append(@, 9)] /\ UNCHANGED r
(* Terminal stutter, not a product transition: no user elects another exit. *)
Done ==
    /\ (r.state = "deleted" \/ (r.state = "active" /\ r.exit = MaxExits))
    /\ c.stage \in {8, 9} /\ UNCHANGED vars
Next == (\E m \in Modes: BeginExit(m) \/ IssueConfirmation(m) \/
            (\E ack, name \in BOOLEAN: ClaimPurge(m, ack, name)))
    \/ CancelExit \/ RegisterExport \/ CompleteDelivery \/ WindowEnds
    \/ ExpireConfirmation \/ Restart \/ PurgeFail \/ RecoverInterrupted
    \/ PurgeComplete \/ RequestClosure \/ AdvanceClosure \/ VerifyArchive
    \/ CancelClosure \/ Done
Admitted == r.purge # 0
(* S1: Export-mode purge needs registered export, digest and trusted receipt.
   Harm: destruction of the only preserved copy. *)
S1 == Admitted /\ r.mode = "export_exit" =>
    r.admission.export /\ r.admission.digest /\ r.admission.receipt
(* S1b: No-export purge needs acknowledgment, exact name, mode-bound fresh form,
   and elapsed window. Harm: unintended destruction without backup. *)
S1b == Admitted /\ r.mode = "delete_without_export" =>
    r.admission.ack /\ r.admission.name /\ r.admission.fresh /\
    r.admission.bound /\ r.admission.elapsed
(* S1c: Only cancel plus new exit/window changes mode; no silent fallback.
   Harm: missing exports turn into unauthorized deletion. *)
S1c == r.mode = r.begunMode /\ (Admitted => r.admission.mode = r.begunMode)
(* S2: No purge before seven-day cancellation window ends.
   Harm: loss of the opportunity to reopen and preserve data. *)
S2 == Admitted => r.admission.elapsed
(* S3: Purge needs an unexpired form for this exit; restart voids unused forms.
   Harm: stale capabilities authorize irreversible deletion. *)
S3 == Admitted => r.admission.fresh /\ r.admission.bound
(* S4: A cancelled exit can never reach purge. Harm: revoked intent deletes data. *)
S4 == Admitted => r.admission.exit \notin r.cancelled
(* S5: A purge retry reuses the same purge_id. Harm: split deletion identities. *)
S5 == Admitted => r.purge = r.firstPurge
(* S6: Stages advance in order; ARCHIVE_VERIFIED only through verify_archive.
   Harm: destructive closure proceeds without trusted preservation. *)
S6 ==
    /\ \A i \in 1..Len(c.path): c.path[i] = i - 1 \/
                (i = Len(c.path) /\ c.path[i] = 9 /\ i \in 2..5)
    /\ (c.stage \in 4..8 => c.verified)
(* L1: Fair confirmed purge eventually completes or ends in purge_failed.
   Harm: a permanently stuck deletion retains inaccessible data. *)
L1 == (r.state = "purging") ~> (r.state \in {"deleted", "purge_failed"})
Resolve == PurgeFail \/ RecoverInterrupted \/ PurgeComplete
Spec == Init /\ [][Next]_vars /\ WF_vars(Resolve)
=============================================================================
