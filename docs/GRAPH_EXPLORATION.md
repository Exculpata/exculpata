# Evidence connections

Status: local implementation on the September 15 exploration branch; upstream
review, merge and supported-release acceptance remain outstanding.

## Reviewer workflow

Open **Case file → People & things**, select a saved identity and choose
**Explore connections**. The graph shows the identity's explicit roles in saved
events/assertions and other identities recorded in those records. Select a
current neighboring identity to explore its neighborhood. Select a record or
its supporting/competing count to inspect the evidence list and complete record.

**Find more originals** opens exact search with the selected name as a literal
phrase. Aliases can be searched individually. Matches are leads containing that
text; the action does not resolve identity or change the recorded relationships.
The existing exact-search capacity and grammar contract still applies.

The evidence list retains reviewer statements and decisions, raw dates, date
uncertainty, recorded entity roles and separate source attributions. Current
original links open the existing source pane; written sources retain unit/page
locators and recording transcripts retain their cited time interval. Source and
full-reader navigation carry the graph return context.

Names do not merge nodes. Lines do not imply an unrecorded relationship, direction
of payment or causation. Changed/missing identity snapshots are historical and
have no graph traversal action; correct the role in its record deliberately.
Changed/unavailable originals retain historical excerpts with live links disabled.

## Bounds and authority

One neighborhood request displays at most eight records, twenty roles per record,
and ten accounts per stance per record. Omission counts distinguish supporting
and competing accounts. The compact SVG shows up to four other roles per record;
the evidence list retains all admitted roles and links to complete records.
Chronology ordering uses the explicit reviewer ordering date, then undated records.
Pages beyond the latest live population redirect to an available page.

This is a live view of saved knowledge. It is not a frozen corpus traversal,
complete occurrence search, model extraction or visual-semantic retrieval. It
does not call a model. Existing record capacity limits remain unchanged.

The narrow graph repository joins existing matter-scoped indexes. Its service
holds the source guard and existing authorized unit of work, validates admitted
source references in one batch, and reads no correction histories. HTTP output
reauthorizes after rendering and uses `Cache-Control: no-store`. Graph labels are
escaped. The browser uses only packaged assets. No persisted graph, schema,
model portfolio, installation or source-byte change is introduced.

## Local synthetic evidence

On the local implementation based on `a49133f1fe022fb098f2519aea007fa562570371`:

- Graph, entity and assertion regression selection: **90 passed, one skipped**.
  The skipped written-PDF/audio combination requires Linux PDF child-process
  resource limits. The written-text/audio combination passed using actual media
  decoding with contributor-host executable paths and a controlled transcript
  provider; this does not evaluate ASR.
- Browser acceptance: Chrome 153, temporary synthetic runtime, unavailable
  generator. Keyboard entry/refocus, both source stances, source/full-reader
  return, literal phrase search, Day/Dusk text contrast and 390-pixel reflow passed.
  The runtime was reopened before the browser journey.
- Adversarial checks cover same-name separation, stale sources and identities,
  cross-matter denial, access revocation during rendering, HTML escaping, bounded
  role/account pagination, repeated navigation and removal of a later page.

### Rebased landing validation, September 19

Rebased onto `a8cb76308c46078dee86ce26fdfdc289749a92c0`; the graph page now
includes that baseline's native main-content skip target and a navigation
regression. Graph, entity, assertion, search-navigation, workbench and guided
usability tests: **138 passed, one skipped** with the synthetic-test storage
reserve set to zero. The first run without that test setting hit the host's
storage-reserve guard in five workbench tests; all passed with the same setting
used by hosted CI. The skipped PDF/audio case still requires Linux.

The existing browser acceptance passed again on the rebased implementation,
including keyboard entry/refocus, supporting and competing originals, full-reader
return, literal phrase search, Day/Dusk contrast and 390-pixel reflow. The [review policy](QUALITY_GATES.md#review-policy) still applies before merge.

The executable browser check is `scripts/browser-accept-evidence-graph.py`.
These checks establish the bounded graph workflow only. They do not establish
Linux acceptance, native visual quality, million-record capacity, deployed-node
state or a supported release.

See [the implementation brief](product-slices/20-graph-exploration.md). Native
visual qualification remains separately scoped and on hold until this slice lands.

### Hosted-review correction

The diagram reserves bottom space for the final additional-role count. Synthetic
one-record and eight-record pages reproduce the previous clipped notice and verify
that every omission label now fits within the SVG viewport.
