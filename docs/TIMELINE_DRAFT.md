# Draft timeline

Case file → Draft timeline extracts bounded date mentions from current source
units, without a model. Every entry retains an exact quoted window and original
passage link. The draft is for attorney review; entries are not findings.

Dates support ISO, US month/day/four-digit-year, named months in either order,
month/day ranges, 12/24-hour times, approximate markers and simple next/previous-day
references. Two-digit years and missing years stay unordered. Timezones are not
inferred. Relative values use only a single unambiguous explicit date in the same
source document, preferring the same sentence. The anchor is included as a citation.
Clock times in a relative-day sentence use that resolved day. Multiple relative-day
references in one sentence and dates outside the supported calendar stay unresolved.
Retrospective wording does not inherit a document-header date. Other document-date
associations still require attorney review. Invalid calendar values are omitted.

Possible conflicts compare literal event wording after date/time removal, retaining
participants, numbers and negation. Document headers do not establish event identity.
This conservative comparison misses paraphrases and does not establish agreement.
The supplied Harbor Street corpus qualifies its 09:26 estimate but does not give two
explicit alternative arrival times. Its golden snapshot therefore does not invent
such a conflict. Dedicated synthetic regression records exercise side-by-side dates.

Limits are 2,500 units, 6,000 characters per unit and 150 entries. A capped banner
reports truncation in the workbench and every export, including drafts with no
date entries. Coverage reports passages included in the date scan and the total
passages in current ready sources. Counts include passages read for document-local
anchors; entry or text limits can still truncate a draft even when all passages
were scanned. Sources that are not ready are outside this population. Counting
streams ready passages with ceilings of 1,000 documents, 10,000 passages, 8 Mi
serialized characters, 8 Mi passage-text characters, and 65,536 characters per record.
There is no default wall-clock cutoff: the same unchanged sources produce the
same export bytes regardless of machine speed. One bounded passage lookahead
and one metadata-only document lookahead distinguish exact ceilings from a
larger population. Input/record limits apply during lookahead too; an individual
read is at most 65,536 characters. Candidate collection remains
bounded at 2,501 and date extraction at 2,500. If a ceiling is reached or source
text is missing, corrupt, or too large, the usable draft remains available with
an explicit unavailable-total warning and `capped` set. No partial count is
presented as an exact total. Entries from a source whose container fails validation
or cannot finish within the budget are discarded: its footer has not been validated.
Unreadable sources are skipped so later readable sources can contribute within
the remaining global budgets. Each gap has a named notice and a `coverage_records`
entry with `source_id`, `source_name`, and status `unreadable` or `limit`. A global
budget exhaustion stops further reading. These reads retain the
existing source/authorization snapshot, with bounded work while its locks are held.
A failed citation window is omitted and counted. Drafts recompute
on demand and are not persisted. Pure results and exports have a null generation
timestamp; only the workbench page carries its recomputation time. No migration or backup format changes are
needed. A 2,500-unit regression exercises the bounded scan budget.

Refresh runs a bounded ephemeral background task (two active jobs per application,
one per matter/actor) with running/completed/failed state. A restart drops refresh
status; opening the page still recomputes from current sources. Existing durable
research and ingestion job coordinators are specific to different workflows and are
not repurposed. No saved timeline is silently presented as current.

The export format is `exculpata-timeline-draft-v2`. CSV exports begin with a metadata row carrying the review notice, coverage notice,
capped flag, included `unit_count`, and `total_unit_count`, followed by one row per
citation. The `row_type` column distinguishes `metadata`, `coverage`, and `citation`;
consumers importing timeline citations must filter `row_type == 'citation'`.
The `format` column identifies v2, and coverage rows expose `coverage_status`.
An unavailable `total_unit_count` is null in JSON and empty in CSV; all formats
include the unavailable-coverage warning. JSON includes these counts and `coverage_notice`; Markdown prints the same coverage
notice. JSON and Markdown retain all citations.
The export response requires the existing matter response lease. CSV uses notebook
presentation/control replacement and whitespace handling before formula protection.
Format characters and Unicode line/paragraph separators become spaces. Formula
prefix detection also covers pipe/percent, fullwidth equivalents, and Unicode minus;
normalization is used only for detection, without rewriting the source representation.
Markdown is a lossy presentation: it escapes notices, coverage records, and entry
fields, including parentheses, em dashes, and middle dots within values, and
replaces every Unicode format character (including joiners, zero-width
characters, and bidi controls) with a space, including in cited excerpts. Single-line
fields also turn line breaks and Unicode line/paragraph separators into spaces and
trim trailing whitespace. Quoted description and excerpt lines trim trailing whitespace
after format characters become spaces. Empty metadata parts are omitted; conflict
groups use "None" when empty after normalization and trimming. Supplied date headings
start with "Date:"; an empty date label uses the heading "No date label" instead.
JSON is the exact source-text
representation; presentation changes never alter source digests or locators.
Promotion rechecks current source support and requires
an explicitly selected entity/revision and role, following AssertionService's existing
contract. It creates an event with Needs review status and retains date uncertainty.
All distinct supporting passages, including a date anchor on another page, are saved
in the same transaction through the existing assertion create/attach operations.
If any account fails validation or storage, the entire promotion rolls back.
Review the saved record to correct the date, attribution or entity role.
