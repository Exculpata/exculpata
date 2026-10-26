# Bates gap fallback

`compute_bates_gaps` is a pure, unwired API over `BatesDocument` values. Each
locator is a `(start, end)` pair; `end=None` is start-only. Citations retain both
`document_id` and `version_id`. No document text is accepted or scanned.

Only ASCII letter prefixes followed by 1-18 digits establish plain series.
A maximal interior hole is Missing only when bracket widths agree, no related
label or uncertain page count touches it, and every document is labeled and
completely read. A zero or absent page count is unknown. Unlabeled or partly
read documents also receive notices for gaps beyond the observed span.

- `missing`: `Missing Bates ABC0002` between plain ABC0001 and ABC0003.
- `possible`: `Possible missing Bates ABC0002 (related label or page count touches gap)` when ABC0002.001 also appears.
- `not_computed`: `Bates series ALL: not computed (unreadable character in a label)` for any invisible or non-ASCII character, including fullwidth digits and letters.

Related labels may have one ASCII separator between the folded prefix and digits;
double separators do not relate. Equal-width brackets set the gap label padding.

The `computed` status records each examined plain series. All facts are cited.
Reversed or mixed-prefix ranges disable the affected series. Unread suffixes,
punctuated labels and outside-span labels retain notices. Suffix precision is
reserved for 378b; wiring and export are reserved for 378c. More than 100,000
facts raises `OverflowError`; results are never truncated.
