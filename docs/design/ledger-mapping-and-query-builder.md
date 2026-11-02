# Ledger mapping and limited query builder

Status: design note. This note defines a proposed contract, not implemented
behavior, a storage migration, or an authorization mechanism.

## Scope and invariants

CSV and XLSX bank ledgers become a typed `transactions` table after a reviewer
confirms a proposed column mapping. Existing delimited and workbook extraction
can supply headers, samples, sheet names, and tabular sections. Full-text
synthesis remains separate from numeric computation. The design preserves the
source and revision principles in [cited records](../architecture/records.md)
and the bounded request behavior in [retrieval input limits](../RETRIEVAL_INPUT_LIMITS.md).

The following rules hold at every capability tier:

* Rows are parsed deterministically from a confirmed mapping. A model may
  propose the mapping but never interprets ledger rows one at a time.
* Totals and counts are computed by the database. A model does not calculate,
  estimate, or repair them.
* A result includes citations for every contributing row. A displayed aggregate
  links to the complete, paginated citation set, not merely a sample.
* Queries are read-only and scoped to one authorized matter in core before
  compilation or execution. This preserves the ADR 0001 matter-isolation
  invariant. Matter scope is not a model-controlled builder field.
* Invalid, ambiguous, and inconsistent rows remain reviewable; they do not
  silently enter totals or graph flows.

## Typed transactions table

One immutable imported row produces at most one transaction version. Corrections
create a new reviewed import or mapping version rather than changing provenance.

| Column | Type and meaning |
| --- | --- |
| `transaction_id` | Opaque stable identifier for the parsed row version. |
| `matter_id` | Core-owned matter partition key. |
| `transaction_date` | Exact `date`; unresolved or invalid values are not invented. |
| `amount_minor` | Nonnegative integer in the account currency's minor unit. |
| `direction` | `debit` or `credit`; gives `amount_minor` its sign convention. |
| `currency` | Reviewed currency code; never inferred per row. |
| `balance_minor` | Nullable signed integer running balance. |
| `account_id` | Reviewed account identity within the matter. |
| `counterparty_id` | Nullable reviewed party identity; raw text is retained. |
| `counterparty_text` | Source value before identity resolution. |
| `memo` | Nullable source description, normalized only as the mapping specifies. |
| `reference` | Nullable source reference or transaction identifier. |
| `mapping_version_id` | Immutable confirmed mapping used to parse the row. |
| `review_state` | `ready`, `needs_review`, or `excluded`. |
| `review_flags` | Bounded reason codes, including balance and parse failures. |
| `source_file_id` | Immutable source file/version identity. |
| `source_sheet`, `source_row` | Workbook/CSV locator; row is one-based in the displayed source. |
| `source_page`, `source_region` | Alternative locator for a scanned table. |

Exactly one locator form is present: file plus sheet/row for native tables, or
file plus page/bounded region for scans. The source cell values and extraction
provenance remain available for review. Scan extraction is not treated as more
certain than the cited image region.

## Mapping and deterministic parsing

The proposal stage examines the header and a bounded representative sample. It
proposes source columns for date, amount or separate debit/credit amounts,
balance, account, counterparty, memo, reference, and optional currency. It also
proposes explicit parsing settings: date formats, decimal and grouping marks,
parentheses/minus handling, debit/credit sign rules, blank markers, header row,
data range, and sheet selection. Samples are suggestions, not row-specific
exceptions.

A reviewer confirms or edits the proposal. Confirmation creates an immutable,
versioned mapping containing source version, header fingerprint, settings,
reviewer, review time, and parent mapping where applicable. A changed source,
header, or parsing rule requires a new confirmation. Import records both mapping
and parser versions so replay is deterministic.

The parser applies one mapping uniformly. It rejects malformed dates, nonintegral
minor-unit amounts, conflicting debit and credit cells, unknown direction,
missing required identity, and locator collisions. Rejected rows are retained as
`needs_review` with source values and reason codes. They cannot be promoted by a
model response.

### Running-balance checks

Within each account and source-defined sequence, the checker compares adjacent
balances using the confirmed convention, for example
`next_balance = prior_balance + credit - debit`. It also detects duplicate or
ambiguous ordering, discontinuities across statement boundaries, absent balances,
and currency changes. A statement boundary can suppress a comparison only when
the mapping records that boundary rule.

A failed or indeterminate check flags every implicated row and the affected
sequence. Such rows default to `needs_review` and are excluded from totals and
graph queries. Review may confirm a documented source condition or correct the
mapping through a new version; it never silently clears a flag. Query results
report excluded-row counts and reason codes so an apparently complete total does
not hide known gaps.

## Builder grammar

The builder accepts only the following JSON-shaped grammar. `?` means optional,
`*` means a list, and alternatives are separated by `|`. Identifiers are opaque
IDs resolved inside the authorized matter. Dates use `YYYY-MM-DD`; ranges are
inclusive. Money values are integer minor units.

```text
Query          := AggregateQuery | TransferQuery | PathQuery
AggregateQuery := {kind:"aggregate", measure:Measure, filters:Filter*,
                   group_by:Group*, date_range:DateRange?, order:Order*,
                   limit:Integer?}
TransferQuery  := {kind:"transfers", from_party:PartyId, to_party:PartyId,
                   date_range:DateRange, filters:Filter*, group_by:Group*,
                   order:Order*, limit:Integer?}
PathQuery      := {kind:"path", from_party:PartyId, to_party:PartyId?,
                   date_range:DateRange, max_hops:Integer,
                   min_flow_minor:Integer?, currency:Currency?,
                   allow_cycles:Boolean, limit:Integer?}
Measure        := "sum_amount_minor" | "count"
Filter         := {field:FilterField, op:ScalarOp, value:Scalar}
                | {field:FilterField, op:"in", value:ScalarList}
FilterField    := "account_id" | "counterparty_id" | "direction"
                | "currency" | "reference"
ScalarOp       := "eq" | "gte" | "lte"
Scalar         := AccountId | PartyId | "debit" | "credit" | Currency
                | Reference | Integer
ScalarList     := [Scalar, ...]
Group          := "account_id" | "counterparty_id" | "direction"
                | "currency" | "transaction_date"
DateRange      := {start:Date, end:Date}
Order          := {field:Group | Measure, direction:"asc" | "desc"}
Integer        := JSON integer
Boolean        := true | false
AccountId      := nonempty opaque JSON string
PartyId        := nonempty opaque JSON string
Currency       := reviewed three-letter currency JSON string
Reference      := bounded nonempty JSON string
Date           := JSON string matching YYYY-MM-DD
```

Validation additionally requires `1 <= limit <= 1000`, no more than 20 filters,
`start <= end`, and a configured maximum date span. `max_hops` is from 1 through
4. `min_flow_minor` is nonnegative. Group and order fields must be compatible
with the selected query. Production limits remain configurable downward.

The builder rejects unknown keys or operators, raw/free-text SQL, SQL fragments
in scalar values, cross-matter or unresolved IDs, model-supplied `matter_id`,
unsupported grouping, invalid dates or money, excessive result limits, and paths
without a bounded date range and hop limit. Unbounded paths are always rejected.
Parameters never become identifiers.

After core obtains the authorized matter from the authenticated request, it
resolves every referenced ID against that matter, validates the grammar, and
compiles an allowlisted template with bound parameters. The database role permits
only reads. Each template includes `matter_id = :authorized_matter`,
`review_state = 'ready'`, and the selected range before any aggregation or graph
expansion. Defense in depth may also apply database row policies, but cannot
replace core enforcement. Logs retain the normalized builder, template version,
scope, and result metadata, not source memo text by default.

## Worked examples

These SQL excerpts show shape, not a free-form SQL interface. Citation queries
reuse the same scoped predicate and return row locators for all selected IDs.

### Total by account

```json
{"kind":"aggregate","measure":"sum_amount_minor","filters":[{"field":"direction","op":"eq","value":"debit"}],"group_by":["account_id"],"date_range":{"start":"2026-01-01","end":"2026-03-31"},"order":[{"field":"sum_amount_minor","direction":"desc"}],"limit":100}
```

```sql
SELECT account_id, SUM(amount_minor) AS sum_amount_minor
FROM transactions
WHERE matter_id = :authorized_matter AND review_state = 'ready'
  AND direction = :p1 AND transaction_date BETWEEN :p2 AND :p3
GROUP BY account_id ORDER BY sum_amount_minor DESC LIMIT :p4;
```

The result count and sum come from the database. A companion selection returns
the `transaction_id` and source locator for every summed row.

### Transfers from party A to party B

```json
{"kind":"transfers","from_party":"party-A","to_party":"party-B","date_range":{"start":"2026-04-01","end":"2026-04-30"},"filters":[],"group_by":[],"order":[{"field":"transaction_date","direction":"asc"}],"limit":200}
```

```sql
SELECT transaction_id, transaction_date, amount_minor, currency
FROM transaction_edges
WHERE matter_id = :authorized_matter AND review_state = 'ready'
  AND from_party_id = :p1 AND to_party_id = :p2
  AND transaction_date BETWEEN :p3 AND :p4
ORDER BY transaction_date ASC LIMIT :p5;
```

`transaction_edges` is a read-only, matter-partitioned projection of reviewed
transactions. Each edge retains its originating transaction citation.

### Four-hop path

```json
{"kind":"path","from_party":"party-A","to_party":"party-E","date_range":{"start":"2026-01-01","end":"2026-06-30"},"max_hops":4,"min_flow_minor":10000,"currency":"USD","allow_cycles":false,"limit":50}
```

```sql
WITH RECURSIVE paths AS (
  SELECT ARRAY[from_party_id,to_party_id] AS nodes, ARRAY[transaction_id] AS rows,
         to_party_id AS tail, amount_minor AS flow_minor, transaction_date, 1 AS hops
  FROM transaction_edges WHERE matter_id=:authorized_matter AND review_state='ready'
    AND from_party_id=:p1 AND transaction_date BETWEEN :p2 AND :p3
    AND amount_minor>=:p4 AND currency=:p5
  UNION ALL
  SELECT p.nodes||e.to_party_id, p.rows||e.transaction_id, e.to_party_id,
         LEAST(p.flow_minor,e.amount_minor), e.transaction_date, p.hops+1
  FROM paths p JOIN transaction_edges e ON e.from_party_id=p.tail
    AND e.matter_id=:authorized_matter AND e.review_state='ready'
    AND e.transaction_date>=p.transaction_date AND e.transaction_date<=:p3
  WHERE p.hops<:p6 AND NOT e.to_party_id=ANY(p.nodes)
)
SELECT nodes, rows, flow_minor FROM paths
WHERE tail=:p7 AND hops<=:p6 ORDER BY hops, flow_minor DESC LIMIT :p8;
```

This example compiles `allow_cycles:false`; the allowlisted cycle-enabled shape
omits the repeated-node predicate but retains the same date and hop bounds.

## Money paths and citations

Graph nodes are reviewed party or account identities; edges are ready transaction
rows, never inferred prose relationships. Temporal expansion requires each next
edge to occur no earlier than the prior edge. The path's flow amount is the
minimum edge amount, not a claim that the same funds were traced. Queries may
include or exclude cycles explicitly; cycle results identify the repeated node
and all edges. Hop and date bounds always apply. Results carry every edge's row
citation, direction, amount, currency, and time. Mixed currencies are not summed
without a separate reviewed conversion design.

## Open questions for the maintainer

1. Should balance-check exceptions require one reviewer or a second approval?
2. What maximum date span and lower production hop cap should each deployment use?
3. Should transfer edges require paired ledger entries, or may one reviewed row
   establish a directed edge while clearly labeled as single-sided?
4. Which party identity states are sufficiently reviewed for path queries?
5. Should cycle search be a distinct builder kind rather than `allow_cycles`?
6. How should duplicate exports of the same transaction be reconciled without
   weakening immutable source citations?
7. When a result has many rows, what citation pagination and export limits keep
   every contributing row accessible?
8. Should reviewed balance exceptions remain excluded from aggregates by default,
   with an explicit non-model-controlled inclusion policy?
