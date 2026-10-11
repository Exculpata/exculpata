# 09: Extend exact search with review-critical operators

Status: proximity is on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#45](https://github.com/Exculpata/exculpata/pull/45) /
[#53](https://github.com/Exculpata/exculpata/pull/53)); see
[word proximity](../EXACT_SEARCH_PROXIMITY.md). Bounded trailing wildcards
and stored-field filters remain; they are still rejected as unsupported.
Depends on 08. Deliver the remainder as two small follow-up changes.

## Finding and outcome

The inspected public search paths do not implement a consistent product grammar
for proximity, wildcard expansion, or field expressions. PostgreSQL's current
web-search conversion does not itself provide these product semantics.

## Small implementation sequence

1. Add ordered and unordered word proximity with an explicit distance definition,
   unit-boundary rules, and highlighted matches. Do not count OCR layout artifacts
   as ordinary words without documenting the tokenizer.
2. Add bounded trailing-prefix wildcards first. Show expansion-limit errors;
   defer arbitrary regular expressions and unrestricted leading wildcards.
3. Add field filters for metadata actually stored and reliably extracted. Start
   with type, filename/path, and collection; add date or email fields only with
   explicit extracted-field coverage and timezone semantics. File modification
   time must not impersonate an event date or sent date.

Do not make unsupported fields or operators silently behave like plain text.
Preserve the normalized query and tokenizer/grammar version for saved searches.

## Code and acceptance

Extend the parser/compiler from 07, the exact service from 08, and Sources help.
Each operator family gets its own fixture additions and browser examples.

Execute CPU/PostgreSQL parity tests for distance boundaries, punctuation,
Unicode, expansion caps, missing metadata, null dates, scope combinations,
and rejection of malicious/over-complex input. Verify performance on a generated
bounded corpus and document the observed limits. If new metadata storage or
indexes are introduced, supply backup/clean-restore and rebuild evidence.
