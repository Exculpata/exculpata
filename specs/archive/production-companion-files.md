# Open native and produced text files from a DAT record

## Kickoff

- Goal. A reviewer can open the native and the explicitly identified produced text from a DAT record when both files were admitted with the selected production. Each link retains the source version of the actual file.
- Expected files. The discovery review production matcher and repository/service, its record presentation and template, the field definition if needed, the operator guide, and synthetic production route tests.
- Acceptance. Upload two synthetic productions with repeated Bates labels, matching native and text filenames, and distinct bytes. Check the production record, native and text source links through the workbench. Missing, ambiguous, conflicting or foreign files must never acquire a link. Existing standalone and paired DAT, OPT, LFP and DII contracts must pass.
- Issue. [Volume import #279](https://github.com/Exculpata/exculpata/issues/279).

## Design

In a volume, the native and the extracted text can share a filename stem.
Treating a DAT-declared text companion as another candidate for document
identity can hide the actual native. The DAT roles have different meanings.
Resolve an explicit text reference against a unique, ready text file selected
in the same receipt and production root. Preserve the original field and link
to the text file using the workbench's authorized, version-specific source
route. Keep missing and ambiguous references visible.

Use the explicit companion role to disambiguate a native match only where
the admitted native is independently identified. An unrelated or contradictory
produced identifier, a second possible native, or conflicting DAT rows still
withhold the association. A text companion supplies neither document identity
nor a native page number. Originating load files, text, native files and page
images retain their own source identities and digests.

The regression risks are cross-production links, stale associations after an
upgrade, and suppressing a real identity conflict. Exercise these alongside the
normal receipt, upload, index and record-view routes.
