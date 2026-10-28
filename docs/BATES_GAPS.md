# Bates gap precision

`compute_bates_gaps` is pure and unwired; facts cite document and version IDs.
Plain labels match `[A-Za-z]+[0-9]{1,18}` exactly. Plain ranges have the same
exact prefix and increasing or equal endpoints. Only their numbers are read.
A label outside a series may name it when its NFKC form contains an ASCII digit
and its case-folded letters contain the folded series prefix, or it has no letters.
For each such label, concatenate its NFKC ASCII digits. Its reach contains the
integer value of every contiguous substring of up to 18 digits. Marks and suffixes
insert characters but do not remove digits, so this conservatively includes
possible numeric readings. Each value n covers n through n + page_count - 1;
an unknown page count, including 0 or less, extends to the series maximum.
For a range with a non-plain end, combine both ends' reach (a plain end contributes
its own number and remains a member). Cover the interval from the minimum through
maximum + page_count - 1, or the series maximum
when the count is unknown. The tradeoff is that leading zeros include 0 in the
reach, making range intervals wide: safe, but less precise.
A begin-only NFKC label exactly matching prefix, optional space, digits, hyphen,
digits reaches start through end + page_count - 1 only when both runs match the
series' plain-label digit width and start <= end; unknown counts stay open.
All other multi-run labels keep the substring reach above.

Within each gap, covered runs are Possible with "label may name series" and cite
the bracket documents plus only the documents whose reach covers that run.
Uncovered runs remain Missing unless a whole-gap uncertainty reason applies.
Outside notices cite only documents whose reach or known page count leaves the
observed span. More than 24 digits falls back to every gap Possible for that series.
A digitless label whose folded letters contain the series prefix also makes every
gap in that series Possible, citing that document. It never starts a new series;
spelled-out and Roman numerals are not parsed. Unrelated labels preserve Missing.
Every non-plain label receives a "Bates labels not read" notice.
Raw invisible characters or non-ASCII after NFKC produce one ALL notice.
Incomplete documents, plain page counts touching a gap (including ranges), and unequal
bracket widths also prevent certain Missing. Reversed or mixed-prefix ranges
block their series. The API raises beyond 100,000 facts; it never truncates.
