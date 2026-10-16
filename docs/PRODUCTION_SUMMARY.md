# Production summary

Choose **Summarize production** on Home for the whole matter, or on a
selected-file receipt for that receipt's currently bound source versions. The
summary links totals, type groups, dates, recorded people and organizations,
and gap notices to sources and original passages. Markdown and JSON exports
use `exculpata-production-summary-v1`. No model is needed.

Page totals count recorded PDF pages only. Text lines, email sections, table
rows and media segments are not physical pages. Valid ISO dates stated in text
and available semantic document dates contribute to the date range; ambiguous
slash dates are not put in calendar order. No source is dated from its upload
or filesystem timestamp. Recorded, non-dismissed entity mentions are counted
only while their source version remains current.

Bates checks read explicit `Bates:` labels and supplied locator metadata, group
by normalized prefix and find internal gaps and overlapping ranges. A gap
between labels does not establish why that range is absent. Reference checks
recognize explicit exhibits, attachments, record numbers and attachment filename
lines. Exact normalized filename stems are matched inside the chosen scope.
Other reference styles and names can need manual review.

Gaps retain the briefing's readability and coverage accounting, including PDF
coverage notices even when native text is searchable. The Harbor Street corpus
has 40 sources, 11 emails, two PDF pages, a date range of 2034-04-16 through
2034-04-20, and one source with no ordered date. Its two PDFs have coverage
notices; record 40 has an extraction failure.

Computations are bounded to 10,000 documents, 20,000 extracted units, 20 million
charged text characters and 100,000 accounting rows. Oversized inputs refuse a
partial summary. Ranked and gap lists show at most 100 entries and report
omitted entries. Lists initially show ten entries, with **Show all** for the
remaining displayed entries. All underlying sources remain available in the
source list. Temporary jobs retain only status, expire after 15 minutes and are
not resumed after restart; run the action again. Viewing or exporting always
reads current authorized sources rather than retaining generated source text.

Run the synthetic browser acceptance at desktop and mobile widths:

```console
.venv/bin/python scripts/browser-accept-production-summary.py \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver \
  --output /tmp/production-summary-browser
```

Eval questions are in `tests/fixtures/synthetic/workflow_eval/production_summary.json`
pending the #267 harness. The separate `production_gaps` fixture keeps the
Harbor Street corpus unchanged.
