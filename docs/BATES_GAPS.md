# Bates gap fallback

`compute_bates_gaps` is pure and unwired; facts cite document and version IDs.
Plain labels match `[A-Za-z]+[0-9]{1,18}` exactly. Plain ranges have the same
exact prefix and increasing or equal endpoints. Only their numbers are read.
A label outside a series may name it when its NFKC form contains an ASCII digit
and its case-folded letters contain the folded series prefix, or it has no letters.
Marks, suffixes, other case and bare digits therefore make every gap in that
series Possible, with cited notices for gaps outside its observed span.
This deliberately sacrifices precision until 378b: no shape whitelist can let
an ambiguous label create certain Missing pages. Unrelated labels preserve Missing.
Raw invisible characters or non-ASCII after NFKC produce one ALL notice.
Incomplete documents, uncertain page counts (including ranges), and unequal
bracket widths also prevent certain Missing. Reversed or mixed-prefix ranges
block their series. The API raises beyond 100,000 facts; it never truncates.
