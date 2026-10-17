from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .contracts import (
    AvailabilityState,
    ContentOperation,
    Matter,
    MatterState,
    Representation,
    RetrievalResult,
    RetrievalRun,
    Segment,
    SourceReference,
    SourceVersion,
    TicketAction,
)
from .isolation import (
    Catalog,
    MatterAccess,
    MatterStateRegistry,
    ReferenceResolutionStatus,
    SearchHit,
    guard_content_operation,
    resolve_reference,
    validate_retrieval_results,
)
from .tickets import TicketStore


class SemanticValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BenchmarkSummary:
    passed: bool
    isolation_bypasses: tuple[int, int]
    ticket_bypasses: tuple[int, int]
    valid_references: tuple[int, int]
    invalid_references: tuple[int, int]


_REQUIRED_STAGES = (
    "authorization",
    "retrieval",
    "reranking",
    "generation",
    "source_resolution",
    "ticket",
)
_METRIC_CATEGORIES = (
    "cross_matter_bypass",
    "ticket_lease_bypass",
    "valid_reference_resolution",
    "invalid_reference_rejection",
)
_EXPECTED_CATEGORY_COUNTS = {
    "retrieval": 2,
    "cross_matter_bypass": 12,
    "ticket_lease_bypass": 14,
    "valid_reference_resolution": 10,
    "invalid_reference_rejection": 8,
}
_COMPONENTS = (
    ("authorization", "in-memory matter authorization"),
    ("retrieval", "Catalog deterministic fixture search"),
    ("reranking", "not implemented; explicit contract marker"),
    ("generation", "not implemented; explicit contract marker"),
    ("source_resolution", "exact in-memory reference resolver"),
    ("ticket", "in-memory ticket and lease policy"),
)
_LIMITATIONS = (
    "No production model or retrieval implementation was evaluated or selected.",
    "Only synthetic in-memory foundation policy behavior was exercised.",
    "Reranking and generation are explicit not-implemented markers, not quality results.",
)


def _fresh_components() -> list[dict[str, Any]]:
    return [
        {
            "stage": stage,
            "implementation": implementation,
            "network_calls": 0,
            "model_calls": 0,
        }
        for stage, implementation in _COMPONENTS
    ]


def canonical_json(value: dict[str, Any]) -> str:
    """Return the one normalized representation used for generation and checks."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _suite_digest(suite: dict[str, Any]) -> str:
    encoded = json.dumps(
        suite, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_evaluation_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or not value.endswith("Z"):
        raise SemanticValidationError("evaluation_time must be an explicit UTC Z timestamp")
    return parsed


def _validate_suite(suite: dict[str, Any], fixture: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "suite_id",
        "suite_version",
        "fixture_id",
        "evaluation_time",
        "description",
        "cases",
    }
    if set(suite) != required or suite["schema_version"] != "2.0":
        raise SemanticValidationError("unsupported or malformed benchmark suite")
    if suite["fixture_id"] != fixture.get("fixture_id"):
        raise SemanticValidationError("suite fixture identity mismatch")
    _parse_evaluation_time(suite["evaluation_time"])
    ids = [case.get("case_id") for case in suite["cases"]]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise SemanticValidationError("suite case identifiers must be present and unique")
    categories = [case.get("category") for case in suite["cases"]]
    if any(category not in {*_METRIC_CATEGORIES, "retrieval"} for category in categories):
        raise SemanticValidationError("suite contains an unsupported case category")
    actual_counts = {
        category: categories.count(category) for category in _EXPECTED_CATEGORY_COUNTS
    }
    if actual_counts != _EXPECTED_CATEGORY_COUNTS:
        raise SemanticValidationError(
            f"suite case category counts mismatch: {actual_counts}"
        )


def _fixture_objects(
    fixture: dict[str, Any], evaluation_time: datetime
) -> tuple[
    MatterStateRegistry,
    MatterAccess,
    Catalog,
    dict[str, SourceVersion],
    dict[str, Representation],
    dict[str, Segment],
]:
    matters = [
        Matter(
            matter_id=item["matter_id"],
            display_name=item["display_name"],
            state=MatterState.ACTIVE,
        )
        for item in fixture["matters"]
    ]
    registry = MatterStateRegistry(matters)
    grants = {
        f"actor-{item['matter_id'].removeprefix('matter-')}": {item["matter_id"]}
        for item in fixture["matters"]
    }
    access = MatterAccess(grants)
    sources: dict[str, SourceVersion] = {}
    representations: dict[str, Representation] = {}
    segments: dict[str, Segment] = {}
    hits: list[SearchHit] = []
    for matter_item in fixture["matters"]:
        matter_id = matter_item["matter_id"]
        label = "A-1" if matter_id == "matter-alpha" else "B-1"
        bates = "ALPHA0001" if matter_id == "matter-alpha" else "BRAVO0001"
        for source_item in matter_item["sources"]:
            kind = source_item["kind"]
            representation_item = source_item["representation"]
            representation_id = representation_item["representation_id"]
            segment_id = representation_item["segment_id"]
            metadata: dict[str, Any] = {
                "representation_ids": [representation_id],
            }
            if kind == "text":
                metadata.update(
                    pages=[{"page": 1, "printed_page": label, "bates": bates}],
                    character_count=representation_item["byte_size"],
                    line_count=3,
                )
            elif kind == "image":
                metadata.update(image_width_px=160, image_height_px=80)
            elif kind == "audio":
                metadata.update(duration_ms=100)
            source = SourceVersion(
                source_version_id=source_item["source_version_id"],
                matter_id=matter_id,
                source_location_id=f"location-{matter_id}",
                relative_path=source_item["path"],
                display_name=Path(source_item["path"]).name,
                media_type={"text": "text/plain", "image": "image/svg+xml", "audio": "audio/wav"}[kind],
                byte_size=source_item["byte_size"],
                sha256=source_item["sha256"],
                discovered_at=evaluation_time,
                last_verified_at=evaluation_time,
                availability=AvailabilityState.AVAILABLE,
                locator_metadata=metadata,
            )
            representation_metadata = metadata | {"segment_ids": [segment_id]}
            representation = Representation(
                representation_id=representation_id,
                matter_id=matter_id,
                source_version_id=source.source_version_id,
                kind=f"synthetic-{kind}-representation",
                processor_version="slice0-deterministic-v1",
                locator_metadata=representation_metadata,
            )
            segment = Segment(
                segment_id=segment_id,
                matter_id=matter_id,
                source_version_id=source.source_version_id,
                representation_id=representation_id,
                locator_metadata=metadata,
            )
            sources[source.source_version_id] = source
            representations[representation_id] = representation
            segments[segment_id] = segment
            hits.append(SearchHit(matter_id, source.source_version_id, source_item["searchable_text"]))
    catalog = Catalog(registry, sources.values(), hits)
    return registry, access, catalog, sources, representations, segments


def _retrieval_outcome(case: dict[str, Any], catalog: Catalog, access: MatterAccess) -> tuple[str, list[str]]:
    parameters = case["parameters"]
    hits = catalog.search(
        access,
        parameters["actor_id"],
        case["matter_id"],
        parameters["query"],
    )
    observed = [hit.source_version_id for hit in hits]
    expected = parameters["expected_source_version_ids"]
    return ("matched" if observed == expected else "mismatched"), observed


def _cross_matter_outcome(
    case: dict[str, Any], fixture: dict[str, Any], evaluation_time: datetime
) -> str:
    registry, access, catalog, sources, _, _ = _fixture_objects(fixture, evaluation_time)
    parameters = case["parameters"]

    def attempt() -> None:
        operation = case["operation"]
        matter_id = case["matter_id"]
        if operation == "search":
            catalog.search(access, parameters["actor_id"], matter_id, parameters["query"])
        elif operation == "source_lookup":
            catalog.source_for_actor(
                access,
                parameters["actor_id"],
                matter_id,
                parameters["source_version_id"],
            )
        elif operation == "mixed_results":
            alpha = sources["version-matter-alpha-text"]
            bravo = sources["version-matter-bravo-text"]
            run = RetrievalRun(
                run_id="benchmark-mixed-run",
                matter_id="matter-alpha",
                query="shared red bicycle",
                created_at=evaluation_time,
            )
            results = [
                RetrievalResult(
                    run_id=run.run_id,
                    matter_id=source.matter_id,
                    source_version_id=source.source_version_id,
                    reference=SourceReference(
                        reference_id=f"reference-{source.matter_id}",
                        matter_id=source.matter_id,
                        source_version_id=source.source_version_id,
                        locator={"page": 1},
                    ),
                    rank=rank,
                    score=1.0 / rank,
                )
                for rank, source in enumerate((alpha, bravo), start=1)
            ]
            validate_retrieval_results(run, results)
        elif operation == "content_guard":
            state = MatterState(parameters["state"])
            guarded_registry = MatterStateRegistry(
                [
                    Matter(
                        matter_id=matter_id,
                        display_name="Synthetic guarded matter",
                        state=state,
                        active_closure_run_id=("benchmark-closure" if state is MatterState.CLOSING else None),
                    )
                ]
            )
            guard_content_operation(
                guarded_registry,
                matter_id,
                ContentOperation(parameters["content_operation"]),
            )
        else:
            raise SemanticValidationError(f"unsupported cross-matter operation: {operation}")

    try:
        attempt()
    except (PermissionError, KeyError):
        return "denied"
    return "bypass_succeeded"


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _ticket_environment(evaluation_time: datetime) -> tuple[TicketStore, _FixedClock, MatterAccess, MatterStateRegistry]:
    registry = MatterStateRegistry(
        [
            Matter(matter_id="matter-alpha", display_name="Synthetic Alpha", state=MatterState.ACTIVE),
            Matter(matter_id="matter-bravo", display_name="Synthetic Bravo", state=MatterState.ACTIVE),
        ]
    )
    access = MatterAccess({"actor-alpha": {"matter-alpha"}, "actor-bravo": {"matter-bravo"}})
    clock = _FixedClock(evaluation_time)
    serial = iter(range(1, 32))
    store = TicketStore(
        registry=registry,
        clock=clock,
        token_source=lambda size: next(serial).to_bytes(size, "big"),
    )
    return store, clock, access, registry


def _ticket_outcome(case: dict[str, Any], evaluation_time: datetime) -> str:
    store, clock, access, registry = _ticket_environment(evaluation_time)

    def mint() -> Any:
        return store.mint(
            access,
            "actor-alpha",
            "matter-alpha",
            TicketAction.OPEN_ORIGINAL,
            "version-matter-alpha-text",
            3,
        )

    def redeem(uri: str, **changes: Any) -> Any:
        values = {
            "actor_id": "actor-alpha",
            "matter_id": "matter-alpha",
            "action": TicketAction.OPEN_ORIGINAL,
            "object_version_id": "version-matter-alpha-text",
            "mapping_revision": 3,
            "helper_instance_id": "helper-1",
        }
        values.update(changes)
        return store.redeem(uri, access, **values)

    def validate(lease_id: str, **changes: Any) -> Any:
        values = {
            "actor_id": "actor-alpha",
            "matter_id": "matter-alpha",
            "action": TicketAction.OPEN_ORIGINAL,
            "object_version_id": "version-matter-alpha-text",
            "mapping_revision": 3,
            "helper_instance_id": "helper-1",
        }
        values.update(changes)
        return store.validate_lease(lease_id, access, **values)

    operation = case["operation"]
    launch = mint()

    def attempt() -> None:
        if operation == "wrong_actor":
            redeem(launch.uri, actor_id="actor-bravo", matter_id="matter-bravo")
        elif operation == "wrong_action":
            redeem(launch.uri, action=TicketAction.REVEAL_ORIGINAL)
        elif operation == "changed_object":
            redeem(launch.uri, object_version_id="changed-version")
        elif operation == "changed_revision":
            redeem(launch.uri, mapping_revision=4)
        elif operation == "closing_ticket":
            registry.begin_closure("matter-alpha", "benchmark-closure")
            redeem(launch.uri)
        elif operation == "unknown_ticket":
            redeem(f"{LEGACY_STEM}://ticket/unknown")
        elif operation == "malformed_uri":
            redeem(f"{LEGACY_STEM}://ticket/bad?query")
        elif operation == "expired":
            clock.now += timedelta(seconds=60)
            redeem(launch.uri)
        elif operation == "replay":
            redeem(launch.uri)
            redeem(launch.uri)
        elif operation == "revoked_ticket":
            store.revoke_matter("matter-alpha")
            redeem(launch.uri)
        elif operation in {
            "wrong_helper_lease",
            "changed_revision_lease",
            "revoked_actor_lease",
            "closing_lease",
        }:
            lease = redeem(launch.uri)
            if operation == "wrong_helper_lease":
                validate(lease.lease_id, helper_instance_id="wrong-helper")
            elif operation == "changed_revision_lease":
                validate(lease.lease_id, mapping_revision=4)
            elif operation == "revoked_actor_lease":
                store.revoke_actor("actor-alpha")
                validate(lease.lease_id)
            else:
                registry.begin_closure("matter-alpha", "benchmark-closure")
                validate(lease.lease_id)
        else:
            raise SemanticValidationError(f"unsupported ticket operation: {operation}")

    try:
        attempt()
    except PermissionError:
        return "denied"
    return "bypass_succeeded"


def _reference_outcome(
    case: dict[str, Any], fixture: dict[str, Any], evaluation_time: datetime
) -> str:
    registry, _, _, sources, representations, segments = _fixture_objects(
        fixture, evaluation_time
    )
    parameters = case["parameters"]
    source = sources[parameters["source_version_id"]]
    locator = parameters["locator"]
    reference = SourceReference(
        reference_id=f"reference-{case['case_id']}",
        matter_id=case["matter_id"],
        source_version_id=parameters.get("reference_source_version_id", source.source_version_id),
        locator=locator,
    )
    representation_id = locator.get("representation_id")
    segment_id = locator.get("segment_id")
    resolution = resolve_reference(
        registry,
        parameters.get("authorized_matter_id", case["matter_id"]),
        reference,
        source,
        representation=(representations.get(representation_id) if representation_id else None),
        segment=(segments.get(segment_id) if segment_id else None),
    )
    return resolution.status.value


def _metric(category: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [case for case in cases if case["category"] == category]
    denominator = len(selected)
    if category.endswith("_bypass"):
        numerator = sum(case["observed_outcome"] == "bypass_succeeded" for case in selected)
        passed = numerator == 0
    elif category == "valid_reference_resolution":
        numerator = sum(case["observed_outcome"] == ReferenceResolutionStatus.RESOLVED.value for case in selected)
        passed = numerator == denominator
    else:
        numerator = sum(case["observed_outcome"] != ReferenceResolutionStatus.RESOLVED.value for case in selected)
        passed = numerator == denominator
    return {
        "metric_id": category,
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator,
        "passed": passed,
    }


def run_benchmark(suite: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    """Execute every committed suite case against the fixture and policy APIs."""
    _validate_suite(suite, fixture)
    evaluation_time = _parse_evaluation_time(suite["evaluation_time"])
    registry, access, catalog, _, _, _ = _fixture_objects(fixture, evaluation_time)
    del registry
    records: list[dict[str, Any]] = []
    for definition in suite["cases"]:
        category = definition["category"]
        expected_sources = definition["parameters"].get(
            "expected_source_version_ids", []
        )
        observed_sources: list[str] = []
        if category == "retrieval":
            observed_outcome, observed_sources = _retrieval_outcome(
                definition, catalog, access
            )
        elif category == "cross_matter_bypass":
            observed_outcome = _cross_matter_outcome(
                definition, fixture, evaluation_time
            )
        elif category == "ticket_lease_bypass":
            observed_outcome = _ticket_outcome(definition, evaluation_time)
        elif category in {
            "valid_reference_resolution",
            "invalid_reference_rejection",
        }:
            observed_outcome = _reference_outcome(
                definition, fixture, evaluation_time
            )
        else:  # protected by _validate_suite
            raise SemanticValidationError(f"unsupported category: {category}")
        records.append(
            {
                "case_id": definition["case_id"],
                "category": category,
                "operation": definition["operation"],
                "matter_id": definition["matter_id"],
                "expected_outcome": definition["expected_outcome"],
                "observed_outcome": observed_outcome,
                "expected_source_version_ids": expected_sources,
                "observed_source_version_ids": observed_sources,
                "passed": observed_outcome == definition["expected_outcome"]
                and observed_sources == expected_sources,
            }
        )
    metrics = [_metric(category, records) for category in _METRIC_CATEGORIES]
    passed = all(case["passed"] for case in records) and all(
        metric["passed"] for metric in metrics
    )
    return {
        "schema_version": "2.0",
        "suite_id": suite["suite_id"],
        "suite_version": suite["suite_version"],
        "suite_sha256": _suite_digest(suite),
        "run_id": f"{suite['suite_id']}-{suite['suite_version']}-reference",
        "fixture_id": suite["fixture_id"],
        "created_at": suite["evaluation_time"],
        "environment": {
            "dataset_classification": "synthetic",
            "confidential_data": False,
            "network_calls": 0,
            "model_calls": 0,
            "production_selection": False,
        },
        "matter_count": len(fixture["matters"]),
        "case_count": len(records),
        "components": _fresh_components(),
        "cases": records,
        "metrics": metrics,
        "limitations": list(_LIMITATIONS),
        "passed": passed,
    }


def validate_benchmark_result(
    result: dict[str, Any],
    schema: dict[str, Any],
    suite: dict[str, Any],
    fixture: dict[str, Any],
) -> BenchmarkSummary:
    """Schema-check and semantically recompute the entire canonical result."""
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(result)
    _validate_suite(suite, fixture)
    if (
        result["suite_id"] != suite["suite_id"]
        or result["suite_version"] != suite["suite_version"]
        or result["suite_sha256"] != _suite_digest(suite)
        or result["fixture_id"] != suite["fixture_id"]
    ):
        raise SemanticValidationError("result suite or fixture identity mismatch")
    environment = result["environment"]
    if (
        environment["dataset_classification"] != "synthetic"
        or environment["confidential_data"]
        or environment["network_calls"] != 0
        or environment["model_calls"] != 0
        or environment["production_selection"]
    ):
        raise SemanticValidationError(
            "Slice 0 must be synthetic, non-confidential, offline, model-free, and non-selecting"
        )
    stages = [component["stage"] for component in result["components"]]
    if stages != list(_REQUIRED_STAGES):
        raise SemanticValidationError("components must represent every stage once in canonical order")
    case_ids = [case["case_id"] for case in result["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise SemanticValidationError("case identifiers must be unique")
    for case in result["cases"]:
        if case["passed"] is not (
            case["observed_outcome"] == case["expected_outcome"]
            and case["observed_source_version_ids"]
            == case["expected_source_version_ids"]
        ):
            raise SemanticValidationError(f"inconsistent case outcome: {case['case_id']}")
    expected = run_benchmark(suite, fixture)
    if canonical_json(result) != canonical_json(expected):
        raise SemanticValidationError(
            "result differs from recomputed canonical executable benchmark output"
        )
    metrics = {metric["metric_id"]: metric for metric in result["metrics"]}
    return BenchmarkSummary(
        passed=result["passed"],
        isolation_bypasses=(metrics["cross_matter_bypass"]["numerator"], metrics["cross_matter_bypass"]["denominator"]),
        ticket_bypasses=(metrics["ticket_lease_bypass"]["numerator"], metrics["ticket_lease_bypass"]["denominator"]),
        valid_references=(metrics["valid_reference_resolution"]["numerator"], metrics["valid_reference_resolution"]["denominator"]),
        invalid_references=(metrics["invalid_reference_rejection"]["numerator"], metrics["invalid_reference_rejection"]["denominator"]),
    )


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or verify an executable benchmark suite")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="generate canonical normalized output")
    generate.add_argument("--suite", type=Path)
    generate.add_argument("--output", type=Path, help="write output instead of stdout")
    verify = subparsers.add_parser("verify", help="verify the committed reference")
    verify.add_argument("--suite", type=Path)
    verify.add_argument("--reference", type=Path)
    args = parser.parse_args(argv)

    root = _project_root()
    suite_path = args.suite or root / "benchmarks/slice0-suite.json"
    if not suite_path.is_absolute():
        suite_path = root / suite_path
    suite = _load_json(suite_path)
    if suite.get("suite_id") == "slice1a-suite":
        from .slice1_evaluation import run_slice1_benchmark, validate_slice1_result
        fixture_root = root / suite["fixture_path"]
        fixture = _load_json(fixture_root / "manifest.json")
        schema = _load_json(root / "schemas/slice1-result.schema.json")
        generated = run_slice1_benchmark(suite, fixture, fixture_root)
        if args.command == "generate":
            payload = canonical_json(generated)
            if args.output:
                args.output.write_text(payload, encoding="utf-8")
                print(f"wrote {args.output}")
            else:
                print(payload, end="")
            return 0
        reference = args.reference or root / "benchmarks/expected/slice1a-reference-result.json"
        if not reference.is_absolute():
            reference = root / reference
        metrics = validate_slice1_result(_load_json(reference), schema, suite, fixture, fixture_root)
        compact = " ".join(f"{key}={value[0]}/{value[1]}" for key, value in metrics.items())
        print(f"verified canonical Slice 1A benchmark: passed=true {compact}")
        return 0
    if suite.get("suite_id") != "slice0-suite":
        raise SemanticValidationError("unsupported benchmark suite")
    fixture = _load_json(root / "tests/fixtures/synthetic/two_matter/v1/manifest.json")
    schema = _load_json(root / "schemas/benchmark-result.schema.json")
    generated = run_benchmark(suite, fixture)
    if args.command == "generate":
        payload = canonical_json(generated)
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(payload, end="")
        return 0

    reference = args.reference or root / "benchmarks/expected/slice0-reference-result.json"
    summary = validate_benchmark_result(_load_json(reference), schema, suite, fixture)
    print(
        "verified canonical benchmark: "
        f"passed={str(summary.passed).lower()} "
        f"cross_matter={summary.isolation_bypasses[0]}/{summary.isolation_bypasses[1]} "
        f"ticket_lease={summary.ticket_bypasses[0]}/{summary.ticket_bypasses[1]} "
        f"valid_references={summary.valid_references[0]}/{summary.valid_references[1]} "
        f"invalid_references={summary.invalid_references[0]}/{summary.invalid_references[1]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
