"""Synthetic contracts for exact calculations and generation verification."""

from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from case_intelligence.exact_arithmetic import (
    BoundInput, CalculationError, Calculator, MAX_DEPTH, MAX_LENGTH,
    bind_input, verify_calculations,
)
from case_intelligence.generation import (
    ANSWER_SCHEMA, EvidenceItem, GenerationGroundingRejected,
    GroundedGenerationService, _verify_text,
)


@pytest.mark.parametrize("expression,exact,display", [
    ("0.1 + 0.2", Decimal("0.3"), "0.3"),
    ("(2 + 3) * 4 - 6 / 2", Decimal(17), "17"),
    ("1 / 3", Fraction(1, 3), "1/3"),
    ("1 / 3 * 3", Decimal(1), "1"),
    ("1 / 8", Decimal("0.125"), "0.125"),
    ("sum(1, 2, 3.25)", Decimal("6.25"), "6.25"),
    ("min(3, -2, 1/3) + max(1, 4)", Decimal(2), "2"),
    ("round(2.345, 2)", Decimal("2.34"), "2.34"),
    ("round(2.355, 2)", Decimal("2.36"), "2.36"),
    ("round(-2.355, 2)", Decimal("-2.36"), "-2.36"),
    ("round(125, -1)", Decimal(120), "120"),
    ("round(1 / 3, 4)", Decimal("0.3333"), "0.3333"),
    ("percent(1, 8)", Decimal("12.5"), "12.5"),
    ("percent(1, 3)", Fraction(100, 3), "100/3"),
    ("-0.000 + +2.000", Decimal(2), "2"),
    ("123456789012345678901234567890 + 0.1",
     Decimal("123456789012345678901234567890.1"),
     "123456789012345678901234567890.1"),
])
def test_exact_grammar_and_display(expression, exact, display):
    with localcontext() as context:
        context.prec = 3
        record = Calculator().calculate(expression)
    assert type(record.exact_value) is type(exact)
    assert record.exact_value == exact
    assert record.display_value == display
    assert record.calculation_id == "C1"
    assert Calculator().calculate(record.expression).exact_value == exact


@pytest.mark.parametrize("expression", [
    "", "unknown + 1", "__import__('os')", "x.real", "[1]", "1 ** 2",
    "1 // 2", "1 % 2", "True", "float(1)", "sum()", "min()", "max()",
    "round(1)", "round(1, 0.5)", "round(1, 10000)", "percent(1)",
    "percent(1, 2, 3)", "1 / 0", "percent(1, 0)", "1 2", "sum(1,)",
    "(1", "1)", "1 # ignored", "1;2", "NaN", "Infinity", "1e999999999",
])
def test_unsupported_or_undefined_expressions_are_refused(expression):
    with pytest.raises(CalculationError):
        Calculator().calculate(expression)


def test_caps_and_ids():
    calculator = Calculator()
    for expression in ("1" * (MAX_LENGTH + 1), "(" * (MAX_DEPTH + 1) + "1"
                       + ")" * (MAX_DEPTH + 1), "-" * (MAX_DEPTH + 1) + "1"):
        with pytest.raises(CalculationError):
            calculator.calculate(expression)
    assert calculator.calculate("1").calculation_id == "C1"
    assert calculator.calculate("2").calculation_id == "C2"


TEXTS = {"E1": "🙂 e\u0301 12.50 shares", "E2": "🙂 e\u0301 99.00 shares"}


def source(evidence_id):
    return TEXTS[evidence_id]


def bound():
    return bind_input("shares", Decimal("12.5"), "E1", (5, 10), "12.50",
                      stored_text=source, opened_ids={"E1"})


def test_bound_unicode_codepoints_and_recomputation():
    item = bound()
    assert isinstance(item, BoundInput)
    assert item.span == (5, 10)
    calculator = Calculator(stored_text=source, opened_ids={"E1"})
    record = calculator.calculate(" percent(shares, 100) ", (item,))
    assert record.exact_value == Decimal("12.5")
    assert record.inputs == (item,)
    assert verify_calculations((record,), stored_text=source, opened_ids={"E1"})
    assert not verify_calculations((record,), stored_text=source, opened_ids=set())
    assert not verify_calculations((record,), stored_text={"E1": TEXTS["E2"]}.__getitem__,
                                   opened_ids={"E1"})


@pytest.mark.parametrize("changes", [
    {"evidence_id": "E2"}, {"evidence_id": "missing"}, {"opened_ids": set()},
    {"span": (-1, 10)}, {"span": (5, 100)}, {"span": (10, 5)},
    {"span": (5, 5)}, {"span": (True, 10)}, {"span": (5.0, 10)},
    {"span": (8, 13)}, {"text": "99.00"}, {"value": Decimal(99)},
    {"value": 12.5}, {"value": Decimal("NaN")}, {"name": "sum"},
    {"name": "x.y"}, {"stored_text": None},
])
def test_binder_refusals(changes):
    arguments = dict(name="shares", value=Decimal("12.5"), evidence_id="E1",
                     span=(5, 10), text="12.50", stored_text=source,
                     opened_ids={"E1", "E2"})
    arguments.update(changes)
    with pytest.raises(CalculationError):
        bind_input(**arguments)


@pytest.mark.parametrize("changes", [
    {"expression": ""}, {"expression": "shares + 1"},
    {"exact_value": Decimal(99)}, {"display_value": "99"},
    {"display_value": "12.50"}, {"exact_value": 12.5},
    {"calculation_id": "forged"},
])
def test_forged_records_refused(changes):
    record = Calculator(stored_text=source, opened_ids={"E1"}).calculate("shares", (bound(),))
    assert not verify_calculations((replace(record, **changes),),
                                   stored_text=source, opened_ids={"E1"})


@pytest.mark.parametrize("changes", [
    {"span": (0, 5)}, {"value": Decimal(99)}, {"text": "99.00"},
    {"evidence_id": "E2"}, {"name": "other"},
])
def test_forged_inputs_refused(changes):
    calculator = Calculator(stored_text=source, opened_ids={"E1", "E2"})
    record = calculator.calculate("shares", (bound(),))
    forged = replace(record, inputs=(replace(bound(), **changes),))
    assert not verify_calculations((forged,), stored_text=source, opened_ids={"E1", "E2"})


def test_required_context_and_unique_inputs_and_records():
    record = Calculator().calculate("1")
    assert verify_calculations(())
    assert not verify_calculations((record,))
    assert not verify_calculations((record,), stored_text=source)
    assert not verify_calculations((record,), opened_ids=set())
    assert not verify_calculations(({},), stored_text=source, opened_ids=set())
    assert not verify_calculations((record, record), stored_text=source, opened_ids=set())
    with pytest.raises(CalculationError):
        Calculator().calculate("shares", (bound(),))
    with pytest.raises(CalculationError):
        Calculator(stored_text=source, opened_ids={"E1"}).calculate("shares", (bound(), bound()))


def test_generated_small_decimal_remains_verifiable():
    record = Calculator().calculate("0." + "0" * 1023 + "1 * 0.1")
    assert record.exact_value == Decimal("1e-1025")
    assert verify_calculations((record,), stored_text=source, opened_ids=set())


def test_exact_fraction_input_and_large_denominator_display():
    item = replace(bound(), value=Fraction(25, 2))
    record = Calculator(stored_text=source, opened_ids={"E1"}).calculate("shares/3", (item,))
    assert record.exact_value == Fraction(25, 6)
    assert verify_calculations((record,), stored_text=source, opened_ids={"E1"})
    expression = "*".join(["(1/1024)"] * 450)
    with pytest.raises(CalculationError):
        Calculator().calculate(expression)


def test_canonical_expression_stays_inside_length_cap():
    record = Calculator().calculate("+".join(["1"] * (MAX_LENGTH // 2)))
    assert record.exact_value == Decimal(MAX_LENGTH // 2)
    assert verify_calculations((record,), stored_text=source, opened_ids=set())


@pytest.fixture
def calculation_case():
    texts = {"S1": "Combined holdings were 13 shares.",
             "S2": "Combined holdings were 125 shares."}
    inputs = tuple(bind_input(name, Decimal(number), identifier,
                              (text.index(number), text.index(number) + len(number)), number,
                              stored_text=texts.__getitem__, opened_ids=set(texts))
                   for name, number, identifier in (("held", "13", "S1"), ("total", "125", "S2"))
                   for text in (texts[identifier],))
    record = Calculator(stored_text=texts.__getitem__, opened_ids=set(texts)).calculate(
        "percent(held, total)", inputs)
    evidence = {key: EvidenceItem(key, "Synthetic filing", "page 1", text)
                for key, text in texts.items()}
    return evidence, dict(calculations=(record,), stored_text=texts.__getitem__,
                         opened_ids=set(texts))


def calculation_claim(text="Combined holdings were 10.4%.", ids=None, **extra):
    return dict(text=text, evidence_ids=["S1", "S2"] if ids is None else ids, **extra)


def verify_answer(claims, evidence, **context):
    return GroundedGenerationService._verify(
        dict(answerable=True, claims=claims, limitation=None, missing_information=""),
        tuple(evidence.values()), 0, **context)


def test_generation_calculation_number_delegation(calculation_case):
    evidence, context = calculation_case
    claim = calculation_claim(calculation_ids=["C1"])
    assert _verify_text(**claim, evidence=evidence, **context) is not None
    assert verify_answer([claim], evidence, **context).claims[0].text == claim["text"]


@pytest.mark.parametrize("extra", [{}, {"calculation_ids": []}])
def test_generation_defaults_preserve_existing_claims(calculation_case, extra):
    evidence, _ = calculation_case
    claim = calculation_claim("Combined holdings were 13 shares.", ["S1"], **extra)
    assert _verify_text(**claim, evidence=evidence) is not None
    assert verify_answer([claim], evidence).omitted_claims == 0
    unsupported = calculation_claim(**extra)
    assert _verify_text(**unsupported, evidence=evidence) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([unsupported], evidence)


@pytest.mark.parametrize("ids", [[], ["C2"], ["invalid"], ["C0"], [1], "C1", None,
                                      ["C1", "C1"], [["C1"]]])
def test_generation_requires_valid_resolved_calculation_ids(calculation_case, ids):
    evidence, context = calculation_case
    claim = calculation_claim(calculation_ids=ids)
    assert _verify_text(**claim, evidence=evidence, **context) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([claim], evidence, **context)


def test_generation_run_level_result_without_claim_link_is_refused(calculation_case):
    evidence, context = calculation_case
    assert _verify_text(**calculation_claim(), evidence=evidence, **context) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([calculation_claim()], evidence, **context)


@pytest.mark.parametrize("earlier", [True, False])
def test_generation_only_earlier_verified_claims_supply_input_citations(calculation_case, earlier):
    evidence, context = calculation_case
    support = calculation_claim("Combined holdings were 125 shares.", ["S2"])
    result = calculation_claim(ids=["S1"], calculation_ids=["C1"])
    answer = verify_answer([support, result] if earlier else [result, support], evidence, **context)
    assert [claim.text for claim in answer.claims] == (
        [support["text"], result["text"]] if earlier else [support["text"]])
    assert answer.omitted_claims == (0 if earlier else 1)


def test_generation_rejected_claim_cannot_supply_input_citations(calculation_case):
    evidence, context = calculation_case
    rejected = calculation_claim("Unrelated fabricated statement 999.", ["S2"])
    result = calculation_claim(ids=["S1"], calculation_ids=["C1"])
    assert _verify_text(**result, evidence=evidence, **context) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([rejected, result], evidence, **context)


@pytest.mark.parametrize("missing", ["stored_text", "opened_ids", "both"])
def test_generation_records_require_source_context(calculation_case, missing):
    evidence, context = calculation_case
    for key in ("stored_text", "opened_ids") if missing == "both" else (missing,):
        context.pop(key)
    claim = calculation_claim(calculation_ids=["C1"])
    assert _verify_text(**claim, evidence=evidence, **context) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([claim], evidence, **context)


@pytest.mark.parametrize("mutation", ["span", "value", "exact", "display", "unopened", "raw"])
def test_generation_rebinds_and_recomputes_records(calculation_case, mutation):
    evidence, context = calculation_case
    record = context["calculations"][0]
    if mutation in {"span", "value"}:
        changes = {"span": (0, 2)} if mutation == "span" else {"value": Decimal(14)}
        record = replace(record, inputs=(replace(record.inputs[0], **changes), record.inputs[1]))
    elif mutation == "exact":
        record = replace(record, exact_value=Decimal("10.4") + 1)
    elif mutation == "display":
        record = replace(record, display_value="10.40")
    elif mutation == "unopened":
        context["opened_ids"] = {"S1"}
    else:
        context["stored_text"] = {"S1": "changed", "S2": evidence["S2"].excerpt}.__getitem__
    context["calculations"] = (record,)
    claim = calculation_claim(calculation_ids=["C1"])
    assert _verify_text(**claim, evidence=evidence, **context) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([claim], evidence, **context)


@pytest.mark.parametrize("expression,number,accepted", [
    ("10.4", "10.400", True), ("1/3", "1/3", True), ("1/3", "0.3333", False),
    ("10.4", "10.5", False), ("10.4", "-10.4", False), ("-10.4", "-10.4", True),
])
def test_generation_exact_or_display_without_silent_rounding(calculation_case, expression, number, accepted):
    evidence, context = calculation_case
    context["calculations"] = (Calculator().calculate(expression),)
    claim = calculation_claim(f"Combined holdings were {number}%.", calculation_ids=["C1"])
    assert (_verify_text(**claim, evidence=evidence, **context) is not None) is accepted
    if accepted:
        assert verify_answer([claim], evidence, **context).omitted_claims == 0
    else:
        with pytest.raises(GenerationGroundingRejected):
            verify_answer([claim], evidence, **context)


@pytest.mark.parametrize("text", [
    "Combined holdings were not 10.4%.", "Unrelated invented assets reached 10.4%.",
    'Combined holdings were "10.4% holdings".',
])
def test_generation_calculations_preserve_other_text_checks(calculation_case, text):
    evidence, context = calculation_case
    claim = calculation_claim(text, calculation_ids=["C1"])
    assert _verify_text(**claim, evidence=evidence, **context) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([claim], evidence, **context)


def test_generation_calculations_preserve_citation_and_structure_checks(calculation_case):
    evidence, context = calculation_case
    claim = calculation_claim(calculation_ids=["C1"])
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([claim], evidence, strict_originals=True, **context)
    for ids in ([], ["S1", "S1"], ["S1", "S2", "S3", "S4", "S5"]):
        assert _verify_text(**dict(claim, evidence_ids=ids), evidence=evidence, **context) is None
    mixed = dict(evidence, S2=replace(evidence["S2"], evidence_kind="transcript"))
    assert _verify_text(**claim, evidence=mixed, **context) is None
    with pytest.raises(GenerationGroundingRejected):
        verify_answer([dict(claim, unexpected=True)], evidence, **context)


def test_generation_schema_calculation_ids_are_optional():
    claim_schema = ANSWER_SCHEMA["properties"]["claims"]["items"]
    assert claim_schema["required"] == ["text", "evidence_ids"]
    assert claim_schema["additionalProperties"] is False
    ids = claim_schema["properties"]["calculation_ids"]
    assert ids["type"] == "array"
    assert ids["items"]["type"] == "string"


@pytest.mark.parametrize("expressions,number,accepted", [
    (("10", "400"), "10,400", False), (("10400",), "10,400", True),
    (("0", "3"), "0,3", False), (("10400.5",), "10,400.5", True),
])
def test_generation_compares_whole_grouped_numbers(calculation_case, expressions, number, accepted):
    evidence, context = calculation_case
    calculator = Calculator()
    context["calculations"] = tuple(calculator.calculate(value) for value in expressions)
    claim = calculation_claim(f"Combined holdings were {number} shares.",
                              calculation_ids=[record.calculation_id for record in context["calculations"]])
    assert (_verify_text(**claim, evidence=evidence, **context) is not None) is accepted
    if accepted:
        assert verify_answer([claim], evidence, **context).omitted_claims == 0
    else:
        with pytest.raises(GenerationGroundingRejected):
            verify_answer([claim], evidence, **context)


@pytest.mark.parametrize("answer_entry", [False, True])
def test_generation_consumable_inputs_cannot_hide_uncited_sources(calculation_case, answer_entry):
    evidence, context = calculation_case
    record = context["calculations"][0]
    context["calculations"] = (replace(record, inputs=iter(record.inputs)),)
    claim = calculation_claim(ids=["S1"], calculation_ids=["C1"])
    if answer_entry:
        with pytest.raises(GenerationGroundingRejected):
            verify_answer([claim], evidence, **context)
    else:
        assert _verify_text(**claim, evidence=evidence, **context) is None
