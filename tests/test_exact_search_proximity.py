"""Synthetic positional meaning, complete enumeration, and plain controls."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import hashlib
import html
import json
from pathlib import Path
import random
import re

import pytest
from fastapi.testclient import TestClient

from case_intelligence.exact_search import (
    GRAMMAR_VERSION, QuerySyntaxError, Proximity, Literal, parse_query,
    matching_spans, tokenize_text,
)
from case_intelligence.exact_search_results import (
    ExactSearchUnavailable, build_search_query, passage_preview, search_documents,
)
from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.workbench import create_workbench_app


def document(index, *texts):
    return PilotDocument(f"{index:032x}", f"Synthetic {index:04}.txt", "", "text/plain", 0, "ready", "",
        [{"number": n, "text": text} for n, text in enumerate(texts, 1)], version_id=f"version-{index}")


def scan(documents, query, **kwargs):
    return search_documents(documents, query, scope=("synthetic-proximity",), **kwargs)


def test_frozen_positional_corpus_matches_reference_and_complete_results():
    path = Path(__file__).parent / "fixtures/synthetic/exact-proximity/v2/corpus.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == path.with_suffix(".sha256").read_text().split()[0]
    corpus = json.loads(path.read_text())
    assert corpus["provenance"] == "synthetic" and corpus["grammar_version"] == GRAMMAR_VERSION
    documents = [document(item["id"], *item["units"]) for item in corpus["documents"]]
    for case in corpus["cases"]:
        expected = [f"{index:032x}" for index in case["expected"]]
        assert [item.document_id for item in scan(documents, case["query"]).items] == expected
        parsed = parse_query(case["query"])
        assert [item.document_id for item in documents if parsed.matches_units(unit.text for unit in item.parsed_units())] == expected


@pytest.mark.parametrize("gap", [0, 1, 5, 100])
@pytest.mark.parametrize("ordered", [False, True])
def test_distance_is_intervening_words_and_order_is_explicit(gap, ordered):
    operator = "BEFORE" if ordered else "NEAR"
    parsed = parse_query(f'"red bicycle" {operator}/{gap} "north depot"')
    middle = "neutral " * gap
    assert parsed.matches_units([f"red bicycle {middle}north depot"])
    assert not parsed.matches_units([f"red bicycle {middle}extra north depot"])
    assert parsed.matches_units([f"north depot {middle}red bicycle"]) is (not ordered)
    assert not parsed.matches_units(["red bicycle", "north depot"])


@pytest.mark.parametrize("query,text,wanted", [
    ("red NEAR/0 red", "red", False),
    ("red NEAR/0 red", "red red", True),
    ('"red red" NEAR/0 red', "red red", False),
    ('"red red" NEAR/0 red', "red red red", True),
    ('"red blue" NEAR/0 "blue green"', "red blue green", False),
    ('"red blue" NEAR/0 "blue green"', "red blue blue green", True),
    ("café BEFORE/0 Straße", "CAFE\u0301, STRASSE", True),
    ("İ NEAR/0 depot", "i\u0307\nDEPOT", True),
    ("İ NEAR/0 depot", "I DEPOT", False),
    ("हिंदी NEAR/0 depot", "हिंदी ... depot", True),
    ("red NEAR/0 bicycle", "red\n\n-- bicycle", True),
    ("red NEAR/0 bicycle", "red 42 bicycle", False),
    ("red NEAR/1 bicycle", "red 42 bicycle", True),
    ("RB-101 BEFORE/0 depot", "rb-101 depot", True),
    ("red NEAR/0 bicycle", "red bi-\ncycle", False),
])
def test_repetition_overlap_unicode_punctuation_and_ocr_tokens(query, text, wanted):
    assert parse_query(query).matches_units([text]) is wanted


def test_boolean_precedence_roundtrip_document_not_and_version_boundary():
    expressions = ["red NEAR/1 bicycle OR green AND depot", "NOT red NEAR/0 bicycle",
        '(red BEFORE/1 bicycle) AND NOT "missing detail"', 'red NEAR/1 bicycle AND depot BEFORE/2 gate']
    documents = ["red bicycle", "bicycle red", "green depot", "red gate bicycle", "neutral", "red bicycle missing detail"]
    for expression in expressions:
        parsed = parse_query(expression)
        assert parse_query(parsed.normalized).expression == parsed.expression
        assert parsed.to_dict()["grammar_version"] == f"{LEGACY_STEM}-exact-v2"
        for text in documents:
            assert parse_query(parsed.normalized).matches_units([text]) == parsed.matches_units([text])
    assert parse_query("NOT red NEAR/0 bicycle").matches_units(["red", "bicycle"])
    assert not parse_query("red NEAR/0 bicycle NOT depot").matches_units(["red bicycle", "depot"])
    assert parse_query("red AND bicycle", grammar_version=f"{LEGACY_STEM}-exact-v1").grammar_version == f"{LEGACY_STEM}-exact-v1"
    assert parse_query("red BEFORE bicycle", grammar_version=f"{LEGACY_STEM}-exact-v1").matches_units(["red before bicycle"])
    assert not parse_query("red BEFORE bicycle", grammar_version=f"{LEGACY_STEM}-exact-v1").matches_units(["red bicycle"])
    with pytest.raises(QuerySyntaxError, match="v2"):
        parse_query("red NEAR/1 bicycle", grammar_version=f"{LEGACY_STEM}-exact-v1")
    with pytest.raises(QuerySyntaxError, match="supported"):
        parse_query("red", grammar_version="unknown")


@pytest.mark.parametrize("query", ["red NEAR bicycle", "red BEFORE bicycle", "red NEAR/101 bicycle",
    "red NEAR/-1 bicycle", "red NEAR/+1 bicycle", "red NEAR/1.0 bicycle", "red NEAR/ bicycle",
    "red W/3 bicycle", "red PRE/3 bicycle", "red WITHIN/3 bicycle", "red ADJ/3 bicycle",
    "red NEAR/1 (blue OR green)", "(red OR green) NEAR/1 bicycle", "red NEAR/1 NOT bicycle",
    "red NEAR/1 bicycle NEAR/1 depot", "NEAR/1 bicycle", "red BEFORE/1", "red NEAR/１ bicycle",
    "red NEAR/2 bicycle*", "type:pdf AND red NEAR/1 bicycle"])
def test_unsupported_or_ambiguous_positional_input_fails_explicitly(query):
    with pytest.raises(QuerySyntaxError):
        parse_query(query)


def test_linear_position_pairing_matches_an_independent_brute_force_oracle():
    rng = random.Random(903)
    for _ in range(300):
        tokens = tuple(rng.choice(("red", "blue", "green")) for _ in range(rng.randrange(1, 40)))
        left = tuple(rng.choice(("red", "blue", "green")) for _ in range(rng.randrange(1, 4)))
        right = tuple(rng.choice(("red", "blue", "green")) for _ in range(rng.randrange(1, 4)))
        gap, ordered = rng.randrange(5), rng.choice((True, False))
        pairs = [(a, b) for a in range(len(tokens)) for b in range(len(tokens))
                 if tokens[a:a + len(left)] == left and tokens[b:b + len(right)] == right]
        expected = any(0 <= b - a - len(left) <= gap or
                       (not ordered and 0 <= a - b - len(right) <= gap) for a, b in pairs)
        node = Proximity(Literal(left), Literal(right), gap, ordered)
        assert bool(tuple(matching_spans(tokens, node))) == expected


def test_proximity_preview_anchors_on_the_pair_not_an_earlier_isolated_operand():
    unit = document(1, "red isolated. " + "neutral " * 120 + "red beside the bicycle at the depot").parsed_units()[0]
    preview = passage_preview(unit, parse_query("red NEAR/2 bicycle"))
    text = "".join(piece for piece, _ in preview["pieces"])
    assert "isolated" not in text and "red beside the bicycle" in text
    assert ("red beside the bicycle", True) in preview["pieces"]


def test_all_matching_documents_remain_browsable_and_budget_is_cooperative():
    documents = [document(index, "red bicycle") for index in range(137)]
    first = scan(documents, "red NEAR/0 bicycle")
    found = []
    for page in range(1, first.pages + 1):
        result = scan(documents, "red NEAR/0 bicycle", page=page, expected_fingerprint=first.fingerprint)
        found.extend(item.document_id for item in result.items)
    assert len(found) == len(set(found)) == 137
    checks = 0
    def stop():
        nonlocal checks
        checks += 1
        if checks == 8:
            raise ExactSearchUnavailable("synthetic budget")
    with pytest.raises(ExactSearchUnavailable):
        parse_query("red NEAR/100 bicycle").matches_units(["red " * 50000], budget_check=stop)


def test_plain_controls_build_literal_proximity_without_boolean_knowledge():
    query = build_search_query(exclude="cancelled", proximity_first="red bicycle", proximity_second="depot",
        proximity_gap="4", proximity_order="first")
    assert parse_query(query).matches_units(["red bicycle arrived at the north depot"])
    assert not parse_query(query).matches_units(["depot red bicycle"])
    assert not parse_query(query).matches_units(["red bicycle depot", "cancelled"])
    assert parse_query(build_search_query(proximity_first="AND", proximity_second="OR NOT", proximity_gap="0")).matches_units(["and or not"])
    for fields in [{"proximity_first": "red"}, {"proximity_second": "depot"},
                   {"proximity_first": "red", "proximity_second": "depot", "proximity_gap": "101"},
                   {"proximity_first": "red", "proximity_second": "depot", "proximity_order": "unknown"}]:
        with pytest.raises(ValueError):
            build_search_query(**fields)


def test_plain_route_scope_pagination_errors_and_postgres_configuration_independence(tmp_path, monkeypatch):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic nearby details", "descriptor": ""}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        bench = app.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        sources = [document(index, "red bicycle near depot") for index in range(31)]
        sources.append(document(100, "red bicycle", "depot"))
        bench.source_store(matter).documents.update({item.document_id: item for item in sources})
        bench.workspace.reconcile_source_organizations(matter.matter_id,
            tuple((item.document_id, "upload", item.display_name) for item in sources))
        subset = bench.workspace.create_source_set(matter.matter_id, "Synthetic nearby subset", [sources[0].document_id], matter.owner_id)
        monkeypatch.setattr(bench, "_retriever", lambda *a, **k: pytest.fail("Exact proximity used ranked retrieval"))
        fields = {"proximity_first": "red bicycle", "proximity_second": "depot", "proximity_gap": "1", "proximity_order": "first"}
        response = client.get(f"/matters/{slug}/exact-search", params=fields)
        assert response.status_code == 200 and "31 sources found" in response.text
        next_href = html.unescape(re.search(r'href="([^"]+)">Next →</a>', response.text).group(1))
        assert "proximity_first=red+bicycle" in next_href and "proximity_order=first" in next_href
        assert "Page 2 of 2" in client.get(next_href).text
        assert "1 source found" in client.get(f"/matters/{slug}/exact-search", params={**fields, "source_set": subset.source_set_id}).text
        # This is a routing contract, not a live PostgreSQL indexed acceptance.
        monkeypatch.setattr(bench, "postgres_ready", True)
        monkeypatch.setattr(bench, "_postgres_projection_configured", True)
        assert "31 sources found" in client.get(f"/matters/{slug}/exact-search", params=fields).text
        invalid = client.get(f"/matters/{slug}/exact-search", params={**fields, "proximity_second": ""})
        assert invalid.status_code == 400 and "Enter both details" in invalid.text
        assert "0 sources found" not in invalid.text


def test_long_proximity_preview_keeps_both_operands_with_an_explicit_omission():
    unit = document(1, "red " + "interveningword " * 100 + "bicycle").parsed_units()[0]
    preview = passage_preview(unit, parse_query("red NEAR/100 bicycle"))
    visible = "".join(text for text, _ in preview["pieces"])
    assert "red" in visible and "bicycle" in visible and "…" in visible
    assert len(visible) <= 603
    assert any(highlight and "red" in text for text, highlight in preview["pieces"])
    assert any(highlight and "bicycle" in text for text, highlight in preview["pieces"])


def test_single_word_conjunctions_use_shared_lookup_without_positional_rescans(monkeypatch):
    import case_intelligence.exact_search as module
    original = module.matching_spans
    calls = []
    def positions(tokens, node, **kwargs):
        calls.append(node)
        return original(tokens, node, **kwargs)
    monkeypatch.setattr(module, "matching_spans", positions)
    query = " ".join(f"w{index}" for index in range(100))
    assert not module.parse_query(query).matches_units(["neutral " * 125000])
    assert calls == []
    text = " ".join(f"w{index}" for index in range(100)) + " neutral" * 1000
    assert module.parse_query(query).matches_units([text])
    assert calls == []


def test_proximity_pairing_consumes_occurrences_incrementally():
    class ObservedTokens(tuple):
        reads = 0
        def __getitem__(self, key):
            self.reads += 1
            assert self.reads <= 8, "Pairing scanned repeated occurrences before its first result"
            return super().__getitem__(key)
    tokens = ObservedTokens(("red",) * 100000)
    node = Proximity(Literal(("red",)), Literal(("red",)), 0)
    assert next(matching_spans(tokens, node)) == (0, 2)


def test_preview_retains_only_explaining_occurrences_for_repeated_matches(monkeypatch):
    import case_intelligence.exact_search_results as module
    original = module.matching_spans
    retained = []
    def positions(*args, **kwargs):
        for span in original(*args, **kwargs):
            retained.append(span)
            yield span
    monkeypatch.setattr(module, "matching_spans", positions)
    unit = document(1, "red " * 10000).parsed_units()[0]
    result = module.passage_preview(unit, parse_query("red NEAR/0 red"))
    assert retained == [(0, 2)]
    assert ("red red", True) in result["pieces"]


@pytest.mark.parametrize("word", ["W", "PRE", "BEFORE", "w", "pre"])
def test_v1_preserves_standalone_words_reserved_only_by_v2(word):
    assert parse_query(word, grammar_version=f"{LEGACY_STEM}-exact-v1").matches_units([word])


def test_reverse_order_match_is_found_before_scanning_large_tail():
    checks = []
    def budget():
        checks.append(1)
        assert len(checks) <= 2, "An early reverse match scanned the irrelevant tail"
    node = parse_query("red NEAR/0 blue").expression
    assert next(matching_spans(("blue", "red") + ("neutral",) * 100000, node,
                              budget_check=budget)) == (0, 2)


def test_mixed_preview_shows_late_proximity_alongside_early_literal():
    source = document(1, "anchor " + "neutral " * 120 + "red " + "interveningword " * 100 + "bicycle")
    preview = search_documents([source], "anchor AND red NEAR/100 bicycle", scope=("synthetic",)).items[0].previews[0]
    visible = "".join(text for text, _ in preview["pieces"])
    assert all(word in visible for word in ("anchor", "red", "bicycle"))
    assert len(visible) <= 605


def test_mixed_preview_keeps_literal_inside_long_proximity_span():
    source = document(1, "red " + "neutral " * 50 + "anchor " + "neutral " * 49 + "bicycle")
    preview = search_documents([source], "anchor AND red NEAR/100 bicycle", scope=("synthetic",)).items[0].previews[0]
    visible = "".join(text for text, _ in preview["pieces"])
    assert all(word in visible for word in ("anchor", "red", "bicycle"))
    assert len(visible) <= 605
