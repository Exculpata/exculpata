# Harbor Street demo records

`harbor-street/` contains 40 original, fully synthetic records created for the
Exculpata demo: 8 reports, 6 interview accounts, 11 notes, 11 emails, a CSV,
2 native-text PDFs, and one deliberately unreadable text record. These files
were written for this repository and are covered by its Apache-2.0 license.
No real case, person, organization, correspondence or source document was used.
All names, places, dates, identifiers and events were invented. Email addresses
use the reserved `example.test` domain.

The fictional matter concerns conflicting accounts of a teal lantern parcel's
transfer. Vela Quenrix, Nerin Talvex, Sovi Pellune and Ilex Rovanni recur across
records from Brindlequay Storehouse and Kestrelune Arcade. The invented timeline
runs from April 16 through April 20, 2034, with most records concerning April 18.
The accounts deliberately leave parcel identity, route and timing unresolved.
Reports distinguish direct observations from plans, estimates and later checks.

The PDFs contain only native text, generated with `pypdf` using the standard
Helvetica font. Their metadata contains only synthetic titles and the author
`RecordBench synthetic demo`; they contain no scans, attachments or imported
assets. No audio is included: the rights in
[`docs/assets/MEDIA_RIGHTS.md`](../docs/assets/MEDIA_RIGHTS.md) cover complete
walkthrough videos, not a separately licensed synthetic audio record for this
corpus.

`unreadable/40-damaged-transfer-note.txt` contains an explanatory synthetic
sentence and one intentional NUL byte. The normal intake accepts the source and
the text extractor rejects its binary content. This demonstrates source
accounting and the briefing's unreadable-record section without hiding data
inside an encrypted or malformed PDF. The publication sanitizer can inspect
every byte of the file.

This provenance note sits outside `harbor-street/` so it is not ingested as a
matter record. The demo uses the checked-in records directly and never generates
or downloads additional evidence at startup.
