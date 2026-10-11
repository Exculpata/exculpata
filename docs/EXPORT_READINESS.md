# Final bundle readiness

Status: implemented for issue #28; source validation below. Deployment is an
operator action after reviewed merge.

## Staff workflow

Choose **Check export** from Work product or the final-export opportunity on
Close matter. The result shows when inspection started and whether a complete
bundle could be prepared from current saved work. A blocked Report appears by
title with an **Open** link to its ordinary Report page and a repair message.
Review source support and retain useful writing before changing unsupported
sections. Return to the check, then download and verify the resulting bundle.

A check preserves saved work and source bytes. It does not download or save an
export. Later edits, source removal or work still running can change readiness;
final download always validates again. Original source bytes remain excluded.
The page shows that remaining checks did not finish after an error, rather than
claiming that unexamined work is ready. Other failures retain the normal bundle
error and recovery text. After a failed close, affected Report titles and messages
remain visible without edit links: ordinary Report editing is unavailable once
closing has begun. The page directs staff back to Close matter for recovery.

## Shared validation and limits

`prepare_matter_work_product` in `workbench.py` is the common preparation helper
for check and download. It retains permissions, source-version checks, frozen
failed-close handling, saved-work count/byte limits, format validation and ZIP
packaging. The explicitly requested check may prepare and discard the entire
bounded artifact in memory; no archive, snapshot or export receipt is persisted.
Ordinary Work product and Close matter page loads do not prepare an export.
Existing [Report export limits](REPORT_EXPORTS.md) still apply.

Inspection collects Report errors from the existing bounded snapshot (at most
500 Reports, 10,000 section/source-reference rows and 32 MiB of Report metadata).
It stops before later packaging if Reports fail; download retains its original
first-error behavior. A successful preview provides no cached download authority.
The result is `Cache-Control: no-store`, with HTML or explicit JSON response
containing readiness, inspection time, problem, Report links and download URL.
A Report’s `url` is empty when repair navigation is unavailable after failed close.

The existing matter-response export lease blocks closure during inspection and
remains held through response delivery, with cleanup on success and failure.
Membership is checked again after preparation before results are returned.
Failed-close checks retain the existing owner/administrator boundary and frozen
catalog rules; they never reopen quarantined processing state. Inspection does
not edit source, Report, membership, assignment, lifecycle or deletion state.
Its content-minimized audit records only completion/attention and problem count;
normal temporary activity bookkeeping remains within the existing boundary.

## Validation and recovery

Generated HTTP regressions first reproduced the absent check route. Fourteen
new cases cover ready and blocked results, multiple Report repair links,
preserving written sections through normal repair and final export, late source
removal, four capacity limits, packaging failures, refusal of concurrent closure,
Report revision preservation, failed-close recovery with and without citations,
foreign-owner canaries, and access revoked after packaging. The failed-close
regression follows rendered actions, reproducing the former Report-link 404 and
verifying that Close matter recovery opens while quarantined sources stay closed. The new and existing
Report-bundle tests pass together: **34 passed**.

Real Chrome acceptance (`scripts/browser-accept-reports-bundle.py
--verify-readiness`) passes all **12 workflows**. It uploads generated material,
opens exact cited support, edits and finalizes a Report, checks readiness without
a download, removes its source, follows the blocked Report link, retains the
written conclusion while removing the unsupported section, checks again and
verifies the current Report in the final ZIP. It also covers foreign-owner denial,
ordinary download failure and final owner closure with external originals intact.
A generated closure failure also verifies the blocked result without Report edit
links, opens Close matter recovery and completes a deliberate deletion retry.
Ready, blocked and failed-close pages were visually checked at 1440 and 430 pixels without
page overflow. The ordinary seven-flow mode remains available.

No schema, source-registry, dependency or stored export-format change is included.
Source rollback removes the check while retaining current exports and saved work;
no migration or state repair is required. Preview performs bounded full export
work, so it costs roughly a download's preparation and is not a cheap background
health check. Repair remains the user's decision; it never removes work or source
references automatically. `make check` passes **1059 application tests** with nine optional skips and all
**194 transcription tests**, compilation, both Compose graphs and publication
inspection. Follow the [review policy](QUALITY_GATES.md#review-policy) before merge.
