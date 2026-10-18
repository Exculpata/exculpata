"""Pure synthetic acceptance example. Not a runtime compiler or persistence API."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from case_intelligence.contracts import SourceLocator


ROOT = Path(__file__).parents[1]
SCHEMA = json.loads((ROOT / "schemas/cited-record.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
SCOPE_SCHEMA = json.loads((ROOT / "schemas/cited-record-scope.schema.json").read_text())
SCOPE_VALIDATOR = Draft202012Validator(SCOPE_SCHEMA)
FIELDS = {
    "event": {"event.description", "event.when"},
    "statement": {"statement.content", "statement.speaker"},
    "entity_mention": {"entity_mention.surface"},
    "coverage": {"coverage.processed"},
}


def digest(value):
    """Test manifest canonicalization: UTF-8, sorted keys, compact JSON, no NaN."""
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def text_digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def clock(value):
    require(value.endswith("Z"), "record clocks must use UTC")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def source_key(citation):
    return (citation["source_id"], citation["source_version_id"],
            citation["locator"]["representation_id"])


def validate_citation(record, citation, sources):
    """Use the existing locator validator, then bind to a supplied synthetic unit."""
    locator = citation["locator"]
    SourceLocator(**locator)
    matching = [s for s in sources if
                (s["source_id"], s["source_version_id"], s["representation_id"])
                == source_key(citation)]
    require(len(matching) == 1, "source/version/representation does not resolve uniquely")
    source = matching[0]
    require(source["matter_id"] == record["matter_id"], "foreign matter citation")
    require(source["availability"] == "available", "support is stale or unavailable")
    unit_locator = {k: v for k, v in locator.items()
                    if k in ("page", "timestamp_start_ms", "timestamp_end_ms")}
    units = [u for u in source["units"] if u["locator"] == unit_locator]
    require(len(units) == 1, "locator does not resolve uniquely")
    text = units[0]["text"]
    require(text_digest(text) == citation["content_sha256"], "changed representation content")
    start, end = locator["character_start"], locator["character_end"]
    require(0 <= start < end <= len(text), "invalid character bounds")
    require(text[start:end] == citation["quote"], "quote differs from pinned text slice")
    require(text_digest(citation["quote"]) == locator["excerpt_sha256"], "wrong excerpt digest")


def validate_record(record, sources, *, resolve_support=True):
    VALIDATOR.validate(record)
    created = clock(record["created_at"])
    if record["review"] is not None:
        require(clock(record["review"]["reviewed_at"]) >= created, "review predates creation")
    supported = set()
    for citation in record["citations"]:
        SourceLocator(**citation["locator"])
        require(text_digest(citation["quote"]) == citation["locator"]["excerpt_sha256"],
                "wrong excerpt digest")
        if resolve_support:
            validate_citation(record, citation, sources)
        supported.update(citation["supports"])
    require(supported == FIELDS[record["record_type"]], "material field lacks citation binding")
    payload = record["payload"]
    if record["record_type"] == "event":
        when = payload["when"]
        if when["precision"] != "unresolved":
            earliest, latest = date.fromisoformat(when["earliest"]), date.fromisoformat(when["latest"])
            require(earliest <= latest, "reversed event range")
            require(when["precision"] != "day" or earliest == latest, "day has unequal bounds")
    if record["record_type"] == "entity_mention":
        require(all(c["quote"] == payload["surface"] for c in record["citations"]),
                "mention surface differs from cited occurrence")


def append_revision(history, candidate, sources, *, resolve_support=True):
    """Copy on append; old review/evidence snapshots are never edited in place."""
    validate_record(candidate, sources, resolve_support=resolve_support)
    chain = sorted((r for r in history if r["record_id"] == candidate["record_id"]),
                   key=lambda r: r["revision"])
    previous = chain[-1] if chain else None
    accepted = next((r for r in reversed(chain) if r["status"] == "accepted"), None)
    require(candidate["revision"] == (previous["revision"] + 1 if previous else 1),
            "revision is duplicate, rewritten or noncontiguous")
    require(candidate["previous_revision"] == (previous["revision"] if previous else None),
            "previous revision mismatch")
    require(candidate["base_accepted_revision"] == (accepted["revision"] if accepted else None),
            "stale accepted revision")
    if previous:
        require((candidate["matter_id"], candidate["record_type"]) ==
                (previous["matter_id"], previous["record_type"]), "identity changed matter or type")
        require(clock(candidate["created_at"]) >= clock(previous["created_at"]),
                "creation clock moved backwards")
        if previous["review"]:
            require(clock(candidate["created_at"]) >= clock(previous["review"]["reviewed_at"]),
                    "creation predates prior review")
    if candidate["status"] != "proposed":
        require(previous is not None and previous["status"] == "proposed",
                "review requires a proposed predecessor")
        require(candidate["review"]["reviewed_revision"] == previous["revision"],
                "review targets a different proposal")
        for field in ("producer", "payload", "citations", "relations"):
            require(candidate[field] == previous[field], "review changed the proposed evidence")
    return [*deepcopy(history), deepcopy(candidate)]


def validate_history(records, sources, matter_id):
    history = []
    for record in sorted(records, key=lambda r: (r["record_id"], r["revision"])):
        require(record["matter_id"] == matter_id, "foreign matter record")
        # Historical review is an input snapshot, not a new acceptance decision.
        # Resolve selected output against present support; fresh appends use the
        # default strict support check above. This is not a historical attestation.
        history = append_revision(history, record, sources, resolve_support=False)
    by_key = {(r["record_id"], r["revision"]): r for r in history}
    for record in history:
        for relation in record["relations"]:
            key = (relation["record_id"], relation["revision"])
            require(key in by_key and key != (record["record_id"], record["revision"]),
                    "unresolved or self relation")
            require(by_key[key]["record_type"] in ("event", "statement"),
                    "conflict target is not an assertion")
    return history


def selected(records):
    result = []
    for record_id in sorted({r["record_id"] for r in records}):
        chain = sorted((r for r in records if r["record_id"] == record_id),
                       key=lambda r: r["revision"])
        accepted = next((r for r in reversed(chain) if r["status"] == "accepted"), None)
        if accepted:
            result.append(accepted)
        if chain[-1]["status"] == "proposed":
            result.append(chain[-1])
    return result


def citation_text(citation):
    loc = citation["locator"]
    position = (f'p.{loc["page"]}' if "page" in loc else
                f'ms[{loc["timestamp_start_ms"]},{loc["timestamp_end_ms"]})')
    return (f'{citation["source_id"]}@{citation["source_version_id"]}/'
            f'{loc["representation_id"]} {position} '
            f'chars[{loc["character_start"]},{loc["character_end"]}) '
            f'quote={json.dumps(citation["quote"])}')


def event_order(record):
    when = record["payload"]["when"]
    return (when["earliest"] is None, when["earliest"] or "", when["latest"] or "",
            record["record_id"], record["revision"])


def validate_coverage(record, scope, sources):
    SCOPE_VALIDATOR.validate(scope)
    payload = record["payload"]
    require(scope["matter_id"] == record["matter_id"], "foreign coverage scope")
    require(payload["scope_id"] == scope["scope_id"] and
            payload["scope_sha256"] == digest(scope), "coverage scope digest mismatch")
    targets = {t["target_id"]: t for t in scope["targets"]}
    require(len(targets) == len(scope["targets"]), "duplicate coverage target")
    require({p["target_id"] for p in payload["portions"]} == set(targets),
            "coverage target omitted or invented")
    seen = set()
    for target in targets.values():
        key = (target["source_id"], target["source_version_id"], target["representation_id"])
        require(key not in seen, "duplicate source in coverage denominator")
        seen.add(key)
        matching = [s for s in sources if
                    (s["source_id"], s["source_version_id"], s["representation_id"]) == key]
        require(len(matching) == 1 and matching[0]["matter_id"] == record["matter_id"],
                "coverage target source does not resolve")
        source = matching[0]
        parts = [p for p in payload["portions"] if p["target_id"] == target["target_id"]]
        if target["unit"] == "unknown":
            require(target["start"] is None and target["end"] is None and
                    target["content_basis_sha256"] is None and
                    target["representation_id"] is None, "unknown extent claims a denominator")
            require(len(parts) == 1 and parts[0]["start"] is None and
                    parts[0]["end"] is None and parts[0]["state"] != "processed",
                    "unknown extent cannot claim processed coverage")
            continue
        require(target["representation_id"] is not None and
                target["content_basis_sha256"] == digest(source["units"]),
                "coverage representation content changed")
        start, end = target["start"], target["end"]
        floor = 1 if target["unit"] == "page" else 0
        require(start is not None and end is not None and floor <= start < end,
                "invalid coverage denominator")
        # The fixture inventory supplies known physical/extracted unit bounds.
        if target["unit"] == "page":
            require([u["locator"] for u in source["units"]] ==
                    [{"page": page} for page in range(start, end)],
                    "page denominator differs from frozen units")
        else:
            unit_cursor = start
            for unit in source["units"]:
                require(set(unit["locator"]) == {"timestamp_start_ms", "timestamp_end_ms"},
                        "media inventory needs exact time bounds")
                locator = SourceLocator(**unit["locator"])
                require(locator.timestamp_start_ms == unit_cursor,
                        "media inventory has a gap or overlap")
                unit_cursor = locator.timestamp_end_ms
            require(unit_cursor == end, "media denominator differs from frozen units")
        require(all(p["start"] is not None and p["end"] is not None for p in parts),
                "known coverage range has unknown bounds")
        cursor = start
        for part in sorted(parts, key=lambda p: p["start"]):
            require(part["start"] == cursor and cursor < part["end"] <= end,
                    "coverage partition has a gap, overlap or reversed bounds")
            if part["state"] == "processed":
                require(source["availability"] == "available", "processed source is unavailable")
            cursor = part["end"]
        require(cursor == end, "coverage partition does not reach scope end")
    for citation in record["citations"]:
        locator = citation["locator"]
        key = source_key(citation)
        matching_targets = [t for t in targets.values() if
                            (t["source_id"], t["source_version_id"], t["representation_id"]) == key]
        require(len(matching_targets) == 1, "coverage quote lies outside scope")
        target = matching_targets[0]
        start = locator.get("page", locator.get("timestamp_start_ms"))
        end = start + 1 if "page" in locator else locator["timestamp_end_ms"]
        require(any(p["target_id"] == target["target_id"] and p["state"] == "processed"
                    and p["start"] is not None and p["start"] <= start < end <= p["end"]
                    for p in payload["portions"]), "coverage quote is not in processed material")


def coverage_lines(records, scope, sources):
    coverage = [r for r in selected(records) if r["record_type"] == "coverage"]
    require(coverage, "chronology requires coverage even when no events exist")
    lines = ["COVERAGE | processing is not evidence of absence"]
    targets = {t["target_id"]: t for t in scope["targets"]}
    for record in coverage:
        validate_coverage(record, scope, sources)
        lines.append(f'{record["record_id"]}@{record["revision"]} [{record["status"]}] '
                     f'scope={scope["scope_id"]}@{digest(scope)} stage={scope["stage"]} | ' +
                     " ; ".join(citation_text(c) for c in record["citations"]))
        if not scope["inventory_complete"]:
            lines.append(f'{record["record_id"]}@{record["revision"]} [{record["status"]}] '
                         "GAP: inventory unsealed; undeclared sources and denominator remain unknown.")
        for part in sorted(record["payload"]["portions"],
                           key=lambda p: (p["target_id"], p["start"] or 0)):
            target = targets[part["target_id"]]
            bounds = ("extent unknown" if target["unit"] == "unknown" else
                      f'{target["unit"]}[{part["start"]},{part["end"]})')
            lines.append(f'{record["record_id"]}@{record["revision"]} [{record["status"]}] '
                         f'{part["state"].upper()} {target["source_id"]}@'
                         f'{target["source_version_id"]}/{target["representation_id"] or "no-representation"} '
                         f'{bounds}: {json.dumps(part["reason"])}')
    return lines


def compile_chronology(bundle):
    """Bounded all-or-error fixture projection; not authorization or discovery."""
    records = validate_history(bundle["records"], bundle["sources"], bundle["matter_id"])
    current = selected(records)
    for record in current:
        validate_record(record, bundle["sources"])
    events = [r for r in current if r["record_type"] == "event"]
    lines = ["Synthetic chronology | compiler v1 | selection: latest accepted + pending proposal",
             "Display order only; same-day, overlapping and unresolved dates do not prove precedence."]
    for status in ("accepted", "proposed"):
        lines.append(f"{status.upper()} assertions")
        for record in sorted((r for r in events if r["status"] == status), key=event_order):
            payload = record["payload"]
            when = payload["when"]
            bounds = ("UNRESOLVED" if when["earliest"] is None else
                      f'{when["earliest"]}..{when["latest"]}')
            relations = ", ".join(f'{r["kind"]} {r["record_id"]}@{r["revision"]}'
                                  for r in record["relations"]) or "none"
            lines.append(f'{record["record_id"]}@{record["revision"]} [{status}] '
                         f'{bounds} ({json.dumps(when["expression"])}; {json.dumps(when["reason"])}) | '
                         f'{json.dumps(payload["description"])} | relations: {relations} | ' +
                         " ; ".join(citation_text(c) for c in record["citations"]))
    rejected = sum(r["status"] == "rejected" for r in records)
    lines.append(f"Excluded rejected revision snapshots: {rejected}; retained in input history.")
    lines.extend(coverage_lines(records, bundle["scope"], bundle["sources"]))
    return "\n".join(lines) + "\n"
