# Discovery review: Production documents

Production documents is a section under **Review**. It lists every item in the
matter, opens the selected item beside the list, and moves between items from
the keyboard. Everything is read from this install; nothing is sent elsewhere.

## Reading the list

- Each row is one stored item. The standard columns lead with Begin Bates, End
  Bates, Team ID, name, folder, type, pages, custodian and date.
- Bates numbers, custodians and dates come only from a load file and are shown
  exactly as produced. Nothing is computed or guessed. An item with neither
  produced Bates nor a team identifier shows **No identifier**.
- A matter of plain file and folder uploads lists by folder and name; the
  empty Bates columns stay hidden.
- A standard column that no item has a value for is hidden, and the page says
  which ones. **Show anyway** brings them back.
- Click a header to sort. Bates and names sort naturally (`ABC2` before
  `ABC10`) and blanks sort last. The filter box matches the shown columns and
  the file name.

## Opening an item

Select a row or press `j`/`k`. The item opens beside the list with up to three
views:

| View | Shows | Offered when |
| --- | --- | --- |
| Document | A supported stored image or PDF, or explicit production page metadata | A native preview or production page record is available |
| Text | Text extracted by the product, one page or section at a time | Extracted text exists |
| Fields | The item's fields, grouped | Always |

Fields shows the chosen fields first, then every load-file field for the item
as produced, then the remaining fields under **More fields**. The Email group
appears only on email items. **Open source page** leads to the full source
page for notes and questions.

Keys: `j`/`↓` next, `k`/`↑` previous, `d` document, `t` text, `f` fields,
`Space` and `Shift+Space` page through text, `/` filter, `c` columns and
fields. Keys are off while typing in a box.

## Load files

Add the production's `.csv` or `.tsv` load file to the matter with its files.
A file counts as a load file when its header row has a recognised path column
or Begin Bates column. Each row attaches to one item:

1. by exact path (either slash direction, any letter case);
2. else by file name, when exactly one item has that name under that path;
3. else by Begin Bates, when exactly one file is named for it.

A row that could name two items attaches to neither. The page reports how many
rows matched, and each item shows the load file, line and how it matched.
Rows past the first for one item are counted and not used.

### Saved load files

Use **Choose files** or **Choose a folder** on the workbench's Add sources
surface to add supported `.dat`, `.opt`, `.lfp`, and `.dii` files, including
uppercase suffixes. Load-file originals are saved without searchable text.
Production documents imports supported DAT/OPT metadata separately; admission
of LFP/DII is available before their interpretation. Metadata import does not make the
load file searchable. Original bytes and their digests are retained. Raw load
files have no byte preview or Text view, and their contents cannot be opened
through the source-content route. Authorized preservation archives retain the originals, and ordinary
matter deletion removes them.

DAT support requires at least two nonempty, distinct column headers and one data record, U+0014 field separators, optional thorn (þ) quoting, and the same number of fields in every record. OPT support requires no header, exactly seven comma-separated fields per record, and at least one nonblank record; quoted commas are not supported. Supported encodings are UTF-8 with an optional BOM, UTF-16 LE or BE with a BOM, and Windows-1252 detected from the first 64 KiB; later decoding errors are rejected. Each record must fit the 1 MiB record limit.

DAT header names are arbitrary; no particular Bates or path columns are
required. Windows-1252 detection is a heuristic: a file whose first 64 KiB is
valid UTF-8 can still be rejected for incompatible bytes later in the file.
Malformed, empty, header-only, binary, and oversized inputs are rejected in
full. The configured document-size limit shown beside the intake explanation
applies to the entire file; its default is 25 MiB. No content is truncated to
fit a limit. References inside load files remain data and are never opened.

Selection review checks names, declared sizes, and scanner availability; it
does not attest file readability or a completed malware scan. Full content
validation and any required scanning occur after transfer. The extended policy
requires scans for DAT/OPT/LFP/DII, as does the all-files policy. Missing, unavailable,
failed, or inconclusive required scanning blocks admission. With scanning
disabled, files are not scanned for malware. Neither valid structure nor a
clean scan makes arbitrary text safe to preview or execute.

Completed load files count as saved and stored-only, with no extracted or
searchable units. They remain visible as exclusions from searchable coverage.
A matter containing only these files needs searchable sources before search
and questions become available. Failed or active work retains its own status.
Structural rejection shows the supported subset and an actionable diagnostic;
the same complete explanation remains available after refresh and in the
selected-file receipt.

Registered-folder ingestion still supports only PDF, DOCX, and TXT. The legacy
pilot selector remains limited to PDF and TXT; its default extended scanning
policy does not provide DAT/OPT/LFP/DII admission without a scanner. Browser-selected
folders do not expand ZIP archives, and ZIP files are not admitted.

### Proposed LFP and DII admission subset

**PROPOSED subset, approved by the owner 2026-11-14T12:08.** These deliberately narrow, generated dialects
are supported for protected intake; they do not establish compatibility with all
LFP or DII producers. A format suffix makes a source eligible for validation.
Recognition examines content and complete validation rejects a malformed or
binary tail after a valid prefix. Selecting a file, passing preflight, or receiving
a clean scan does not certify metadata associations or rendering.

LFP accepts one `IM,key,boundary,0,location` command per physical line, optionally
followed by `,rotation`. The literal key is nonempty and at most 70 characters.
Boundary is exactly `D`, empty, or one ASCII space; `D` declares a document start.
Offset is exactly `0`; nonzero offsets and project-specific boundary letters are
unsupported. Location is either `filename;type` or
`@volume;relative-directory;filename;type`, with type exactly `2` or `4`. Rotation
is omitted or exactly `0`, `1`, `2`, or `3`; retaining it does not apply a rotation
to an image. There is no quoting or escaping. Commas/semicolons cannot be escaped
inside components. Other commands and blank/comment lines are outside this subset.

DII begins with exactly `@FULLTEXT DOC`. Each block starts with `@T ` and a literal
identifier, then requires one explicit `@BATESBEG ` value. The block ends before
the next valid `@T ` start or at EOF; no `@END` or `<<DOC>>` boundary is accepted.
Optional singleton metadata is `@DOCID`, `@PARENTID`, and either `@PGCOUNT` or
`@C PGCOUNT`. A supplied count is 1–19 ASCII digits with value 1 through
9,223,372,036,854,775,807; its spelling, including leading zeroes, is preserved.
Both count spellings in one block are a duplicate even if they agree. Repeated
metadata, unknown tags, multiline values, and metadata after image entries are
rejected. Metadata is followed by exactly one `@D @I\relative\directory\`
declaration (or `@D @I\` for the production root) and at least one bare safe image
filename per line with suffix `.tif`, `.tiff`, `.png`, `.jpg`, or `.jpeg` (ASCII
case-insensitive). This image-suffix restriction is the proposed local subset,
not a claim that every DII producer uses it. The image entries are counted incrementally. A supplied count
does not expand into entries, pages, or Bates labels.

These inputs use the same supported encodings and 1 MiB source-byte physical-record
limit as the other load files, with an additional 1 MiB decoded UTF-8 record limit.
DII scalar values and retained block metadata are separately
bounded at 1 MiB, while a document can contain many individually valid streamed
image entries. A valid prefix never permits decoding errors, unsupported controls,
executable/archive signatures, malformed later blocks, or oversized values to be
accepted. Complete admission rejects any unsupported record; diagnostic recovery
in a later interpretation step is not permission to admit a damaged upload.

Every supplied image/volume/directory reference remains inert data. Absolute,
drive, UNC, URI, traversal and expansion forms are refused; no path is opened,
resolved, joined to a filesystem root, or fetched over a network. `@I\` is part
of the proposed DII notation, not filesystem authority. Admitted originals keep
their exact bytes and hashes and remain protected from GET, HEAD and Range byte
previews. LFP/DII admission creates no text units, searchable coverage, family,
page-label, image-decoding or association claim. DAT remains the family authority;
DII interpretation, when supplied, is document metadata only and never invents
per-page Bates from `@T`, a filename, or a declared count.

The proposed subset follows the public LFP overview,
its Import a New Page command reference, and the supported-format
reference for document-oriented DII records. The accepted syntax and
compatibility limits are documented in the grammar proposal.
The mandatory DII document mode/Bates tag, exact location forms, and excluded
offsets, commands, quoting and multiline values are explicit compatibility limits.

## DAT metadata, explicit page records and families

DAT inspection preserves every supplied header and cell, including long values,
unknown columns, repeated locators and recoverable rejected records. Select the
DAT source and open Fields to inspect raw records and exact accepted, rejected,
matched, unmatched and conflicted totals. Unmatched rows do not create source
items. Pending or failed metadata remains visibly unknown until a complete,
validated import is published.

Associations require an unambiguous admitted production root and intake
provenance. Matching checks supplied paths, produced Bates and control numbers
together. A path cannot override a contradictory identifier, and a Team ID is
never an import match key. Ambiguous or unproven matches remain inspectable and
unassociated. Adding an admitted source can resolve a formerly unmatched row.

Equivalent repeated DAT rows retain every locator. Unequal competing rows do
not select a winner or merge complementary values: their whole DAT association
is withheld with a cited conflict. Conflicting CSV or TSV metadata likewise
withholds the DAT association. Imported identifiers and Team IDs remain separate;
a transaction refuses a conflicting association or assignment without changing
either supplied record.

OPT Document inspection shows the literal supplied page label, volume, inert
path, source version, starting line and record ordinal. Supplied page counts
and observed record counts remain distinct. An ordinal is a record position,
never a Bates number. Previous and Next traverse stored records without a
million-record cutoff, including records visible only on the OPT source.
Expired generation links require reloading rather than silently rebinding.

Only explicit, valid OPT document groups can associate through an unambiguous
accepted DAT class. Rejected records break group continuity. No OPT means no
per-page Bates labels; labels are never expanded from a range or page count.
A uniquely admitted JPEG or PNG can appear beside its explicit record. Missing
images, TIFF images without rendering support, and ambiguous references show
metadata with an explanation. A separately available PDF does not establish
correspondence between its pages and OPT records. OPT page labels do not enter
Text, ordinary Fields values or list columns.

Families use conservative two-level declarations. The declaring parent and
terminal member must be unique, both endpoints must be supplied, and members
must actually declare the same valid pair. Merely falling numerically inside a
range does not establish membership. Conflicts, overlaps, missing endpoints,
unsupported comparisons and cycles remain visible without guessed ancestry.
The stored index sorts groups by the parent's selected value and retains
parent-first member order across bounded pages. Filtered child matches show
parent context without counting it as another match or identifier target.

Preservation includes original bytes, raw metadata and every rejected locator.
Derived index state can be rebuilt. Matter deletion removes preserved production
records through the existing lifecycle authority; frozen matters refuse new
writes. A failed or incomplete import never certifies complete coverage.

## Team identifiers

Items that arrive without Bates numbers can be given the team's own
identifiers. Open **Columns and fields**, then under **Team identifiers** enter
a prefix, a starting number and the number of digits, and assign.

- Items are numbered in the order the list shows at that moment, so sort and
  filter first. Only items without a team identifier are numbered, and items
  with produced Bates are skipped unless you include them.
- A team identifier appears in the **Team ID** column and in the item heading
  when the item has no produced Bates. It is never shown as Bates and is not
  written onto the document or its file.
- No two items in a matter share an identifier. If any number in the batch is
  already a team identifier or a produced Bates number in the matter, nothing
  is assigned and the page says so.
- **Remove team identifiers** clears them from the items listed now, after a
  confirmation tick.

Team identifiers are the team's work product. They are kept in preservation
archives and deleted with the matter.

## Changing what shows

- **Your own view.** Open **Columns and fields**, tick and order the columns
  and the fields shown first on each item, and save. It applies to you on this
  matter. Load-file headers found in the matter are offered as columns too.
- **The matter's starting view.** Save for everyone on the matter. Reviewers
  without their own view start from it.
- **Reset** removes the saved view and returns to the next one in line: your
  view, then the matter's view, then the standard set.
- **The standard set** for an install is
  `src/case_intelligence/config/discovery_review_fields.json`. It defines each
  field, its group, the load-file headers that feed it, the standard columns
  and the standard item fields. To have a vendor's header fill Custodian, add
  the header to that field's `load_file_headers`. To add a field, add an entry
  with a group and its headers. No code changes. A malformed file stops
  startup with the reason.

Saved views store field keys only. They are deleted with the matter and are
not part of preservation archives.

## Review codes

Any active matter member can add labels under **Review codes**. Labels are
trimmed, unique within the matter (including retired labels), and at most 80
characters. Codes stay in creation order. Retiring hides a code's button and
preserves its history and existing assignments in the Coding field.

On the open item, click a code to apply or remove it. Keys `1` through `9`
toggle codes with those stored optional shortcuts when not typing in a field.
Choose a digit when adding a code; active codes in a matter cannot share a digit.
Retirement never moves another code to a different key.
The buttons also work as ordinary form posts without script. **Coding** is
included in the default columns and item fields; the empty default column stays
hidden until an item has a code. Retired labels show "(retired)". Fields shows
who applied each code and when, with local time when script is available.
Applying or removing a code preserves the filter, sort, item, view and text page.

Each actual apply or remove appends an event with the signed-in member and UTC
time. Repeating the current state stores nothing. The latest event decides the
current state. Definitions and events are preserved in archives and deleted
only with the matter.


## Stored list index

List and item reads advance a rebuildable matter-local projection. A GET attempts
at most 128 steps, each limited to 256 logical row touches, including reads,
parser records and mutations. Progress is persisted with a versioned phase
cursor. There is no index background execution; use the pending notice's refresh
link to continue the requested view. Progress survives interrupted requests.
A new source-store epoch starts bounded reconciliation and reuses verified caches.
DAT/OPT work and index publication share that request budget. Committed source
notifications restart bounded validation of production dependencies; an unchanged
production root retains its matching generation. Completed production generations
participate in item fingerprints and both sorted and pending list cursors.
The existing generic index columns retain field presence and separate group and
member keys. List reads fetch displayed values; only the selected item loads its
complete field payload, so undisplayed wide DAT cells do not fill a page-sized
Python collection.

Until every phase finishes, the list shows every source in document identity
order, with exact indexed, pending and error coverage. The requested sort and
filter remain pending. Unknown metadata reads "Metadata pending"; failed
projections read "Metadata unavailable: indexing error". These labels are
presentation state, separate from source values. Completed empty and absent
fields retain their usual wording and parser diagnostics remain visible.

Completed lists use authenticated, matter- and generation-scoped keyset pages.
Changed sources or human work restart old navigation. Numeric deep links restart
at the beginning. Literal substring filters examine one candidate window per
page, including empty windows. Coverage counts distinguish examined candidates
and matches from the unexamined population; reverse navigation does not count
items twice. Selected columns and the heading title define the search fields.

Identifier assignment and clearing still evaluate their complete requested order
and filter through the existing reader, including items beyond displayed scan
windows. The projection only reads identifiers and Coding events. The four index
tables are excluded from preservation archives and deleted during matter purge;
identifiers and review-code events retain their existing preservation treatment.

The step budget bounds logical row work, not bytes, database-plan cost or elapsed
time. Large individual values, source integrity reads and decoding, Coding change
aggregates, and equal-prefix sort buckets can cost more. Cold source-store opening
still loads the registry. Completion within one GET is guaranteed only when the
stable target's work and preparation boundaries fit the documented 128-step
work envelope. Large matters and heavily coded items may need further reads.
The million-item 300 ms qualification is opt-in and separate from CI correctness.

While production records are still indexing, the list answers with a visible “Indexing, reload shortly.” panel instead of a conflict.
After three restarts without completion, further source changes pause that attempt with “Indexing paused: the sources keep changing. Reload to resume.”; only its Reload link resumes work and grants three more restarts.
