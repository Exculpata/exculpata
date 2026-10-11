"""Synthetic examples of routing intent; no case material or retrieval is used."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import re

import pytest

from case_intelligence import ask_router
from case_intelligence.ask_router import RouteDecision, classify
from case_intelligence.exact_search import Literal, ParsedQuery, QuerySyntaxError


@pytest.mark.parametrize("text,expected_kind", [
    ("", "question"),
    (" ", "question"),
    ("\t\r\n  ", "question"),
    ("bicycle", "question"),
    ("red bicycle", "question"),
    ("Where was the bicycle found", "question"),
    ("Where was the bicycle found?", "question"),
    ("Summarize the synthetic reports", "question"),
    ("(red)", "question"),
    ("(red bicycle)", "question"),
    ("red (blue bicycle)", "question"),
    ("café bicycle", "question"),
    ("What is all the fuss about?", "question"),
    ("After all, what happened?", "question"),
    ("The bicycle was there all the time", "question"),
    ("All of a sudden the bicycle stopped", "question"),
    ("all I need is a summary", "question"),
    ("All documents agree", "question"),
    ("Every report is false", "question"),
    ("Is every report about the bicycle false?", "question"),
    ("All of the records seem consistent", "question"),
    ("Please all the documents agree", "question"),
    ("Every report that is false needs checking", "question"),
    ("Every document which is disputed needs review", "question"),
    ("Does every document that mentions bicycles agree?", "question"),
    ("Tell me whether every document mentioning bicycles agrees", "question"),
    ("Did you list all documents?", "question"),
    ("I do not want you to list all documents.", "question"),
    ("Before you list all documents, explain the risks.", "question"),
    ("Do you review all documents about bicycles?", "question"),
    ("Please explain how to enumerate all records", "question"),
    ("Check every source", "every_source"),
    ("Review all documents about bicycles", "every_source"),
    ("Please enumerate all records about bicycles", "every_source"),
    ("Can you list all documents?", "every_source"),
    ("Please can you list all documents?", "every_source"),
    ("Please could you check every source?", "every_source"),
    ("Please would you review all documents?", "every_source"),
    ("Please will you enumerate every record?", "every_source"),
    ("Could you please check every source?", "every_source"),
    ("I need every document mentioning bicycles", "question"),
    ("All documents about bicycles agree.", "question"),
    ("All documents about bicycles agree", "question"),
    ("Every report containing the claim is unreliable.", "question"),
    ("Every report containing the claim is unreliable", "question"),
    ("Every report concerning bicycles contradicts the claim", "question"),
    ("I need all documents about bicycles", "question"),
    ("I want every report containing the claim", "question"),
    ("List all documents about bicycles", "every_source"),
    ("I need all documents deleted", "question"),
    ("I want every report removed.", "question"),
    ("I need all files archived", "question"),
    ("I want every source renamed", "question"),
    ("I need all documents about bicycles deleted", "question"),
    ("I want every report concerning bicycles summarized", "question"),
    ("I need all documents containing bicycles renamed.", "question"),
    ("I want every source with bicycle references archived", "question"),
    ("I need to find all documents about bicycles", "every_source"),
    ("I want to review every report concerning bicycles", "every_source"),
    ("I need you to list all documents about bicycles", "every_source"),
    ("I want you to please check every source", "every_source"),
    ("I need all documents", "every_source"),
    ("I want every report?", "every_source"),
    ("List all PDFs", "every_source"),
    ("Find every video", "every_source"),
    ("Review every image", "every_source"),
    ("Enumerate all spreadsheets", "every_source"),
    ("Check all audio files", "every_source"),
    ("List all PDF documents", "every_source"),
    ("Find every matching video file", "every_source"),
    ("Review all available image files", "every_source"),
    ("List all photographs", "every_source"),
    ("Check every workbook", "every_source"),
    ("Find all text files", "every_source"),
    ("List all CSV files", "every_source"),
    ("List each and every document", "every_source"),  # Only uppercase Boolean operators establish exact intent.
    ("I need each and every record", "every_source"),  # Only uppercase Boolean operators establish exact intent.
    ("List each and every document?", "every_source"),
    ("I need each and every record.", "every_source"),
    ("All documents", "every_source"),
    ("Every report?", "every_source"),
    ("All records with bicycle references", "question"),
    ("Every document containing bicycle", "question"),
    ("Do all the documents agree?", "question"),
    ("Are all the reports consistent?", "question"),
    ("find every reason the bicycle failed", "question"),
    ("show all red bicycles", "question"),
    ("small record summary", "question"),
    ("everyday document review", "question"),
    ('"red bicycle"', "exact"),
    ('  "red bicycle"  ', "exact"),
    ('"bicycle"', "exact"),
    ('What does "red bicycle" mean', "question"),
    ('Who mentioned "red bicycle"', "question"),
    ('"find every document"', "exact"),
    ('"all the time"', "exact"),
    ('"say \\"hello\\""', "exact"),
    ("Smith AND Jones", "exact"),
    ("Smith NOT Jones", "exact"),
    ("red AND bicycle", "exact"),
    ("red and bicycle", "question"),  # Only uppercase Boolean operators establish exact intent.
    ("red AnD bicycle", "question"),  # Only uppercase Boolean operators establish exact intent.
    ("red OR blue", "exact"),
    ("red or blue", "question"),  # Only uppercase Boolean operators establish exact intent.
    ("NOT bicycle", "exact"),
    ("red not bicycle", "question"),  # Only uppercase Boolean operators establish exact intent.
    ("What changed and why", "question"),  # Only uppercase Boolean operators establish exact intent.
    ("(red AND bicycle)", "exact"),
    ("green (red AND bicycle)", "exact"),
    ("red (blue OR green)", "exact"),
    ("NOT (red bicycle)", "exact"),
    ("red NEAR/3 bicycle", "exact"),
    ("red near/0 bicycle", "exact"),
    ("red BEFORE/100 bicycle", "exact"),
    ('"red bicycle" BEFORE/2 shed', "exact"),
    ('find every document containing "red bicycle"', "exact"),
    ("list all red OR blue", "exact"),
    ('What does "red bicycle" mean?', "question"),
    ('"red bicycle', "question"),
    ('What does "red bicycle mean', "question"),
    ('"red bicycle" "unfinished', "question"),
    ('""', "question"),
    ('"..."', "question"),
    ('red"bicycle"', "question"),
    ('"red"bicycle', "question"),
    ("'red bicycle'", "question"),
    ("“red bicycle”", "question"),
    ("red AND", "question"),
    ("AND red", "question"),
    ("red OR", "question"),
    ("NOT", "question"),
    ("red AND OR bicycle", "question"),
    ("(red OR bicycle", "question"),
    ("red NEAR/101 bicycle", "question"),
    ("red NEAR/3", "question"),
    ("red W/3 bicycle", "question"),
    ("How do red and blue differ?", "question"),
    ("red\nAND bicycle", "question"),
    ('"' + "a" * 513 + '"', "question"),
    ("find every", "every_source"),
    ("list all", "every_source"),
    ("FIND EVERY DOCUMENT ABOUT THE BICYCLE", "every_source"),
    ("find all matching records", "every_source"),
    ("find every document that mentions the bicycle", "every_source"),
    ("find each document about the bicycle", "every_source"),
    ("Please find every email about bicycles", "every_source"),
    ("list all records mentioning the bicycle", "every_source"),
    ("List every source about the bicycle", "every_source"),
    ("show me all of the reports about the bicycle", "every_source"),
    ("show all the files about the bicycle", "every_source"),
    ("locate every single transcript about the bicycle", "every_source"),
    ("identify all statements about the bicycle", "every_source"),
    ("retrieve every recording mentioning the bicycle", "every_source"),
    ("return all matching results", "every_source"),
    ("collect all exhibits about the bicycle", "every_source"),
    ("search for all messages about the bicycle", "every_source"),
    ("give me every reference to the bicycle", "every_source"),
    ("get all occurrences of bicycle", "every_source"),
    ("find all mentions of the bicycle", "every_source"),
    ("list all matches for bicycle", "every_source"),
    ("all the documents about the bicycle", "question"),
    ("All the records mentioning the bicycle", "question"),
    ("all evidence relating to the bicycle", "question"),
    ("every document that mentions the bicycle", "question"),
    ("every record which mentions the bicycle", "question"),
    ("I need every document that mentions the bicycle", "question"),
    ("Can you list all records about the bicycle?", "every_source"),
    ('find every document with "red bicycle', "every_source"),
    ('list all records containing "red bicycle"?', "every_source"),
    (" \tlist   all   records about the bicycle\n", "every_source"),
])
def test_synthetic_route_table(text, expected_kind):
    decision = classify(text)

    assert isinstance(decision, RouteDecision)
    assert decision.kind == expected_kind
    assert decision == classify(text)
    assert decision.reason == decision.reason.strip()
    assert 0 < len(decision.reason) <= 120
    assert decision.reason.endswith(".")
    assert "\n" not in decision.reason


@pytest.mark.parametrize("suffix", ["", "?"])
@pytest.mark.parametrize("text,expected_kind", [
    ("Did Smith and Jones meet on Tuesday", "question"),
    ("Why did the officer not stop the car", "question"),
    ("Was it red or blue", "question"),
    ("who called 911 and when", "question"),
    ("show me all the texts between Dana and Lee", "every_source"),
])
def test_ordinary_boolean_words_do_not_depend_on_question_mark(text, expected_kind, suffix):
    assert classify(text + suffix).kind == expected_kind


@pytest.mark.parametrize("text", [
    'red and "blue bicycle"',
    'red or "blue bicycle"',
    'not "blue bicycle"',
    "red or blue NEAR/3 bicycle",
    "not blue BEFORE/3 bicycle",
])
def test_lowercase_boolean_nodes_preserve_phrase_and_proximity_intent(text):
    assert classify(text).kind == "exact"


@pytest.mark.parametrize("text", [
    '"red AND blue" and green',
    '"red OR blue" or green',
    'not "red NOT blue"',
    '"AND" and green',
    '"(AND)" or green',
    'not ("red AND blue" or green)',
    'red and "blue AND green" or "amber NOT white"',
    r'"red \" AND blue" and green',
    r'"red \\ AND blue" and green',
    r'"red \" OR blue" or green',
    r'not "red \\ NOT blue"',
])
def test_quoted_operator_words_keep_the_phrase_explanation(text):
    assert classify(text) == RouteDecision(
        "exact", "Chose exact search because your words are in quotes.",
    )


@pytest.mark.parametrize("text", [
    '"red AND blue" AND green',
    '"red OR blue" OR green',
    'NOT "red NOT blue"',
    '"red AND blue" and (green OR amber)',
    '("red AND blue")AND(green)',
    r'"red \" AND blue" AND green',
    r'"red \\" AND green',
])
def test_unquoted_uppercase_operators_keep_the_boolean_explanation(text):
    assert classify(text) == RouteDecision(
        "exact", "Chose exact search because you used a Boolean operator.",
    )


@pytest.mark.parametrize("text", [
    'red NEAR/2 "blue AND green" and amber',
    'not red BEFORE/2 "blue OR green"',
    'red near/0 "blue NOT green" or amber',
])
def test_quoted_operator_words_preserve_the_proximity_explanation(text):
    assert classify(text) == RouteDecision(
        "exact", "Chose exact search because you specified how close words must be.",
    )


@pytest.mark.parametrize("text,expected_kind", [
    ('"red AND blue" and', "question"),
    ('"red AND blue"AND green', "question"),
    (r'"red \q AND blue" and green', "question"),
    (r'"red \" AND blue and green', "question"),
    ('find every document with "red AND blue', "every_source"),
])
def test_quoted_operator_detection_still_requires_valid_grammar(text, expected_kind):
    assert classify(text).kind == expected_kind


@pytest.mark.parametrize("field,value", [("kind", "exact"), ("reason", "Changed.")])
def test_route_decision_is_frozen(field, value):
    decision = classify("Where was the synthetic bicycle found?")

    with pytest.raises(FrozenInstanceError):
        setattr(decision, field, value)


@pytest.mark.parametrize("text", [
    "syntheticcanaryword",
    '"syntheticcanaryword"',
    "find every record about syntheticcanaryword",
    '"syntheticcanaryword',
])
def test_reasons_do_not_echo_input(text):
    assert "syntheticcanaryword" not in classify(text).reason.casefold()


def test_empty_input_explains_that_nothing_was_typed():
    assert "nothing was typed" in classify(" \t\n ").reason.casefold()


@pytest.mark.parametrize("text,phrase,expected_kind", [
    ("red AND bicycle", False, "question"),
    ('"red bicycle"', False, "question"),
    ("red NEAR/3 bicycle", False, "question"),
    ("ordinary synthetic words", True, "exact"),
])
def test_exact_routing_obeys_the_parser_structure(monkeypatch, text, phrase, expected_kind):
    seen = []

    def parse_synthetic_query(value):
        seen.append(value)
        return ParsedQuery(value, Literal(("synthetic",), phrase=phrase))

    monkeypatch.setattr(ask_router, "parse_query", parse_synthetic_query)

    assert classify(text).kind == expected_kind
    assert seen == [text]


@pytest.mark.parametrize("text,expected_kind", [
    ('"synthetic phrase"', "question"),
    ('find every record about "synthetic phrase"', "every_source"),
])
def test_parser_rejection_falls_back_to_natural_language(monkeypatch, text, expected_kind):
    def reject_synthetic_query(value):
        raise QuerySyntaxError("Synthetic invalid query", 0)

    monkeypatch.setattr(ask_router, "parse_query", reject_synthetic_query)

    assert classify(text).kind == expected_kind


def test_advertised_upload_formats_are_recognized_as_source_populations():
    template = (Path(__file__).resolve().parents[1] / "src/case_intelligence/templates/workbench_setup.html").read_text()
    extensions = {
        item[1:].upper()
        for accept in re.findall(r'\baccept="([^"]+)"', template)
        for item in accept.split(",")
        if item.startswith(".")
    }
    assert extensions
    for extension in sorted(extensions):
        for text in (f"List all {extension} files", f"Review all {extension}s", f"Find every .{extension} file"):
            assert classify(text).kind == "every_source", text
