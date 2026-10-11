# Synthetic browser acceptance

The `synthetic-browser` job in the quality workflow runs the existing intake
receipt, Dusk readability and Report bundle journeys on Ubuntu 24.04 and Python
3.12. Reports always include `--verify-readiness`: the browser checks export preview, missing
source repair, cross-matter denial and interrupted-close recovery as well as
Markdown/Word downloads. The Report script enters the visible reading/editor
views and expands the blank-document and written-section controls before editing.
The runner also includes the [events/assertions journey](EVIDENCE_ASSERTIONS.md),
which checks at 390 pixels that the source drawer's own "Return to review
context" link is inside the viewport and receives the click once the drawer has
settled. The page behind the drawer has a same-named link that can scroll under
the sticky question box, so the journey targets the drawer's link explicitly.
The [assistant dock journey](WORKSPACE_LAYOUT.md#assistant-dock-suggestions-and-close)
checks the labelled Close control and the suggested questions with the keyboard
at 1440, 390 and 320 pixels: on an open source, choosing a suggestion limits the
next question to that source before filling the box, and nothing is sent. With
a seeded cited answer it also chooses **Suggest people, things and dates from
this passage** from the keyboard, checks that the proposal card takes focus and
that Dismiss adds nothing; that a slow Add holds Dismiss and Suggest; then adds the proposal and checks the announced result
and its links, the **From an answer** mark in the inbox and the found date in the
timeline draft, that a repeat finds nothing new, and that the control fits at 390
and 320 pixels.
The [one-box journey](WORKSPACE_LAYOUT.md#one-box-on-home) enables the optional
Home entry point and checks exact search, cited answers and a prefilled
every-source criterion, route switching, native keyboard use and a 430-pixel
viewport. It confirms that opening the criterion creates no review run.
With `CASE_INTELLIGENCE_DEEPER_INVESTIGATION` enabled, the same journey also
checks that a supported answer shows no additional offer, while a deterministic
`answerable=false` result offers **Investigate this more deeply** without
starting work. One native click at 430 pixels posts the same question through
the existing CSRF-protected Ask flow and creates exactly one queued research
job in the same conversation. Refreshing before or after the click creates no
extra work. The research worker is stopped only to keep this queue-state check
deterministic; the journey does not assess investigation quality.
The discovery briefing journey enables `CASE_INTELLIGENCE_BRIEFING`, holds a
synthetic indexing job to check the existing processing presentation, then
waits for real extraction, indexing and automatic discovery to finish. It
checks all four sections, exact counts, recorded intake reasons, Suggested
labels and every source link, including the collapsed coverage caveats. At
430 pixels it chooses a question with native Tab and Enter, verifies the
unobscured focused input and unchanged answer/research counts, and repeats
with one box disabled to check the existing Ask composer. A final application
restart checks that disabling the briefing flag restores the existing Home.
It also holds actual Home responses across two controlled readiness cycles to
verify that an obsolete response cannot replace the briefing, a fresh read is
required, and the unsent draft and focus outside the briefing survive.

All applications are generated for the run and listen on ephemeral loopback
ports. They use the unavailable generator or a fixed synthetic generator and
synthetic text fixtures. This is
not a model-quality, media-codec, live PostgreSQL or deployed-node acceptance
check. No model weights or external evidence are downloaded. Production resource
settings are not changed: the runner supplies a zero storage reserve only to its
small disposable test applications.

## Contributor commands

Install the normal contributor Python dependencies first. On Ubuntu 24.04 install
the browser libraries listed in `scripts/install-ci-browser-deps.py`. Then run:

```console
.venv/bin/python -m pytest -q tests/test_browser_acceptance_runner.py
.venv/bin/python scripts/run-browser-acceptance.py --output /tmp/generated-browser-acceptance
```

The output directory must not exist. Use a new name on each attempt; the runner
will not replace old receipts or treat them as evidence of a new successful run.
Linux x86_64 and Apple Silicon macOS have pinned archives. Unsupported hosts fail
before any journey starts. All temporary paths passed to the applications are
resolved, including on macOS where the system temporary directory can traverse a
symlink.

### Hosted dependency setup

The Ubuntu 24.04 x86_64 GitHub Actions job uses `install-ci-browser-deps.py` to
bound APT index refresh to 60 seconds and package download to 180 seconds, with
up to 10 seconds to kill remaining acquisition processes. If either fails or
times out, it retries acquisition once using only the official HTTPS Ubuntu
mirrors already listed on the runner. It restores the original mirror list
afterward; repository suites, components, signing keys and authentication are
unchanged. Before changing mirrors, it parses the Ubuntu Deb822 source stanzas
and requires every active stanza to use only the expected mirror list. Disabled
stanzas cannot authorize fallback; mixed URIs, missing required fields and
ambiguous or malformed source configuration fail closed without a mirror write.
The primary attempt uses the runner's normal APT sources. For every fallback
APT command (index refresh, download-only acquisition and offline installation),
the helper sets `Dir::Etc::sourcelist` to the validated `ubuntu.sources` file and
`Dir::Etc::sourceparts` to `/dev/null`. Additional `sources.list` or
`sources.list.d` entries therefore cannot delay fallback refresh or supply its
package candidates; those files are not modified. Refresh errors still fail the
attempt via `--error-on=any`.

Only after successful acquisition does it install the complete dependency list
with `--no-download`. Package unpacking has no helper timeout or retry; missing
archives and install failures fail the job. The existing 20-minute job limit,
FFmpeg executable checks, browser archive verification and all journeys remain
required. Setup failure is not a browser acceptance result. The helper is for
the disposable hosted runner, not a local machine or deployed node.

`tests/test_ci_browser_dependencies.py` mocks package commands to cover success,
bounded fallback and failure without network or root access. These tests do not
prove live Ubuntu package acquisition; the hosted browser job supplies that
evidence.

The default timeout is 300 seconds **per journey**, configurable with
`--timeout-seconds 1..600`. Each journey runs in its own process session. On
success, failure, timeout, SIGINT or SIGTERM the runner terminates that process
group, gives it a short grace period, kills remaining members and reaps the
leader. A nonzero child exit fails even if it left a success receipt. Missing,
malformed, failed, nonsynthetic or incomplete receipts also fail. An ordinary
journey failure still runs the remaining journeys; interruption stops the run. The
runner tests exercise these failure contracts with generated subprocesses and
receipts, without downloading or opening a browser.

The runner starts children with fresh home, temporary and cache directories,
offline model flags and a small environment allowlist. Deployment settings,
proxy credentials and Python import overrides from the calling shell are not
inherited. Browser and driver paths are explicit, so Selenium Manager is not
used to resolve or install a different version.

Matter-context navigation sends native Enter once, waits for the activated
control to detach, then requires the replacement document to finish loading.
Chrome's specific inspector error, `Node with given id does not belong to the
document`, counts as detachment only during that wait, just like Selenium's
stale-element response. Other driver errors and navigation timeouts still fail;
workflow assertions remain unchanged and the action is never retried.

Matter-knowledge pointer navigation samples target geometry inside its existing
20-second wait. An offscreen center is scrolled into view and resets stability;
an onscreen obstruction remains a failure unless it clears. Two consecutive
identical, unobstructed rectangles are required before one native click, followed
by the existing detachment and complete-document waits. Positioning timeouts
retain at most six geometry/hit-test samples without page text or URLs.
`tests/test_matter_knowledge_click.py` covers displaced scrolling, obstruction,
moving geometry, bounded diagnostics and a single activation even when navigation
fails. This handles displaced positioning; it does not prove the timing cause of
an earlier hosted failure whose receipt contained no geometry samples.

## Browser pins and deliberate updates

`config/browser-testing.json` records one exact Chrome for Testing version,
matching ChromeDriver URLs and SHA-256 hashes for each supported platform. The
initial archives come from Google's [Chrome for Testing availability
index](https://googlechromelabs.github.io/chrome-for-testing/). SHA-256 values were
computed from the downloaded versioned archives and checked into the repository;
there is no runtime lookup of a moving stable/latest channel. Every archive is
verified before extraction or execution. ZIP paths, framework symlinks and total
expanded size are checked before any browser starts.

For an offline replay, download both named archives beforehand and pass their
directory with `--archives /path/to/verified-archives`. The same committed hashes
are checked on every run. Archives and extracted browsers stay outside the
artifact output. They are not committed or uploaded.

To update the browser intentionally:

1. Choose a specific available version from the official index and obtain both
   Chrome and ChromeDriver archives for each supported platform.
2. Compute each SHA-256 locally, update the version, URLs and hashes together,
   and inspect that all URLs identify the same exact release.
3. Run the runner tests and all real journeys. Require the actual hosted
   `synthetic-browser` PR job to pass, and review its bounded receipts and
   screenshots before accepting the update. A unit-test pass alone does not
   qualify a browser update.
4. Keep the update in a reviewable commit. For rollback, revert the pin update
   as a new commit and rerun the same gates; do not change a release tag or
   replace a hash to silence a failed download check.

## Retained diagnostics

The intake journey uses nonblocking Chrome navigation. Explicit reloads wait
for the old document to detach and the replacement document to finish loading
before checking receipt rows or reselecting an interrupted upload. A stalled
reload fails within the existing browser wait deadline.
Both the standard stale-element response and ChromeDriver's specific
detached-node inspector response count as old-document detachment; other
driver errors still fail the run. The Report journey logs completed checks as
they happen and caps page-load and script commands at 30 seconds, so a stalled
command can report its failure before the overall journey deadline.

The runner copies only an explicit allowlist of generated receipts, screenshots,
failure HTML and browser-error JSON, plus the last 256 KiB of each child log.
Receipts and browser-error JSON are limited to 64 KiB each; other allowed files
to 2 MiB each, with a 16 MiB total copy budget. Oversized and nonregular files
are omitted and listed in `journeys.json`. The small `summary.json` and
`journeys.json` are additional runner metadata. The output never recursively
copies a journey directory. Runtime databases, browser profiles, downloaded
archives and exported bundles are excluded. CI uploads only this sanitized
output directory and retains it for five days, including when a journey fails.
When a journey fails, the runner also prints, under the failed journey's name,
the runner's own reason (such as a non-zero exit or a rejected receipt) and the
last 12 lines of its child log (Linux and macOS native driver stack frames
omitted, each line bounded to 300 characters), so the job log shows where it
stopped even when the artifact cannot be downloaded.

Check `summary.json` first, then the failed journey's log and screenshot. A
receipt proves only the generated steps recorded in its `checks` array. The
[review policy](QUALITY_GATES.md#review-policy) applies separately before merge.

The additional `browser-accept-source-folders.py` journey also checks continuous
source inspection, list-only keyboard navigation, retained library filters, and
screenshots at 1440 and 390 pixels. See
[the browsing contract](CONTINUOUS_SOURCE_BROWSING.md) for scope and boundaries.
Run this journey separately with the same pinned Chrome and ChromeDriver.

The additional `browser-accept-exact-search.py` and
`browser-accept-full-text-review.py` journeys accept the same explicit verified
Chrome and ChromeDriver binaries. They cover matter Search navigation, source
inspection and scoped-question returns to paginated results, and desktop/mobile
full-review controls ahead of the decision list. These additional journeys are
separate from the two default hosted journeys. See
[search and review navigation](SEARCH_REVIEW_NAVIGATION.md).

Report downloads wait for Chrome's temporary download to disappear, a nonempty
final file, and a readable ZIP container for ZIP/Word outputs before inspecting
contents. A reserved filename alone is not completion. Synthetic regressions
cover empty placeholders, partial archives, and completed archives while retaining
the existing bounded browser wait.

## Dusk source readability

The pinned runner also runs a disposable source-library theme journey with 26
synthetic text sources and a generated collection. It checks rendered text and
placeholder contrast of at least 4.5:1 for status cards (idle, hover and selected),
collection chips, filters, bulk actions, and disabled controls, including paged
navigation. Disabled text uses the same readability target even though inactive
controls are exempt from WCAG text contrast requirements. The contrast calculation
composites ancestor backgrounds and opacity. Keyboard traversal checks a solid
focus outline of at least 2px and 3:1 against the control and its parent surface.
The journey also checks mobile filters and a Light theme round trip.

The `dusk` artifact directory contains bounded synthetic screenshots of status
cards, filters, disabled and enabled bulk actions, keyboard focus and mobile
layout. These checks cover source-library controls, not every Dusk screen or
native operating-system select popup. Dusk overrides use existing theme tokens;
Light styles and source workflows are unchanged. This journey is independent of
the intake-selection acceptance and does not require changes to its runner steps.

The standalone Dusk journey clears deployment environment settings before it
constructs its disposable application, using the shared synthetic environment
helper. This applies both to direct invocation and invocation through the runner.

The Dusk journey checks that disabled-button overrides remain confined to the
source library and preserve disabled styling elsewhere.
After switching back to Light, it waits for the complete computed-style snapshot
to match the original, including color, background color, opacity and border
color for every captured element. The theme attribute alone does not establish
that styles have settled. A timeout identifies the first differing element and
property (or a changed element count); the comparison and timeout remain strict.
