# Inspect a selected production before confirming upload

## Kickoff

- Goal. From Add sources, a reviewer can inspect a selected DAT, OPT, LFP or DII index, see its detected format, sample records and reference findings, then choose whether to upload the folder.
- Expected files. A bounded load-file inspection helper, a workbench route, the existing upload selection interface, synthetic route and selection tests, and the operator guide.
- Acceptance. A synthetic folder with each supported format can be inspected before the first receipt or admitted source exists. Verify the configured scanner and size policy, the first twenty logical records, missing or ambiguous selected paths, authorization, immutable file selection and the ordinary confirmation path.
- Issue. [Volume import #279](https://github.com/Exculpata/exculpata/issues/279).

## Design

The existing preflight uses the selected filenames and sizes. Add a separate
inspection action for a load file in the current selection. The workbench
applies the existing upload byte limit, scanning and structural validation to
an ephemeral copy. The importer parsers supply record boundaries and format
semantics. Show a bounded, escaped sample and complete record counts. Compare
inert references with selected relative names. A successful inspection says
nothing about the bytes of companion files or their future admission.

Discard temporary files when the request ends. A user who cancels inspection
or switches selections must never receive stale results. Keep the ordinary
upload receipt, scanner and final validation authoritative for every uploaded
file. Verify that inspection creates no matter source or intake receipt.
