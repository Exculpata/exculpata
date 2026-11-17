"""Synthetic acceptance contract for the code-checked synthesis evaluation."""

from copy import deepcopy
import io
import ipaddress
import json
from pathlib import Path
import re

import pytest

from case_intelligence import defense_eval, generation
from case_intelligence.generator_schema import refuse_tool_request
import case_intelligence.synthesis_eval as evaluation


ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    # fixture-write: Persist an isolated synthetic suite or answer mutation.
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def write_rows(path, rows):
    # fixture-write: Persist isolated synthetic page passages or answer rows.
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def suite(tmp_path):
    directory = tmp_path / "suite"
    directory.mkdir()
    pages = [
        {"doc_id": "SYN-A", "page": 1, "text":
         "Family holdings were 6 shares plus 6 shares out of 100 shares. "
         "Account SYN-AC-17 belongs to Velra Tovin, also called V. Tovin."},
        {"doc_id": "SYN-B", "page": 2, "text":
         "The denominator was 100 shares. Family holdings exceeded ten percent. "
         "The opening on 2025-02-03 preceded the purchase on 2025-02-04 and then the notice."},
        {"doc_id": "SYN-C", "page": 1, "text":
         "Family holdings were 6 shares plus 6 shares out of 100 shares."},
        {"doc_id": "SYN-D", "page": 1, "text":
         "Velra Tovin and Neral Works control the account. "
         "The approval letter was referenced but not produced."},
    ]
    checks = [
        ("aggregation", "What percentage did the family hold?", "12 percent",
         {"type": "numeric", "value": "12", "unit": "percent", "absolute_tolerance": "0.1"}),
        ("contradiction", "Did the family exceed ten percent, and what percentage did it hold?", "yes; 12 percent",
         {"type": "boolean_with_value", "value": True, "numeric":
          {"value": "12", "unit": "percent", "absolute_tolerance": "0.1"}}),
        ("multi_hop", "Who controls the account?", "Velra Tovin; Neral Works",
         {"type": "set", "members": ["Velra Tovin", "Neral Works"],
          "aliases": {"Velra Tovin": ["V. Tovin"]}}),
        ("timeline", "What happened first through last?", "opening; purchase; notice",
         {"type": "ordered", "events": ["opening", "purchase", "notice"]}),
        ("gap", "When was the purchase whose approval letter is missing?", "2025-02-04",
         {"type": "date", "value": "2025-02-04"}),
        ("trap", "Who approved the purchase?", "Not in the record: approval letter is missing.",
         {"type": "not_in_record"}),
    ]
    steps = [
        ["Add the two holdings on SYN-A page 1.", "Divide by the denominator on SYN-B page 2."],
        ["Combine the holdings on SYN-A page 1.", "Compare with the threshold on SYN-B page 2."],
        ["Read the person on SYN-D page 1.", "Read the company on SYN-D page 1."],
        ["Opening precedes purchase on SYN-B page 2.", "Purchase precedes notice on SYN-B page 2."],
        ["Read the purchase date on SYN-B page 2.", "Find the missing letter reference on SYN-D page 1."],
        ["Find the letter reference on SYN-D page 1.", "The letter is not produced per SYN-D page 1."],
    ]
    questions = []
    for index, (kind, text, canonical, check) in enumerate(checks):
        questions.append({
            "question_id": f"q{index}", "type": kind, "text": text,
            "final_answer": {"text": canonical, "check": check},
            "required_pieces": ([[["SYN-A", 1], ["SYN-C", 1]], [["SYN-B", 2]]]
                                if index < 2 else [[["SYN-D", 1] if index in (2, 5) else ["SYN-B", 2]]]),
            "reasoning_steps": steps[index],
            "expected_flags": ["approval letter"] if kind == "trap" else [],
            "forbidden": ["13 percent"] if index == 0 else [],
        })
    questions[4]["required_pieces"].append([["SYN-D", 1]])
    pages.extend({"doc_id": f"SYN-F{index:02}", "page": 1,
                  "text": "Routine unrelated packing inventory."} for index in range(41))
    write_json(directory / "suite.json", {
        "answer_key_version": "1.0.0", "seed": 7, "questions": questions,
    })
    write_rows(directory / "passages.jsonl", pages)
    return directory


def invoke(phase, suite, output, *options):
    return evaluation.main([phase, "--suite", str(suite), "--output-dir", str(output), *options])


def key_answers(suite, output):
    assert invoke("answer", suite, output, "--answerer", "stub-key", "--seed", "7") == 0
    return read_rows(output / "answers.jsonl")


def score(suite, output, answers):
    write_rows(output / "answers.jsonl", answers)
    assert invoke("score", suite, output, "--force") == 0
    return json.loads((output / "scorecard.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("answerer,points,percent,zeros", [
    ("stub-key", 65, 100, 0), ("stub-fabricate", 0, 0, 6),
])
def test_bounds_contract_reports_and_summary(suite, tmp_path, capsys, monkeypatch,
                                            answerer, points, percent, zeros):
    def offline(*args, **kwargs):
        pytest.fail("Stub evaluation must not contact a service")

    monkeypatch.setattr(generation, "open_service_request", offline)
    calls = []
    original = defense_eval._model_record

    def model_record(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(defense_eval, "_model_record", model_record)
    output = tmp_path / "run"
    assert invoke("answer", suite, output, "--answerer", answerer, "--seed", "7") == 0
    answers = read_rows(output / "answers.jsonl")
    assert len(answers) == 6
    for answer in answers:
        assert set(answer) == {"question_id", "status", "answer_text", "final_value", "citations", "run"}
        assert answer["status"] in {"answered", "partly_answered", "not_in_record"}
        assert answer["final_value"] is None or isinstance(answer["final_value"], str)
        assert {"model", "revision", "thinking", "elapsed_ms", "error"} <= answer["run"].keys()
        assert answer["run"]["error"] is None
        assert all(set(cite) == {"doc_id", "page", "quote"} for cite in answer["citations"])
    capsys.readouterr()
    assert invoke("score", suite, output) == 0
    count = 6 if answerer == "stub-key" else 0
    assert capsys.readouterr().out.strip() == (
        f"synthesis {points:.1f}/100; answer {count}/6; sources {count}/6; zeros {zeros}; judge none"
    )
    card = json.loads((output / "scorecard.json").read_text(encoding="utf-8"))
    assert card["aggregates"]["code_score"] == points
    assert card["aggregates"]["code_percent"] == percent
    assert card["aggregates"]["total"] == points
    assert card["aggregates"]["zeros"] == zeros
    assert card["code_max"] == 65 and card["judge"] == "none"
    assert len(card["per_type"]) == 6
    assert all(row["code_score"] == points for row in card["per_type"].values())
    for row in card["questions"]:
        assert row["code_score"] == row["total"] == points
        assert row["work_shown"] is None and row["uncertainty"] is None
        assert bool(row["zero_reasons"]) == bool(zeros)
        assert {"coverage_share", "forbidden_hits", "retrieval_miss"} <= row.keys()
    assert (card["answerer"], card["thinking"], card["top_k"], card["seed"]) == (
        answerer, "server-default", 24, 7,
    )
    assert calls and card["model"] == original("stub", None)
    assert re.fullmatch(r"[0-9a-f]{64}", card["suite_fingerprint"])
    report = (output / "scorecard.md").read_text(encoding="utf-8").lower()
    assert all(text in report for text in ("not scored", "pending", "65", "100"))
    assert all(answer["question_id"] in report for answer in answers)


@pytest.mark.parametrize("index,value,correct", [
    (0, "12.05 percent", True), (0, "13 percent", False),
    (1, "yes; 12 percent", True), (1, "no; 12 percent", False),
    (1, "yes; 13 percent", False),
    (2, "Neral Works; V. Tovin", True), (2, "Velra Tovin", False),
    (3, "opening; purchase; notice", True), (3, "purchase; opening; notice", False),
    (4, "2025-02-04", True), (4, "2025-02-03", False),
])
def test_typed_answers_are_all_or_nothing(suite, tmp_path, index, value, correct):
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    answers[index]["final_value"] = value
    answers[index]["answer_text"] = value
    row = score(suite, output, answers)["questions"][index]
    assert row["answer"] == (40 if correct else 0)
    assert row["sources"] == 25 and not row["zero_reasons"]
    assert row["total"] == (65 if correct else 25)
    if index == 0 and not correct:
        assert row["forbidden_hits"] == ["13 percent"]


def test_numeric_relative_tolerance(suite, tmp_path):
    key = json.loads((suite / "suite.json").read_text())
    check = key["questions"][0]["final_answer"]["check"]
    del check["absolute_tolerance"]
    check["relative_tolerance"] = "0.01"
    write_json(suite / "suite.json", key)
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    for value, points in (("12.05 percent", 40), ("13 percent", 0)):
        answers[0].update(final_value=value, answer_text=value)
        assert score(suite, output, answers)["questions"][0]["answer"] == points


def test_source_groups_are_proportional_and_accept_page_alternatives(suite, tmp_path):
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    alternative = {"doc_id": "SYN-C", "page": 1,
                   "quote": "Family holdings were 6 shares plus 6 shares out of 100 shares."}
    answers[0]["citations"] = [alternative, deepcopy(alternative)]
    row = score(suite, output, answers)["questions"][0]
    assert row["answer"] == 40 and row["sources"] == 12.5
    assert row["coverage_share"] == 0.5 and row["total"] == 52.5
    assert not row["zero_reasons"]
    answers[0]["citations"].append({"doc_id": "SYN-B", "page": 2, "quote": "The denominator was 100 shares."})
    assert score(suite, output, answers)["questions"][0]["sources"] == 25
    answers[0]["citations"] = []
    row = score(suite, output, answers)["questions"][0]
    assert row["sources"] == 0 and row["coverage_share"] == 0
    assert row["answer"] == 40 and not row["zero_reasons"]


@pytest.mark.parametrize("mutation", ["doc", "page", "quote", "account", "person", "company", "date", "doc_text"])
def test_fabrication_zeroes_the_question(suite, tmp_path, mutation):
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    answer = answers[0]
    if mutation in {"doc", "page", "quote"}:
        answer["citations"][0][{"doc": "doc_id", "page": "page", "quote": "quote"}[mutation]] = {
            "doc": "SYN-ABSENT", "page": 999, "quote": "The family sold every share.",
        }[mutation]
    else:
        answer["answer_text"] += " " + {
            "account": "Account SYN-AC-99 funded the purchase.",
            "person": "Zelvor Nuvik approved the purchase.",
            "company": "Zorlen Holdings funded the purchase.",
            "date": "The purchase was on 2039-12-31.",
            "doc_text": "Document SYN-ABSENT confirms the purchase.",
        }[mutation]
    row = score(suite, output, answers)["questions"][0]
    assert row["total"] == row["code_score"] == 0
    assert row["zero_reasons"]


def test_quote_normalization_and_known_uncited_facts_do_not_zero(suite, tmp_path):
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    answers[0]["citations"][0] = {"doc_id": "SYN-A", "page": 1,
                                  "quote": "FAMILY   holdings were 6 shares\nplus 6 shares out of 100 shares."}
    answers[0]["answer_text"] += " Velra Tovin controls the account."
    row = score(suite, output, answers)["questions"][0]
    assert not row["zero_reasons"] and row["answer"] == 40


@pytest.mark.parametrize("status,text,points", [
    ("not_in_record", "Not in the record: the approval letter is missing.", 40),
    ("not_in_record", "Not in the record.", 0),
    ("answered", "Velra Tovin approved it.", 0),
])
def test_trap_requires_abstention_and_the_missing_piece(suite, tmp_path, status, text, points):
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    answers[5].update(status=status, answer_text=text, final_value=None)
    row = score(suite, output, answers)["questions"][5]
    assert row["answer"] == points and not row["zero_reasons"]


@pytest.mark.parametrize("thinking", ["on", "off", "server-default"])
def test_direct_requests_use_bounded_transport_no_tools_and_local_thinking(suite, tmp_path, monkeypatch, thinking):
    output = tmp_path / "run"
    expected = key_answers(suite, output)
    questions = json.loads((suite / "suite.json").read_text())["questions"]
    bodies, bounded = [], []
    original = generation._bounded_json_request

    def bounded_request(*args, **kwargs):
        bounded.append(True)
        return original(*args, **kwargs)

    def transport(request, **kwargs):
        if request.get_method() == "GET":
            return io.BytesIO(b'{"data":[{"id":"synthetic-eval","revision":"synthetic-revision"}]}')
        body = json.loads(request.data)
        bodies.append(body)
        prompt = "\n".join(message["content"] for message in body["messages"] if message["role"] == "user")
        index = next(i for i, question in enumerate(questions) if question["text"] in prompt)
        message = {"content": json.dumps(expected[index]), "reasoning_content": "Invalid decoy answer"}
        return io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())

    endpoint = "http://" + str(ipaddress.IPv4Address(2130706433)) + ":9"
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", endpoint)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic-eval")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING", "1")
    monkeypatch.setattr(generation, "open_service_request", transport)
    monkeypatch.setattr(generation, "_bounded_json_request", bounded_request)
    assert invoke("answer", suite, output, "--answerer", "direct", "--thinking", thinking, "--force") == 0
    actual = read_rows(output / "answers.jsonl")
    assert len(bodies) == len(bounded) == len(actual) == 6
    assert len({body["messages"][0]["content"] for body in bodies}) == 1
    for body, answer, reference in zip(bodies, actual, expected):
        assert refuse_tool_request(body) is None
        assert not {"tools", "tool_choice", "functions"}.intersection(body)
        assert all(message["role"] in {"system", "user"} for message in body["messages"])
        assert body["messages"][0]["role"] == "system"
        if thinking == "server-default":
            assert "chat_template_kwargs" not in body
        else:
            assert body["chat_template_kwargs"]["enable_thinking"] is (thinking == "on")
        assert answer["answer_text"] == reference["answer_text"]
        assert answer["run"]["thinking"] == thinking
        assert answer["run"]["ignored_global_disable_thinking"] == "1"
        assert len(answer["run"]["retrieved"]) == 24
        assert "reasoning_steps" not in json.dumps(body)
    assert generation.os.environ["CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING"] == "1"


@pytest.mark.parametrize("top_k,success", [(1, True), (40, True), (0, False), (41, False)])
def test_retrieval_has_its_own_bounds_and_reports_misses(suite, tmp_path, top_k, success):
    output = tmp_path / "run"
    result = invoke("answer", suite, output, "--answerer", "stub-key", "--top-k", str(top_k))
    assert result == (0 if success else 2)
    if success:
        answers = read_rows(output / "answers.jsonl")
        assert all(len(answer["run"]["retrieved"]) == top_k for answer in answers)
        card = score(suite, output, answers)
        assert card["top_k"] == top_k
        assert card["questions"][0]["retrieval_miss"] is (top_k == 1)


@pytest.mark.parametrize("filename,phase", [("answers.jsonl", "answer"), ("scorecard.json", "score"), ("scorecard.md", "score")])
def test_outputs_are_not_overwritten_without_force(suite, tmp_path, filename, phase):
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    score(suite, output, answers)
    target = output / filename
    # fixture-write: Mark a synthetic output to detect unintended replacement.
    target.write_text("synthetic preserved output\n", encoding="utf-8")
    assert invoke(phase, suite, output) == 2
    assert target.read_text(encoding="utf-8") == "synthetic preserved output\n"
    if phase == "score":
        write_rows(output / "answers.jsonl", answers)
    assert invoke(phase, suite, output, "--force") == 0
    assert target.read_text(encoding="utf-8") != "synthetic preserved output\n"


def test_suite_fingerprint_tracks_passage_bytes(suite, tmp_path):
    output = tmp_path / "run"
    answers = key_answers(suite, output)
    first = score(suite, output, answers)["suite_fingerprint"]
    pages = read_rows(suite / "passages.jsonl")
    pages[-1]["text"] += " Additional synthetic inventory."
    write_rows(suite / "passages.jsonl", pages)
    assert invoke("answer", suite, output, "--force") == 0
    second = score(suite, output, read_rows(output / "answers.jsonl"))["suite_fingerprint"]
    assert first != second


def test_new_files_pass_private_terms_check():
    pieces = [
        ("fede", "ral"), ("defen", "der"), (r"\bf", r"pd\b"), ("fd", r"\.org"),
        (r"\bc", r"ja\b"), ("ker", "beros"), ("sc", "sa"), (r"\bn", "eil"),
        ("oh", "io"), ("cleve", "land"), ("l4", "0s"), ("gb", "10"),
        ("skiz", "bit"), ("graf", "ton"), ("qw", "en"), ("deep", "seek"),
    ]
    pattern = re.compile("|".join("".join(parts) for parts in pieces), re.IGNORECASE)
    for name in ("src/case_intelligence/synthesis_eval.py", "tests/test_synthesis_eval.py"):
        assert pattern.search((ROOT / name).read_text(encoding="utf-8")) is None, name
