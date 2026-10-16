# Installer terminal evidence — issue 258

Synthetic presentation evidence recorded 2026-10-16. These captures do **not**
claim a supported Linux installation or doctor acceptance. The banner and stop
lines in the text and cast files use the current labels (`Exculpata installer`,
`This account runs this command.`, and `Install stopped`). The PNG frames were
re-rendered from those updated casts; they are not a new terminal recording.
The rest of each capture is unchanged.

- [Terminal preview](terminal-preview.png) renders the actual PTY bytes in the
  [asciinema v2 cast](terminal-preview.cast), captured with an actual 80-column
  PTY and rendered on its fixed monospace grid. The wordmark uses seven-column
  glyph cells; a regression checks its 73-column width and U/L/P stem alignment.
  The dark canvas belongs to the
  preview; the installer does not set a terminal background. The cast contains
  no environment dump, host identity, password, or token.
- [Narrow progress](narrow-progress.png), [failure](narrow-failure.png), and
  [40-column cast](narrow-terminal.cast) record the actual console running a
  bounded synthetic child, not a Docker engine. The wordmark falls back to one
  line, each spinner frame leaves the final column unused, and cleanup adds no
  blank line. The child exits 17; its complete output is retained at mode 0600
  during capture, and the temporary fixtures are removed afterwards.
- [Plain preview](plain-preview.txt) is the same real invocation with `--plain`,
  with trailing blank cells trimmed in the text artifact.
- [Animation timing](animation-timing.json) measures three actual PTY runs each
  with and without animation, rendering the banner and eight synthetic phase
  titles. Median added time was 0.452 seconds. The shared typing-delay budget is
  0.45 seconds; progress runs concurrently with the child and adds no sleep to it.
- [Forced failure capture](synthetic-failure.txt) executes a synthetic child
  named `docker`, emitting 56 lines and exiting 17. The real logger retains the
  full output at mode 0600 and displays the final 40 lines and path. This tests
  the subprocess/logging boundary; it is not a Docker engine build. The
  temporary node and its log were removed after capture.

Preview invocation (the checked Python 3.12 interpreter was used for capture):

```console
./install --models none --auth local --enable-account-management \
  --server-name localhost --bind-address 127.0.0.1 \
  --root /tmp/exculpata-synthetic-preview --non-interactive --dry-run
```

On this macOS ARM host, the real installer correctly exits 1 at PHASE 01 with
an unsupported-platform BLOCK. Neither configured Docker context has a running
daemon, no Colima/Lima VM is available, and the disk has approximately 69–70 GiB
free, below the unchanged 100 GiB reserve. No host networking, security,
service accounts, persistent access, or deployment settings were changed.

Still required before acceptance: a complete synthetic CPU install on a
disposable supported x86-64 Linux host with sufficient storage and Docker,
covering every selected phase, the real collapsed build and final handoff,
`./install doctor`, a plain run, and a real forced Docker build failure.
The CPU `--models none` path retains its existing omission of model phase 07;
the restyle does not manufacture a model-staging event.

Palette review: banner and phases use cyan; OK uses green; WARN/BLOCK use amber;
details use neutral gray. Each surface has one accent, and status words remain
visible in plain output. Independent hostile review found a plain-mode child
Compose-coloring gap; the fix now forces plain child rendering for plain-mode
runs and for logged operations, with a real inherited-FD regression.
