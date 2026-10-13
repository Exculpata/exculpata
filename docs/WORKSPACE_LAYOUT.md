# Matter workspace layout

Matter pages share a full-width outer shell and the same content gutter.
Review, Case file, Notes, and the other matter tools keep their header
and navigation aligned when moving between pages. Reading cards may still use
an inner reading width. `static/workspace-layout.css` owns this shared geometry;
feature styles own the content inside it.

## Compact matter bar

Every page of an active matter, including the source and media readers, Check
export, closing and other confirmation or recovery pages, starts with one slim
bar of six destinations: **Home**, **Review** (documents and recordings; rule-based
screening is reached from here), **Ask** (questions and investigations),
**Search**, **Case file** (overview, notes, people & things, timeline, review map
and reports) and **Settings**. The current destination carries
`aria-current="page"`. Case file sections share one sub-navigation instead of
per-page copies. A matter search box beside the destinations submits an ordinary
exact search (`/exact-search?search=1&words=…`); the Search page itself uses its
full form instead. When the bar is too narrow for direct links, the existing
section navigator replaces them. Opening that navigator can widen its row;
the breakpoint keeps the closed-row measurement, so the menu stays instead
of switching back to direct links.

The readiness status keeps its polling, actions and focus behavior. A ready matter
shows one line; its extraction caveat stays in the document and opens with
**Extraction limits**. Other states, and pages without scripts, show the full
guidance. The bar renders the retention notice itself: the full notice on Home,
and on every page with the bar whenever deletion is due or in its grace period.
Other pages show a small link back to it while a warning applies; the matter list
keeps its days-remaining chip. A matter that is closing or recovering from a
failed close keeps its own pages without the bar, so they offer no edit links.

Stylesheets and scripts load through content-versioned URLs
(`/static/<file>?v=<digest>`). A matching version is cacheable as immutable; an
unversioned or stale request must revalidate. A deployment therefore never pairs
new page markup with an older cached stylesheet or script.

`tests/test_compact_matter_navigation.py` and
`tests/test_static_asset_versioning.py` hold the synthetic regressions.

## One box on Home

Set `CASE_INTELLIGENCE_ONE_BOX=1` before starting the application to replace
Home's four task cards with **Ask, search or find records**. The flag defaults
off; `1`, `true`, `yes` and `on` enable it, ignoring case and surrounding spaces.
With it off, the existing cards and workflows remain and the new POST endpoint
returns 404. **Review records manually** and **Investigate a topic** stay as
quiet links to the source list and Ask composer.

The CSRF-protected form classifies the text using the existing Ask router:

- Quoted phrases, valid Boolean expressions and proximity syntax open exact
  search with the original expression.
- Ordinary questions use the existing focused, cited-answer handler. Request
  keys preserve its retry handling; no separate answer implementation is added.
- Requests for every matching record open a new criterion form with the full
  text prefilled. An existing criterion is not overwritten or run. The eligible
  source count remains visible; saving the rule and starting a review require
  the existing explicit actions.

Each destination explains the routing choice. **Not what you meant?** reveals
two buttons that send the same text through either alternative. They require
CSRF and matter access too. The box accepts up to 2,000 characters; exact search
retains its 512-character limit. An overlong exact-search choice returns to Home
with the unchanged text and an explanation instead of truncating it.
Encoded destinations also have a 7,000-byte limit, with space reserved for answer
IDs and recovery messages below the gateway's request-line limit. Text that would
exceed it (including long Unicode input) stays in the Home form with a request to
shorten it; no redirect or answer job is created.

The form uses the existing readiness hint and polling, and remains disabled
while sources cannot be queried or the page is an administrator's read-only
view. The server checks readiness again on submission. Native forms and the
switch disclosure remain keyboard accessible without JavaScript.

`tests/test_one_box_routes.py` covers routing and the authorization boundaries.
The registered `scripts/browser-accept-one-box.py` journey checks all three
destinations, original-source links, explicit switching, a 430-pixel viewport,
keyboard submission and the absence of a review run before confirmation:

```console
python scripts/browser-accept-one-box.py \
  --chrome-binary /path/to/chrome \
  --chromedriver /path/to/chromedriver \
  --output /tmp/recordbench-one-box-acceptance
```

## Follow-up question retrieval

Focused answers reserve retrieval space for the complete current question.
Short follow-ups may add the previous user turn within the remaining
512-character search limit; older context is trimmed first. Selected notebook
text can use only the space still available after that. The complete question,
bounded conversation history and selected notebook context still reach generation
through their separate inputs.

Questions longer than 512 characters, up to the existing 2,000-character answer
limit, are searched in ordered windows of at most 512 characters. Windows keep
words together when possible; a word longer than a search window must be split.
These questions use at most seven windows, without appended history, notebook
text or broad-summary facets. Their existing 20 primary-candidate budget is
divided across windows, and results are interleaved by rank. If a requested
source kind is absent, its existing 12-candidate supplemental budget is divided
across the same windows within the authorized source scope. Internal callers
requesting fewer candidates than windows receive an explicit validation error
before searching; no candidate budget is increased implicitly.

This remains focused retrieval, not an exhaustive review. Search validation,
source authorization, citation checks, generation grounding and the selected
evidence limits still apply. The synthetic composition regressions in
`tests/test_answer_retrieval_question.py` exercise synchronous and durable answers,
saved original-source support, notebook context and the long-question boundaries.

## Offer to investigate a thin answer

Set `CASE_INTELLIGENCE_DEEPER_INVESTIGATION=1` before starting the application
to offer **Investigate this more deeply** under a saved answer when the generator
explicitly returned `answerable: false`. The flag defaults off and uses the same
`1`, `true`, `yes`, `on` values as the one box. Main conversation and Ask dock
use the same offer. Supported answers, verification rejections, no-evidence
responses, research results, and older answers without recorded provenance get
no extra action.

The verifier records an optional `generator_answerable: false` payload marker;
no schema changes are needed. The offer resolves the original question through
its completed answer job and retains that job's source set. It never infers
answerability from response wording or citation counts, substitutes another
team member's identity, or broadens an unavailable source scope.
`WorkspaceStore.answer_investigation_basis` owns the member-scoped, read-only
question binding and availability checks; the shared template helper uses that
public result without accessing the database connection or its lock.

Nothing starts until the reviewer clicks. The native form posts the original
question and conversation with `review_task=research` to the existing
CSRF-protected Ask handler. Stable per-answer, per-reviewer request keys reuse
the same saved investigation on retries. Existing membership, readiness, local
answering availability, source scope and concurrent-work checks still apply.
Saved context and case notes are not reused by research; the offer states this
when they were supplied to the original answer.

`tests/test_deeper_investigation.py` covers provenance, both presentations,
source and question binding, authorization, and duplicate submission. The
existing `scripts/browser-accept-one-box.py` journey also clicks the offer after
an unsupported answer and confirms that no investigation starts automatically.

## Discovery briefing on Home

Set `CASE_INTELLIGENCE_BRIEFING=1` before starting the application to show the
discovery briefing before Home's resume panel and task launcher. The flag
defaults off; `1`, `true`, `yes` and `on` enable it, ignoring case and surrounding
spaces. With it off, no briefing is assembled and its markup and assets are
absent. The feature adds no route, saved briefing, or model call.

Once source preparation, media overviews and automatic discovery finish, four
sections show what arrived, suggested people and places, dates mentioned in the
records, and recorded intake or extraction problems. Every line has source or
review links. **Coverage and limitations** keeps the caveats in one initially
collapsed disclosure. Counts and questions follow the deterministic rules and
explicit read limits in [Discovery briefing](DISCOVERY_BRIEFING.md); mentioned
dates are not document dates or established events.
The briefing does not enable automatic discovery: it summarizes suggestions
already recorded. Enable the existing `CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY=1`
flag separately to discover people and dates as sources become ready.

While work is active, the existing readiness line remains and the briefing is
hidden. Readiness polling replaces only the briefing region after completion,
preserving text and focus in the question box. Suggestion buttons use the same
fresh readiness snapshot as the briefing. A new processing phase hides the
previous briefing again. Read-limit and changed-record refusals leave Home's
normal workflows available with links to sources and a manual refresh.

Each **Suggested** question is a native button that fills the one box without
submitting. When `CASE_INTELLIGENCE_ONE_BOX` is off, it opens and fills the
existing Ask dock instead. Busy, disabled or read-only composers are left
unchanged. Question selection never starts a search, answer or review. A
nonmember administrator retains the existing read-only Home without assembling
a briefing under another person's membership.

`tests/test_briefing_home.py` covers visibility, authorization, completion
boundaries, source links and graceful refusal. The registered
`scripts/browser-accept-briefing.py` journey checks processing transitions,
source links, question selection, both composer variants and a narrow viewport.
Each linked destination must return HTML 200 and expose its main landmark after
navigation; failures retain both a screenshot and page source. Run
`scripts/browser-accept-matter-knowledge.py` with `--briefing` to check the
existing Case notes journey with this flag enabled after fixture isolation.

## Scroll regions

Long right-hand content must not make left-hand controls unreachable:

- The fixed global matter rail scrolls its list independently, keeping its
  creation, search, and collapse controls available.
- On desktop, Saved conversations stays within Review's scrolling pane. Its
  list scrolls independently and reserves the actual height of the sticky
  question composer, including when that composer grows. Tablet support drawers
  reserve the composer's bottom offset too, including after viewport resizing.
  On short tablets with support open, the entire history panel scrolls so its
  heading and filter cannot consume all available space above the composer.
- The conversation layout stacks at 760 pixels and below. Its non-sticky
  history retains the 250-pixel list cap and scrolls away above the active
  conversation. At 761–900 pixels, history remains beside the conversation
  both with and without a support drawer.
- Case note tools use the document scroll so note creation and discovery
  controls stay reachable without a nested sidebar scroll trap.
- Independent navigation and conversation scroll regions remain keyboard
  reachable. Case note tools intentionally use ordinary document scrolling.
- Readiness polling retains existing action controls and their keyboard focus.
  If an action changes between a link and a button, focus follows its replacement;
  if focused Details disappears, focus moves to the primary readiness action.
  Updates never move focus from an unrelated control.

Run the bounded synthetic browser acceptance with a matching Chrome and driver:

```console
python scripts/browser-accept-workspace-layout.py \
  --chrome-binary /path/to/chrome \
  --chromedriver /path/to/chromedriver \
  --output /tmp/recordbench-layout-acceptance
```

The script uses temporary invented matters, conversations and notes, an
unavailable generator, and an explicit synthetic storage policy. It clears
inherited Exculpata service settings. It checks alignment at 1800×1000,
1440×480 and 390×844, then uses native wheel events and keyboard navigation to
verify independent scrolling beside long content. Support-open checks cover
761, 820 and 900 pixel widths, then resize the open page to 820×650 without
navigation and hit-test the last saved-conversation link above the composer.
Without support, the same tablet widths verify separate history/content columns,
independent list scrolling and unobscured active content; the 760/761 boundary
checks the stacked list cap and confirms history scrolls out of the reading area. The output contains local
screenshots and a receipt identifying the tested commit and working-tree state.

This layout-only contribution starts from the accepted selected integration.
The frozen review-acceptance manifest remains unchanged: its integrity tests
pass without importing the broader integration's changed media test or updating
its recorded digest.

## Save an assistant passage during review

Each generated claim in the assistant dock has **Save passage to case notes**.
The existing save checks matter access and current citation support, creates a
suggested note for human review, and deduplicates repeated saves. A save only
updates the claim's accessible inline feedback; it keeps the reader document,
section, scroll, URL, filters, selected chat and unsent question in place. A
pending response cannot replace a newly opened reader or chat. An uncertain
response can be retried safely. Saving does not confirm the generated claim.

Without JavaScript, the form returns to the validated same-matter reader URL
with a notice or error. A full page submission cannot preserve an unsent
client-only draft or arbitrary pixel scroll; source section and filters remain
in the return URL. Full conversation's existing forms and redirects still work.
No new storage, conversation, graph or generation contract is introduced.

Supplemental browser acceptance uses an explicitly selected Chromium executable
and Playwright available to Node, plus the existing Python development environment:

```console
node scripts/browser-accept-assistant-passage.cjs /path/to/chromium /tmp/new-dock-acceptance
```

It starts an ephemeral loopback application with generated text and an unavailable
model. The receipt and screenshot contain only synthetic evidence. It checks
keyboard feedback, retained reader/composer state, repeated saves, pending chat
and reader navigation, Back/Forward, reload and JavaScript-disabled submission.
Simulated error responses test inline recovery; the Python route regressions in
`tests/test_assistant_passage_save.py` separately prove real stale-citation,
revoked-access and CSRF rejection without writes. This supplemental run does not
replace the pinned browser or other hosted Quality gates.

## Assistant dock suggestions and Close

The dock header has a labelled **Close** button (the chevron stays as its icon);
its accessible name, "Close Ask Exculpata", includes the visible label. Closing
moves focus to the collapsed **Ask Exculpata** tab, and reopening returns focus
to the question box with any unsent question kept.

An empty chat, including one started with **New chat**, offers three suggested
questions in a labelled group. Each fills
the question box and moves focus there; nothing is sent until the reviewer
chooses Send. Suggestions depend on the page:

- With a ready source open in the reader, the dock offers **Summarize this
  source**, **Who and what appears here** and **What dates appear here**.
  Choosing one first runs the existing **Ask using this source** action, so the
  next question's scope becomes that source alone, and only then fills the box.
  If the source cannot be selected, nothing is filled in and the dock says so.
- Elsewhere the general suggestions remain, searching all searchable sources
  unless the reviewer chooses a scope.

The dock keeps its page when it refreshes from its own route; only a path inside
the same matter is accepted for that. No schema, model or generation change is
involved. The dock is not shown on Home, People & things, the timeline, the graph,
Search or Reports, so no suggestions are offered there.
`tests/test_assistant_suggestions.py` and the pinned
`scripts/browser-accept-assistant-dock.py` journey cover this behavior.

Each verified claim in the dock also offers **Suggest people, things and dates
from this passage** below **Save passage to case notes**; see
[suggestions from an answer](ENTITY_DISCOVERY.md#suggestions-from-an-answer).
It stays on the current page and shows a proposal card under the claim, which
takes focus and lists what it found; **Dismiss** adds nothing and returns focus
to the button. While **Add to suggestions** is pending, Dismiss and the Suggest
button are held, so the status never says nothing was added while the request may
still succeed. **Add to suggestions** announces how many it added in a status
line, then shows **Review suggestions** and, when it added a date, **See dates in
the timeline draft**. When nothing new is found, the status says so and
**Review suggestions** stays available. Without JavaScript, and from the full
conversation, the proposal is its own page, and the full conversation returns to
the answer afterwards.

## Manual source review workspace

**Review** opens the source library. **Find records matching a rule** retains
criterion-based extracted-text review. The source reader offers a compact header,
optional filtered queue, previous/next source navigation, and an adjustable
questions/notes pane. Queue visibility and pane width are optional local browser
preferences. Source details and extraction limitations remain expandable; hiding
these details does not establish complete extraction.

PDFs open at whole-page fit; **Fit width** switches to page width and the choice is
a per-viewer browser preference (pages without scripts keep whole-page fit). The
queue shows the current folder, its immediate child folders with source counts, and
**Up one folder**. Folder steps keep the open source and re-scope only the queue,
using the library's exact folder boundaries and preserving its other filters. Folder
steps keep the reader's current section, find and transcript state.

**Find in this file** searches the open source's extracted sections: literal,
case-insensitive words, tolerant of line breaks, not a regular expression. Results
list each matching section with its count and first snippet (up to 200 sections;
the heading counts every matching section); choosing one selects that section, so
a note written next cites it. Previous and Next keep the search, and Clear keeps
the review origin. Work is bounded per request: matches are counted without being
retained, counting stops at 10,000 (a total or section count that left matches
uncounted is shown as "At least" or "N+"), and at most 2,000
matches are highlighted in one section. A missing match does not prove the words
are absent from the original file. Recordings keep their transcript search beside the
player. `tests/test_reader_v2.py` holds the synthetic regressions.

**Ask using this source** selects the next question's source scope without
replacing the reader. Saved answers remain labeled as historical answers with
their own citations. Changing question scope does not change the evidence behind
an earlier answer. Asynchronous assistant operations are fenced against newer
conversation and scope choices. The full conversation and report/export routes
remain available.
Same-conversation history refreshes preserve the current saved-context checkbox
and selection revision, including choices made while an answer was running.
If a terminal request's history cannot refresh, the dock keeps the draft and
directs the reviewer to Full conversation; stale failures cannot replace newer
conversation or source state.
Cancel, terminal polling, saved-chat selection and preferred-chat restoration
share explicit refresh ownership. A failed read resumes status polling only for
an active request; uncertain cancellation never automatically resends Cancel.
Newer source, conversation or question actions invalidate earlier recovery work.

**Write a note** saves human-authored prose with canonical source support into
existing Case notes, with manual origin and Needs review status. Saving a note
or marking a source reviewed does not confirm its claims, transcript or entities.
An immutable draft identity makes uncertain retries safe; **Write another note**
starts a separate note, including on the same passage. Before sending, the reader
keeps only retry identity and source coordinates in that browser history entry,
so Back and reload cannot silently turn a restored draft into a second note.
Note prose is not persisted by this mechanism. If the browser does not restore
the text, inspect saved Case notes or explicitly start another note. The server rechecks matter
access, source version and extraction basis before saving. A changed source is
rejected for renewed review. Existing AI passage saves retain suggested status
and preserve later human decisions on retries.

PDF notes identify the source page selected by Exculpata's section controls.
The embedded browser PDF toolbar does not report page changes to the outer
reader; select the matching Exculpata section before annotating another page.
Media notes capture a playback position and attach the canonical transcript span
containing it. A position without transcript support cannot produce a
source-linked note through this form. The playback element remains mounted when
questions, notes, queue visibility or pane width change. Source changes and
transcript pagination use normal navigation; they do not promise uninterrupted
playback across documents or transcript page boundaries.

Without JavaScript, the note form is available in normal document flow and
successful submissions return to a validated same-matter reader URL. Validation
errors retain the submitted draft. Full-page navigation cannot preserve arbitrary
pixel scroll, native PDF toolbar state or an unsent client-only question.

Supplemental deterministic acceptance:

```console
.venv/bin/python -m pytest -q tests/test_manual_review_notes.py tests/test_assistant_passage_save.py tests/test_answer_cited_context.py tests/test_review_acceptance_pack.py
node scripts/browser-accept-manual-review.cjs /path/to/chromium /tmp/new-manual-review-acceptance
```

The browser fixture generates a PDF and a short video, supplies deterministic
transcript/answer data, and delays request responses deliberately. It checks
source-linked human notes, uncertain retries, stale history/ask/poll/cancel
responses, explicit source scope, mounted playback, filtered navigation, keyboard
focus, layout preferences, narrow screens and the non-JavaScript note fallback.
A 640×302 CSS viewport approximates the available space at 200% zoom; this is
not native browser zoom qualification. Transcript pagination and playback-time
restoration through browser history remain unqualified. This synthetic cloud
verification does not establish real-model, GPU, security-profile, recovery or
production-server acceptance, and does not replace the current Quality gates.
