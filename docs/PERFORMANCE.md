# Time to briefing benchmark

[`scripts/benchmark-time-to-briefing.py`](../scripts/benchmark-time-to-briefing.py)
measures the wait from starting a synthetic folder selection to whole-corpus
query readiness, automatic discovery completion and, when available on main,
briefing construction. It uses the existing application and intake routes; it
does not change product behavior.

## Run it

Use the contributor Python environment prepared by `make bootstrap`:

```console
.venv/bin/python scripts/benchmark-time-to-briefing.py --output /tmp/recordbench-time-to-briefing.json
```

The default sizes are 100, 1,000 and 5,000 files, with deterministic seed
`20261009` and `--density sparse`. To choose a size or seed:

```console
.venv/bin/python scripts/benchmark-time-to-briefing.py --sizes 20 --seed 20261009 --output /tmp/recordbench-time-to-briefing-small.json
```

Use the denser 1,000-file workload to observe the existing automatic-discovery
storage limit:

```console
.venv/bin/python scripts/benchmark-time-to-briefing.py --sizes 1000 --density dense --output /tmp/recordbench-time-to-briefing-dense.json
```

`--temp-root` changes the temporary parent directory from `/tmp`. Both that
directory and `--output` must be outside the checkout. Each size runs in a fresh
subprocess, creates its corpus and application storage in a temporary directory,
then removes those inputs and runtime files. The JSON receipt remains at the
chosen output path and is updated after each completed size. The default
`--timeout` is 7,200 seconds per size.

Fetch main before running when checking whether briefing has landed. The script
records the `origin/main` commit and checks that revision for
`src/case_intelligence/briefing.py`. If present, the checkout must also contain
the module and the benchmark calls `build_briefing`. Otherwise the receipt
records `build_briefing.status` as `not_available`, with null timings.
`--main-ref` can name a different main reference; the N=20 shape test explicitly
uses `HEAD` so that it also works in a shallow CI checkout without `origin/main`.

The strict JSON receipt records the measured Git commit, checkout cleanliness,
main revision, Python version, CPU count, configuration (including `density`),
deterministic corpus digest, file counts and bytes, stage timings, final
readiness, outcome, peak RSS and database bytes. It contains no hostnames,
usernames or absolute paths.

## Workload and measurement boundaries

The corpus cycles evenly through text notes, email, CSV schedules and native-text
PDF reports. Invented records are arranged by production, synthetic custodian
and document category. The default **sparse** corpus deliberately limits added
entity-name content to one record in every 100, in addition to ordinary email
headers. It gives a completion baseline within the existing discovery budget;
it is not representative of entity-rich discovery. File count alone therefore
does not establish supported matter capacity.

The **dense** corpus repeats invented names and dates in every file's content
rows. The 1,000-file dense run is intended to expose the fixed 16 MiB automatic-
discovery storage budget without changing that budget. Its measured outcome is
reported separately below. The digest includes each relative filename and its
bytes; the same size, seed and density reproduce the corpus.

The benchmark starts a fresh SQLite/basic application with two background
ingestion workers and automatic discovery enabled. It uses the normal metadata
preflight, selection receipt, resumable upload session, chunk and finalization
routes through an in-process HTTP test client, transferring and finalizing files
sequentially while ingestion and discovery run in the background. Application
environment overrides are cleared and outbound network access is blocked. No external services,
PostgreSQL retrieval index, embedding/reranker/generation models or malware
scanner are exercised. PDFs have native text; scanned-page OCR and media
transcription are outside this workload. These measurements describe this small
synthetic workload, not production discovery capacity or model latency. With no
PostgreSQL connection, the application indexing callback returns immediately; the measured indexing
stage is its readiness/job handoff and cleanup, not learned indexing. A zero
sampled duration means no active interval was observed, not proven zero work.

Corpus generation, application startup and matter creation finish before the
selection clock starts. Browser rendering, a human's file picker time and network
transfer latency are outside the measurement. The stage fields mean:

| Stage | `wall_seconds` |
| --- | --- |
| `selection` | Folder enumeration, file metadata preflight and complete sealed selection receipt. |
| `intake` | Upload-session admission and transfer until readiness first reports all selected files saved. |
| `extraction` | Sampled union of intervals with one or more sources extracting. |
| `indexing` | Sampled union of intervals with one or more sources indexing. |
| `automatic_discovery` | Sampled union of intervals with ready sources awaiting discovery while the current progress is not budget-paused. |
| `can_query` | Cumulative time from selection start until readiness permits queries and every selected file is searchable. |
| `build_briefing` | Direct wall time of `build_briefing`, called after whole-corpus readiness and a terminal automatic-discovery outcome, when available. |

`completed_seconds` is the first observed completion time measured from selection
start. Readiness and discovery are sampled at a target interval of 50 ms; query
and lock time can lengthen that interval. Short activities can fall between
samples. Active intervals are counted once even when multiple workers perform
the same stage. Stages overlap, so their durations must not be summed. In
particular, `can_query` is a cumulative milestone, not another processing
duration; automatic discovery can finish after it. “Slowest stage” below compares
the processing stage durations and excludes that cumulative milestone.

A run's `outcome` is `complete` when automatic discovery finishes, or
`discovery_budget_exhausted` when its existing budget is reached. On exhaustion,
`automatic_discovery.status` is `budget_exhausted`, its `completed_seconds`
stays null, and `budget_exhausted_seconds` records the first observed exhaustion
time from selection start. This is an incomplete discovery outcome, not a
successful time to complete briefing. Normal intake and extraction continue
until all selected files are searchable, so the receipt still records the
whole-corpus `can_query` milestone and last-observed discovery coverage. Later
uploads can unpause discovery while their inventories are sealed; the benchmark waits for
the final whole-corpus budget pause. Discovery active time includes those
unpaused intervals, while `budget_exhausted_seconds` keeps the first pause. No
budget is raised and no source is silently omitted to make the dense run complete. The
16 MiB suggestion-data budget is not a cap on the database file size.

Peak RSS is the worker process's high-water mark read after the app closes,
including imports, corpus generation, the app and measurement code; helper
subprocess RSS is not included. Each size has its own process,
so a larger earlier run cannot set a later run's peak. Database bytes sum the
application's SQLite/database files after the app closes; they exclude source
files and other runtime artifacts.

## Container results

Measured on October 9, 2026 in this **container, using SQLite and basic
retrieval**, with Python 3.12.14, 5 reported CPUs, two ingestion workers and seed
`20261009`. These are single-run container numbers, not hardware targets or
statistical estimates. Corpus generation, app startup and browser rendering are
excluded. The test suite was idle during these measurements.

The receipts record HEAD `d6f9f7ed2b9bc0e159e8045acb5ae3f6bab13f39` and
`clean: false`: the dense-mode and measurement refinements in this PR were
uncommitted. Application code was based on main revision
`61c4bbd8027c192b87180071cc0c0c0ff96e3131`. **Briefing was not available on main**;
`build_briefing` is `not_available`, not a measured zero. The later main
revision `1424d005db6942064c0b0d98a775f12414531bf2` also lacked briefing and only
changed an existing test fixture. Validation subsequently updated the branch
to main `d0314aa`; that update changes tests, browser acceptance and documentation,
with no application-code changes from the measured revision.

The following baseline uses **sparse** entity content. It must not be read as a
claim that ordinary 5,000-file discovery completes within the discovery budget.

All timings are seconds. RSS and database sizes are MiB (bytes / 1,048,576).

| Files | Selection | Intake | Extraction | Indexing | Automatic discovery | Can query (cumulative) | Build briefing | Slowest stage |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 100 | 0.02 | 1.33 | 1.89 | 0.00 | 0.41 | 2.07 | Not available | extraction |
| 1,000 | 0.13 | 65.28 | 38.28 | 1.77 | 24.02 | 65.42 | Not available | intake |
| 5,000 | 0.56 | 901.02 | 500.87 | 16.22 | 277.62 | 901.83 | Not available | intake |

| Files | Corpus MiB | Peak RSS MiB | Database MiB |
| ---: | ---: | ---: | ---: |
| 100 | 0.11 | 89.20 | 2.71 |
| 1,000 | 1.04 | 101.85 | 14.78 |
| 5,000 | 5.24 | 136.46 | 69.44 |

### Dense 1,000-file budget outcome

The dense run used the same container, Git metadata and seed, with 20 repeated
people/date-bearing statements per file. It hit the existing **16 MiB discovery
budget at 3.14 seconds**. All 1,000 files became searchable, but only
40 completed automatic discovery; 960 remained pending, with
0 unsealed and 0 source failures. The final readiness flag was
`budget_reached: true`, and discovery has no completion timestamp.

| Files | Outcome | Discovery budget observed at (s) | Discovery complete / ready | Can query (cumulative s) | Peak RSS MiB | Database MiB |
| ---: | --- | ---: | --- | ---: | ---: | ---: |
| 1,000 | `discovery_budget_exhausted` | 3.14 | 40 / 1,000 | 244.94 | 102.85 | 32.81 |

This is not a completed-discovery timing. Increasing file count is not the only
limit: richer entity content reaches the current suggestion budget even at
1,000 small files. The default corpus is deliberately sparse to stay inside that
budget; it must not be used as a capacity promise for ordinary discovery.

## Profile the 1,000-file run

```console
.venv/bin/python scripts/benchmark-time-to-briefing.py --sizes 1000 --density sparse --profile --output /tmp/recordbench-time-to-briefing-profile.json
```

`--profile` collects cProfile data from the foreground, request, ingestion and
discovery threads with one interpreter-wide profiler on Python 3.12+. Its receipt
ranks the top three application functions under
`src/case_intelligence` by self time, excluding idle runtime locks, selectors
and inclusive thread runners. PDF extraction helper subprocesses are not
profiled by this cProfile run. Cumulative time includes called functions and can
overlap across rows and threads. Profiling changes timings; use a separate
unprofiled receipt for the timing table above.

The profile used the same 1,000-file corpus (matching digest), commit, dirty
checkout status and container configuration as the sparse baseline.

| Rank | Application function | Calls | Self seconds | Cumulative seconds |
| ---: | --- | ---: | ---: | ---: |
| 1 | `src/case_intelligence/workspace_store.py:611` — `_upload_item` | 8,008,000 | 19.67 | 32.93 |
| 2 | `src/case_intelligence/workspace_store.py:6101` — `upload_session` | 8,000 | 2.22 | 49.71 |
| 3 | `src/case_intelligence/workspace_store.py:6123` — `<genexpr>` | 8,008,000 | 1.68 | 34.89 |

The 8,008,000 upload-item conversions for only 1,000 files point to repeated
full-session item materialization as the next intake path to investigate. No
optimization is included in this measurement-only change.

These hotspots identify where this workload spends Python application time;
they do not establish the improvement from any proposed optimization. The
benchmark's 20-file regression checks receipt structure and privacy properties,
not timing thresholds.
