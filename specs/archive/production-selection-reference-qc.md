# Check every selected index reference before upload

## Kickoff

- Goal. A reviewer inspecting a production folder sees how many supplied paths match a selected file and where missing, unsafe, ambiguous or ineligible paths need attention, including records beyond the twenty-row preview.
- Expected files. The selection inspection route, streaming inspection helper, Add sources inspection control, operator guide and synthetic route and browser checks.
- Acceptance. Show missing references after record twenty and after the eighth DII image in one block. Compare only the selected production root. Repeated paths, unsupported items, case variants, malformed manifests and oversized inputs have explicit outcomes. The normal scanner, final validation and upload receipt still apply.
- Issue. [Volume import #279](https://github.com/Exculpata/exculpata/issues/279).

## Design

Send the current selection's relative names and upload eligibility with the
chosen index. Keep the manifest bounded by the existing selection and metadata
limits. Read the index bytes once into the normal inspection staging area and
scan and validate those exact bytes. The configured DAT path roles and the
existing OPT, LFP and DII parsers supply each reference. Count and compare
every reference against the same selected root, with bounded finding examples
and exact totals. A directory name or embedded reference never permits reading
another file. Files not mentioned by this index might be native files or text
belonging to another index.

The existing raw inspection request remains supported. A malformed or
incomplete inventory cannot yield a clean coverage claim. Explicitly identify
name-only findings and keep content verification distinct from a name match.
