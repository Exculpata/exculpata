"""Synthetic interchange acceptance; no model, runtime store or network required."""
from copy import deepcopy
import json
from pathlib import Path
import random

from jsonschema import Draft202012Validator, ValidationError
import pytest

from tests.cited_records_support import (
    SCHEMA, SCOPE_SCHEMA, VALIDATOR, append_revision, compile_chronology, digest,
    selected, text_digest, validate_coverage, validate_history, validate_record,
)


FIXTURES = Path(__file__).parent / "fixtures"
INVALID = (ValidationError, ValueError)


@pytest.fixture
def bundle():
    return json.loads((FIXTURES / "cited_records.json").read_text())


def record(bundle, name="event-arrival", revision=1):
    return deepcopy(next(r for r in bundle["records"]
                         if r["record_id"] == name and r["revision"] == revision))


def replace(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def decision(proposal, status="accepted"):
    result = deepcopy(proposal)
    result.update(revision=proposal["revision"] + 1,
                  previous_revision=proposal["revision"], status=status,
                  created_at="2030-05-01T13:00:00Z",
                  review={"reviewer_id": "reviewer-synthetic", "reviewed_revision": proposal["revision"],
                          "reviewed_at": "2030-05-01T13:01:00Z", "reason": "Synthetic decision."})
    if proposal["status"] == "accepted":
        result["base_accepted_revision"] = proposal["revision"]
    return result


def test_schema_meta_and_all_fixture_records(bundle):
    Draft202012Validator.check_schema(SCHEMA)
    Draft202012Validator.check_schema(SCOPE_SCHEMA)
    for item in bundle["records"]:
        VALIDATOR.validate(item)
        validate_record(item, bundle["sources"])
    validate_history(bundle["records"], bundle["sources"], bundle["matter_id"])


@pytest.mark.parametrize("tier", ["baseline", "assisted", "extended"])
@pytest.mark.parametrize("name", ["event-arrival", "statement-nested", "mention-rowan-report"])
@pytest.mark.parametrize("status", ["proposed", "accepted", "rejected"])
def test_no_tier_or_review_status_permits_uncited_assertions(bundle, tier, name, status):
    item = record(bundle, name)
    item["producer"]["tier"] = tier
    if status != "proposed":
        item = decision(item, status)
    item["citations"] = []
    with pytest.raises(ValidationError):
        validate_record(item, bundle["sources"])


@pytest.mark.parametrize(("path", "value"), [
    (("source_id",), "same-name-source"), (("source_version_id",), "log-v2"),
    (("locator", "representation_id"), "log-ocr-v2"), (("locator", "page"), 5),
    (("quote",), "The courier arrived."), (("content_sha256",), "0" * 64),
    (("locator", "excerpt_sha256"), "0" * 64), (("locator", "character_end"), 1000),
    (("locator", "character_start"), 67), (("locator", "page"), 0),
    (("supports",), ["event.description"]),
])
def test_citation_pins_bounds_hashes_and_field_bindings_fail_closed(bundle, path, value):
    item = record(bundle)
    replace(item["citations"][0], path, value)
    with pytest.raises(INVALID):
        validate_record(item, bundle["sources"])


@pytest.mark.parametrize("field", ["source_version_id", "content_sha256"])
def test_missing_source_pin_is_invalid(bundle, field):
    item = record(bundle)
    del item["citations"][0][field]
    with pytest.raises(ValidationError):
        validate_record(item, bundle["sources"])


@pytest.mark.parametrize(("field", "value", "message"), [
    ("matter_id", "foreign-matter", "foreign matter"),
    ("availability", "stale", "stale or unavailable"),
    ("availability", "unavailable", "stale or unavailable"),
    ("source_version_id", "log-v2", "does not resolve"),
    ("representation_id", "revised-ocr", "does not resolve"),
])
def test_accepted_status_never_overrides_present_support_validity(bundle, field, value, message):
    item = record(bundle, revision=2)
    bundle["sources"][0][field] = value
    with pytest.raises(ValueError, match=message):
        validate_record(item, bundle["sources"])
    assert item["status"] == "accepted"


def test_changed_surroundings_and_duplicate_source_cannot_hide_behind_exact_quote(bundle):
    item = record(bundle, "mention-rowan-report")
    bundle["sources"][0]["units"][1]["text"] += " Context changed."
    with pytest.raises(ValueError, match="changed representation content"):
        validate_record(item, bundle["sources"])
    bundle["sources"].append(deepcopy(bundle["sources"][0]))
    with pytest.raises(ValueError, match="does not resolve uniquely"):
        validate_record(item, bundle["sources"])


def test_historical_source_pin_survives_a_new_version(bundle):
    item = record(bundle, revision=2)
    newer = deepcopy(bundle["sources"][0])
    newer["source_version_id"] = "log-v2"
    newer["units"][0]["text"] = "A different later account."
    validate_record(item, [*bundle["sources"], newer])
    with pytest.raises(ValueError, match="does not resolve"):
        validate_record(item, [newer])


@pytest.mark.parametrize("end", [0, 1, 2501])
def test_media_offsets_are_exact_unit_bounds(bundle, end):
    item = record(bundle, "statement-nested")
    item["citations"][0]["locator"]["timestamp_end_ms"] = end
    with pytest.raises(INVALID):
        validate_record(item, bundle["sources"])


@pytest.mark.parametrize(("path", "value"), [
    (("review",), None), (("review", "reviewer_id"), ""),
    (("review", "reviewed_at"), "2030-05-01T10:00:00Z"),
    (("review", "reviewed_at"), "2030-05-01T11:01:00+00:00"),
    (("schema_version",), "2.0"), (("unknown",), True),
])
def test_review_and_schema_fields_fail_closed(bundle, path, value):
    item = record(bundle, revision=2)
    replace(item, path, value)
    with pytest.raises(INVALID):
        validate_record(item, bundle["sources"])


@pytest.mark.parametrize(("path", "value", "message"), [
    (("revision",), 2, "duplicate, rewritten or noncontiguous"),
    (("previous_revision",), 1, "previous revision mismatch"),
    (("base_accepted_revision",), None, "stale accepted revision"),
    (("created_at",), "2030-05-01T10:00:00Z", "clock moved backwards"),
])
def test_append_rejects_overwrite_stale_base_and_clock_regression(bundle, path, value, message):
    chain = [record(bundle, revision=n) for n in (1, 2)]
    proposal = record(bundle, revision=3)
    replace(proposal, path, value)
    with pytest.raises(ValueError, match=message):
        append_revision(chain, proposal, bundle["sources"])


@pytest.mark.parametrize(("path", "value", "message"), [
    (("review", "reviewed_revision"), 1, "different proposal"),
    (("payload", "description"), "Edited during review.", "changed the proposed evidence"),
    (("producer", "version"), "2", "changed the proposed evidence"),
    (("citations", 0, "supports"), ["event.when", "event.description"], "changed the proposed evidence"),
])
def test_acceptance_targets_exact_proposed_revision_and_payload(bundle, path, value, message):
    proposal = record(bundle, revision=3)
    accepted = decision(proposal)
    replace(accepted, path, value)
    with pytest.raises(ValueError, match=message):
        append_revision([record(bundle, revision=n) for n in (1, 2, 3)], accepted, bundle["sources"])


def test_review_requires_proposal_and_preserves_history_with_copy_on_append(bundle):
    sources = bundle["sources"]
    chain = [record(bundle, revision=n) for n in (1, 2)]
    before = deepcopy(chain)
    with pytest.raises(ValueError, match="proposed predecessor"):
        append_revision(chain, decision(chain[-1]), sources)
    pending = append_revision(chain, record(bundle, revision=3), sources)
    assert [(r["revision"], r["status"]) for r in selected(pending)] == [(2, "accepted"), (3, "proposed")]
    rejected = append_revision(pending, decision(pending[-1], "rejected"), sources)
    assert [(r["revision"], r["status"]) for r in selected(rejected)] == [(2, "accepted")]
    accepted = append_revision(pending, decision(pending[-1]), sources)
    assert [r["revision"] for r in selected(accepted)] == [4]
    accepted[0]["payload"]["description"] = "Mutated returned copy."
    assert chain == before


def test_relations_resolve_exact_revisions_in_same_matter(bundle):
    for mutation, message in [("revision", "unresolved"), ("matter", "foreign matter")]:
        records = deepcopy(bundle["records"])
        target = next(r for r in records if r["record_id"] == "event-arrival")
        if mutation == "revision":
            records = [r for r in records if r["record_id"] != "event-arrival"]
        else:
            target["matter_id"] = "foreign-matter"
        with pytest.raises(ValueError, match=message):
            validate_history(records, bundle["sources"], bundle["matter_id"])


def test_conflict_reference_labels_rejected_target_without_promoting_it(bundle):
    proposal = next(r for r in bundle["records"] if r["record_id"] == "event-range")
    proposal["relations"] = [{"kind": "conflicts_with", "record_id": "event-rejected",
                              "revision": 2, "reason": "Retain the reference to the rejected account."}]
    lines = compile_chronology(bundle).splitlines()
    line = next(line for line in lines if line.startswith("event-range@1 "))
    assert "conflicts_with event-rejected@2 [rejected,historical/superseded]" in line
    assert not any(line.startswith("event-rejected@") for line in lines)
    assert "quote=" in line


def test_conflict_reference_stays_pinned_to_superseded_accepted_revision(bundle):
    bundle["records"].append(decision(record(bundle, "event-arrival", 3)))
    lines = compile_chronology(bundle).splitlines()
    line = next(line for line in lines if line.startswith("event-witness@2 "))
    assert "conflicts_with event-arrival@2 [accepted,historical/superseded]" in line
    assert "conflicts_with event-arrival@4" not in line
    assert any(line.startswith("event-arrival@4 [accepted]") for line in lines)
    assert not any(line.startswith("event-arrival@2 ") for line in lines)


def test_conflict_rationale_escapes_multiline_text_without_losing_attribution(bundle):
    rationale = 'Competing account.\nFORGED EVENT\r\n"quoted wording"\u2028SECOND LINE'
    proposal = next(r for r in bundle["records"] if r["record_id"] == "event-range")
    proposal["relations"] = [{"kind": "conflicts_with", "record_id": "event-arrival",
                              "revision": 2, "reason": rationale}]
    lines = compile_chronology(bundle).splitlines()
    matching = [line for line in lines if "FORGED EVENT" in line]
    assert len(matching) == 1 and matching[0].startswith("event-range@1 [proposed]")
    assert "conflicts_with event-arrival@2 [accepted,current]" in matching[0]
    assert f"reason={json.dumps(rationale)}" in matching[0] and "quote=" in matching[0]


@pytest.mark.parametrize(("name", "values"), [
    ("event-undated", {"earliest": "2030-05-01"}),
    ("event-range", {"earliest": "2030-04-11"}),
    ("event-arrival", {"latest": "2030-04-09"}),
    ("event-arrival", {"earliest": "2030-02-30"}),
])
def test_no_partial_unknown_dates_reversed_ranges_or_false_day_precision(bundle, name, values):
    item = record(bundle, name)
    item["payload"]["when"].update(values)
    with pytest.raises(INVALID):
        validate_record(item, bundle["sources"])


def test_nested_negation_and_same_named_mentions_remain_distinct(bundle):
    statement = record(bundle, "statement-nested")
    assert statement["payload"]["content"] == "I heard Rowan say, 'I may not have seen the courier.'"
    assert statement["payload"]["speaker"].startswith("Unknown speaker")
    mentions = [r for r in selected(bundle["records"]) if r["record_type"] == "entity_mention"]
    assert len(mentions) == len({r["record_id"] for r in mentions}) == 2
    assert {r["payload"]["surface"] for r in mentions} == {"Rowan"}
    assert all(r["payload"]["identity"] == "unresolved" for r in mentions)


def test_one_mention_cannot_merge_separate_occurrences(bundle):
    mention = record(bundle, "mention-rowan-report")
    mention["citations"].extend(record(bundle, "mention-rowan-audio")["citations"])
    with pytest.raises(ValidationError):
        validate_record(mention, bundle["sources"])


@pytest.mark.parametrize("field", ["description", "expression", "reason"])
def test_multiline_text_cannot_forge_chronology_lines(bundle, field):
    target = next(r for r in bundle["records"] if r["record_id"] == "event-range")
    payload = target["payload"] if field == "description" else target["payload"]["when"]
    payload[field] += "\nFORGED HEADING\r\nUNATTRIBUTED\u2028SECOND LINE"
    lines = compile_chronology(bundle).splitlines()
    forged = [line for line in lines if "FORGED HEADING" in line]
    assert len(forged) == 1 and forged[0].startswith("event-range@1 [proposed]")
    assert 'quote=' in forged[0]


def test_identifier_with_trailing_newline_is_not_an_identifier(bundle):
    item = record(bundle)
    item["record_id"] += "\n"
    with pytest.raises(ValidationError):
        validate_record(item, bundle["sources"])


def test_model_producer_metadata_does_not_require_the_model_to_read_existing_records(bundle):
    for item in bundle["records"]:
        item["producer"] = {"kind": "model", "name": "synthetic-unavailable-producer",
                            "version": "1", "capability": "record.propose",
                            "tier": "extended", "model": {"name": "fixture-only", "revision": "a" * 40}}
    # No provider registry, weight loader or network dependency exists in this reader.
    output = compile_chronology(bundle)
    assert 'event-arrival@2 [accepted]' in output and 'event-arrival@3 [proposed]' in output


def test_current_revision_can_recover_without_revalidating_superseded_support(bundle):
    chain = [record(bundle, revision=n) for n in (1, 2, 3)]
    source = deepcopy(bundle["sources"][0])
    source["source_version_id"] = "log-v2"
    chain[-1]["citations"][0]["source_version_id"] = "log-v2"
    chain.append(decision(chain[-1]))
    history = validate_history(chain, [source], bundle["matter_id"])
    assert len(history) == 4 and [r["revision"] for r in selected(history)] == [4]
    validate_record(selected(history)[0], [source])
    with pytest.raises(ValueError, match="does not resolve"):
        validate_record(history[1], [source])


def test_producer_model_pin_and_manual_null_are_not_interchangeable(bundle):
    item = record(bundle)
    validate_record(item, bundle["sources"])
    item["producer"].update(kind="model", model={"name": "synthetic-model", "revision": "a" * 40})
    validate_record(item, bundle["sources"])
    for kind, model in [("model", None), ("model", {"name": "synthetic-model", "revision": "latest"}),
                        ("model", {"name": "synthetic-model", "revision": "a" * 40 + "\n"}),
                        ("manual", item["producer"]["model"])]:
        item["producer"].update(kind=kind, model=model)
        with pytest.raises(ValidationError):
            validate_record(item, bundle["sources"])


def test_chronology_is_pure_deterministic_cited_and_preserves_uncertainty(bundle):
    before = deepcopy(bundle)
    output = compile_chronology(bundle)
    assert output == compile_chronology(bundle) == (FIXTURES / "cited_records_chronology.txt").read_text()
    assert bundle == before
    random.Random(17).shuffle(bundle["records"])
    assert compile_chronology(bundle) == output
    lines = [line for line in output.splitlines() if line.startswith("event-")]
    assert [line.split()[0] for line in lines] == ["event-arrival@2", "event-witness@2", "event-arrival@3", "event-range@1", "event-undated@1"]
    assert all("quote=" in line and "source-log@log-v1/log-text-v1" in line for line in lines)
    assert "event-rejected@" not in output and "UNRESOLVED" in lines[-1]
    assert "2030-04-08..2030-04-10" in lines[-2] and "2030-05-01" not in "\n".join(lines)


def test_export_restores_complete_contract_from_separate_artifact(bundle, tmp_path):
    expected_digest, expected_output = digest(bundle), compile_chronology(bundle)
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps(bundle, ensure_ascii=False))
    restored_path = tmp_path / "clean-restore" / "records.json"
    restored_path.parent.mkdir()
    restored_path.write_bytes(exported.read_bytes())
    exported.unlink()
    bundle.clear()  # Neither original in-memory records nor export are available.
    restored = json.loads(restored_path.read_text())
    assert digest(restored) == expected_digest  # Includes every version, review and citation.
    assert compile_chronology(restored) == expected_output
    assert text_digest(record(restored)["citations"][0]["quote"]) == record(restored)["citations"][0]["locator"]["excerpt_sha256"]


def test_coverage_media_inventory_cannot_move_backwards_even_with_matching_final_extent(bundle):
    source = bundle["sources"][1]
    source["units"].extend([
        {"locator": {"timestamp_start_ms": 2500, "timestamp_end_ms": 2000}, "text": "Backwards unit."},
        {"locator": {"timestamp_start_ms": 2000, "timestamp_end_ms": 2500}, "text": "Overlapping unit."},
    ])
    target = next(t for t in bundle["scope"]["targets"] if t["target_id"] == "target-audio")
    target["content_basis_sha256"] = digest(source["units"])
    for item in bundle["records"]:
        if item["record_type"] == "coverage":
            item["payload"]["scope_sha256"] = digest(bundle["scope"])
    with pytest.raises(ValueError, match="end must exceed start"):
        compile_chronology(bundle)


@pytest.mark.parametrize(("field", "value"), [
    ("stage", "different-operation"), ("inventory_complete", True), ("scope_id", "scope-v2"),
])
def test_coverage_frozen_scope_digest_detects_changed_operation_or_population(bundle, field, value):
    bundle["scope"][field] = value
    with pytest.raises(ValueError, match="scope digest mismatch"):
        validate_coverage(record(bundle, "coverage-capture", 2), bundle["scope"], bundle["sources"])


def test_unsealed_inventory_unknown_extent_and_budget_gaps_remain_visible(bundle):
    output = compile_chronology(bundle)
    assert "GAP: inventory unsealed; undeclared sources and denominator remain unknown." in output
    assert "extent unknown" in output and "%" not in output
    assert "UNPROCESSED source-log@log-v1/log-text-v1 page[5,6)" in output
    assert "Capture budget ended before this page." in output
    assert "FAILED source-unreadable@unreadable-v1/no-representation extent unknown" in output


@pytest.mark.parametrize("mutation", ["processed", "denominator"])
def test_unknown_coverage_extent_cannot_become_processed_or_acquire_denominator(bundle, mutation):
    item = record(bundle, "coverage-capture", 2)
    if mutation == "processed":
        item["payload"]["portions"][-1]["state"] = "processed"
        message = "unknown extent cannot claim processed"
    else:
        bundle["scope"]["targets"][-1].update(start=0, end=10)
        item["payload"]["scope_sha256"] = digest(bundle["scope"])
        message = "unknown extent claims a denominator"
    with pytest.raises(ValueError, match=message):
        validate_coverage(item, bundle["scope"], bundle["sources"])


@pytest.mark.parametrize("target_id", ["target-audio", "invented-target"])
def test_coverage_cannot_omit_or_invent_target(bundle, target_id):
    item = record(bundle, "coverage-capture", 2)
    if target_id == "target-audio":
        item["payload"]["portions"] = [p for p in item["payload"]["portions"] if p["target_id"] != target_id]
    else:
        item["payload"]["portions"][0]["target_id"] = target_id
    with pytest.raises(ValueError, match="coverage target omitted or invented"):
        validate_coverage(item, bundle["scope"], bundle["sources"])


@pytest.mark.parametrize(("index", "field", "value"), [
    (1, "start", 4), (1, "start", 6), (1, "end", 5), (2, "end", 6),
])
def test_coverage_partition_rejects_overlap_gap_and_empty_page_ranges(bundle, index, field, value):
    item = record(bundle, "coverage-capture", 2)
    item["payload"]["portions"][index][field] = value
    with pytest.raises(ValueError, match="partition"):
        validate_coverage(item, bundle["scope"], bundle["sources"])


def test_coverage_partition_must_reach_frozen_scope_end(bundle):
    item = record(bundle, "coverage-capture", 2)
    del item["payload"]["portions"][2]
    with pytest.raises(ValueError, match="does not reach scope end"):
        validate_coverage(item, bundle["scope"], bundle["sources"])


@pytest.mark.parametrize("state", ["unprocessed", "failed"])
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_budget_and_failed_gaps_require_nonblank_reason(bundle, state, reason):
    item = record(bundle, "coverage-capture", 2)
    portion = item["payload"]["portions"][1]
    portion["state"] = state
    if reason is None:
        del portion["reason"]
    else:
        portion["reason"] = reason
    with pytest.raises(ValidationError):
        validate_record(item, bundle["sources"])


def test_coverage_hash_binds_uncited_unit_text_too(bundle):
    item = record(bundle, "coverage-capture", 2)
    bundle["sources"][0]["units"][4]["text"] = "A changed uncited page."
    validate_record(item, bundle["sources"])  # Its quotations still resolve exactly.
    with pytest.raises(ValueError, match="coverage representation content changed"):
        validate_coverage(item, bundle["scope"], bundle["sources"])


def test_no_event_results_still_compile_honest_coverage(bundle):
    bundle["records"] = [r for r in bundle["records"] if r["record_type"] == "coverage"]
    output = compile_chronology(bundle)
    assert not any(line.startswith("event-") for line in output.splitlines())
    assert "coverage-capture@2 [accepted]" in output
    assert "UNPROCESSED source-log" in output and "inventory unsealed" in output


def test_fully_processed_zero_dated_events_never_means_no_events_occurred(bundle):
    bundle["scope"]["targets"] = [t for t in bundle["scope"]["targets"] if t["target_id"] == "target-audio"]
    bundle["scope"]["inventory_complete"] = True
    bundle["sources"] = [s for s in bundle["sources"] if s["source_id"] == "source-audio"]
    bundle["records"] = [r for r in bundle["records"] if r["record_type"] == "coverage"]
    for item in bundle["records"]:
        item["payload"]["scope_sha256"] = digest(bundle["scope"])
        item["payload"]["execution_receipt"]["outcome"] = "completed"
        item["payload"]["portions"] = [p for p in item["payload"]["portions"] if p["target_id"] == "target-audio"]
        item["citations"] = [c for c in item["citations"] if c["source_id"] == "source-audio"]
    output = compile_chronology(bundle)
    assert "PROCESSED source-audio" in output and "zero dated events found" in output
    assert "processing is not evidence of absence" in output
    assert not any(line.startswith("event-") for line in output.splitlines())
    assert "GAP:" not in output


def test_chronology_without_coverage_fails_even_if_events_are_valid(bundle):
    bundle["records"] = [r for r in bundle["records"] if r["record_type"] != "coverage"]
    with pytest.raises(ValueError, match="chronology requires coverage"):
        compile_chronology(bundle)


@pytest.fixture
def unreadable_bundle(bundle):
    bundle["scope"]["targets"] = [t for t in bundle["scope"]["targets"] if t["target_id"] == "target-unreadable"]
    bundle["sources"] = [s for s in bundle["sources"] if s["source_id"] == "source-unreadable"]
    bundle["records"] = [r for r in bundle["records"] if r["record_type"] == "coverage"]
    for item in bundle["records"]:
        item["citations"] = []
        item["payload"]["scope_sha256"] = digest(bundle["scope"])
        item["payload"]["portions"] = [p for p in item["payload"]["portions"] if p["target_id"] == "target-unreadable"]
        item["payload"]["execution_receipt"] = {
            "receipt_id": "receipt-unreadable", "stage": bundle["scope"]["stage"],
            "started_at": "2030-05-01T09:00:00Z", "finished_at": "2030-05-01T09:05:00Z",
            "outcome": "failed",
        }
    return bundle


@pytest.mark.parametrize("extent", ["unknown", "twelve-pages"])
def test_failed_unreadable_coverage_uses_receipt_without_inventing_quote(unreadable_bundle, extent):
    bundle = unreadable_bundle
    if extent == "twelve-pages":
        physical_extent = {"unit": "page", "start": 1, "end": 13}
        bundle["sources"][0]["physical_extent"] = physical_extent
        bundle["scope"]["targets"][0].update(physical_extent)
        for item in bundle["records"]:
            item["payload"]["scope_sha256"] = digest(bundle["scope"])
            item["payload"]["portions"][0].update(
                start=1, end=13, reason="OCR failed for known twelve-page source; no extracted text.")
    output = compile_chronology(bundle)
    assert "coverage-capture@2 [accepted]" in output
    assert "FAILED source-unreadable@unreadable-v1/no-representation" in output
    assert "receipt-unreadable" in output and "quote=" not in output
    assert bundle["records"][-1]["payload"]["portions"][0]["reason"] in output
    assert ("extent unknown" if extent == "unknown" else "page[1,13)") in output


@pytest.mark.parametrize("state", ["unprocessed", "processed"])
def test_quote_free_coverage_exception_cannot_claim_nonfailed_processing(unreadable_bundle, state):
    item = record(unreadable_bundle, "coverage-capture", 2)
    item["payload"]["portions"][0]["state"] = state
    with pytest.raises(INVALID):
        validate_record(item, unreadable_bundle["sources"])


@pytest.mark.parametrize("missing", ["execution_receipt", "reason"])
def test_quote_free_failure_requires_receipt_and_explanation(unreadable_bundle, missing):
    item = record(unreadable_bundle, "coverage-capture", 2)
    if missing == "execution_receipt":
        del item["payload"][missing]
    else:
        del item["payload"]["portions"][0][missing]
    with pytest.raises(ValidationError):
        validate_record(item, unreadable_bundle["sources"])


@pytest.mark.parametrize("field", ["source_id", "source_version_id"])
def test_quote_free_failure_still_requires_exact_source_version(unreadable_bundle, field):
    bundle = unreadable_bundle
    item = record(bundle, "coverage-capture", 2)
    bundle["scope"]["targets"][0][field] = "unknown-pin"
    item["payload"]["scope_sha256"] = digest(bundle["scope"])
    with pytest.raises(ValueError, match="target source does not resolve"):
        validate_coverage(item, bundle["scope"], bundle["sources"])


@pytest.mark.parametrize(("field", "value"), [
    ("stage", "different-operation"), ("finished_at", "2030-05-01T08:59:00Z"),
    ("finished_at", "2030-05-01T12:00:00Z"), ("outcome", "partial"), ("outcome", "completed"),
])
def test_failure_receipt_cannot_falsify_stage_time_or_outcome(unreadable_bundle, field, value):
    bundle = unreadable_bundle
    item = record(bundle, "coverage-capture", 2)
    item["payload"]["execution_receipt"][field] = value
    with pytest.raises(INVALID):
        validate_record(item, bundle["sources"])
        validate_coverage(item, bundle["scope"], bundle["sources"])


@pytest.mark.parametrize("field", ["representation_id", "content_basis_sha256"])
def test_unreadable_failure_cannot_fabricate_representation_or_text_digest(unreadable_bundle, field):
    bundle = unreadable_bundle
    item = record(bundle, "coverage-capture", 2)
    bundle["scope"]["targets"][0][field] = "invented-text" if field == "representation_id" else "a" * 64
    if field == "representation_id":
        bundle["sources"][0][field] = "invented-text"  # Matching invented pins do not create extracted text.
    item["payload"]["scope_sha256"] = digest(bundle["scope"])
    with pytest.raises(INVALID):
        validate_coverage(item, bundle["scope"], bundle["sources"])


def test_quote_free_failure_cannot_reference_undefined_scope_target(unreadable_bundle):
    bundle = unreadable_bundle
    item = record(bundle, "coverage-capture", 2)
    item["payload"]["portions"][0]["target_id"] = "undefined-target"
    with pytest.raises(ValueError, match="coverage target omitted or invented"):
        validate_coverage(item, bundle["scope"], bundle["sources"])


def test_chronology_events_cannot_escape_declared_coverage_scope(bundle):
    bundle["scope"]["targets"] = [t for t in bundle["scope"]["targets"] if t["target_id"] == "target-audio"]
    bundle["scope"]["inventory_complete"] = True
    for item in bundle["records"]:
        if item["record_type"] == "coverage":
            item["payload"]["scope_sha256"] = digest(bundle["scope"])
            item["payload"]["execution_receipt"]["outcome"] = "completed"
            item["payload"]["portions"] = [p for p in item["payload"]["portions"] if p["target_id"] == "target-audio"]
            item["citations"] = [c for c in item["citations"] if c["source_id"] == "source-audio"]
    # Log events still have valid exact citations, but their source is outside scope.
    with pytest.raises(ValueError, match="outside chronology scope"):
        compile_chronology(bundle)


@pytest.mark.parametrize("declared_extent", ["unknown", "mismatched-pages"])
def test_quote_free_scope_cannot_discard_or_change_known_physical_extent(unreadable_bundle, declared_extent):
    bundle = unreadable_bundle
    bundle["sources"][0]["physical_extent"] = {"unit": "page", "start": 1, "end": 13}
    if declared_extent == "mismatched-pages":
        bundle["scope"]["targets"][0].update(unit="page", start=1, end=12)
    for item in bundle["records"]:
        item["payload"]["scope_sha256"] = digest(bundle["scope"])
        if declared_extent == "mismatched-pages":
            item["payload"]["portions"][0].update(start=1, end=12)
    validate_history(bundle["records"], bundle["sources"], bundle["matter_id"])
    message = ("known physical extent was discarded" if declared_extent == "unknown"
               else "physical extent does not match source inventory")
    with pytest.raises(ValueError, match=message):
        compile_chronology(bundle)


def test_event_in_scope_but_in_unprocessed_portion_cannot_enter_chronology(bundle):
    for item in bundle["records"]:
        if item["record_type"] == "coverage":
            first, *remaining = item["payload"]["portions"]
            item["payload"]["portions"] = [
                dict(first, start=1, end=3),
                dict(first, start=3, end=4, state="unprocessed", reason="Capture budget excluded this page."),
                dict(first, start=4, end=5),
                *remaining,
            ]
            validate_coverage(item, bundle["scope"], bundle["sources"])
    validate_history(bundle["records"], bundle["sources"], bundle["matter_id"])
    validate_record(record(bundle, "event-range"), bundle["sources"])
    # The event's page 3 is in scope; coverage quotations on page 1 and audio remain processed.
    with pytest.raises(ValueError, match="event support lacks processed chronology coverage"):
        compile_chronology(bundle)


def test_fresh_quote_free_coverage_proposal_and_acceptance_validate_scope(unreadable_bundle):
    bundle = unreadable_bundle
    history = append_revision([], record(bundle, "coverage-capture"), bundle["sources"], scope=bundle["scope"])
    history = append_revision(history, record(bundle, "coverage-capture", 2), bundle["sources"], scope=bundle["scope"])
    assert [(r["revision"], r["status"]) for r in selected(history)] == [(2, "accepted")]
    assert all(r["citations"] == [] for r in history)


@pytest.mark.parametrize("revision", [1, 2])
def test_fresh_coverage_append_requires_scope_even_with_valid_receipt(unreadable_bundle, revision):
    bundle = unreadable_bundle
    history = [] if revision == 1 else [record(bundle, "coverage-capture")]
    with pytest.raises(ValueError, match="scope"):
        append_revision(history, record(bundle, "coverage-capture", revision), bundle["sources"])


@pytest.mark.parametrize("invalid_basis", ["receipt-stage", "readable-target"])
def test_fresh_quote_free_acceptance_rejects_invalid_coverage_basis(unreadable_bundle, invalid_basis):
    bundle = unreadable_bundle
    if invalid_basis == "receipt-stage":
        for item in bundle["records"]:
            item["payload"]["execution_receipt"]["stage"] = "different-operation"
        message = "receipt stage differs from scope"
    else:
        source, target = bundle["sources"][0], bundle["scope"]["targets"][0]
        source.update(availability="available", representation_id="readable-text-v1",
                      units=[{"locator": {"page": 1}, "text": "Readable synthetic page."}])
        target.update(representation_id="readable-text-v1", content_basis_sha256=digest(source["units"]),
                      unit="page", start=1, end=2)
        for item in bundle["records"]:
            item["payload"]["scope_sha256"] = digest(bundle["scope"])
            item["payload"]["portions"][0].update(start=1, end=2)
        message = "quote-free coverage requires failed unreadable targets"
    # A structurally reconstructable proposal cannot bypass current acceptance checks.
    history = validate_history([record(bundle, "coverage-capture")], bundle["sources"], bundle["matter_id"])
    with pytest.raises(ValueError, match=message):
        append_revision(history, record(bundle, "coverage-capture", 2), bundle["sources"], scope=bundle["scope"])


@pytest.mark.parametrize("status", ["proposed", "accepted"])
@pytest.mark.parametrize(("invalid_target", "message"), [
    ("missing-revision", "unresolved"), ("foreign-matter", "foreign|matter"),
    ("non-assertion", "assertion"), ("self", "unresolved|self"),
    ("duplicate-key", "duplicate"),
])
def test_fresh_append_resolves_conflict_targets_before_accepting_snapshot(bundle, status, invalid_target, message):
    history = [record(bundle, "event-arrival", revision) for revision in (1, 2)]
    proposal = record(bundle, "event-witness")
    relation = proposal["relations"][0]
    if invalid_target == "missing-revision":
        relation["revision"] = 99
    elif invalid_target == "foreign-matter":
        for target in history:
            target["matter_id"] = "foreign-matter"
    elif invalid_target == "non-assertion":
        target = record(bundle, "mention-rowan-report")
        history.append(target)
        relation.update(record_id=target["record_id"], revision=target["revision"])
    elif invalid_target == "self":
        relation.update(record_id=proposal["record_id"], revision=1 if status == "proposed" else 2)
    else:
        history.append(deepcopy(history[-1]))
    # These are valid-shaped inputs, including an identical proposed basis for
    # acceptance; neither changed evidence nor missing proposal explains rejection.
    validate_record(proposal, bundle["sources"])
    candidate = proposal
    if status == "accepted":
        history.append(deepcopy(proposal))
        candidate = decision(proposal)
        assert candidate["relations"] == history[-1]["relations"]
    with pytest.raises(ValueError, match=message):
        append_revision(history, candidate, bundle["sources"])


def test_fresh_conflicting_proposal_and_acceptance_preserve_exact_target_revision(bundle):
    history = [record(bundle, "event-arrival", revision) for revision in (1, 2, 3)]
    proposal = record(bundle, "event-witness")
    history = append_revision(history, proposal, bundle["sources"])
    history = append_revision(history, decision(proposal), bundle["sources"])
    accepted = next(r for r in selected(history) if r["record_id"] == "event-witness")
    assert accepted["status"] == "accepted" and accepted["revision"] == 2
    assert accepted["relations"] == proposal["relations"]
    assert accepted["relations"][0]["record_id"] == "event-arrival"
    assert accepted["relations"][0]["revision"] == 2  # Never retarget the newer proposal.


def test_fresh_append_cannot_disable_support_and_relation_checks(bundle):
    proposal = record(bundle, "event-witness")
    proposal["relations"][0]["revision"] = 99
    with pytest.raises(TypeError, match="resolve_support"):
        append_revision([], proposal, bundle["sources"], resolve_support=False)
