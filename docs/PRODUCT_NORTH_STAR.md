# Exculpata product north star

Exculpata is a free, local-first review workspace for case teams. Its purpose
is to help attorneys, investigators, and support staff understand their records,
find source-supported information, preserve what they learn, and make informed
human decisions. Federal criminal defense and teams serving people who cannot
afford counsel are central to that purpose.

This is the product direction, not a claim that the alpha already delivers it.
It guides choices without exhausting what Exculpata can become. The
[assessment and delivery plan](PRODUCT_DIRECTION_2026-09-12.md) separates current
behavior from gaps. The [exit-alpha cruise](EXIT_ALPHA_CRUISE.md) selects the
active implementation step; [release readiness](RELEASE_READINESS.md) defines
what must be proven before a supported release.

## The experience we are building

A team creates a matter, records what it needs to understand, and adds a charging
document, filings, reports, or other background before discovery arrives.
Background material stays distinguishable from discovery, with visible origins
and explicit choices about which material a question may consider.

The team brings in a discovery folder or production. Exculpata preserves its
hierarchy, production occurrences, native files, document relationships, and
available metadata. Every selected item has an accountable outcome. Unsupported
files, failed extraction, unreadable pages, and attachments needing work remain
visible and actionable. Load-file productions need their own validated importer.

A reviewer can immediately browse folders, read documents, navigate matching
passages, listen to recordings, run simple or Boolean searches, and save human
work. These are first-class workflows in their own right. Keyboard navigation,
readability, clear status, sensible defaults, and recovery from interruption are
part of the feature, including when models are unavailable.

The same workspace supports focused sourced answers, multi-step investigation,
and deliberate full-collection review. People, organizations, aliases, places,
objects, identifiers, dates, and events become reviewable working records with
original support. Selecting one makes it easy to find further occurrences and
competing accounts. Suggestions remain distinguishable from human conclusions.

Matter memory preserves objectives, user-supplied information, notes, questions,
answers, findings, decisions, and unresolved work. A returning reviewer can see
what changed and continue. Reviewers can inspect, correct, exclude, and remove
remembered material under the matter's retention and access policy. Memory
supplies working context; generated text does not become source evidence.

Reports and exports compile that accumulated work with its source support,
coverage, disagreements, and human decisions intact. Teams should not have to
reconstruct the case from isolated chat sessions. Durable team matters and
temporary review matters, with deliberate retention and eventually reopenable
packages, remain part of this direction; the current lifecycle contract applies
until each extension is implemented and accepted.

## What thorough review means

Exculpata must work toward accounting for the whole requested population.
A ranked shortlist cannot stand in for a request to find every occurrence.

| Reviewer intent | Required contract |
| --- | --- |
| Find every exact match | Enumerate all matches under a recorded grammar and authorized population; paginate without silently dropping matches; identify material that could not be searched. |
| Explore a question | Show the searches, selected evidence, follow-up leads, original citations, limits, and unanswered questions. |
| Review a whole collection | Freeze the population; account for every source and eligible extracted range; checkpoint work, expose failures and omissions, and resume without losing prior work. |
| Understand the collection | Synthesize saved findings across the reviewed population, retain competing accounts, and make omitted inputs and original support inspectable. |

Processing coverage and recognition quality are different measurements. Visiting
every extracted range does not prove that a model recognized every relevant
fact, that OCR captured every page, or that a transcript is correct. Improve and
measure recall on representative synthetic collections while making these gaps
clear. The ambition is exhaustive accountable processing and excellent retrieval;
never certify an absence solely because a model found nothing.

## Local first, adaptable, and scalable

Administrators choose managed storage and supported model portfolios appropriate
to their hardware. Installation should explain or detect dependencies, guide
runtime and model setup, explain license acknowledgements, verify staged
artifacts, and offer a usable recovery path. Staging credentials must not become
runtime dependencies or appear in logs.

Generation, embeddings, reranking, OCR, transcription, alignment, and optional
speaker clustering have replaceable providers. Model choice should expand over
time without changing citations, review semantics, or matter ownership. Each
supported combination needs immutable artifacts, license records, offline
readiness, and representative quality and resource evidence. Compatibility with
an endpoint alone is insufficient to call an arbitrary model supported.

The same product should serve a small local server and grow toward multiple
GPUs, workers, and datacenter installations. Hardware changes capacity and
throughput, not access to core review features. Scheduling must preserve fair
access across matters, bounded memory, durable progress, cancellation, and
operator visibility. Large-scale support follows measured installation, recovery,
concurrency, storage, and corpus evidence on the exact topology.

Core review remains free and open source. A model download may need separate
license acceptance. Remote model use is not needed for the core experience;
any future remote option must be an explicit operator and matter policy with
clear disclosure of what leaves the installation.

## A core that can grow

The application owns authorization, source identities and versions, citations,
human versus machine provenance, durable jobs, retention, exports, and audit.
Intake, search, review, synthesis, entities, and model providers build on those
shared contracts. Extract those boundaries incrementally as features are built;
use the [core evolution plan](CORE_EVOLUTION.md) instead of a wholesale rewrite.

Judge each increment by whether a case team can do useful work more easily and
trust what the product says it did. Pair a visible user outcome with synthetic
regressions, browser evidence where applicable, applicable documentation, and
honest limits. Do not wait for the entire vision before delivering a reliable
core, and do not let a growing feature list replace release acceptance.
