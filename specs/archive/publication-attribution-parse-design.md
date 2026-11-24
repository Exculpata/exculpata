# Publication attribution parsing: design note (r3)

## Decisions (one screen)

1. **Locate, then parse.** Locating a span is structural and permissive: the author field, the committer field, or the bracket contents of a recognized trailer line. Parsing is one strict function, `parse_attribution_address(value: bytes) -> bytes | None`. It takes exact bytes, with no trimming. There is no second grammar and no bypass.
2. **Grammar (lead decision 1).**
   - Local part: RFC 5322 dot-atoms over `A-Za-z0-9` and ``!#$%&'*+/=?^_`{|}~-``, plus `[` and `]`. Dots only between atoms, so no leading, trailing or doubled dots. No quoted strings.
   - Domain: ASCII LDH labels, at least two, no leading or trailing hyphen in a label. Non-ASCII, single-label and empty domains are unsupported.
3. **A span that parses is granted.** The scanner hashes the whole address, lowercased, against `MAINTAINER_EMAIL_DIGESTS`. Any `EMAIL` token wholly inside the span skips both email rules. A `[bot]` address takes exactly this path.
4. **A span that fails the parse is never silent (lead decision 3).**
   - It is not granted. It emits this finding rule, exactly: `unsupported-attribution-address; use parse_attribution_address() with one RFC 5322 dot-atom address`.
   - Ordinary content rules still run over the original bytes, so a protected token inside a malformed value is still reported.
5. **The same path covers author, committer and trailer brackets.** One policy, no separate trailer path.
6. **No exact-match grants (lead decision 2).** `PUBLIC_BOT_NOREPLY_ADDRESSES` grants nothing in an attribution span unless the address parses. The bracketed members parse under decision 2, so the history needs no exception (see Blast radius). Body and file handling of automation addresses is unchanged (`_recognized_public_automation_email`, `scripts/publication-check.py:263-277`, called at `:355`).
7. **Remove** the permissive trailer capture group, the `.strip()` in `_scan_bytes`, and the grant-by-token-containment logic.
8. **No override seam.** Production code gets no environment or CLI way to inject digests. Tests inject synthetic digests by monkeypatch, or by editing the isolated scanner copy for the subprocess entry points.
9. **Hook step dropped.** CI uses the repo copy. The maintainer's local guard keeps the old scanner until it is reinstalled into a new directory (procedure under Blast radius). That is a maintainer step, not a build step, and `scripts/install-publication-hook.py` stays untouched.
10. **Failing-first rule.** Only the defect regressions must fail at 30285da. The controls must pass there, to show preservation. Each list below was run against the real `_scan_bytes` at 30285da with injected synthetic digests, in memory.
11. **Domain split (lead decision 4).**
    - This note's prose and examples use only domains the file scan allows: `example.com` and `example.org`. The allowlist is at `scripts/publication-check.py:358-366`: `example.com`, `example.net`, `example.org`, `example.test`, `example.invalid`, `users.noreply.github.com`, and any `.example.test` subdomain.
    - Executable test fixtures use `synthetic.invalid`, which is not on that list, so it never appears in this note as an address. In the prose, a fixture address is described by shape only.
    - That lets the note live at `specs/publication-attribution-parse-design.md` and pass the unchanged file scan. The "Self-scan" section says how that is checked. This text was scanned that way with an empty result.
12. **Unchanged:** body, file, tree, ref, archive, media and PDF scanning, and the 6 `REVIEWED_COMMIT_EMAIL_IDENTITIES` entries.

## Problem

PR #501 failed review twice on the same defect. The attribution exemption treats "an `EMAIL` token lies inside a span" as "this address is fine". Findings open at 30285da:

- **P2:** a trailer bracket such as `<person@example.com,>` or `<person@example.com/path>` is admitted as an address.
- **P2:** a distinct valid address containing a protected address as a substring is blocked (a local part such as `other'` followed by a protected local part).
- **P3:** the field matrix covers two of the three synthetic digests.

Review r1 found four more P2s in the first design: grammar too strict, doubled dots allowed, no real diagnostic, wrong hook step. Review r2 found two more: a bot bypass that contradicted the fail-closed policy, and a note that would itself fail the file scan. Both are fixed below, as is one baseline misclassification (P3).

## Root cause (verified against 30285da)

- `scripts/publication-check.py:61-64` (`ATTRIBUTION_TRAILER`): the capture is the negative class `[^<>\s\0:;@]+@[^<>\s\0:;@]+`. It admits `,` and `/` in the domain and is the only validation.
- `:310-312` and `:352-354`: any `EMAIL` token wholly inside a span is skipped, and the span itself is never validated.
- `:314-322`: the whole-address hash is added, but the `EMAIL.finditer(data)` hash still runs over the full buffer. `EMAIL` (`:120-123`) has no `'`, `=` or `!` in its local class, so `other'` followed by a protected address yields the bare protected token. That is the substring false block.
- `:815-819`: identity fields pass an unvalidated whole-field span, so both defects apply to author and committer.
- `tests/test_publication_gate.py:795,804` define two addresses and select `[0, 1]`. `tests/test_publication_candidate.py:94` does the same. That is the P3.

Two further facts shape the fix:

- `EMAIL` yields no token for an empty local part, a single-label domain or a non-ASCII domain. For these, the old fallback produces nothing, so the parse failure must itself produce a finding.
- `synthetic.invalid` is not in the example-domain allowlist at `:358-367`. That is why `non-example-email-address` fires on synthetic fixtures, and why this note cannot use it in addresses.

## Approach

### Grammar (bytes, ASCII only, exact input)

```
address = local "@" domain
local   = atom ("." atom)*          total 1 to 64 bytes
atom    = 1 or more of [A-Za-z0-9!#$%&'*+/=?^_`{|}~\[\]-]
domain  = label ("." label)+        total 253 bytes or fewer
label   = [A-Za-z0-9] ([A-Za-z0-9-]{0,61} [A-Za-z0-9])?
```

- The 64, 253 and 63 length limits are inherited from `EMAIL`. The lead did not set them.
- Whitespace, NUL, `<>`, `,;:\"()@` in the local part, and any byte at or above 0x80 all fail the parse. `[` and `]` are atom characters.
- The last label need not be alphabetic, so a domain ending in a digit label parses. No `EMAIL` token exists for it. Other rules still scan the original bytes, so nothing is lost.

### Locating spans

- **Author and committer:** the `%ae` and `%ce` fields, at offset `len(name) + 1` in the `name NUL email` buffer, as now. The span is the whole field, with no trimming.
- **Trailers:**
  - Git stays the oracle for the final trailer paragraph (`%(trailers:unfold=false)` plus the `message.endswith(trailers)` check).
  - New line pattern, keeping the current key and name rules: `(?m)^[A-Za-z0-9-]+[ \t]*:[ \t]+[^<>\s\0][^<>\r\n\0]*[ \t]+<([^\r\n]*)>[ \t]*\r?$`.
  - Group 1 is everything up to the last `>`. It may contain `<`, `>` or NUL, so `<<addr>>` and `<addr\0>` become spans that fail the parse instead of unrecognized lines.
  - The locator trims ASCII space and tab at both ends of group 1 and returns exact bounds. An empty `<>` gives an empty span, which fails the parse and is reported.
- **Not recognized, so no span:** a bracket that is not at line end, a nameless `<addr>` line, continuation lines, and a non-final paragraph. These fall to the content rules only, as in the body. State that limit in the docs.

### Scanner changes in `_scan_bytes`

For each span, take `raw = data[start:end]` and call `parse_attribution_address(raw)`. There is no pre-parse branch.

1. On a result, the span is granted and its lowercase is hashed against `MAINTAINER_EMAIL_DIGESTS`.
2. On `None`, the span is not granted. Append `Finding(location, UNSUPPORTED_ATTRIBUTION_ADDRESS)` once per call, before the maintainer and non-example rules.
3. A single `granted(match)` predicate is true for a token wholly inside a granted span. Use it in both the maintainer-hash loop and the non-example loop. "Wholly inside" is deliberately narrower than "overlaps". Spans end at `<`, `>`, NUL, a line end or the buffer end, none of which are `EMAIL` token characters, so no token should straddle an edge. If one ever does, it is not skipped.
4. The content-token hash and every other rule keep running over the original bytes. `operator-deny-term`, `internal-fqdn`, secret and home-path checks therefore still see the address.
5. `_recognized_public_automation_email` stays in the non-example loop for body and file content only. Inside a granted span the token is already skipped by step 3. Inside a failed span the address is not granted, so the automation rule is not an attribution grant.

`UNSUPPORTED_ATTRIBUTION_ADDRESS` is a module constant. It follows the precedent at `:487-490` (`unsupported-media-metadata; use _scan_media() with ffprobe JSON object output`).

### How the diagnostic is reported

- `main()` prints `  {rule}: {location}` to stderr (`:1049-1050`), so the line is `  unsupported-attribution-address; use parse_attribution_address() ...: git-metadata`.
- It names a helper and never the value. `Finding` holds only a location and a rule.
- It satisfies `tests/test_checker_fail_closed_contract.py:113-115`, which needs `"unsupported"` and the helper name in the finding string. It also satisfies `CODING_STANDARDS.md:58-64`: an unsupported diagnostic naming a supported helper, with an empty result for the control.
- `scan_history` dedups through `main`'s `set`. No new output channel and no new exit path.

### Findings by case

Order within one `_scan_bytes` call: unsupported, then maintainer, then non-example. Shapes below use `P` for a protected local part and `example.com` as the domain. Fixtures in tests use `synthetic.invalid`.

| Span content (shape) | Result |
| --- | --- |
| `person@example.com` | none |
| `other=P@...`, `other'P@...`, `other!P@...` (distinct valid address) | none (granted) |
| a well-formed `[bot]` local part, any allowed domain | none (granted) |
| a protected address | `maintainer-email-address` |
| `person@example.com,` or `.../path` or `;` | unsupported (non-example too if the domain is not allowed) |
| `<<person@...>>`, `mailto:person@...`, `extra person@...` | unsupported (non-example too if the domain is not allowed) |
| a protected address followed by `,` | unsupported, maintainer (non-example too if the domain is not allowed) |
| `other..'P@...` | unsupported, maintainer (the token path sees the bare protected address) |
| `.P@...` and `P.@...` | unsupported (the token keeps the dot, so there is no hash match; non-example too if the domain is not allowed) |
| empty local part, single-label domain, non-ASCII domain | unsupported only (no token exists) |
| empty `<>` | unsupported only |
| a `[bot]` local part with a malformed tail (`,`) or a single-label domain | unsupported (no longer silent) |

An allowed example domain inside a malformed span gives unsupported only. That is deliberate. The old state was silent, and now it is a finding.

## Blast radius

- **Callers passing spans:** only two sites in `scan_history`, the identity loop at `:815-820` and the message scan at `:848-851`. All other callers omit the parameter and are unchanged. They are PDF, media, archive, content, tree names, ref names and tag messages.
- **Entry points:** `make publication-check`, `scripts/pre-push-publication.py` (runs the sibling scanner with `-I` against a temporary clone, with `--skip-history` for later tips) and `scripts/check-publication-candidate.py` (calls the pre-push guard). All three share this scanner and policy. None needs an edit.
- **History scan, rechecked for decision 2 (no bypass).** Probe: `git rev-list --all` (1582 commits), `git show -s` per commit, then the final grammar applied in memory. It printed counts only and wrote nothing.
  - Distinct author and committer emails: 7. All 7 parse. 0 empty, 0 malformed.
  - Identity fields (author or committer) whose local part contains `[`: 10. All 10 parse. This is the public Dependabot no-reply identity.
  - Trailer address spans on structurally recognized lines, all refs: 58, the same count with the old and the new locator. All 58 parse. 6 of them carry a `[`. 1 is the other automation no-reply member. 0 failures.
  - So the bracketed `[bot]` author and trailer fields now parse, get the whole-address hash, and pass. No exception is needed, and the history scan raises no new findings. Parse success only removes the "unsupported" outcome, and the hash and token rules see the same bytes as before, so the new finding set is a subset of the old one.
  - At 30285da the full scan of this checkout reports 0 findings with location `git-metadata`. In this environment it reports 19 unrelated findings, 18 `document-inspection-failed` and 1 `private-network-address`, none attribution related.
  - Every identity email in history parses, which includes the fields of the 6 reviewed commits. The exact-commit filter at `:825-831` removes only `maintainer-email-address`, so it cannot hide the unsupported finding.
- **How the build proves it, read-only and without printing an address.**
  1. A regression test on throwaway repositories reuses the Dependabot fixture (`test_dependabot_commit_identity_is_public_and_denied_mail_still_blocks`, `tests/test_publication_gate.py:466`) in author, committer and trailer roles and expects no findings.
  2. Before merge, run `python3 -I -B scripts/publication-check.py` on the base and on the head, with stderr captured. `main()` prints only `  {rule}: {location}` lines. A location is `git-metadata`, a path or a `git:<12 hex>:<path>` string, never an address. Reduce each run to the lines containing `git-metadata`, compare the two reductions, and count lines containing `unsupported-attribution-address`. Pass means the count is 0 and the two reductions are equal. Only rule names and counts are pasted into the PR.
  3. `-B` stops bytecode writes, and the scan only reads. Check that `git status --short` is empty afterwards.
- **Reviewed or `--allow-public-git-identity` identities that do not parse** would now be a finding. None exists in this repository.
- **Unchanged:** the 6-entry `REVIEWED_COMMIT_EMAIL_IDENTITIES`, the squash co-author span logic at `:833-845`, baseline handling at `:693` and `:975`, deny terms and tree scanning.
- **Installed guard (maintainer step, not this PR).**
  - `make hooks` with `core.hooksPath` set only verifies the existing external guard and prints `Preserving installed external pre-push guard` (`Makefile:19-26`). It copies nothing, so it never refreshes the scanner.
  - A fresh install (`Makefile:27-33`) creates a new `$(PUBLICATION_GUARD_ROOT)/reviewed-XXXXXXXX` directory via `mktemp`. It builds an external venv and runs `scripts/install-publication-hook.py "$guard/hooks"`.
  - The installer refuses an existing destination, and refuses a destination or interpreter inside the checkout (`:13-19`). It copies four scripts, writes a hook that runs `-I`, and sets the local `core.hooksPath`.
  - To refresh: unset the local `core.hooksPath` and rerun `make hooks`, or run the installer by hand with the existing external interpreter and a new directory. Then verify `git config --local core.hooksPath`, and that the installed `publication-check.py` defines `parse_attribution_address`.
  - The PR body says the old guard stays active until then. None of this was run for the note.
- **Docs:** `docs/PUBLIC_ALPHA.md:86-88` says "All other addresses are allowed in those fields". `CHANGELOG.md:5-7` repeats it. Both need the grammar, the unsupported rule and the unrecognized-layout limit. `docs/PUBLICATION_CHECKLIST.md` needs the same.

## Tests that prove it

Fixtures are synthetic (`*@synthetic.invalid`) with injected digests. Results come from `scan_history` on throwaway repositories unless noted. Every item below was classified by running the real `_scan_bytes` at 30285da on identity-field and trailer spans, in memory.

**Defect regressions (each must fail at 30285da):**

- Distinct addresses containing a protected one, expect no finding, for each of the three digests in author, committer and trailer: the `other'`, `other=` and `other!` prefixes. At 30285da all 9 combinations per role return `maintainer-email-address`.
- Malformed trailers and identity values from the table above. At 30285da:
  - the comma and `/path` forms return nothing;
  - the `;`, `<<...>>`, `mailto:` and `extra` forms return only `non-example-email-address` in a trailer;
  - a protected address plus `,`, and the `other..'` form, return only `maintainer-email-address`;
  - the leading and trailing dot forms return nothing.

  Expect the exact new lists, including the unsupported rule string.
- No-token malformed values, expecting only the unsupported rule: empty local part, single-label domain, non-ASCII domain, empty `<>`. At 30285da each returns nothing.
- Leading, trailing and doubled dot cases in author, committer and trailer. At 30285da each returns nothing.
- **Bracketed automation trailers that are malformed.** A `[bot]` local part with a trailing `,`, and one with a single-label domain, in a trailer.
  - At 30285da both return nothing (granted by token containment). The same malformed tail on a `PUBLIC_BOT_NOREPLY_ADDRESSES` member also returns nothing.
  - After the change each reports the unsupported rule.
  - This is the `[bot]` trailer case moved out of the controls (lead decision 5). The earlier "non-member with brackets reports unsupported" negative is dropped, because a well-formed bracketed address now parses and passes (see Controls).
- `<...\0>` in a trailer. `_trailer_address_spans` now returns a span for it, and `_scan_bytes` reports the unsupported rule. This replaces `test_nul_inside_trailer_address_is_not_exempted` (`tests/test_publication_gate.py:917`), which asserts `()`.
- Update `test_malformed_trailer_values_keep_content_email_rules` (`:879-888`) to the new lists. Move the nameless `  <addr>` layout to a separate control test.
- Contract adapter in `tests/test_checker_fail_closed_contract.py`:
  - Add `_attribution(monkeypatch, unsupported)` with param id `publication-malformed-attribution-address` and helper `"parse_attribution_address"`.
  - Control: a `Signed-off-by: Synthetic Person <...>` trailer holding a plain dot-atom address on the fixture domain. Probe: the same trailer with the domain cut to one label. That leaves no `EMAIL` token, so only the new diagnostic can fire, and the probe returns nothing at 30285da.
  - Build spans with `_trailer_address_spans` and call the real `_scan_bytes`. Mock nothing.
  - The control returns `[]`. The probe returns strings containing `unsupported` and `parse_attribution_address`.

**Controls (each passes at 30285da, kept to prove preservation):**

- Field matrix over all three digests and {author, committer, `Co-Authored-By`, `Signed-off-by`}, each yielding exactly `[maintainer-email-address]`. Parametrize with `range(len(addresses))` so a future set size cannot shrink coverage. Extend the fixture to return all three.
- Dot-atom punctuation (`person!x@...`, `person+tag@...`, `o'connor@...`, `a.b@...`) allowed in author and trailer roles, each returning nothing. Existing public no-reply controls stay.
- Well-formed bracketed addresses pass: a `[bot]` local part on `synthetic.invalid`, and each `PUBLIC_BOT_NOREPLY_ADDRESSES` member, in author, committer and trailer roles, each returning nothing. The existing Dependabot identity test passes unchanged. A member in a body (file rule) still returns nothing, which keeps decision 6's "unchanged" claim honest.
- Upper-case and padded address (a padded non-protected address returns nothing, a padded upper-case protected one returns `maintainer-email-address`), mixed-case keys, several trailers in one block, a later paragraph (no span), and CRLF via `git hash-object` (extend `test_crlf_trailers_preserve_raw_commit_bytes` to three digests).
- The nameless `  <addr>` layout returns only `non-example-email-address`, which records the unrecognized-layout limit.
- Body, subject, non-final paragraph, `Contact` line and `scan_tree` content still report both email rules. The existing layout matrix stays (existing tests, not re-run for this note).
- Reviewed identity: monkeypatch an exact-commit reviewed identity, then a trailer with the same address on that commit still blocks. The six-entry assertion stays.
- Shared policy in `tests/test_publication_candidate.py`: `candidate` and `pre-push` over one payload table (three protected, one dot-atom substring, one malformed, one body). Exit codes are identical. The protected and body rows pass at 30285da. The substring and malformed rows fail there, so they count as regressions.

**Digest-injection helper.** `str.index` at `tests/test_publication_candidate.py:98-99` raises `ValueError` when a marker is missing, so it does not skip silently. The real reasons for a helper are different:

- the slice replaces whatever sits between two comments, with no check on what it replaced;
- it needs three digests now.

The helper uses `re.subn` on the single `MAINTAINER_EMAIL_DIGESTS` assignment and asserts exactly one replacement. It then checks that the injected scanner's digest set equals the intended one.

**Failing-first statement for the PR body.** Name 30285da as the commit. State that the defect regressions above fail there and the controls pass there. Run the controls before the fix to show that.

## Self-scan

This note is a file the unchanged scanner reads. Before adding it, run the real `_scan_bytes` on its bytes with no deny terms and expect an empty list. The prose avoids non-example domains, internal-looking names, home paths and secret-shaped assignments, and it prints no real address. The same check must hold for `docs/PUBLIC_ALPHA.md`, `docs/PUBLICATION_CHECKLIST.md` and `CHANGELOG.md` after their edits.

## Exact file fence

Edit:
- `scripts/publication-check.py`
- `tests/test_publication_gate.py`
- `tests/test_publication_candidate.py`
- `tests/test_checker_fail_closed_contract.py`
- `docs/PUBLIC_ALPHA.md`
- `docs/PUBLICATION_CHECKLIST.md`
- `CHANGELOG.md`

Add:
- `specs/publication-attribution-parse-design.md` (this note; the directory does not exist yet, and `VISION.md:20` supports it)

Do not touch:
- `scripts/pre-push-publication.py`, `scripts/check-publication-candidate.py`, `scripts/install-publication-hook.py`
- `Makefile`
- `.github/workflows/*`
- any other test

The diff should stay near 500 changed lines, and tests are the bulk. If it passes that, split the docs and changelog into a second PR.

## Check against the project rules

- **VISION.md:** strictness goes up, nothing is exempted by token containment any more, and the protected set is checked everywhere. Removing the containment grant and the exact-address grant removes exceptions rather than adding one.
- **AGENTS.md:**
  - The PR needs a Kickoff section and a regression test that fails at 30285da, with that commit named.
  - Fix level is code: a typed parse plus an unsupported diagnostic.
  - No `--no-verify` and no force-push.
  - Public text stays neutral and synthetic.
- **CODING_STANDARDS.md:**
  - Synthetic fixtures only.
  - Each temporary-repository write carries a `# fixture-write:` comment or a `fixture_write` handle.
  - The bounded fail-closed contract is met by the new adapter and the named diagnostic.
  - Run the publication tests and `make check-fast` before pushing. They were not run for this note, because they write fixtures. The history probes and the 30285da case matrix were read-only, in memory.
