# Reuse producer field mappings for DAT intake

## Kickoff

- Goal. A reviewer can map a producer's DAT headings during selection, reuse a saved mapping in that matter, and verify which mapping and load-file digest the receipt records.
- Expected files. Receipt storage and HTTP handling, a mirrored SQLite migration, archive and lifecycle registration, the production inspection interface, DAT metadata projection, synthetic tests and operator guidance.
- Acceptance. Use two producers with different headings and overlapping Bates values. Confirm separate profiles and open the expected items. Reuse a profile on another selection. Changing the file, losing a mapped heading, retrying with a different profile, or supplying an invalid mapping must never silently publish different metadata. Existing receipts without profiles continue to work.
- Issue. [Volume import #279](https://github.com/Exculpata/exculpata/issues/279).

## Design

Keep a confirmed mapping on the individual receipt entry, with the producer,
exact header-to-role pairs and SHA-256 of the inspected DAT. A later selection
can reuse a previous mapping for this matter. The name of a producer never
establishes that two productions belong together. Preserve the original bytes,
headings and positional values. Only a structurally valid uploaded DAT with
the confirmed digest can use its explicit field mapping. Other formats retain
their own grammars. Ordinary protected matter membership governs inspection,
intake and review.

Store the mapping atomically with the selection entry. A retry must agree
with the original selection. Include the mapping in preservation and receipt
inspection, and remove it through normal matter deletion. Exercise restoration
from a clean database backup and isolation between matters.
