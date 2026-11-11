"""Synthetic contracts for exact calculations and generation verification."""

from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from case_intelligence.exact_arithmetic import (
    BoundInput, CalculationError, Calculator, MAX_DEPTH, MAX_LENGTH,
    bind_input, verify_calculations, _evaluate,
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
        canonical, value, rendered = _evaluate(expression, {})
    assert type(value) is type(exact)
    assert value == exact
    assert rendered == display
    assert _evaluate(canonical, {})[1] == exact


@pytest.mark.parametrize("expression", [
    "", "unknown + 1", "__import__('os')", "x.real", "[1]", "1 ** 2",
    "1 // 2", "1 % 2", "True", "float(1)", "sum()", "min()", "max()",
    "round(1)", "round(1, 0.5)", "round(1, 10000)", "percent(1)",
    "percent(1, 2, 3)", "1 / 0", "percent(1, 0)", "1 2", "sum(1,)",
    "(1", "1)", "1 # ignored", "1;2", "NaN", "Infinity", "1e999999999",
])
def test_unsupported_or_undefined_expressions_are_refused(expression):
    with pytest.raises(CalculationError):
        _evaluate(expression, {})


def test_caps_and_ids():
    calculator = Calculator(stored_text=source, opened_ids={'E1'})
    for expression in ("1" * (MAX_LENGTH + 1), "(" * (MAX_DEPTH + 1) + "1"
                       + ")" * (MAX_DEPTH + 1), "-" * (MAX_DEPTH + 1) + "1"):
        with pytest.raises(CalculationError):
            _evaluate(expression, {})
    assert calculator.calculate("shares", (bound(),)).calculation_id == "C1"
    assert calculator.calculate("shares * 2", (bound(),)).calculation_id == "C2"


TEXTS = {"E1": "e\u0301 Z 12.50 shares", "E2": "e\u0301 Z 99.00 shares"}


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
    record = Calculator(stored_text=source, opened_ids={"E1"}).calculate("shares", (bound(),))
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
    record = Calculator(stored_text=source, opened_ids={"E1"}).calculate(
        "shares / 12.5 * 0." + "0" * 1023 + "1 * 0.1", (bound(),))
    assert record.exact_value == Decimal("1e-1025")
    assert verify_calculations((record,), stored_text=source, opened_ids={"E1"})


def test_exact_fraction_input_and_large_denominator_display():
    item = replace(bound(), value=Fraction(25, 2))
    record = Calculator(stored_text=source, opened_ids={"E1"}).calculate("shares/3", (item,))
    assert record.exact_value == Fraction(25, 6)
    assert verify_calculations((record,), stored_text=source, opened_ids={"E1"})
    expression = "*".join(["(1/1024)"] * 450)
    with pytest.raises(CalculationError):
        Calculator().calculate(expression)


def test_canonical_expression_stays_inside_length_cap():
    canonical, value, display = _evaluate("+".join(["1"] * (MAX_LENGTH // 2)), {})
    assert value == Decimal(MAX_LENGTH // 2)
    assert len(canonical) <= MAX_LENGTH
    assert display == str(MAX_LENGTH // 2)


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
    assert _verify_text(**claim, evidence=evidence, **context).text == claim["text"]
    assert verify_answer([claim], evidence, **context).claims[0].text == claim["text"]


@pytest.mark.parametrize("extra", [{}, {"calculation_ids": []}])
def test_generation_defaults_preserve_existing_claims(calculation_case, extra):
    evidence, _ = calculation_case
    claim = calculation_claim("Combined holdings were 13 shares.", ["S1"], **extra)
    assert _verify_text(**claim, evidence=evidence).text == claim["text"]
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
    context = grounded_calculations(evidence, context, (expression,))
    claim = calculation_claim(f"Combined holdings were {number} shares.", calculation_ids=["C1"])
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
    context = grounded_calculations(evidence, context, expressions)
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


@pytest.mark.parametrize('raw,selected', [
    ('125 shares', '12'), ('-13 shares', '13'), ('1,250.50 shares', '250.50'),
    ('1.5e3 shares', '1.5'), ('5 million shares', '5'),
    ('Smith owed 5 thousandths of a share.', '5'),
    ('The growth factor was 1.05 to the pow-\ner of 10.', '1.05'),
    ('5 x 1', '5'), ('0 x 1000', '0'), ('5 (qzrv)', '5'),
])
def test_reviewed_partial_and_context_refusals(raw, selected):
    start = raw.index(selected)
    with pytest.raises(CalculationError):
        bind_input('x', Fraction(selected.replace(',', '')), 'S1',
                   (start, start + len(selected)), selected,
                   stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})


@pytest.mark.parametrize('expression', [
    '999', 'shares + 1000000', 'shares + shares / shares * 1000000',
    'shares + shares * (1000000 / shares)', 'shares * 0',
    'sum(shares, shares / shares * 7)',
    'shares + round(shares / 10000000000000000000000000000000000000000 + 7, 0)',
])
def test_reviewed_semantic_grounding_refusals(expression):
    item = bind_input('shares', Decimal(5), 'S1', (0, 1), '5',
                      stored_text={'S1': '5 shares'}.__getitem__, opened_ids={'S1'})
    calculator = Calculator(stored_text={'S1': '5 shares'}.__getitem__, opened_ids={'S1'})
    with pytest.raises(CalculationError):
        calculator.calculate(expression, (item,))


# Frozen synthetic meanings: set, id, text, category, selected label records.
FROZEN_NUMERIC_SETS = {
    'main': [
        ('M01', 'Smith bought 5,000 shares on 3/4/2020.', 'dates', [(13, 18, '5000', '5000', True, False, None, 'share count', []), (29, 30, '3', '3', False, False, None, 'month in compound date', []), (31, 32, '4', '4', False, False, None, 'day in compound date', []), (33, 37, '2020', '2020', False, False, None, 'year in compound date', [])], []),
        ('M02', 'Smith sold 5,000 shares at $3.20 per share on March 3, 2020.', 'dates', [(11, 16, '5000', '5000', True, False, None, 'share count', []), (27, 32, '3.20', '3.20', True, False, None, 'price per share', []), (52, 53, '3', '3', True, False, None, 'separate day', []), (55, 59, '2020', '2020', True, False, None, 'separate year', [])], []),
        ('M03', 'The witness contributed $5,000 to the 2019 plan.', 'dates', [(24, 30, '5000', '5000', True, False, None, 'cash contribution', []), (38, 42, '2019', '2019', True, False, None, 'plan year', [])], []),
        ('M04', 'The transfer of 750 shares closed on 2020-03-04.', 'dates', [(16, 19, '750', '750', True, False, None, 'share count', []), (37, 41, '2020', '2020', False, False, None, 'year in ISO date', []), (42, 44, '3', '3', False, False, None, 'month in ISO date', []), (45, 47, '4', '4', False, False, None, 'day in ISO date', [])], []),
        ('M05', 'The March 8 schedule records 2 x 10^3 shares transferred in 2021.', 'dates', [(10, 11, '8', '8', True, False, None, 'calendar day', []), (29, 30, '2', '2000', True, True, None, 'coefficient multiplied by following power', []), (33, 35, '10', '10', False, False, None, 'base in power notation', []), (36, 37, '3', '3', False, False, None, 'exponent in power notation', []), (60, 64, '2021', '2021', True, False, None, 'calendar year', [])], []),
        ('M06', 'The witness entered 3 times between 10:00 and 11:00.', 'times', [(20, 21, '3', '3', True, False, None, 'number of entries', []), (36, 38, '10', '10', False, False, None, 'hour in compound time', []), (39, 41, '0', '0', False, False, None, 'minute in compound time', []), (46, 48, '11', '11', False, False, None, 'hour in compound time', []), (49, 51, '0', '0', False, False, None, 'minute in compound time', [])], []),
        ('M07', 'The clerk sealed 12 packets at 12:30 p.m.', 'times', [(17, 19, '12', '12', True, False, None, 'packet count', []), (31, 33, '12', '12', False, False, None, 'hour in compound time', []), (34, 36, '30', '30', False, False, None, 'minute in compound time', [])], []),
        ('M08', 'The guard called 14 times on March 3.', 'times', [(17, 19, '14', '14', True, False, None, 'call count', []), (35, 36, '3', '3', True, False, None, 'calendar day', [])], []),
        ('M09', 'The hearing lasted 90 minutes; the recess lasted 15 minutes.', 'times', [(19, 21, '90', '90', True, False, None, 'duration in minutes', []), (49, 51, '15', '15', True, False, None, 'separate duration', [])], []),
        ('M10', 'At noon the counter logged 4 x 10^3 access events.', 'times', [(27, 28, '4', '4000', True, True, None, 'coefficient multiplied by power', []), (31, 33, '10', '10', False, False, None, 'power base', []), (34, 35, '3', '3', False, False, None, 'power exponent', [])], []),
        ('M11', 'Counsel identified 8 documents on pages 10-12.', 'ranges', [(19, 20, '8', '8', True, False, None, 'document count', []), (40, 42, '10', '10', False, False, None, 'range endpoint in compound', []), (43, 45, '12', '12', False, False, None, 'range endpoint in compound', [])], []),
        ('M12', 'The ledger covers pages 10 to 12.', 'ranges', [(24, 26, '10', '10', True, False, None, 'separate page endpoint', []), (30, 32, '12', '12', True, False, None, 'separate page endpoint', [])], []),
        ('M13', 'The balance was between $5 million and $6 million.', 'ranges', [(24, 34, '5000000', '5000000', True, False, None, 'explicit scale on first endpoint', []), (39, 49, '6000000', '6000000', True, False, None, 'explicit scale on second endpoint', [])], []),
        ('M14', 'The disputed reserve was between 5 and 6 million dollars.', 'ranges', [(33, 34, '5', '5000000', True, True, None, 'million applies to both endpoints', []), (39, 48, '6000000', '6000000', True, False, None, 'explicit endpoint scale', [])], []),
        ('M15', 'The estimate ran from 2 through 4 million dollars.', 'ranges', [(22, 23, '2', '2000000', True, True, None, 'shared million endpoint', []), (32, 41, '4000000', '4000000', True, False, None, 'explicit endpoint scale', [])], []),
        ('M16', 'The affidavit cites Exhibit 10 to the 2019 10-K.', 'section_numbers', [(28, 30, '10', '10', True, False, None, 'exhibit number', []), (38, 42, '2019', '2019', True, False, None, 'filing year', []), (43, 45, '10', '10', False, False, None, 'number attached to filing identifier', [])], []),
        ('M17', 'The agreement protects 8 accounts under section 1.2.3.', 'section_numbers', [(23, 24, '8', '8', True, False, None, 'account count', []), (48, 49, '1', '1', False, False, None, 'compound section component', []), (50, 51, '2', '2', False, False, None, 'compound section component', []), (52, 53, '3', '3', False, False, None, 'compound section component', [])], []),
        ('M18', 'Paragraph 12 lists 4 attachments.', 'section_numbers', [(10, 12, '12', '12', True, False, None, 'paragraph number', []), (19, 20, '4', '4', True, False, None, 'attachment count', [])], []),
        ('M19', 'The 2nd Amended Agreement lists 6 signatories.', 'section_numbers', [(4, 7, '2', '2', False, False, None, 'glued ordinal outside grammar', []), (32, 33, '6', '6', True, False, None, 'signatory count', [])], []),
        ('M20', 'Section 7 records alternatives of 2 or 3 million units.', 'section_numbers', [(8, 9, '7', '7', True, False, None, 'section number', []), (34, 35, '2', '2000000', True, True, None, 'shared scale in alternatives', []), (39, 48, '3000000', '3000000', True, False, None, 'explicit scale', [])], []),
        ('M21', 'The account held 5,000 shares ($2 million).', 'currency', [(17, 22, '5000', '5000', True, False, None, 'share count independent of valuation', []), (30, 42, '-2000000', '2000000', True, False, None, 'positive parenthetical valuation conflicts with contract negative convention', [])], []),
        ('M22', 'The trust paid $1,250.50 for 25 units.', 'currency', [(15, 24, '1250.50', '1250.50', True, False, None, 'dollar payment', []), (29, 31, '25', '25', True, False, None, 'unit count', [])], []),
        ('M23', 'The invoice states US$3.20 per unit and 10 units.', 'currency', [(19, 26, '3.20', '3.20', False, False, None, 'currency code outside grammar', []), (40, 42, '10', '10', True, False, None, 'unit count', [])], []),
        ('M24', 'The schedule lists ($300) as the adjustment and $900 as the credit.', 'currency', [(19, 25, '-300', '-300', True, False, None, 'accounting negative', []), (48, 52, '900', '900', True, False, None, 'remaining credit', [])], []),
        ('M25', 'The alternative demands were $5 and $6 million.', 'currency', [(29, 31, '5', '5000000', True, True, None, 'shared million across currency amounts', []), (36, 46, '6000000', '6000000', True, False, None, 'explicit dollar scale', [])], []),
        ('M26', 'The analyst estimated 5, 6 or 7 x 10^6 shares.', 'enumerations', [(22, 23, '5', '5000000', True, True, None, 'shared power for first alternative', []), (25, 26, '6', '6000000', True, True, None, 'shared power for middle alternative', []), (30, 31, '7', '7000000', True, True, None, 'direct power multiplier', []), (34, 36, '10', '10', False, False, None, 'power base', []), (37, 38, '6', '6', False, False, None, 'power exponent', [])], []),
        ('M27', 'The draft demands 2, 3 and 4 million dollars.', 'enumerations', [(18, 19, '2', '2000000', True, True, None, 'shared million in list', []), (21, 22, '3', '3000000', True, True, None, 'shared million in list', []), (27, 36, '4000000', '4000000', True, False, None, 'explicit list scale', [])], []),
        ('M28', 'Counsel requested 5, 6 or 7 copies.', 'enumerations', [(18, 19, '5', '5', True, False, None, 'plain list count', []), (21, 22, '6', '6', True, False, None, 'plain list count', []), (26, 27, '7', '7', True, False, None, 'plain list count', [])], []),
        ('M29', 'The reserve was 5 or maybe 6 million dollars.', 'enumerations', [(16, 17, '5', '5000000', True, True, None, 'shared million with uncertainty modifier', []), (27, 36, '6000000', '6000000', True, False, None, 'explicit scale', [])], []),
        ('M30', 'The witness recalled 2 or 3 x 10^3 messages.', 'enumerations', [(21, 22, '2', '2000', True, True, None, 'shared power in alternatives', []), (26, 27, '3', '3000', True, True, None, 'direct power multiplier', []), (30, 32, '10', '10', False, False, None, 'power base', []), (33, 34, '3', '3', False, False, None, 'power exponent', [])], []),
        ('M31', 'The fund owns $1.2 million and $3.4 million of notes.', 'scaled_amounts', [(14, 26, '1200000', '1200000', True, False, None, 'first independently scaled amount', []), (31, 43, '3400000', '3400000', True, False, None, 'second independently scaled amount', [])], []),
        ('M32', 'The exhibit reports 5\nmillion outstanding shares.', 'scaled_amounts', [(20, 29, '5000000', '5000000', True, False, None, 'newline is a plain scale separator', [])], []),
        ('M33', 'The fund reported 5 bln dollars of assets.', 'scaled_amounts', [(18, 19, '5', '5000000000', True, False, None, 'D4 unknown word bln means billion', [])], []),
        ('M34', 'The custodian held 2 thousand certificates and 3 million shares.', 'scaled_amounts', [(19, 29, '2000', '2000', True, False, None, 'explicit thousand scale', []), (47, 56, '3000000', '3000000', True, False, None, 'independent million scale', [])], []),
        ('M35', 'The reconciliation records 10.4 million x 10^3 units.', 'scaled_amounts', [(27, 39, '10400000', '10400000000', True, True, None, 'explicit scale followed by another multiplier', []), (42, 44, '10', '10', False, False, None, 'power base', []), (45, 46, '3', '3', False, False, None, 'power exponent', [])], []),
        ('M36', 'The damages schedule reports 10.4 x 10^3 units.', 'exponents', [(29, 33, '10.4', '10400', True, True, None, 'direct power multiplier', []), (36, 38, '10', '10', False, False, None, 'power base', []), (39, 40, '3', '3', False, False, None, 'power exponent', [])], []),
        ('M37', 'The attached calculation records [10.4] x 10^3 shares.', 'exponents', [(34, 38, '10.4', '10400', True, True, None, 'square bracket does not terminate multiplier', []), (42, 44, '10', '10', False, False, None, 'power base', []), (45, 46, '3', '3', False, False, None, 'power exponent', [])], []),
        ('M38', 'The scanned schedule prints 10.4 . 10^3 units.', 'exponents', [(28, 32, '10.4', '10400', True, True, None, 'spaced dot is multiplication notation', []), (35, 37, '10', '10', False, False, None, 'power base', []), (38, 39, '3', '3', False, False, None, 'power exponent', [])], []),
        ('M39', 'The electronic ledger reports 1.5e3 shares.', 'exponents', [(30, 35, '1500', '1500', False, False, None, 'scientific exponent outside contract', [])], []),
        ('M40', 'The expert calculated 10.4 times 10 to the 3rd units.', 'exponents', [(22, 26, '10.4', '10400', True, True, None, 'verbal power multiplier', []), (33, 35, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (43, 46, '3', '3', False, False, None, 'ordinal exponent outside grammar', [])], []),
        ('M41', 'The purchase schedule contains this row:\n| 5,000 | $ 1,234 | $ 2,000 |', 'tables', [(43, 48, '5000', '5000', True, False, None, 'share count in its own cell', []), (51, 58, '1234', '1234', False, False, None, 'spaced currency prefix outside contract', []), (61, 68, '2000', '2000', False, False, None, 'spaced currency prefix outside contract', [])], []),
        ('M42', 'The inventory schedule reads (in thousands):\n| Shares | 5 | 8 | 12 |', 'tables', [(56, 57, '5', '5000', True, False, None, 'D4 header scales the cell', []), (60, 61, '8', '8000', True, False, None, 'D4 header scales the cell', []), (64, 66, '12', '12000', True, False, None, 'D4 header scales the cell', [])], []),
        ('M43', 'The distribution row is:\n| Shares | 700 | Value | $2 million |', 'tables', [(36, 39, '700', '700', True, False, None, 'share count', []), (50, 60, '2000000', '2000000', True, False, None, 'independent cell valuation', [])], []),
        ('M44', 'The reconciliation row is:\n| Credit | $1.2 million | Debit | $3.4 million |', 'tables', [(38, 50, '1200000', '1200000', True, False, None, 'independent scaled cell', []), (61, 73, '3400000', '3400000', True, False, None, 'independent scaled cell', [])], []),
        ('M45', 'The exhibit lists unit counts in this row:\n| 2 x 10^3 | 4 |', 'tables', [(45, 46, '2', '2000', True, True, None, 'coefficient with power inside cell', []), (49, 51, '10', '10', False, False, None, 'power base', []), (52, 53, '3', '3', False, False, None, 'power exponent', []), (56, 57, '4', '4', True, False, None, 'separate unscaled cell', [])], []),
        ('M46', 'The broker recorded 10 shares @ 5.', 'plain_counts', [(20, 22, '10', '10', True, False, None, 'share count followed by unit price', []), (32, 33, '5', '5', True, False, None, 'quoted unit price', [])], []),
        ('M47', 'The register assigns 5,000 shares #2.', 'plain_counts', [(21, 26, '5000', '5000', True, False, None, 'share count followed by footnote', []), (35, 36, '2', '2', True, False, None, 'footnote number', [])], []),
        ('M48', 'The clerk counted 24 files.', 'plain_counts', [(18, 20, '24', '24', True, False, None, 'ordinary file count', [])], []),
        ('M49', 'The witness identified 12 witnesses and 8 folders.', 'plain_counts', [(23, 25, '12', '12', True, False, None, 'ordinary witness count', []), (40, 41, '8', '8', True, False, None, 'ordinary folder count', [])], []),
        ('M50', 'The audit found 3 or 4 thousand duplicate records.', 'plain_counts', [(16, 17, '3', '3000', True, True, None, 'shared thousand scale across alternative counts', []), (21, 31, '4000', '4000', True, False, None, 'explicit thousand scale', [])], []),
    ],
    'stress': [
        ('S01', 'The schedule writes 1.04 x 10^4 shares.', 'exponents', [(20, 24, '1.04', '10400', True, True, None, 'coefficient with changed exponent', []), (27, 29, '10', '10', False, False, None, 'power base', []), (30, 31, '4', '4', False, False, None, 'power exponent', [])], []),
        ('S02', 'The ledger records [10.4])} x 10^3 units.', 'exponents', [(20, 24, '10.4', '10400', True, True, None, 'stacked closing punctuation preserves multiplier', []), (30, 32, '10', '10', False, False, None, 'power base', []), (33, 34, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S03', 'The exhibit gives {10.4} x 10^{3} units.', 'exponents', [(19, 23, '10.4', '10400', True, True, None, 'braced coefficient', []), (27, 29, '10', '10', False, False, None, 'power base', []), (31, 32, '3', '3', False, False, None, 'braced exponent', [])], []),
        ('S04', 'The balance is 10.4 : 10^3 units.', 'exponents', [(15, 19, '10.4', '0.0104', True, True, None, 'colon means division in this arithmetic entry', []), (22, 24, '10', '10', False, False, None, 'power base', []), (25, 26, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S05', 'The printed balance is 10.4. 10^3 appears in the next calculation.', 'exponents', [(23, 27, '10.4', '10.4', True, False, None, 'sentence ends before independent power', []), (29, 31, '10', '10', False, False, None, 'power base in next sentence', []), (32, 33, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S06', 'The copied formula reads 10.4 ; x 10^3 units.', 'exponents', [(25, 29, '10.4', '10400', True, True, None, 'spaced semicolon within copied multiplier notation', []), (34, 36, '10', '10', False, False, None, 'power base', []), (37, 38, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S07', 'The witness counted 10; 3 were sealed.', 'plain_counts', [(20, 22, '10', '10', True, False, None, 'count ends at attached semicolon', []), (24, 25, '3', '3', True, False, None, 'independent count after boundary', [])], []),
        ('S08', 'The expert writes 10.4 x ( 10 ^ 3 ) units.', 'exponents', [(18, 22, '10.4', '10400', True, True, None, 'spaces and brackets inside power notation', []), (27, 29, '10', '10', False, False, None, 'bracketed power base', []), (32, 33, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S09', 'The exhibit states 10.4 times 10 to the 3rd units.', 'exponents', [(19, 23, '10.4', '10400', True, True, None, 'verbal exponent', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (40, 43, '3', '3', False, False, None, 'ordinal exponent', [])], []),
        ('S10', 'The estimates are 5, 6, 7 and 8 x 10^6 shares.', 'enumerations', [(18, 19, '5', '5000000', True, True, None, 'shared power in comma chain', []), (21, 22, '6', '6000000', True, True, None, 'shared power in comma chain', []), (24, 25, '7', '7000000', True, True, None, 'shared power in comma chain', []), (30, 31, '8', '8000000', True, True, None, 'direct power multiplier', []), (34, 36, '10', '10', False, False, None, 'power base', []), (37, 38, '6', '6', False, False, None, 'power exponent', [])], []),
        ('S11', 'The witness estimated 5 or maybe 6 million shares.', 'enumerations', [(22, 23, '5', '5000000', True, True, None, 'uncertain coordinator inherits scale', []), (33, 42, '6000000', '6000000', True, False, None, 'explicit scale', [])], []),
        ('S12', 'The demand runs from 5 through 6 million dollars.', 'ranges', [(21, 22, '5', '5000000', True, True, None, 'range inherits scale', []), (31, 40, '6000000', '6000000', True, False, None, 'explicit endpoint scale', [])], []),
        ('S13', 'The competing demands are $5 and $6 million.', 'currency', [(26, 28, '5', '5000000', True, True, None, 'currency list inherits scale', []), (33, 43, '6000000', '6000000', True, False, None, 'explicit scale', [])], []),
        ('S14', 'Counsel identified 5 witnesses who described the records as legible and preserved in ordinary folders bearing separate labels for each hearing exhibit.', 'plain_counts', [(19, 20, '5', '5', True, False, None, 'long ordinary prose tail', [])], []),
        ('S15', 'The clerk counted 10 **sealed packets**.', 'plain_counts', [(18, 20, '10', '10', True, False, None, 'markdown applies to noun', [])], []),
        ('S16', 'The registry lists 5\nmillion shares.', 'scaled_amounts', [(19, 28, '5000000', '5000000', True, False, None, 'newline belongs to contract scale token', [])], []),
        ('S17', 'The guard entered 3 times between 10:00 and 11:00.', 'times', [(18, 19, '3', '3', True, False, None, 'entry count', []), (34, 36, '10', '10', False, False, None, 'hour in compound time', []), (37, 39, '0', '0', False, False, None, 'minute in compound time', []), (44, 46, '11', '11', False, False, None, 'hour in compound time', []), (47, 49, '0', '0', False, False, None, 'minute in compound time', [])], []),
        ('S18', 'The statement reports 5,000 shares ($2 million).', 'currency', [(22, 27, '5000', '5000', True, False, None, 'share count independent of valuation', []), (35, 47, '-2000000', '2000000', True, False, None, 'positive parenthetical valuation', [])], []),
        ('S19', 'The invoice row is:\n| 8,000 | $ 3,210 | $ 4,000 |', 'tables', [(22, 27, '8000', '8000', True, False, None, 'count in separate cell', []), (30, 37, '3210', '3210', False, False, None, 'spaced currency', []), (40, 47, '4000', '4000', False, False, None, 'spaced currency', [])], []),
        ('S20', 'The fund owns $2 thousand and $4 million of notes.', 'scaled_amounts', [(14, 25, '2000', '2000', True, False, None, 'independent scale', []), (30, 40, '4000000', '4000000', True, False, None, 'independent scale', [])], []),
        ('S21', 'The expert records 0 x 10^3 units.', 'exponents', [(19, 20, '0', '0', True, True, None, 'zero coefficient has unchanged value despite multiplier', []), (23, 25, '10', '10', False, False, None, 'power base', []), (26, 27, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S22', 'The ledger writes 5 x 10^0 units.', 'exponents', [(18, 19, '5', '5', True, True, None, 'unit multiplier leaves value unchanged', []), (22, 24, '10', '10', False, False, None, 'power base', []), (25, 26, '0', '0', False, False, None, 'zero power', [])], []),
        ('S23', 'The witness estimates 10.4 x one thousand units.', 'exponents', [(22, 26, '10.4', '10400', True, True, None, 'spelled multiplier outside numeric vocabulary', [])], []),
        ('S24', 'The expert reports 10.4 multiplied by a factor of 1,000 units.', 'exponents', [(19, 23, '10.4', '10400', True, True, None, 'filler interrupts adjacent numeric multiplier', []), (50, 55, '1000', '1000', True, False, None, 'multiplier operand', [])], []),
        ('S25', 'The schedule prints 10.4 times roughly 1,000 units.', 'exponents', [(20, 24, '10.4', '10400', True, True, None, 'approximate multiplier', []), (39, 44, '1000', '1000', True, False, None, 'multiplier operand', [])], []),
        ('S26', 'The exhibit writes 10.4 x 10<sup>3</sup> units.', 'exponents', [(19, 23, '10.4', '10400', True, True, None, 'HTML superscript power', []), (26, 28, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (33, 34, '3', '3', False, False, None, 'markup closing angle is a forbidden prefix mark', [])], []),
        ('S27', 'The schedule gives 10.4 * 10 ** 3 units.', 'exponents', [(19, 23, '10.4', '10400', True, True, None, 'star multiplication and double-star power', []), (26, 28, '10', '10', False, False, None, 'power base', []), (32, 33, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S28', 'The estimate is 5 or [6 million] units.', 'enumerations', [(16, 17, '5', '5000000', True, True, None, 'shared scale with bracketed later member', []), (22, 31, '6000000', '6000000', True, False, None, 'explicit scale in square brackets', [])], []),
        ('S29', 'The custodian holds $5 million and $6 in petty cash.', 'currency', [(20, 30, '5000000', '5000000', True, False, None, 'independent scaled amount', []), (35, 37, '6', '6', True, False, None, 'literal small independent cash adjustment', [])], []),
        ('S30', 'The log lists 5 notices and $6 million in claims.', 'plain_counts', [(14, 15, '5', '5', True, False, None, 'distinct count before another amount', []), (28, 38, '6000000', '6000000', True, False, None, 'separate valuation', [])], []),
        ('S31', 'The estimate is 5 bln dollars.', 'scaled_amounts', [(16, 17, '5', '5000000000', True, False, None, 'D4 unknown scale', [])], []),
        ('S32', 'The table is labeled (in thousands):\n| 7 | 9 |', 'tables', [(39, 40, '7', '7000', True, False, None, 'D4 header magnitude', []), (43, 44, '9', '9000', True, False, None, 'D4 header magnitude', [])], []),
        ('S33', 'The ledger states 10.4 . [10^3] units.', 'exponents', [(18, 22, '10.4', '10400', True, True, None, 'spaced dot before bracketed power', []), (26, 28, '10', '10', False, False, None, 'power base', []), (29, 30, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S34', 'The calculation reads 10.4" times 10^3 units.', 'exponents', [(22, 26, '10.4', '10400', True, True, None, 'closing quote preserves multiplier', []), (34, 36, '10', '10', False, False, None, 'power base', []), (37, 38, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S35', 'The clerk filed 5,000 records #2.', 'plain_counts', [(16, 21, '5000', '5000', True, False, None, 'count before footnote', []), (31, 32, '2', '2', True, False, None, 'footnote identifier', [])], []),
        ('S36', 'The formula lists 10.4 10^3 units.', 'exponents', [(18, 22, '10.4', '10400', True, True, None, 'no explicit multiplication symbol before power', []), (23, 25, '10', '10', False, False, None, 'power base', []), (26, 27, '3', '3', False, False, None, 'power exponent', [])], []),
        ('S37', 'The export contains 1.04e4 units.', 'exponents', [(20, 26, '10400', '10400', False, False, None, 'glued exponent is outside contract', [])], []),
        ('S38', 'Counsel obtained 5 files: 6 remain sealed.', 'plain_counts', [(17, 18, '5', '5', True, False, None, 'ordinary prose has no scale marker', []), (26, 27, '6', '6', True, False, None, 'independent clause count', [])], []),
        ('S39', 'The total is 5 **million** shares.', 'scaled_amounts', [(13, 14, '5', '5000000', True, True, None, 'emphasis wraps magnitude word outside token', [])], []),
        ('S40', 'The demand is 5 or perhaps 6 million dollars.', 'enumerations', [(14, 15, '5', '5000000', True, True, None, 'scale follows a two-word uncertainty coordinator', []), (27, 36, '6000000', '6000000', True, False, None, 'explicit scale', [])], []),
    ],
    'probe': [
        ('P001', 'The reserve was $5-$6 million.', 'review_ranges', [(16, 18, '5', '5000000', True, True, None, 'shared range million', []), (19, 29, '6000000', '6000000', False, False, None, 'glued preceding dash is a sign outside grammar', [])], []),
        ('P002', 'The reserve was 5 \u2013 6 million dollars.', 'review_ranges', [(16, 17, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (20, 29, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P003', 'The reserve was 5 - 6 million dollars.', 'review_ranges', [(16, 17, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (20, 29, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P004', 'The reserve was $5 \u2013 $6 million.', 'review_ranges', [(16, 18, '5', '5000000', True, True, None, 'literal independent quantity', []), (21, 31, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P005', 'The reserve was 5 and/or 6 million dollars.', 'review_ranges', [(16, 17, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (25, 34, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P006', 'The reserve was 5 / 6 million dollars.', 'review_ranges', [(16, 17, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (20, 29, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P007', 'The schedule prints 10.4\u200b\xd7\u200b10^3 units.', 'review_format', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P008', 'The schedule prints 10.4 \xadx 10^3 units.', 'review_format', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P009', 'The schedule prints 10.4\u206210^3 units.', 'review_format', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (25, 27, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (28, 29, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P010', 'The schedule prints 10.4 `x 10^3` units.', 'review_format', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P011', '| Amount | 5 | million |', 'review_tables', [(11, 12, '5', '5000000', True, True, None, 'right shared million scales first endpoint', [])], []),
        ('P012', '| Amount | 10.4 | x 10^3 |', 'review_tables', [(11, 15, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (20, 22, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (23, 24, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P013', 'Smith sued under Section 16(b) of the Act.', 'review_legal', [(25, 27, '16', '16', True, False, None, 'literal independent quantity', [])], []),
        ('P014', 'The motion under Rule 12(b)(6) was denied.', 'review_legal', [(22, 24, '12', '12', True, False, None, 'literal independent quantity', []), (28, 29, '6', '6', False, False, None, 'omitted paired parentheses', [])], []),
        ('P015', 'Smith held 12.5%, or 1.25 million shares, of the stock.', 'review_percent', [(11, 16, '12.5', '12.5', True, False, None, 'literal independent quantity', []), (21, 33, '1250000', '1250000', True, False, None, 'literal independent quantity', [])], []),
        ('P016', 'Smith owned 5% and 2 million shares.', 'review_percent', [(12, 14, '5', '5', True, False, None, 'literal independent quantity', []), (19, 28, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P017', 'In 2019, 5 million shares were transferred.', 'review_dates', [(3, 7, '2019', '2019', True, False, None, 'literal independent quantity', []), (9, 18, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P018', 'On March 3, 2020, 2 million shares were sold.', 'review_dates', [(9, 10, '3', '3', True, False, None, 'literal independent quantity', []), (12, 16, '2020', '2020', True, False, None, 'literal independent quantity', []), (18, 27, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P019', 'The sheet was 8.5 x 11 inches.', 'review_dimensions', [(14, 17, '8.5', '8.5', True, False, None, 'literal independent quantity', []), (20, 22, '11', '11', True, False, None, 'literal independent quantity', [])], []),
        ('P020', 'a 5 x 7 photograph', 'review_dimensions', [(2, 3, '5', '5', True, False, None, 'literal independent quantity', []), (6, 7, '7', '7', True, False, None, 'literal independent quantity', [])], []),
        ('P021', 'See Exhibit 5 B for the ledger.', 'review_g0', [(12, 13, '5', '5', True, False, None, 'literal independent quantity', [])], []),
        ('P022', 'The officer stood 5 m from the door.', 'review_g0', [(18, 19, '5', '5', True, False, None, 'literal independent quantity', [])], []),
        ('P023', 'The schedule prints 10.4 (thousands).', 'review_plural', [(20, 24, '10.4', '10400', True, False, None, 'D4 plural scale vocabulary', [])], []),
        ('P024', 'The schedule prints 10.4 (in thousands).', 'review_plural', [(20, 24, '10.4', '10400', True, False, None, 'D4 content word before plural scale', [])], []),
        ('P025', 'The schedule prints 10.4 X 10^3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P026', 'The schedule prints 10.4 x10^3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (26, 28, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P027', 'The schedule prints 10.4 (x 10^3) units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P028', 'The schedule prints 10.4 (x10^3) units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P029', 'The schedule prints 10.4\n\xd7 10^3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P030', 'The schedule prints 10.4, x 10^3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P031', 'The schedule prints 10.4 E+03 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '3', '3', False, False, None, 'signed exponent component', [])], []),
        ('P032', 'The schedule prints 10.4 times 10 to the power of 3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (31, 33, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (50, 51, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P033', 'The schedule prints 10.4 \xd7 10\xb3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '3', '3', False, False, None, 'superscript exponent component', [])], []),
        ('P034', 'The schedule prints 10.4 x 10<sup>3</sup> units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (34, 35, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P035', 'The schedule prints 10.4 &times; 10^3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (33, 35, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (36, 37, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P036', 'The estimate was 5, 6, or 7 million.', 'review_ranges', [(17, 18, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (20, 21, '6', '6000000', True, True, None, 'literal independent quantity', []), (26, 35, '7000000', '7000000', True, False, None, 'literal independent quantity', [])], []),
        ('P037', 'The estimate was 5 or 6 MM.', 'review_ranges', [(17, 18, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (22, 26, '6000000', '6000000', False, False, None, 'abbreviation outside binder grammar', [])], []),
        ('P038', 'The estimate was $5 or $6 million.', 'review_ranges', [(17, 19, '5', '5000000', True, True, None, 'literal independent quantity', []), (23, 33, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P039', 'between 5 and 6 (million)', 'review_ranges', [(8, 9, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (14, 15, '6', '6000000', True, True, None, 'parenthesized million', [])], []),
        ('P040', 'The schedule prints 10.4 \\times 10^{3} units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (32, 34, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (36, 37, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P041', 'The schedule prints 10.4&#215;10^3 units.', 'review_markup', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (26, 29, '215', '215', False, False, None, 'HTML entity code is not a document quantity', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (33, 34, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P042', 'from 5 up to 6 million', 'review_residual', [(5, 6, '5', '5000000', True, False, None, 'D4 content words up to end reachability', []), (13, 22, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P043', '5 (five) million', 'review_residual', [(0, 1, '5', '5000000', True, False, None, 'D4 spelled content intervenes', [])], []),
        ('P044', 'The formula is 10.4 x 1 x 1000.', 'review_completion', [(15, 19, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (22, 23, '1', '1', True, False, None, 'unit operand before further multiplication', []), (26, 30, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('P045', 'At 9:30 a.m. on June 1, 2021, Smith sold 2,500 shares at $14.75 per share.', 'review_normal', [(3, 4, '9', '9', False, False, None, 'time component', []), (5, 7, '30', '30', False, False, None, 'time component', []), (21, 22, '1', '1', True, False, None, 'literal independent quantity', []), (24, 28, '2021', '2021', True, False, None, 'literal independent quantity', []), (41, 46, '2500', '2500', True, False, None, 'literal independent quantity', []), (57, 63, '14.75', '14.75', True, False, None, 'literal independent quantity', [])], []),
        ('P046', '1,250,000 shares, or 12.5%', 'review_normal', [(0, 9, '1250000', '1250000', True, False, None, 'literal independent quantity', []), (21, 26, '12.5', '12.5', True, False, None, 'literal independent quantity', [])], []),
        ('P047', 'from 10 to 11 a.m.', 'review_normal', [(5, 7, '10', '10', True, False, None, 'literal independent quantity', []), (11, 13, '11', '11', True, False, None, 'literal independent quantity', [])], []),
        ('P048', '100 shares to 3 buyers', 'review_normal', [(0, 3, '100', '100', True, False, None, 'literal independent quantity', []), (14, 15, '3', '3', True, False, None, 'literal independent quantity', [])], []),
        ('P049', '18 U.S.C. \xa7 1001', 'review_normal', [(0, 2, '18', '18', True, False, None, 'literal independent quantity', []), (12, 16, '1001', '1001', True, False, None, 'literal independent quantity', [])], []),
        ('P050', '15 percent in 2020', 'review_normal', [(0, 2, '15', '15', True, False, None, 'literal independent quantity', []), (14, 18, '2020', '2020', True, False, None, 'literal independent quantity', [])], []),
        ('P051', '2 for 1 split', 'review_normal', [(0, 1, '2', '2', True, False, None, 'literal independent quantity', []), (6, 7, '1', '1', True, False, None, 'literal independent quantity', [])], []),
        ('P052', 'Counts 1, 2 and 3', 'review_normal', [(7, 8, '1', '1', True, False, None, 'literal independent quantity', []), (10, 11, '2', '2', True, False, None, 'literal independent quantity', []), (16, 17, '3', '3', True, False, None, 'literal independent quantity', [])], []),
        ('P053', '$5 million, or $2.50 per share', 'review_normal', [(0, 10, '5000000', '5000000', True, False, None, 'literal independent quantity', []), (15, 20, '2.50', '2.50', True, False, None, 'literal independent quantity', [])], []),
        ('P054', '$2.50 per share, or $5 million', 'review_normal', [(0, 5, '2.50', '2.50', True, False, None, 'literal independent quantity', []), (20, 30, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P055', '5,000 shares, $2 million in cash', 'review_normal', [(0, 5, '5000', '5000', True, False, None, 'literal independent quantity', []), (14, 24, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P056', '10 percent, or 1 million shares', 'review_normal', [(0, 2, '10', '10', True, False, None, 'literal independent quantity', []), (15, 24, '1000000', '1000000', True, False, None, 'literal independent quantity', [])], []),
        ('P057', '$5, $6 million was paid by Jones', 'review_cost', [(0, 2, '5', '5', True, False, None, 'literal independent quantity', []), (4, 14, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P058', 'The damaged schedule gives 5, million shares.', 'review_g0', [(27, 28, '5', '5000000', True, True, None, 'right shared million scales first endpoint', [])], []),
        ('P059', 'Exhibit 5. Million Corp filed it.', 'review_g0', [(8, 9, '5', '5', True, False, None, 'literal independent quantity', [])], []),
        ('P060', 'The payment was USD 5.', 'review_currency', [(16, 21, '5', '5', False, False, None, 'currency code span outside grammar', [])], []),
        ('P061', 'The payment was 5 CHF.', 'review_currency', [(16, 21, '5', '5', False, False, None, 'currency code span outside grammar', [])], []),
        ('P062', 'The payment was US$3.20.', 'review_currency', [(16, 23, '3.20', '3.20', False, False, None, 'currency prefix outside grammar', [])], []),
        ('P063', 'The ratio is 10.4 : 10^3.', 'review_colon', [(13, 17, '10.4', '10.4', True, False, None, 'ratio literal operand not division quantity', []), (20, 22, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (23, 24, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P064', 'Count 5 x 1 copies.', 'review_completion', [(6, 7, '5', '5', True, True, None, 'neutral multiplier keeps coefficient value', []), (10, 11, '1', '1', True, False, None, 'literal independent quantity', [])], []),
        ('P065', 'Pages 10 - 12 contain 8 documents.', 'review_normal', [(6, 8, '10', '10', True, False, None, 'literal independent quantity', []), (11, 13, '12', '12', True, False, None, 'literal independent quantity', []), (22, 23, '8', '8', True, False, None, 'literal independent quantity', [])], []),
        ('P066', 'The estimate was 5 ~ 6 million.', 'variant_ranges', [(17, 18, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (21, 30, '6000000', '6000000', False, False, None, 'approximation before second endpoint', [])], []),
        ('P067', 'The estimate was 5 / or 6 million.', 'variant_ranges', [(17, 18, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (24, 33, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P068', 'The schedule prints 10.4\u200d*\u206010^3 units.', 'variant_format', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P069', 'The schedule prints 10.4\ufeff \xb7 10^3 units.', 'variant_format', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P070', 'The schedule prints 10.4 `\\times 10^{3}` units.', 'variant_format', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (33, 35, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (37, 38, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P071', '| reserve | 5 | (million) |', 'variant_tables', [(12, 13, '5', '5000000', True, True, None, 'right shared million scales first endpoint', [])], []),
        ('P072', 'The claim arises under 17 U.S.C. 107.', 'variant_legal', [(23, 25, '17', '17', True, False, None, 'literal independent quantity', []), (33, 36, '107', '107', True, False, None, 'literal independent quantity', [])], []),
        ('P073', '2.5% or 3 million shares were reported.', 'variant_percent', [(0, 4, '2.5', '2.5', True, False, None, 'literal independent quantity', []), (8, 17, '3000000', '3000000', True, False, None, 'literal independent quantity', [])], []),
        ('P074', 'In 2018, the fund bought 2 million shares.', 'variant_dates', [(3, 7, '2018', '2018', True, False, None, 'literal independent quantity', []), (25, 34, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P075', 'The trade closed on 2024-05-12 at $6.25 per share.', 'variant_dates', [(20, 24, '2024', '2024', False, False, None, 'literal independent quantity', []), (25, 27, '5', '5', False, False, None, 'literal independent quantity', []), (28, 30, '12', '12', False, False, None, 'literal independent quantity', []), (34, 39, '6.25', '6.25', True, False, None, 'literal independent quantity', [])], []),
        ('P076', 'The clerk copied 45 pages and filed 9 exhibits.', 'variant_normal', [(17, 19, '45', '45', True, False, None, 'literal independent quantity', []), (36, 37, '9', '9', True, False, None, 'literal independent quantity', [])], []),
        ('P077', '15% in 2020 and 20% in 2021.', 'variant_normal', [(0, 3, '15', '15', True, False, None, 'literal independent quantity', []), (7, 11, '2020', '2020', True, False, None, 'literal independent quantity', []), (16, 19, '20', '20', True, False, None, 'literal independent quantity', []), (23, 27, '2021', '2021', True, False, None, 'literal independent quantity', [])], []),
        ('P078', 'The investor held 75,000 shares at $8.50 per share.', 'variant_normal', [(18, 24, '75000', '75000', True, False, None, 'literal independent quantity', []), (35, 40, '8.50', '8.50', True, False, None, 'literal independent quantity', [])], []),
        ('P079', 'Section 16(m) addresses 4 claims.', 'variant_normal', [(8, 10, '16', '16', True, False, None, 'literal independent quantity', []), (24, 25, '4', '4', True, False, None, 'literal independent quantity', [])], []),
        ('P080', 'Count 5 reaches real text end', 'variant_normal', [(6, 7, '5', '5', True, False, None, 'literal independent quantity', [])], []),
        ('P081', 'No output: 0 x 700 units.', 'new_zero', [(11, 12, '0', '0', True, True, None, 'zero times nonunit magnitude remains zero', []), (15, 18, '700', '700', True, False, None, 'literal independent quantity', [])], []),
        ('P082', 'No output: 0 x 1 x 700 units.', 'new_zero', [(11, 12, '0', '0', True, True, None, 'unknown chained continuation is conservatively refused', []), (15, 16, '1', '1', True, False, None, 'literal independent quantity', []), (19, 22, '700', '700', True, False, None, 'literal independent quantity', [])], []),
        ('P083', 'The estimate is 8 x 0.5 units.', 'new_fraction_factor', [(16, 17, '8', '4', True, True, None, 'decimal multiplier halves value', []), (20, 23, '0.5', '0.5', True, False, None, 'literal independent quantity', [])], []),
        ('P084', 'The estimate is 8 x 1.000 units.', 'new_identity', [(16, 17, '8', '8', True, True, None, 'decimal identity factor', []), (20, 25, '1', '1', True, False, None, 'literal independent quantity', [])], []),
        ('P085', 'The estimate is 3 x 9^2 units.', 'new_power_base', [(16, 17, '3', '243', True, True, None, 'power of nondecimal-base factor', []), (20, 21, '9', '9', False, False, None, 'power base is a forbidden component in every notation', []), (22, 23, '2', '2', False, False, None, 'power exponent component', [])], []),
        ('P086', '7%, 8% or 2 million shares.', 'new_unit_owner', [(0, 2, '7', '7', True, False, None, 'literal independent quantity', []), (4, 6, '8', '8', True, False, None, 'literal independent quantity', []), (10, 19, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P087', '$2 thousand, $3 million and $4 in cash.', 'new_unit_owner', [(0, 11, '2000', '2000', True, False, None, 'literal independent quantity', []), (13, 23, '3000000', '3000000', True, False, None, 'literal independent quantity', []), (28, 30, '4', '4', True, False, None, 'literal independent quantity', [])], []),
        ('P088', '4! 5 million was the next estimate.', 'new_clause', [(0, 1, '4', '4', True, False, None, 'literal independent quantity', []), (3, 12, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P089', '4? 5 million was the answer.', 'new_clause', [(0, 1, '4', '4', True, False, None, 'literal independent quantity', []), (3, 12, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P090', '4; 5 million belongs to another account.', 'new_clause', [(0, 1, '4', '4', True, False, None, 'literal independent quantity', []), (3, 12, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P091', '4 x-ray images accompany 5 million records.', 'new_content', [(0, 1, '4', '4', True, False, None, 'literal independent quantity', []), (25, 34, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P092', '4 cafe\u0301 receipts accompany 5 million records.', 'new_content', [(0, 1, '4', '4', True, False, None, 'literal independent quantity', []), (27, 36, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P093', '4 \u2192 5 million units.', 'new_symbols', [(0, 1, '4', '4000000', True, True, None, 'symbol-separated shared scale', []), (4, 13, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P094', '4 \u2022 5 million units.', 'new_symbols', [(0, 1, '4', '4000000', True, True, None, 'symbol-separated shared scale', []), (4, 13, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P095', '4 __ 5 million units.', 'new_symbols', [(0, 1, '4', '4000000', True, True, None, 'underscore-separated shared scale', []), (5, 14, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P096', '4 \u0301 million units.', 'new_symbols', [(0, 1, '4', '4000000', True, True, None, 'combining mark before magnitude', [])], []),
        ('P097', '4 x 2e3 units.', 'new_scientific', [(0, 1, '4', '8000', True, True, None, 'nonunit scientific factor', []), (4, 7, '2000', '2000', False, False, None, 'scientific token outside grammar', [])], []),
        ('P098', 'Clause 9(k) covers 2 transactions.', 'new_legal', [(7, 8, '9', '9', True, False, None, 'literal independent quantity', []), (19, 20, '2', '2', True, False, None, 'literal independent quantity', [])], []),
        ('P099', 'There were 4 buyers; each acquired 25 shares.', 'new_normal', [(11, 12, '4', '4', True, False, None, 'literal independent quantity', []), (35, 37, '25', '25', True, False, None, 'literal independent quantity', [])], []),
        ('P100', 'The annual report lists 2022 results for 6 trusts.', 'new_normal', [(24, 28, '2022', '2022', True, False, None, 'literal independent quantity', []), (41, 42, '6', '6', True, False, None, 'literal independent quantity', [])], []),
        ('P101', 'Interest was 0.75% on $4 million principal.', 'new_normal', [(13, 18, '0.75', '0.75', True, False, None, 'literal independent quantity', []), (22, 32, '4000000', '4000000', True, False, None, 'literal independent quantity', [])], []),
        ('P102', 'Appendix 4(a) has 18 pages.', 'new_normal', [(9, 10, '4', '4', True, False, None, 'literal independent quantity', []), (18, 20, '18', '18', True, False, None, 'literal independent quantity', [])], []),
        ('P103', 'The witness read 3 emails dated April 7, 2024.', 'new_normal', [(17, 18, '3', '3', True, False, None, 'literal independent quantity', []), (38, 39, '7', '7', True, False, None, 'literal independent quantity', []), (41, 45, '2024', '2024', True, False, None, 'literal independent quantity', [])], []),
        ('P104', 'The witness produced 3 records [sealed] and 2 attachments.', 'new_normal', [(21, 22, '3', '3', True, False, None, 'literal independent quantity', []), (44, 45, '2', '2', True, False, None, 'literal independent quantity', [])], []),
        ('P105', 'The schedule prints 10.4 &times; 10<sup>3</sup> units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (33, 35, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (40, 41, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P106', 'The schedule prints 10.4 &times; 10&sup3; units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (33, 35, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (39, 40, '3', '3', False, False, None, 'named entity spelling component', [])], []),
        ('P107', 'The schedule prints 10.4&#215;10<sup>3</sup> units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (26, 29, '215', '215', False, False, None, 'entity code is decoration', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (37, 38, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P108', 'The schedule prints 10.4 &#xD7; 10&#179; units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 30, '215', '215', False, False, None, 'entity code is decoration', []), (32, 34, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (36, 39, '179', '179', False, False, None, 'entity code is decoration', [])], []),
        ('P109', 'The schedule prints 10.4 &middot; 10<sup>3</sup> units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (34, 36, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (41, 42, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P110', 'The schedule prints 10.4 \u2715 10\xb3 units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '3', '3', False, False, None, 'Unicode power component', [])], []),
        ('P111', 'The schedule prints 10.4 \u2a2f 10\xb3 units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '3', '3', False, False, None, 'Unicode power component', [])], []),
        ('P112', 'The schedule prints 10.4 \u2219 10\xb3 units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '3', '3', False, False, None, 'Unicode power component', [])], []),
        ('P113', 'The schedule prints 10.4 \u2022 10<sup>3</sup> units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (34, 35, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P114', 'The schedule prints 10.4 10\xb3 units.', 'r9_p2_1', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (25, 27, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (27, 28, '3', '3', False, False, None, 'Unicode power component', [])], []),
        ('P115', '| Amount | 10.4 | 10<sup>3</sup> |', 'r9_p2_1', [(11, 15, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (18, 20, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (25, 26, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P116', 'The fund holds $5 (mn) in cash.', 'r9_p2_2', [(15, 17, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P117', 'The fund holds $5 (bn) in cash.', 'r9_p2_2', [(15, 17, '5', '5000000000', True, True, None, 'literal independent quantity', [])], []),
        ('P118', 'The fund holds 5 [MM] in notes.', 'r9_p2_2', [(15, 16, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P119', '| Reserve | 5 | mn |', 'r9_p2_2', [(12, 13, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P120', '| Reserve | 5 | bn |', 'r9_p2_2', [(12, 13, '5', '5000000000', True, True, None, 'literal independent quantity', [])], []),
        ('P121', '| 2019 | 5,000 | $2 million |', 'r9_p2_3', [(2, 6, '2019', '2019', True, False, None, 'literal independent quantity', []), (9, 14, '5000', '5000', True, False, None, 'literal independent quantity', []), (17, 27, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P122', '| Date | Shares | Value |\n| 2020 | 1,000 | $1.2 million |', 'r9_p2_3', [(28, 32, '2020', '2020', True, False, None, 'literal independent quantity', []), (35, 40, '1000', '1000', True, False, None, 'literal independent quantity', []), (43, 55, '1200000', '1200000', True, False, None, 'literal independent quantity', [])], []),
        ('P123', '| 3/4/2020 | 1,000 | 12.5% | $1.2 million |', 'r9_p2_3', [(2, 3, '3', '3', False, False, None, 'date component', []), (4, 5, '4', '4', False, False, None, 'date component', []), (6, 10, '2020', '2020', False, False, None, 'date component', []), (13, 18, '1000', '1000', True, False, None, 'literal independent quantity', []), (21, 26, '12.5', '12.5', True, False, None, 'literal independent quantity', []), (29, 41, '1200000', '1200000', True, False, None, 'literal independent quantity', [])], []),
        ('P124', 'Smith bought 500, 1,000 and 2 million shares on three dates.', 'r9_p2_3', [(13, 16, '500', '500', True, False, None, 'literal independent quantity', []), (18, 23, '1000', '1000', True, False, None, 'literal independent quantity', []), (28, 37, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P125', 'Counts 1 through 3 and 5 million dollars of forfeiture.', 'r9_p2_3', [(7, 8, '1', '1', True, False, None, 'literal independent quantity', []), (17, 18, '3', '3', True, False, None, 'literal independent quantity', []), (23, 32, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P126', 'The meeting ran from 9 to 11, and 5 million shares were voted.', 'r9_p2_3', [(21, 22, '9', '9', True, False, None, 'literal independent quantity', []), (26, 28, '11', '11', True, False, None, 'literal independent quantity', []), (34, 43, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P127', 'Smith bought 5,000 shares in 2019 and 2 million shares in 2020.', 'r9_p2_3', [(13, 18, '5000', '5000', True, False, None, 'literal independent quantity', []), (29, 33, '2019', '2019', True, False, None, 'literal independent quantity', []), (38, 47, '2000000', '2000000', True, False, None, 'literal independent quantity', []), (58, 62, '2020', '2020', True, False, None, 'literal independent quantity', [])], []),
        ('P128', 'Page 4 \u2013 $5 million settlement', 'r9_p2_3', [(5, 6, '4', '4', True, False, None, 'literal independent quantity', []), (9, 19, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P129', 'Item 4 - 5 million shares', 'r9_p2_3', [(5, 6, '4', '4', True, False, None, 'literal independent quantity', []), (9, 18, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P130', '5 \u2012 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P131', '5 \u27f6 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P132', '5 \\ 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P133', '5 \uff0f 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P134', '5 \u2215 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P135', '5 | 6 million', 'r9_break_symbol', [(0, 1, '5', '5', True, False, None, 'independent cell before independently scaled amount', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P136', '5 \u22ef 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P137', '5 ~ 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', False, False, None, 'approximation mark before amount', [])], []),
        ('P138', '5 \u223c 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P139', '5 -- 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (5, 14, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P140', '5 <-> 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (6, 15, '6000000', '6000000', False, False, None, 'approximation mark before amount', [])], []),
        ('P141', '5 \u2192 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P142', '5 \u2026 6 million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P143', "5 'or' 6 million", 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (7, 16, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P144', '$5 \u2013 6 mn', 'r9_break_symbol', [(0, 2, '5', '5000000', True, True, None, 'literal independent quantity', []), (5, 9, '6000000', '6000000', False, False, None, 'unsupported abbreviation span', [])], []),
        ('P145', '5 (or 6) million', 'r9_break_symbol', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (6, 7, '6', '6000000', True, True, None, 'shared trailing scale', [])], []),
        ('P146', 'The schedule prints 10.4\u2009\xd7\u200910^3 units.', 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P147', 'The schedule prints 10.4\u202f\xd7\u202f10^3 units.', 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P148', 'The schedule prints 10.4 \xd7\u0301 10^3 units.', 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P149', "The schedule prints 10.4 ''x'' 10^3 units.", 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (31, 33, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (34, 35, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P150', 'The schedule prints 10.4 \u201cx\u201d 10^3 units.', 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (29, 31, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (32, 33, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P151', 'The schedule prints 10.4\xa0x\xa010^3 units.', 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (30, 31, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P152', 'The schedule prints 10.4 ~x 10^3 units.', 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P153', 'The schedule prints 10.4 #x 10^3 units.', 'r9_break_symbol', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P154', '| Amount | 5 | \u2012 | million |', 'r9_break_symbol', [(11, 12, '5', '5000000', True, True, None, 'right shared million scales first endpoint', [])], []),
        ('P155', '| Amount | 5 |\n| million |', 'r9_break_symbol', [(11, 12, '5', '5000000', True, True, None, 'right shared million scales first endpoint', [])], []),
        ('P156', 'Amount\t5\tmillion', 'r9_break_symbol', [(7, 16, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P157', '5 x 1 (x 1000)', 'r9_break_completion', [(0, 1, '5', '5000', True, True, None, 'literal independent quantity', []), (4, 5, '1', '1', True, False, None, 'literal independent quantity', []), (9, 13, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('P158', '5 x 1 - 1000', 'r9_break_completion', [(0, 1, '5', '5000', True, True, None, 'literal independent quantity', []), (4, 5, '1', '1', True, False, None, 'literal independent quantity', []), (8, 12, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('P159', '5 x 10^0 x 10^3', 'r9_break_completion', [(0, 1, '5', '5000', True, True, None, 'literal independent quantity', []), (4, 6, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (7, 8, '0', '0', False, False, None, 'power exponent component', []), (11, 13, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (14, 15, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P160', '5 x 1 times 1000', 'r9_break_completion', [(0, 1, '5', '5000', True, True, None, 'literal independent quantity', []), (4, 5, '1', '1', True, False, None, 'literal independent quantity', []), (12, 16, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('P161', '5% x 1 x 1000', 'r9_break_completion', [(0, 2, '5', '5000', True, True, None, 'literal independent quantity', []), (5, 6, '1', '1', True, False, None, 'literal independent quantity', []), (9, 13, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('P162', '5 x 1, 6 x 1000', 'r9_break_completion', [(0, 1, '5', '5', True, True, None, 'independent completed identity before comma', []), (4, 5, '1', '1', True, False, None, 'literal independent quantity', []), (7, 8, '6', '6000', True, True, None, 'literal independent quantity', []), (11, 15, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('P163', '5 x 1; x 1000', 'r9_break_completion', [(0, 1, '5', '5', True, True, None, 'attached semicolon completes identity', []), (4, 5, '1', '1', True, False, None, 'literal independent quantity', []), (9, 13, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('P164', 'The schedule prints 10.4 x 10\u207b\xb3 units.', 'r9_break_power', [(20, 24, '10.4', '0.0104', True, True, None, 'literal independent quantity', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 31, '-3', '-3', False, False, None, 'Unicode power component', [])], []),
        ('P165', 'The schedule prints 10.4 \xd7 10\xb3 units.', 'r9_break_power', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '3', '3', False, False, None, 'Unicode power component', [])], []),
        ('P166', 'The schedule prints 10.4 X10^3 units.', 'r9_break_power', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (26, 28, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P167', 'The schedule prints 10.4 x 10 to the 3 units.', 'r9_break_power', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (37, 38, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P168', 'The schedule prints 10.4 x 1 000 units.', 'r9_break_power', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (27, 28, '1', '1', False, False, None, 'grouped operand component', []), (29, 32, '0', '0', False, False, None, 'grouped operand component', [])], []),
        ('P169', '$10.4 \\cdot 10^3$', 'r9_break_power', [(0, 5, '10.4', '10400', True, True, None, 'literal independent quantity', []), (12, 14, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (15, 16, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P170', '5, 6, 7 or 8 bn', 'r9_break_list', [(0, 1, '5', '5000000000', True, True, None, 'right shared million scales first endpoint', []), (3, 4, '6', '6000000000', True, True, None, 'literal independent quantity', []), (6, 7, '7', '7000000000', True, True, None, 'literal independent quantity', []), (11, 15, '8000000000', '8000000000', False, False, None, 'unsupported abbreviation', [])], []),
        ('P171', '5 or 6 Mio', 'r9_break_list', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (5, 10, '6000000', '6000000', False, False, None, 'unsupported abbreviation', [])], []),
        ('P172', '5 + 6 million', 'r9_break_list', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P173', '5\n6 million', 'r9_break_list', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (2, 11, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P174', '5 \xb1 6 million', 'r9_break_list', [(0, 1, '5', '5000000', True, True, None, 'right shared million scales first endpoint', []), (4, 13, '6000000', '6000000', False, False, None, 'approximation prefix', [])], []),
        ('P175', '\u20ac5 and \u20ac6 million', 'r9_break_list', [(0, 2, '5', '5000000', False, True, None, 'non-dollar currency outside grammar', []), (7, 17, '6000000', '6000000', False, False, None, 'non-dollar currency outside grammar', [])], []),
        ('P176', 'Smith sold 2,500 shares at $14.75 on June 1, 2021, for $36,875.', 'r9_break_normal', [(11, 16, '2500', '2500', True, False, None, 'literal independent quantity', []), (27, 33, '14.75', '14.75', True, False, None, 'literal independent quantity', []), (42, 43, '1', '1', True, False, None, 'literal independent quantity', []), (45, 49, '2021', '2021', True, False, None, 'literal independent quantity', []), (55, 62, '36875', '36875', True, False, None, 'literal independent quantity', [])], []),
        ('P177', 'Smith held 12.5% of the 10,000,000 outstanding shares.', 'r9_break_normal', [(11, 16, '12.5', '12.5', True, False, None, 'literal independent quantity', []), (24, 34, '10000000', '10000000', True, False, None, 'literal independent quantity', [])], []),
        ('P178', 'Smith held 4.9%, Jones held 5.1% and Doe held 2 million shares.', 'r9_break_normal', [(11, 15, '4.9', '4.9', True, False, None, 'literal independent quantity', []), (28, 32, '5.1', '5.1', True, False, None, 'literal independent quantity', []), (46, 55, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('P179', 'Smith held 9.9% before 2021 and 10.2% after.', 'r9_break_normal', [(11, 15, '9.9', '9.9', True, False, None, 'literal independent quantity', []), (23, 27, '2021', '2021', True, False, None, 'literal independent quantity', []), (32, 37, '10.2', '10.2', True, False, None, 'literal independent quantity', [])], []),
        ('P180', 'Section 13(d) ... within 10 days.', 'r9_break_normal', [(8, 10, '13', '13', True, False, None, 'literal independent quantity', []), (25, 27, '10', '10', True, False, None, 'literal independent quantity', [])], []),
        ('P181', 'Section 16 (b) of the Act applies.', 'r9_break_normal', [(8, 10, '16', '16', True, False, None, 'literal independent quantity', [])], []),
        ('P182', 'Item 5.02(b) of Form 8-K', 'r9_break_normal', [(5, 9, '5.02', '5.02', True, False, None, 'literal independent quantity', []), (21, 22, '8', '8', False, False, None, 'identifier component', [])], []),
        ('P183', 'The filing on Form 4 reported 2,000 shares.', 'r9_break_normal', [(19, 20, '4', '4', True, False, None, 'literal independent quantity', []), (30, 35, '2000', '2000', True, False, None, 'literal independent quantity', [])], []),
        ('P184', 'began at 9 a.m. and 5 million shares', 'r9_break_normal', [(9, 10, '9', '9', True, False, None, 'literal independent quantity', []), (20, 29, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P185', 'Smith held 10 percent, or 1 million shares.', 'r9_break_normal', [(11, 13, '10', '10', True, False, None, 'literal independent quantity', []), (26, 35, '1000000', '1000000', True, False, None, 'literal independent quantity', [])], []),
        ('P186', 'The stock rose 5% to $6 million', 'r9_break_normal', [(15, 17, '5', '5', True, False, None, 'literal independent quantity', []), (21, 31, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P187', 'Smith owned 2 lots, 3 parcels and $4 million', 'r9_break_normal', [(12, 13, '2', '2', True, False, None, 'literal independent quantity', []), (20, 21, '3', '3', True, False, None, 'literal independent quantity', []), (34, 44, '4000000', '4000000', True, False, None, 'literal independent quantity', [])], []),
        ('P188', 'Exhibits 10.1 and 10.2 to the 8-K', 'r9_break_normal', [(9, 13, '10.1', '10.1', True, False, None, 'literal independent quantity', []), (18, 22, '10.2', '10.2', True, False, None, 'literal independent quantity', []), (30, 31, '8', '8', False, False, None, 'identifier component', [])], []),
        ('P189', 'Between 2019 and 2021, Smith bought 3 million shares.', 'r9_break_normal', [(8, 12, '2019', '2019', True, False, None, 'literal independent quantity', []), (17, 21, '2021', '2021', True, False, None, 'literal independent quantity', []), (36, 45, '3000000', '3000000', True, False, None, 'literal independent quantity', [])], []),
        ('P190', 'From 2019 to 2021 Smith held 3 million shares.', 'r9_break_normal', [(5, 9, '2019', '2019', True, False, None, 'literal independent quantity', []), (13, 17, '2021', '2021', True, False, None, 'literal independent quantity', []), (29, 38, '3000000', '3000000', True, False, None, 'literal independent quantity', [])], []),
        ('P191', 'The table shows 2019: 5 million shares.', 'r9_break_normal', [(16, 20, '2019', '2019', True, False, None, 'literal independent quantity', []), (22, 31, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P192', 'Smith bought 5,000 shares at $3.20 per share, or $16,000.', 'r9_break_normal', [(13, 18, '5000', '5000', True, False, None, 'literal independent quantity', []), (29, 34, '3.20', '3.20', True, False, None, 'literal independent quantity', []), (49, 56, '16000', '16000', True, False, None, 'literal independent quantity', [])], []),
        ('P193', 'pages 10 - 12', 'r9_break_normal', [(6, 8, '10', '10', True, False, None, 'literal independent quantity', []), (11, 13, '12', '12', True, False, None, 'literal independent quantity', [])], []),
        ('P194', '$1,250.00', 'r9_break_normal', [(0, 9, '1250', '1250', True, False, None, 'literal independent quantity', [])], []),
        ('P195', '(300)', 'r9_break_normal', [(0, 5, '-300', '-300', True, False, None, 'literal independent quantity', [])], []),
        ('P196', '12.5%', 'r9_break_normal', [(0, 5, '12.5', '12.5', True, False, None, 'literal independent quantity', [])], []),
        ('P197', '5 million', 'r9_break_normal', [(0, 9, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P198', '5\nmillion', 'r9_break_normal', [(0, 9, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P199', 'the 2021 8-K', 'r9_break_cost', [(4, 8, '2021', '2021', True, False, None, 'literal independent quantity', []), (9, 10, '8', '8', False, False, None, 'filing identifier component', [])], []),
        ('P200', 'fiscal 2020 10-K', 'r9_break_cost', [(7, 11, '2020', '2020', True, False, None, 'literal independent quantity', []), (12, 14, '10', '10', False, False, None, 'filing identifier component', [])], []),
        ('P201', '5,000 x $3.20 = $16,000', 'r9_break_cost', [(0, 5, '5000', '5000', True, False, None, 'literal independent quantity', []), (8, 13, '3.20', '3.20', True, False, None, 'literal independent quantity', []), (16, 23, '16000', '16000', True, False, None, 'literal independent quantity', [])], []),
        ('P202', 'Smith bought 100\n5 million shares', 'r9_break_cost', [(13, 16, '100', '100', True, False, None, 'literal independent quantity', []), (17, 26, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('P203', 'The reserve was 5 or about 6 million dollars.', 'r9_p3_hedge', [(16, 17, '5', '5000000', True, False, None, 'D4 intervening content word before later magnitude', []), (27, 36, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P204', 'The reserve was between 5 and approximately 6 million dollars.', 'r9_p3_hedge', [(24, 25, '5', '5000000', True, False, None, 'D4 intervening content word before later magnitude', []), (44, 53, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P205', 'The reserve was 5 or possibly 6 million dollars.', 'r9_p3_hedge', [(16, 17, '5', '5000000', True, False, None, 'D4 intervening content word before later magnitude', []), (30, 39, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P206', 'The reserve was 5 or roughly 6 million dollars.', 'r9_p3_hedge', [(16, 17, '5', '5000000', True, False, None, 'D4 intervening content word before later magnitude', []), (29, 38, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('P207', 'The reserve was 5 and a half million dollars.', 'r9_p3_hedge', [(16, 17, '5', '5500000', True, False, None, 'D4 content-word fraction phrasing', [])], []),
        ('P208', 'The schedule prints 10.4 \\cdot 10^{3} units.', 'r9_p3_tex', [(20, 24, '10.4', '10400', True, True, None, 'right-hand multiplier makes coefficient 10400', []), (31, 33, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (35, 36, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P209', 'The estimate is 8 x 1.000 units.', 'r9_p3_locale', [(16, 17, '8', '8', True, True, None, 'dot decimal identity under contract locale', []), (20, 25, '1', '1', True, False, None, 'literal independent quantity', [])], []),
        ('P210', 'The European schedule prints 10.4 x 1.000 units.', 'r9_p3_locale', [(29, 33, '10.4', '10400', True, False, None, 'D4 dot-grouping locale outside reader', []), (36, 41, '1', '1000', True, False, None, 'D4 dot-grouping locale outside reader', [])], []),
        ('P211', 'The schedule prints 24 &#xd7; 10\xb2 units.', 'r9_own_power', [(20, 22, '24', '2400', True, True, None, 'authored explicit power magnitude', []), (25, 28, '215', '215', False, False, None, 'entity code is decoration', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (32, 33, '2', '2', False, False, None, 'Unicode power component', [])], []),
        ('P212', 'The schedule prints 24 &sdot; 10\xb9 units.', 'r9_own_power', [(20, 22, '24', '240', True, True, None, 'authored explicit power magnitude', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (32, 33, '1', '1', False, False, None, 'Unicode power component', [])], []),
        ('P213', 'The schedule prints 24 &#183; 10\u2074 units.', 'r9_own_power', [(20, 22, '24', '240000', True, True, None, 'authored explicit power magnitude', []), (25, 28, '183', '183', False, False, None, 'entity code is decoration', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (32, 33, '4', '4', False, False, None, 'Unicode power component', [])], []),
        ('P214', 'The schedule prints 24 \\times 10<sup>-2</sup> units.', 'r9_own_power', [(20, 22, '24', '0.24', True, True, None, 'authored explicit power magnitude', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (37, 39, '-2', '-2', False, False, None, 'power exponent component', [])], []),
        ('P215', 'The schedule prints 24 \u2219 10\u207b\xb9 units.', 'r9_own_power', [(20, 22, '24', '2.4', True, True, None, 'authored explicit power magnitude', []), (25, 27, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (27, 29, '-1', '-1', False, False, None, 'Unicode power component', [])], []),
        ('P216', 'The schedule prints 24 \u2a2f 10\u2070 units.', 'r9_own_power', [(20, 22, '24', '24', True, True, None, 'authored explicit power magnitude', []), (25, 27, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (27, 28, '0', '0', False, False, None, 'Unicode power component', [])], []),
        ('P217', 'The schedule prints 7 \u2715 10\u2075 units.', 'r9_own_power', [(20, 21, '7', '700000', True, True, None, 'literal independent quantity', []), (24, 26, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (26, 27, '5', '5', False, False, None, 'Unicode power component', [])], []),
        ('P218', 'The schedule prints 7 \u2715 10\u2076 units.', 'r9_own_power', [(20, 21, '7', '7000000', True, True, None, 'literal independent quantity', []), (24, 26, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (26, 27, '6', '6', False, False, None, 'Unicode power component', [])], []),
        ('P219', 'The schedule prints 7 \u2715 10\u2077 units.', 'r9_own_power', [(20, 21, '7', '70000000', True, True, None, 'literal independent quantity', []), (24, 26, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (26, 27, '7', '7', False, False, None, 'Unicode power component', [])], []),
        ('P220', 'The schedule prints 7 \u2715 10\u2078 units.', 'r9_own_power', [(20, 21, '7', '700000000', True, True, None, 'literal independent quantity', []), (24, 26, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (26, 27, '8', '8', False, False, None, 'Unicode power component', [])], []),
        ('P221', 'The schedule prints 7 \u2715 10\u2079 units.', 'r9_own_power', [(20, 21, '7', '7000000000', True, True, None, 'literal independent quantity', []), (24, 26, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (26, 27, '9', '9', False, False, None, 'Unicode power component', [])], []),
        ('P222', 'Reserve 9 {tn} is disputed.', 'r9_own_scale', [(8, 9, '9', '9000000000000', True, True, None, 'literal independent quantity', [])], []),
        ('P223', 'Reserve 9 {mil} is disputed.', 'r9_own_scale', [(8, 9, '9', '9000000', True, True, None, 'literal independent quantity', [])], []),
        ('P224', 'Reserve 9 {mio} is disputed.', 'r9_own_scale', [(8, 9, '9', '9000000', True, True, None, 'literal independent quantity', [])], []),
        ('P225', 'Reserve 9 {MM} is disputed.', 'r9_own_scale', [(8, 9, '9', '9000000', True, True, None, 'literal independent quantity', [])], []),
        ('P226', 'Reserve $5 (m) was reported.', 'r9_own_single', [(8, 10, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P227', 'Reserve 5 [b] was reported.', 'r9_own_single', [(8, 9, '5', '5000000000', True, True, None, 'literal independent quantity', [])], []),
        ('P228', 'Reserve 7 {k} was reported.', 'r9_own_single', [(8, 9, '7', '7000', True, True, None, 'literal independent quantity', [])], []),
        ('P229', 'Reserve 8 (t) was reported.', 'r9_own_single', [(8, 9, '8', '8000000000000', True, True, None, 'literal independent quantity', [])], []),
        ('P230', 'Reserve 9 [M] was reported.', 'r9_own_single', [(8, 9, '9', '9000000', True, True, None, 'literal independent quantity', [])], []),
        ('P231', 'Reserve 5% (m) was reported.', 'r9_own_single', [(8, 10, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P232', '| Reserve | 5 | m |', 'r9_own_single', [(12, 13, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P233', '| Reserve | 5 | k |', 'r9_own_single', [(12, 13, '5', '5000', True, True, None, 'literal independent quantity', [])], []),
        ('P234', '| Reserve | 5 | B |', 'r9_own_single', [(12, 13, '5', '5000000000', True, True, None, 'literal independent quantity', [])], []),
        ('P235', '| Reserve | 5 | t |', 'r9_own_single', [(12, 13, '5', '5000000000000', True, True, None, 'literal independent quantity', [])], []),
        ('P236', '| 5 | m |', 'r9_own_single', [(2, 3, '5', '5000000', True, True, None, 'right shared million scales first endpoint', [])], []),
        ('P237', 'The reserve was 5 m in cash.', 'r9_own_single', [(16, 17, '5', '5000000', True, True, None, 'right shared million scales first endpoint', [])], []),
        ('P238', 'The reserve was $5m.', 'r9_own_single', [(16, 19, '5000000', '5000000', False, False, None, 'glued abbreviation outside span grammar', [])], []),
        ('P239', 'Section $5 (m) is a monetary entry.', 'r9_own_single', [(8, 10, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P240', 'Rule 5% [m] is a magnitude entry.', 'r9_own_single', [(5, 7, '5', '5000000', True, True, None, 'literal independent quantity', [])], []),
        ('P241', 'Section 27(m) applies.', 'r9_own_citation', [(8, 10, '27', '27', True, False, None, 'literal independent quantity', [])], []),
        ('P242', 'Rule 14(b) applies.', 'r9_own_citation', [(5, 7, '14', '14', True, False, None, 'literal independent quantity', [])], []),
        ('P243', 'Clause 8(k) applies.', 'r9_own_citation', [(7, 8, '8', '8', True, False, None, 'literal independent quantity', [])], []),
        ('P244', 'Article 22(t) applies.', 'r9_own_citation', [(8, 10, '22', '22', True, False, None, 'literal independent quantity', [])], []),
        ('P245', 'Paragraph 31(M) applies.', 'r9_own_citation', [(10, 12, '31', '31', True, False, None, 'literal independent quantity', [])], []),
        ('P246', 'Rule 23(b)(2) governs.', 'r9_own_citation', [(5, 7, '23', '23', True, False, None, 'literal independent quantity', []), (11, 12, '2', '2', False, False, None, 'omitted accounting parentheses', [])], []),
        ('P247', 'Section 28 [k] applies.', 'r9_own_citation', [(8, 10, '28', '28', True, False, None, 'literal independent quantity', [])], []),
        ('P248', '| Units | 750 | $8 million |', 'r9_own_cell', [(10, 13, '750', '750', True, False, None, 'literal independent quantity', []), (16, 26, '8000000', '8000000', True, False, None, 'literal independent quantity', [])], []),
        ('P249', '| 2023 | 750 | 8e6 |', 'r9_own_cell', [(2, 6, '2023', '2023', True, False, None, 'literal independent quantity', []), (9, 12, '750', '750', True, False, None, 'literal independent quantity', []), (15, 18, '8000000', '8000000', False, False, None, 'scientific cell outside span grammar', [])], []),
        ('P250', '| Amount | 7 | 10<sup>3</sup> |', 'r9_own_cell', [(11, 12, '7', '7000', True, True, None, 'literal independent quantity', []), (15, 17, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (22, 23, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('P251', '| Amount | 7 | \\cdot 10\xb3 |', 'r9_own_cell', [(11, 12, '7', '7000', True, True, None, 'literal independent quantity', []), (21, 23, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (23, 24, '3', '3', False, False, None, 'Unicode power component', [])], []),
        ('P252', '| Amount | 7 | (mil) |', 'r9_own_cell', [(11, 12, '7', '7000000', True, True, None, 'literal independent quantity', [])], []),
        ('P253', 'The witness acquired 700, 800 and 9 million shares on separate dates.', 'r9_own_coord', [(21, 24, '700', '700', True, False, None, 'literal independent quantity', []), (26, 29, '800', '800', True, False, None, 'literal independent quantity', []), (34, 43, '9000000', '9000000', True, False, None, 'literal independent quantity', [])], []),
        ('P254', 'The register shows 800 shares in 2022 and 3 million shares in 2023.', 'r9_own_coord', [(19, 22, '800', '800', True, False, None, 'literal independent quantity', []), (33, 37, '2022', '2022', True, False, None, 'literal independent quantity', []), (42, 51, '3000000', '3000000', True, False, None, 'literal independent quantity', []), (62, 66, '2023', '2023', True, False, None, 'literal independent quantity', [])], []),
        ('P255', 'Item 8 - $9 million claim', 'r9_own_coord', [(5, 6, '8', '8', True, False, None, 'literal independent quantity', []), (9, 19, '9000000', '9000000', True, False, None, 'literal independent quantity', [])], []),
        ('P256', 'There are 7 \xbd million units.', 'r9_own_word', [(10, 11, '7', '3500000', True, True, None, 'literal independent quantity', [])], []),
    ],
    'repro': [
        ('R001', 'The schedule prints 10.4\u271510<sup class="e">3</sup> units.', 'p2_1', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (25, 27, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (42, 43, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R002', 'The schedule prints 10.4\u271510<sup >3</sup> units.', 'p2_1', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (25, 27, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (33, 34, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R003', 'The schedule prints 10.4\u271510<small><sup>3</sup></small> units.', 'p2_1', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (25, 27, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (39, 40, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R004', 'The schedule prints 10.4 &bull; 10<sup style="x">3</sup> units.', 'p2_1', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (32, 34, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (49, 50, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R005', '| Amount | 10.4 | 10<sup class="e">3</sup> |', 'p2_1', [(11, 15, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (18, 20, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (35, 36, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R006', 'The schedule prints 10.4\u27151,000 units.', 'p2_1', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (25, 30, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('R007', 'The schedule prints 10.4\u22191,000 units.', 'p2_1', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (25, 30, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('R008', '| Amount | 10.4 |\u27151,000 |', 'p2_1', [(11, 15, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (18, 23, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('R009', 'The fund holds $5 (m.) in cash.', 'p2_2', [(15, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R010', 'The fund holds $5 [M.] in notes.', 'p2_2', [(15, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R011', 'The fund holds 5 ($m) in notes.', 'p2_2', [(15, 16, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R012', 'The fund holds 5 (\u20acm) in notes.', 'p2_2', [(15, 16, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R013', '| Reserve | 5 | $m |', 'p2_2', [(12, 13, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R014', '| Reserve | 5 |\xa3m |', 'p2_2', [(12, 13, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R015', '| Reserve | 5 | m. |', 'p2_2', [(12, 13, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R016', '| Reserve | 5 | (m.) |', 'p2_2', [(12, 13, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R017', '| Reserve | 5 | k USD |', 'p2_2', [(12, 13, '5', '5000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R018', 'The fund holds 5 (\u200bm) in notes.', 'p2_2', [(15, 16, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R019', 'The fund holds 5\uff08m\uff09 in notes.', 'p2_2', [(15, 16, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R020', 'The fund holds 5 <m> in notes.', 'p2_2', [(15, 16, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R021', 'Reserves were 5\u2013(m) in total.', 'p2_2', [(14, 15, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R022', 'Reserve: 5 / m', 'p2_2', [(9, 10, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R023', 'Smith held 5 or 6 ($m) of notes.', 'p2_2', [(11, 12, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (16, 17, '6', '6000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R024', 'Smith bought 500, 1,000 and 2 million shares on three dates.', 'p2_3', [(13, 16, '500', '500', True, False, None, 'literal independent quantity', []), (18, 23, '1000', '1000', True, False, None, 'literal independent quantity', []), (28, 37, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R025', 'The meeting ran from 9 to 11, and 5 million shares were voted.', 'p2_3', [(21, 22, '9', '9', True, False, None, 'literal independent quantity', []), (26, 28, '11', '11', True, False, None, 'literal independent quantity', []), (34, 43, '5000000', '5000000', True, False, None, 'literal independent quantity', [])], []),
        ('R026', 'The witness acquired 700, 800 and 9 million shares on separate dates.', 'p2_3', [(21, 24, '700', '700', True, False, None, 'literal independent quantity', []), (26, 29, '800', '800', True, False, None, 'literal independent quantity', []), (34, 43, '9000000', '9000000', True, False, None, 'literal independent quantity', [])], []),
        ('R027', 'Liability under\xa7 16(b) of the Act was alleged.', 'p2_4', [(17, 19, '16', '16', True, False, None, 'literal independent quantity', [])], []),
        ('R028', 'Defendants filed a 12(b)(6) motion.', 'p2_4', [(19, 21, '12', '12', True, False, None, 'literal independent quantity', []), (25, 26, '6', '6', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R029', 'Defendants moved under Fed. R. Civ. P. 12(b)(6).', 'p2_4', [(39, 41, '12', '12', True, False, None, 'literal independent quantity', []), (45, 46, '6', '6', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R030', 'The claims arise under Sections 10(b) and 20(a).', 'p2_4', [(32, 34, '10', '10', True, False, None, 'literal independent quantity', []), (42, 44, '20', '20', True, False, None, 'literal independent quantity', [])], []),
        ('R031', 'The claims arise under Sec. 10(b) of the Exchange Act.', 'p2_4', [(28, 30, '10', '10', True, False, None, 'literal independent quantity', [])], []),
        ('R032', 'The claim under 10(b) fails.', 'p2_4', [(16, 18, '10', '10', True, False, None, 'literal independent quantity', [])], []),
        ('R033', 'The plan is under 26 U.S.C. 401(k).', 'p2_4', [(18, 20, '26', '26', True, False, None, 'literal independent quantity', []), (28, 31, '401', '401', True, False, None, 'literal independent quantity', [])], []),
        ('R034', 'Smith held a 401(k) account.', 'p2_4', [(13, 16, '401', '401', True, False, None, 'literal independent quantity', [])], []),
        ('R035', 'Smith contributed to the 401(k) plan.', 'p2_4', [(25, 28, '401', '401', True, False, None, 'literal independent quantity', [])], []),
        ('R036', 'See Part 2(b) of the form.', 'p2_4', [(9, 10, '2', '2', True, False, None, 'literal independent quantity', [])], []),
        ('R037', 'See Title 7(b) of the code.', 'p2_4', [(10, 11, '7', '7', True, False, None, 'literal independent quantity', [])], []),
        ('R038', 'On March 5 500 shares were sold.', 'p2_4', [(9, 10, '5', '5', True, False, None, 'literal independent quantity', []), (11, 14, '500', '500', True, False, None, 'literal independent quantity', [])], []),
        ('R039', 'In Q3 250 shares were sold.', 'p2_4', [(4, 5, '3', '3', False, False, None, 'unsupported component outside whole-token grammar', []), (6, 9, '250', '250', True, False, None, 'literal independent quantity', [])], []),
        ('R040', 'Item 7 100 shares were pledged.', 'p2_4', [(5, 6, '7', '7', True, False, None, 'literal independent quantity', []), (7, 10, '100', '100', True, False, None, 'literal independent quantity', [])], []),
        ('R041', 'Smith sold 12 100-share lots.', 'p2_4', [(11, 13, '12', '12', True, False, None, 'literal independent quantity', []), (14, 17, '100', '100', False, False, None, 'attached hyphen unit outside grammar', [])], []),
        ('R042', 'Smith bought 1,000 shares in fiscal 2019 and 2 million in 2020.', 'p2_4', [(13, 18, '1000', '1000', True, False, None, 'literal independent quantity', []), (36, 40, '2019', '2019', True, False, None, 'literal independent quantity', []), (45, 54, '2000000', '2000000', True, False, None, 'literal independent quantity', []), (58, 62, '2020', '2020', True, False, None, 'literal independent quantity', [])], []),
        ('R043', 'The reserve was 5 / 6 / 7 million.', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (20, 21, '6', '6000000', True, True, None, 'right shared scale modifies amount', []), (24, 33, '7000000', '7000000', True, False, None, 'literal independent quantity', [])], []),
        ('R044', 'The reserve was 5 and/or 6 million.', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (25, 34, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('R045', 'The reserve was 5\u2014 or 6\u2014 million.', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (22, 23, '6', '6000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R046', 'The reserve was 5 (or 6) mn.', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (22, 23, '6', '6000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R047', 'The reserve was 5\n6 million', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (18, 27, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('R048', 'Values of 5 and 6 [bn] were shown.', 'break_chain', [(10, 11, '5', '5000000000', True, True, None, 'right shared scale modifies amount', []), (16, 17, '6', '6000000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R049', 'Amounts: $5, $6, and $7 bn.', 'break_chain', [(9, 11, '5', '5000000000', True, True, None, 'right shared scale modifies amount', []), (13, 15, '6', '6000000000', True, True, None, 'right shared scale modifies amount', []), (21, 26, '7000000000', '7000000000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R050', 'The reserve was 5 to 6 MM.', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (21, 25, '6000000', '6000000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R051', 'The reserve was 5, 6, or 7 (m).', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (19, 20, '6', '6000000', True, True, None, 'right shared scale modifies amount', []), (25, 26, '7', '7000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R052', 'The reserve was 5 or 6 (m).', 'break_chain', [(16, 17, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (21, 22, '6', '6000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R053', 'The reserve was between $5 and $6 m.', 'break_chain', [(24, 26, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (31, 35, '6000000', '6000000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R054', 'Smith held 5 or 6 (k) shares.', 'break_chain', [(11, 12, '5', '5000', True, True, None, 'right shared scale modifies amount', []), (16, 17, '6', '6000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R055', 'Smith held $5 or $6 m. of notes.', 'break_chain', [(11, 13, '5', '5000000', True, True, None, 'right shared scale modifies amount', []), (17, 21, '6000000', '6000000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R056', 'The schedule prints 10.4 \xd7 10\u2074 units.', 'break_power_fragment', [(20, 24, '10.4', '104000', True, True, None, 'external magnitude changes selected coefficient', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (29, 30, '4', '4', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R057', 'The schedule prints 10.4 x 10<SUP>3</SUP> units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (34, 35, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R058', 'The schedule prints 10.4 x 10 <sup>3</sup> units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (35, 36, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R059', 'The schedule prints 10.4 x 10e3 units.', 'break_power_fragment', [(20, 24, '10.4', '104000', True, True, None, 'external magnitude changes selected coefficient', []), (27, 31, '10000', '10000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R060', 'The schedule prints 10.4 \\times 10^{3} units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (32, 34, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (36, 37, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R061', 'The schedule prints 10.4 x 10&lt;sup&gt;3&lt;/sup&gt; units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (40, 41, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R062', 'The schedule prints 10.4 x10\xb3 units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (26, 28, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (28, 29, '3', '3', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R063', 'The schedule prints 10.4 \u2715 10 <sup>3</sup> units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (35, 36, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R064', 'The schedule prints 10.4 &#10005; 10<sup>3</sup> units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 32, '10005', '10005', False, False, None, 'entity code decoration', []), (34, 36, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (41, 42, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R065', 'The schedule prints 10.4 \u2715 10\xa0<sup>3</sup> units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (35, 36, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R066', 'The schedule prints 10.4 \u2715 1.0e3 units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 32, '1000', '1000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R067', 'The schedule prints 10.4 \u2715 10^(3) units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 29, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R068', 'The schedule prints 10.4 E+3 units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 28, '3', '3', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R069', 'The schedule prints 10.4 [x 10^3] units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R070', 'The schedule prints 10.4 (x1000) units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (27, 31, '1000', '1000', False, False, None, 'glued x numeric identifier component', [])], []),
        ('R071', 'The schedule prints 10.4 (x 1,000) units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (28, 33, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('R072', 'The schedule prints 10.4 \u2192 x10^3 units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R073', 'The schedule prints 10.4 -- x 10^3 units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (30, 32, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (33, 34, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R074', 'The schedule prints 10.4, times 10^3, units.', 'break_power_fragment', [(20, 24, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (32, 34, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (35, 36, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R075', '$10.4\u2715 10\xb3', 'break_power_fragment', [(0, 5, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (7, 9, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (9, 10, '3', '3', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R076', '10.4 times ten to the third', 'break_residual', [(0, 4, '10.4', '10400', True, False, None, 'D4 spelled exponent', [])], []),
        ('R077', '10.4 \u2715 10 to the third power', 'break_residual', [(0, 4, '10.4', '10400', True, False, None, 'D4 spelled exponent', []), (7, 9, '10', '10', False, False, None, 'power component with spelled exponent', [])], []),
        ('R078', '10.4 \u2715 10 to the power three', 'break_residual', [(0, 4, '10.4', '10400', True, False, None, 'D4 spelled exponent', []), (7, 9, '10', '10', False, False, None, 'power component with spelled exponent', [])], []),
        ('R079', '10.4 \u2715 one thousand', 'break_residual', [(0, 4, '10.4', '10400', True, False, None, 'D4 spelled operand', [])], []),
        ('R080', '5 (mln)', 'break_residual', [(0, 1, '5', '5000000', True, False, None, 'D4 unknown abbreviation', [])], []),
        ('R081', '5 plus 6 million', 'break_residual', [(0, 1, '5', '5000000', True, False, None, 'D4 shared scale under additive reading', []), (7, 16, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('R082', 'The amount 10.4 [sic] x 10^3 applies.', 'break_residual', [(11, 15, '10.4', '10400', True, False, None, 'D4 intervening sic content', []), (24, 26, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (27, 28, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R083', 'The total of 10.4 (in 10^3) was booked.', 'break_residual', [(13, 17, '10.4', '10400', True, False, None, 'D4 in power units', []), (22, 24, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (25, 26, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R084', 'The reserve was 5 vs 6 million.', 'break_residual', [(16, 17, '5', '5000000', True, False, None, 'D4 vs content boundary', []), (21, 30, '6000000', '6000000', True, False, None, 'literal independent quantity', [])], []),
        ('R085', 'The schedule prints 10.4 by 10^3 units.', 'break_residual', [(20, 24, '10.4', '10400', True, False, None, 'D4 by operator vocabulary', []), (28, 30, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (31, 32, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R086', "Price: 10.4 ('000)", 'break_residual', [(7, 11, '10.4', '10400', True, False, None, 'D4 thousands heading', []), (14, 17, '0', '0', False, False, None, 'abbreviated thousands heading, not independent quantity', [])], []),
        ('R087', '| 2021 | 3,000 | $4.5 million |', 'break_schedule', [(2, 6, '2021', '2021', True, False, None, 'literal independent quantity', []), (9, 14, '3000', '3000', True, False, None, 'literal independent quantity', []), (17, 29, '4500000', '4500000', True, False, None, 'literal independent quantity', [])], []),
        ('R088', '| 3/4/2021 | 3,000 | 9.5% | $4.5 million |', 'break_schedule', [(2, 3, '3', '3', False, False, None, 'unsupported component outside whole-token grammar', []), (4, 5, '4', '4', False, False, None, 'unsupported component outside whole-token grammar', []), (6, 10, '2021', '2021', False, False, None, 'unsupported component outside whole-token grammar', []), (13, 18, '3000', '3000', True, False, None, 'literal independent quantity', []), (21, 25, '9.5', '9.5', True, False, None, 'literal independent quantity', []), (28, 40, '4500000', '4500000', True, False, None, 'literal independent quantity', [])], []),
        ('R089', '| Smith | 2021 | 3,000 | 4.5 million |', 'break_schedule', [(10, 14, '2021', '2021', True, False, None, 'literal independent quantity', []), (17, 22, '3000', '3000', True, False, None, 'literal independent quantity', []), (25, 36, '4500000', '4500000', True, False, None, 'literal independent quantity', [])], []),
        ('R090', '| Smith | 3,000 | 4.5 | million |', 'break_schedule', [(10, 15, '3000', '3000', True, False, None, 'literal independent quantity', []), (18, 21, '4.5', '4500000', True, True, None, 'separate bare scale cell', [])], []),
        ('R091', 'Smith bought 300, 400 and 2 million shares.', 'break_schedule', [(13, 16, '300', '300', True, False, None, 'literal independent quantity', []), (18, 21, '400', '400', True, False, None, 'literal independent quantity', []), (26, 35, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R092', 'Smith paid $300, $400 and $2 million.', 'break_schedule', [(11, 15, '300', '300', True, False, None, 'literal independent quantity', []), (17, 21, '400', '400', True, False, None, 'literal independent quantity', []), (26, 36, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R093', 'Smith bought 300 shares in Q1 2019 and 2 million in 2020.', 'break_schedule', [(13, 16, '300', '300', True, False, None, 'literal independent quantity', []), (28, 29, '1', '1', False, False, None, 'unsupported component outside whole-token grammar', []), (30, 34, '2019', '2019', True, False, None, 'literal independent quantity', []), (39, 48, '2000000', '2000000', True, False, None, 'literal independent quantity', []), (52, 56, '2020', '2020', True, False, None, 'literal independent quantity', [])], []),
        ('R094', 'On 3 March 2020 and 2 million shares', 'break_schedule', [(3, 4, '3', '3', True, False, None, 'literal independent quantity', []), (11, 15, '2020', '2020', True, False, None, 'literal independent quantity', []), (20, 29, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R095', 'See Section 9(k) and Rule 3(m).', 'break_normal', [(12, 13, '9', '9', True, False, None, 'literal independent quantity', []), (26, 27, '3', '3', True, False, None, 'literal independent quantity', [])], []),
        ('R096', 'See Schedule 13(d).', 'break_normal', [(13, 15, '13', '13', True, False, None, 'literal independent quantity', [])], []),
        ('R097', 'See Paragraph 4(t) of the order.', 'break_normal', [(14, 15, '4', '4', True, False, None, 'literal independent quantity', [])], []),
        ('R098', 'See Article 5(m) of the treaty.', 'break_normal', [(12, 13, '5', '5', True, False, None, 'literal independent quantity', [])], []),
        ('R099', 'Count 3 (b) of the indictment.', 'break_normal', [(6, 7, '3', '3', True, False, None, 'literal independent quantity', [])], []),
        ('R100', 'Item 7 (m) applies.', 'break_normal', [(5, 6, '7', '7', True, False, None, 'literal independent quantity', [])], []),
        ('R101', 'Smith sold 2,500 shares at $14.75 on June 1, 2021.', 'break_normal', [(11, 16, '2500', '2500', True, False, None, 'literal independent quantity', []), (27, 33, '14.75', '14.75', True, False, None, 'literal independent quantity', []), (42, 43, '1', '1', True, False, None, 'literal independent quantity', []), (45, 49, '2021', '2021', True, False, None, 'literal independent quantity', [])], []),
        ('R102', 'The meeting began at 9:30 a.m. and 2 million shares were voted.', 'break_normal', [(21, 22, '9', '9', False, False, None, 'unsupported component outside whole-token grammar', []), (23, 25, '30', '30', False, False, None, 'unsupported component outside whole-token grammar', []), (35, 44, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R103', 'Smith held 4.9% and Jones 2 million shares.', 'break_normal', [(11, 15, '4.9', '4.9', True, False, None, 'literal independent quantity', []), (26, 35, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R104', 'On May 5, 2020, Smith bought 1,000 shares.', 'break_normal', [(7, 8, '5', '5', True, False, None, 'literal independent quantity', []), (10, 14, '2020', '2020', True, False, None, 'literal independent quantity', []), (29, 34, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('R105', 'On 5 May 2020 Smith bought 1,000 shares and 2 million options.', 'break_normal', [(3, 4, '5', '5', True, False, None, 'literal independent quantity', []), (9, 13, '2020', '2020', True, False, None, 'literal independent quantity', []), (27, 32, '1000', '1000', True, False, None, 'literal independent quantity', []), (44, 53, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R106', 'Smith bought 1,000 shares during 2019, and 2 million later.', 'break_normal', [(13, 18, '1000', '1000', True, False, None, 'literal independent quantity', []), (33, 37, '2019', '2019', True, False, None, 'literal independent quantity', []), (43, 52, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R107', 'The vote was 12 to 3.', 'break_normal', [(13, 15, '12', '12', True, False, None, 'literal independent quantity', []), (19, 20, '3', '3', True, False, None, 'literal independent quantity', [])], []),
        ('R108', 'Pages 10 through 12 discuss it.', 'break_normal', [(6, 8, '10', '10', True, False, None, 'literal independent quantity', []), (17, 19, '12', '12', True, False, None, 'literal independent quantity', [])], []),
        ('R109', 'The price was $3.20 per share, or $16,000 in total.', 'break_normal', [(14, 19, '3.20', '3.20', True, False, None, 'literal independent quantity', []), (34, 41, '16000', '16000', True, False, None, 'literal independent quantity', [])], []),
        ('R110', 'Smith owned 15% of the 2 million shares.', 'break_normal', [(12, 15, '15', '15', True, False, None, 'literal independent quantity', []), (23, 32, '2000000', '2000000', True, False, None, 'literal independent quantity', [])], []),
        ('R111', 'Count 2 charges 3 defendants.', 'break_normal', [(6, 7, '2', '2', True, False, None, 'literal independent quantity', []), (16, 17, '3', '3', True, False, None, 'literal independent quantity', [])], []),
        ('R112', 'The stock split 2 for 1 in 2019.', 'break_normal', [(16, 17, '2', '2', True, False, None, 'literal independent quantity', []), (22, 23, '1', '1', True, False, None, 'literal independent quantity', []), (27, 31, '2019', '2019', True, False, None, 'literal independent quantity', [])], []),
        ('R113', 'Smith bought 300 shares on May 1 and 2 million shares on June 1.', 'break_normal', [(13, 16, '300', '300', True, False, None, 'literal independent quantity', []), (31, 32, '1', '1', True, False, None, 'literal independent quantity', []), (37, 46, '2000000', '2000000', True, False, None, 'literal independent quantity', []), (62, 63, '1', '1', True, False, None, 'literal independent quantity', [])], []),
        ('R114', '129 records ordinary ordinary ordinary ordinary ordinary ordinary ordinary ordinary ordinary ordinary ordinary ordinary ordinary end.', 'break_reconstructed_tail', [(0, 3, '129', '129', True, False, None, 'literal independent quantity', [])], []),
        ('R115', '5\u200b million', 'break_closed', [(0, 1, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R116', '5\xad million', 'break_closed', [(0, 1, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R117', '5\u2060 million', 'break_closed', [(0, 1, '5', '5000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R118', '10.4 hundred thousand', 'break_closed', [(0, 12, '1040', '1040000', False, False, None, 'unsupported repeated scale; 10.4 times 100 times 1000', [])], []),
        ('R119', '2 thousand million', 'break_closed', [(0, 10, '2000', '2000000000', True, True, None, 'repeated scale', [])], []),
        ('R120', '12-month', 'break_closed', [(0, 8, '12', '12', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R121', '5kg', 'break_closed', [(0, 3, '5', '5', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R122', '8million', 'break_closed', [(0, 8, '8000000', '8000000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R123', '10.4 x 2e3', 'break_closed', [(0, 4, '10.4', '20800', True, True, None, 'external magnitude changes selected coefficient', []), (7, 10, '2000', '2000', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R124', 'The ledger reports 10.4 \u2a2f 10<SuP title="e" class="power">3</SuP> units.', 'variant_p2_1', [(19, 23, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (26, 28, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (57, 58, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R125', 'The ledger reports 10.4 \u22c5 10<span><b><SUP class="power" title="e">3</SUP></b></span> units.', 'variant_p2_1', [(19, 23, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (26, 28, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (66, 67, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R126', 'The ledger reports 10.4 \u2297 1,000 units.', 'variant_p2_1', [(19, 23, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (26, 31, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('R127', 'The ledger reports 10.4 \\cdot 1,000 units.', 'variant_p2_1', [(19, 23, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (30, 35, '1000', '1000', True, False, None, 'literal independent quantity', [])], []),
        ('R128', 'The ledger reports 10.4 &times; 10<font><i><sup >3</sup></i></font> units.', 'variant_p2_1', [(19, 23, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', []), (32, 34, '10', '10', False, False, None, 'power base is a forbidden component in every notation', []), (49, 50, '3', '3', False, False, None, 'power exponent component', [])], []),
        ('R129', 'The fund reports $8 {M.} in cash.', 'variant_p2_2', [(17, 19, '8', '8000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R130', '| Fund | 8 | (\u20acM.) |', 'variant_p2_2', [(9, 10, '8', '8000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R131', '| Fund | 8 | K EUR |', 'variant_p2_2', [(9, 10, '8', '8000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R132', 'The fund reports 8 \uff3b m. \uff3d in notes.', 'variant_p2_2', [(17, 18, '8', '8000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R133', 'The fund reports 8(m) in cash.', 'variant_p2_2', [(17, 18, '8', '8000000', True, True, None, 'right shared scale modifies amount', [])], []),
        ('R134', '| Reserves | 8 | USD |', 'variant_p2_2', [(13, 14, '8', '8', True, False, None, 'literal independent quantity', [])], []),
        ('R135', '| Weight | 8 | kg |', 'variant_p2_2', [(11, 12, '8', '8', True, False, None, 'literal independent quantity', [])], []),
        ('R136', '| Length | 8 | mm |', 'variant_p2_2', [(11, 12, '8', '8', True, False, None, 'literal independent quantity', [])], []),
        ('R137', 'Jones acquired 200, 600 and 3 million shares on separate dates.', 'variant_p2_3', [(15, 18, '200', '200', True, False, None, 'literal independent quantity', []), (20, 23, '600', '600', True, False, None, 'literal independent quantity', []), (28, 37, '3000000', '3000000', True, False, None, 'literal independent quantity', [])], []),
        ('R138', 'The hearing ran from 8 to 10, and 4 million shares were voted.', 'variant_p2_3', [(21, 22, '8', '8', True, False, None, 'literal independent quantity', []), (26, 28, '10', '10', True, False, None, 'literal independent quantity', []), (34, 43, '4000000', '4000000', True, False, None, 'literal independent quantity', [])], []),
        ('R139', 'Jones bought 150\n3 million shares', 'variant_p2_3', [(13, 16, '150', '150', True, False, None, 'literal independent quantity', []), (17, 26, '3000000', '3000000', True, False, None, 'literal independent quantity', [])], []),
        ('R140', '| Date | Shares | Cash |\n| 2024 | 400 | $3 million |', 'variant_p2_3', [(27, 31, '2024', '2024', True, False, None, 'literal independent quantity', []), (34, 37, '400', '400', True, False, None, 'literal independent quantity', []), (40, 50, '3000000', '3000000', True, False, None, 'literal independent quantity', [])], []),
        ('R141', '<table><tr><td>400</td><td>$3 million</td></tr></table>', 'variant_p2_3', [(15, 18, '400', '400', True, False, None, 'literal independent quantity', []), (27, 37, '3000000', '3000000', True, False, None, 'literal independent quantity', [])], []),
        ('R142', 'The petition invokes 19[q] and seeks $800.', 'variant_p2_4', [(21, 23, '19', '19', True, False, None, 'literal independent quantity', []), (37, 41, '800', '800', True, False, None, 'literal independent quantity', [])], []),
        ('R143', 'The petition invokes 42(f)(2).', 'variant_p2_4', [(21, 23, '42', '42', True, False, None, 'literal independent quantity', []), (27, 28, '2', '2', False, False, None, 'unsupported component outside whole-token grammar', [])], []),
        ('R144', 'The account under 403(b) holds $800.', 'variant_p2_4', [(18, 21, '403', '403', True, False, None, 'literal independent quantity', []), (31, 35, '800', '800', True, False, None, 'literal independent quantity', [])], []),
        ('R145', 'On July 7 700 shares were sold.', 'variant_p2_4', [(8, 9, '7', '7', True, False, None, 'literal independent quantity', []), (10, 13, '700', '700', True, False, None, 'literal independent quantity', [])], []),
    ],
    'cites': [
        ('C01', 'The court applied 16(b) to the transaction.', 'uncued_citation', [(18, 20, '16', '16', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C02', 'Smith is liable under 12(b)(6) grounds.', 'uncued_citation', [(22, 24, '12', '12', True, False, None, 'literal legal identifier or independent quantity', []), (28, 29, '6', '6', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', [])], []),
        ('C03', 'Defenses under 10(b) and 10b-5 were raised.', 'uncued_citation', [(15, 17, '10', '10', True, False, None, 'literal legal identifier or independent quantity', []), (25, 27, '10', '10', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', []), (29, 30, '5', '5', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', [])], []),
        ('C04', 'The 13(d) group held 5,000 shares.', 'uncued_citation', [(4, 6, '13', '13', True, False, None, 'literal legal identifier or independent quantity', []), (21, 26, '5000', '5000', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C05', 'A 401(k) plan received $5,000.', 'uncued_citation', [(2, 5, '401', '401', True, False, None, 'literal legal identifier or independent quantity', []), (23, 29, '5000', '5000', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C06', 'The filing listed 10-K, II, and 7(a).', 'uncued_citation', [(18, 20, '10', '10', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', []), (32, 33, '7', '7', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C07', '9(k) of the lease controls 2 deposits.', 'uncued_citation', [(0, 1, '9', '9', True, False, None, 'literal legal identifier or independent quantity', []), (27, 28, '2', '2', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C08', 'The 23(b)(3) class had 5 members.', 'uncued_citation', [(4, 6, '23', '23', True, False, None, 'literal legal identifier or independent quantity', []), (10, 11, '3', '3', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', []), (23, 24, '5', '5', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C09', 'The objection rests on 8[c] and seeks $750.', 'uncued_citation', [(23, 24, '8', '8', True, False, None, 'literal legal identifier or independent quantity', []), (38, 42, '750', '750', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C10', 'The remedy follows 14{d}, with 4 exceptions.', 'uncued_citation', [(19, 21, '14', '14', True, False, None, 'literal legal identifier or independent quantity', []), (31, 32, '4', '4', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C11', 'The motion invokes 56(c)(4).', 'uncued_citation', [(19, 21, '56', '56', True, False, None, 'literal legal identifier or independent quantity', []), (25, 26, '4', '4', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', [])], []),
        ('C12', 'The award under 11(b) was $2 million.', 'uncued_citation', [(16, 18, '11', '11', True, False, None, 'literal legal identifier or independent quantity', []), (26, 36, '2000000', '2000000', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C13', 'The parties stipulated to 9(a); 3 claims remain.', 'uncued_citation', [(26, 27, '9', '9', True, False, None, 'literal legal identifier or independent quantity', []), (32, 33, '3', '3', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C14', 'The finding rests on 15(t).', 'uncued_citation', [(21, 23, '15', '15', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C15', 'The order applies 17 (e) to 20 accounts.', 'uncued_citation', [(18, 20, '17', '17', True, False, None, 'literal legal identifier or independent quantity', []), (28, 30, '20', '20', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C16', 'The pleading invokes 5(m) and demands $900.', 'uncued_citation', [(21, 22, '5', '5', True, False, None, 'literal legal identifier or independent quantity', []), (38, 42, '900', '900', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C17', 'The exemption under 501(c)(3) covers 2 trusts.', 'uncued_citation', [(20, 23, '501', '501', True, False, None, 'literal legal identifier or independent quantity', []), (27, 28, '3', '3', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', []), (37, 38, '2', '2', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C18', 'The objection under 26(b)(1) concerns 100 records.', 'uncued_citation', [(20, 22, '26', '26', True, False, None, 'literal legal identifier or independent quantity', []), (26, 27, '1', '1', False, False, None, 'identifier component or omitted paired parentheses outside token grammar', []), (38, 41, '100', '100', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C19', 'The judgment addresses 7[b], 8[d], and $25,000.', 'uncued_citation', [(23, 24, '7', '7', True, False, None, 'literal legal identifier or independent quantity', []), (29, 30, '8', '8', True, False, None, 'literal legal identifier or independent quantity', []), (39, 46, '25000', '25000', True, False, None, 'literal legal identifier or independent quantity', [])], []),
        ('C20', 'The claim under 10(b) fails; the 401(k) account held $6,000.', 'uncued_citation', [(16, 18, '10', '10', True, False, None, 'literal legal identifier or independent quantity', []), (33, 36, '401', '401', True, False, None, 'literal legal identifier or independent quantity', []), (53, 59, '6000', '6000', True, False, None, 'literal legal identifier or independent quantity', [])], []),
    ],
    'r10': [
        ('T001', '10.4 \u2716 1000', 'review_p2_symbol', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T002', '10.4 &#x2716; 1000', 'review_p2_symbol', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T003', 'The reserve was 5 \u2716 1,000,000.', 'review_p2_symbol', [(16, 17, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T004', 'The total is 10.4 \u2716 1,000 units.', 'review_p2_symbol', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T005', 'Total: 5 &#x2716; 1000 shares.', 'review_p2_symbol', [(7, 8, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T006', 'Total: 5&#x2716;1000 shares.', 'review_p2_symbol', [(7, 8, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T007', 'The reserves were 5 and 6 \u2716 1,000,000.', 'review_p2_symbol', [(18, 19, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T008', '10.4 \u2731 1000', 'review_p2_symbol', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T009', '10.4 \u204e 1000', 'review_p2_symbol', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T010', 'Net debt was 1,234 (EUR m) at year end.', 'review_p2_bracket', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T011', 'Net debt was 1,234 (EUR m).', 'review_p2_bracket', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T012', 'Net debt was 1,234 (USD k) at year end.', 'review_p2_bracket', [(13, 18, '1234', '1234000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T013', 'Net debt was 1,234 (GBP mn) at year end.', 'review_p2_bracket', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T014', 'Net debt was 1,234 [EUR m] at year end.', 'review_p2_bracket', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T015', 'The fund holds 5 (m USD) in notes.', 'review_p2_bracket', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T016', 'The fund holds 5 (USD m) in notes.', 'review_p2_bracket', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T017', 'The fund holds 5 ($, m) in notes.', 'review_p2_bracket', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T018', 'The fund holds 5 (m-$) in notes.', 'review_p2_bracket', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T019', 'The reserves were 5, 6 and 7 (EUR m) respectively.', 'review_p2_bracket', [(18, 19, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T020', 'The reserves were 5 and 6 (USD bn).', 'review_p2_bracket', [(18, 19, '5', '5000000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T021', 'Total: 10.4<o:p></o:p> x 1,000 units.', 'review_p2_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T022', '<p class=MsoNormal>Total 10.4<o:p></o:p></p><p>x 10<sup>3</sup></p>', 'review_p2_markup', [(25, 29, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T023', 'Total: 10.4 <st1:place>x</st1:place> 1,000 units.', 'review_p2_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T024', 'Total: 10.4 <u1:p>(m)</u1:p> units.', 'review_p2_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T025', 'Total: 10.4 <tspan>x 1,000</tspan> units.', 'review_p2_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T026', 'Total: 10.4<mo>&#xD7;</mo>1000', 'review_p2_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T027', 'Total: 10.4 <x-unit>m</x-unit>.', 'review_p2_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T028', '10.4 \u2715 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T029', '10.4 \xd7 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T030', '10.4 \u2a09 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T031', '10.4 \u2297 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T032', '10.4 \u2217 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T033', '10.4 \ufe61 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T034', '10.4 \uff0a 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T035', '10.4 \u2062 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T036', '10.4 \u2573 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T037', '10.4 \u2613 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T038', '10.4 \U0001f7a8 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T039', '10.4 \u22c6 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T040', '10.4 \u30fb 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T041', '10.4 \uff65 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T042', '10.4 \u2e31 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T043', '10.4 \u066d 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T044', '10.4 \u0445 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T045', '10.4 \u0425 1000', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T046', '10.4 \u2716 10^3', 'review_symbol_control', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T047', '| Net debt | 1,234 | EUR m |', 'review_bracket_control', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T048', '| Net debt | 1,234 | (EUR m) |', 'review_bracket_control', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T049', '| Net debt | 1,234 | in EUR m |', 'review_bracket_control', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T050', 'Net debt was 1,234 (\u20ac m) at year end.', 'review_bracket_control', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T051', '<table><tr><td>Net debt</td><td>1,234</td><td>EUR m</td></tr></table>', 'review_bracket_control', [(32, 37, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T052', 'The fund holds 5 (TEUR) in notes.', 'review_bracket_control', [(15, 16, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T053', 'The fund holds 5 (Tsd.) in notes.', 'review_bracket_control', [(15, 16, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T054', 'The fund holds 5 (\u20acm) in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T055', 'The fund holds 5 (m\u20ac) in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T056', 'The fund holds 5 ($ m) in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T057', 'The fund holds 5 (m\u0301) in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T058', 'The fund holds 5 (\u1d0d) in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T059', 'The fund holds 5 (\uff4d) in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T060', 'The fund holds 5 (\u217f) in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T061', 'The fund holds 5 \u27e8m\u27e9 in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T062', 'The fund holds 5 \u3010m\u3011 in notes.', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T063', 'The fund holds 5 (m)', 'review_bracket_control', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T064', 'Total: 10.4 <nobr>x 1,000</nobr> units.', 'review_markup_control', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T065', 'Total: 10.4 <math><mo>&times;</mo><mn>1000</mn></math> units.', 'review_markup_control', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T066', 'The fund holds 5(m), net of tax.', 'review_p3_subsection', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T067', 'The fund holds 5(m); the rest is cash.', 'review_p3_subsection', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T068', 'The fund holds 8(m) (unaudited).', 'review_p3_subsection', [(15, 16, '8', '8000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T069', 'Reserve 5(k): see note.', 'review_p3_subsection', [(8, 9, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T070', 'The fund holds 5(m).', 'review_p3_subsection', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T071', 'Reserve: 5 / k', 'review_p3_gap', [(9, 10, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T072', 'Reserve: 5 \u2013 m', 'review_p3_gap', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T073', 'Reserve: 5 \u2014 m', 'review_p3_gap', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T074', 'Reserve: 5 = m', 'review_p3_gap', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T075', 'Reserve: 5 ~ m', 'review_p3_gap', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T076', 'Reserve: 5 @ m', 'review_p3_gap', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T077', 'Reserve: 5 # m', 'review_p3_gap', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T078', 'Reserve: 5 ... m', 'review_p3_gap', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T079', 'The fund holds 5 "m" in notes.', 'review_p3_gap', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T080', "The fund holds 5 'm' in notes.", 'review_p3_gap', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T081', 'The fund holds 5 \xabm\xbb in notes.', 'review_p3_gap', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T082', 'The fund holds 5 \u2039m\u203a in notes.', 'review_p3_gap', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T083', 'The fund holds 5 \u201cm\u201d in notes.', 'review_p3_gap', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T084', 'Total: 5&#8203;000 shares.', 'review_p3_grouping', [(7, 8, '5', '5000', False, False, None, 'proper component of unsupported grouped numeral', []), (15, 18, '0', '5000', False, False, None, 'proper component of unsupported grouped numeral', [])], []),
        ('T085', 'Total: 5&thinsp;000 shares.', 'review_p3_grouping', [(7, 8, '5', '5000', False, False, None, 'proper component of unsupported grouped numeral', []), (16, 19, '0', '5000', False, False, None, 'proper component of unsupported grouped numeral', [])], []),
        ('T086', 'Total: 5&#x202F;000 shares.', 'review_p3_grouping', [(7, 8, '5', '5000', False, False, None, 'proper component of unsupported grouped numeral', []), (16, 19, '0', '5000', False, False, None, 'proper component of unsupported grouped numeral', [])], []),
        ('T087', 'Total: 5 000 shares.', 'review_p3_grouping', [(7, 8, '5', '5000', False, False, None, 'proper component of unsupported grouped numeral', []), (9, 12, '0', '5000', False, False, None, 'proper component of unsupported grouped numeral', [])], []),
        ('T088', 'The total is 10.4 (in 000s) units.', 'review_break_residual', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T089', 'The total is 10.4 (000) units.', 'review_break_residual', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T090', '| Net debt | 1,234 | EUR millions |', 'review_break_residual', [(13, 18, '1234', '1234000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T091', 'The reserve was 9(b), not 9.', 'review_withdrawn', [(16, 17, '9', '9', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T092', '5 \u2063 1000', 'review_withdrawn', [(0, 1, '5', '5', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T093', '5\u2064 1000', 'review_withdrawn', [(0, 1, '5', '5', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T094', 'Reserve: 5 / mn', 'review_break_control', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T095', 'Reserve: 5 : m', 'review_break_control', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T096', 'Reserve: 5 -- m', 'review_break_control', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T097', 'Reserve: 5 \xb7 m', 'review_break_control', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T098', 'Reserve: 5 m', 'review_break_control', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T099', 'Reserve: 5 M.', 'review_break_control', [(9, 10, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T100', 'The fund holds 5 (m) and 6 (k).', 'review_break_control', [(25, 26, '6', '6000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T101', 'The total is 10.4 (x 10^3) units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T102', 'The total is 10.4 (\xd71,000) units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T103', 'The total is 10.4 [10^3] units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T104', 'The total is 10.4 (thousand) units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T105', 'The total is 10.4 (10 to the 3rd) units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T106', 'The total is 10.4 E+03 units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T107', 'The total is 10.4 \u2013 x1000 units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T108', 'The total is 10.4, x 1,000, units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T109', 'The total is 10.4 (000s) units.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T110', 'The total is 10.4 k\u20ac.', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T111', 'The total is 10.4 (k\u20ac).', 'review_break_control', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T112', 'The total is 10.4 T\u20ac.', 'review_break_control', [(13, 17, '10.4', '10400000000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T113', 'The reserves were 5, 6 and 7 (m) respectively.', 'review_break_control', [(18, 19, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T114', 'The reserves were 5, 6 and 7 \u2716 10^6 respectively.', 'review_break_control', [(18, 19, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T115', 'The reserves were 5 and 6 (bn).', 'review_break_control', [(18, 19, '5', '5000000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T116', '| Amount | 5 | m |', 'review_break_fragment', [(11, 12, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T117', '| Amount | 5 | k |', 'review_break_fragment', [(11, 12, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T118', '<table><tr><td>5</td><td>m</td></tr></table>', 'review_break_fragment', [(15, 16, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T119', '<table><tr><td>5</td><td>k</td></tr></table>', 'review_break_fragment', [(15, 16, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T120', '5\tm', 'review_break_fragment', [(0, 1, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T121', '5\t(m)', 'review_break_fragment', [(0, 1, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T122', '5\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0m', 'review_break_fragment', [(0, 1, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T123', '| | m | m |\n| Amount | 5 | 6 |', 'review_break_fragment', [(23, 24, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T124', 'Total: 10.4 &times; 10<sup>3</sup>', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T125', 'Total: 10.4 &#215; 1,000', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T126', 'Total: 10.4 &amp;times; 1,000', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T127', 'Total: 10.4 x 10<SUP\nclass=a>3</SUP>', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T128', 'Total: 10.4 x 10<sup>&#51;</sup>', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T129', 'Total: 10.4 x 10<sup><b>3</b></sup>', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T130', 'Total: 10.4 x 10<sup/>3', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T131', 'Total: 10.4 x 10<span style="vertical-align:super">3</span>', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T132', 'Total: 10.4 x 10<span style="font-size:smaller;vertical-align:super">3</span>', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T133', 'Total: 10.4 <br>x 1,000', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T134', 'Total: 10.4 <!-- copied -->x 1,000', 'review_break_markup', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T135', 'Total: 10.4 <abbr>m</abbr>', 'review_break_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T136', 'Total: 10.4 <em>million</em>', 'review_break_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T137', 'Total: 10.4 <em>m</em>', 'review_break_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T138', 'Total: 10.4 <font>m</font>', 'review_break_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T139', 'Total: 10.4 <sub>m</sub>', 'review_break_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T140', 'Total: 10.4 <small>(m)</small>', 'review_break_markup', [(7, 11, '10.4', '10400000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T141', '10.4 x 10^3', 'review_original_d5', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T142', '[10.4] x 10^3', 'review_original_d5', [(1, 5, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T143', 'between 5 and 6 million', 'review_original_d5', [(8, 9, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T144', '5, 6 or 7 x 10^6', 'review_original_d5', [(0, 1, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T145', '5, 6 or 7 x 10^6', 'review_original_d5', [(3, 4, '6', '6000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T146', '10.4 . 10^3', 'review_original_d5', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T147', 'On March 5, 2019, Smith bought 5,000 shares at $12.50 per share.', 'review_normal', [(31, 36, '5000', '5000', True, False, None, 'independent literal quantity or legal identifier', []), (47, 53, '12.50', '12.50', True, False, None, 'independent literal quantity or legal identifier', []), (12, 16, '2019', '2019', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T148', 'The defendant held 12.5% of the outstanding shares in 2019.', 'review_normal', [(19, 24, '12.5', '12.5', True, False, None, 'independent literal quantity or legal identifier', []), (54, 58, '2019', '2019', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T149', 'Count 3 alleges that 200 shares were sold.', 'review_normal', [(6, 7, '3', '3', True, False, None, 'independent literal quantity or legal identifier', []), (21, 24, '200', '200', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T150', 'Under Rule 10b-5, the defendant sold 1,250 shares.', 'review_normal', [(37, 42, '1250', '1250', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T151', 'He paid $1,250.00 in cash on June 3.', 'review_normal', [(8, 17, '1250', '1250', True, False, None, 'independent literal quantity or legal identifier', []), (34, 35, '3', '3', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T152', 'The fund held 5 million shares.', 'review_normal', [(14, 23, '5000000', '5000000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T153', 'Smith sold 300 shares to Jones, 200 to Lee and 100 to Kim.', 'review_normal', [(32, 35, '200', '200', True, False, None, 'independent literal quantity or legal identifier', []), (47, 50, '100', '100', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T154', 'Section 5(b)(2) applies.', 'review_normal', [(8, 9, '5', '5', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T155', 'Smith moved to dismiss under Rule 12(b)(6) on May 2.', 'review_normal', [(34, 36, '12', '12', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T156', 'The price fell from $12.50 to $10.25 per share.', 'review_normal', [(20, 26, '12.50', '12.50', True, False, None, 'independent literal quantity or legal identifier', []), (30, 36, '10.25', '10.25', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T157', 'He transferred 1,000 shares, i.e., 10% of the class.', 'review_normal', [(15, 20, '1000', '1000', True, False, None, 'independent literal quantity or legal identifier', []), (35, 38, '10', '10', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T158', 'Of the 500 shares, 200 were sold in 2020.', 'review_normal', [(7, 10, '500', '500', True, False, None, 'independent literal quantity or legal identifier', []), (19, 22, '200', '200', True, False, None, 'independent literal quantity or legal identifier', []), (36, 40, '2020', '2020', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T159', 'Smith held 4,000 shares (Class A) and 1,000 shares (Class B).', 'review_normal', [(11, 16, '4000', '4000', True, False, None, 'independent literal quantity or legal identifier', []), (38, 43, '1000', '1000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T160', 'Smith held 4,000 Class A shares and 1,000 Class B shares.', 'review_normal', [(11, 16, '4000', '4000', True, False, None, 'independent literal quantity or legal identifier', []), (36, 41, '1000', '1000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T161', 'The 2 defendants paid $3,000 each.', 'review_normal', [(4, 5, '2', '2', True, False, None, 'independent literal quantity or legal identifier', []), (22, 28, '3000', '3000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T162', 'Exhibit 12 lists 300 transactions.', 'review_normal', [(8, 10, '12', '12', True, False, None, 'independent literal quantity or legal identifier', []), (17, 20, '300', '300', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T163', 'Smith owned 25 percent of XYZ Co. in 2018.', 'review_normal', [(12, 14, '25', '25', True, False, None, 'independent literal quantity or legal identifier', []), (37, 41, '2018', '2018', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T164', 'The plan held 1,500 shares in a 401(k) account.', 'review_normal', [(14, 19, '1500', '1500', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T165', 'Smith bought 100 shares x 3 times.', 'review_normal', [(13, 16, '100', '100', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T166', 'Smith bought 3 lots of 100 shares.', 'review_normal', [(13, 14, '3', '3', True, False, None, 'independent literal quantity or legal identifier', []), (23, 26, '100', '100', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T167', 'The vote was 7 (yes) to 2 (no).', 'review_normal', [(24, 25, '2', '2', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T168', 'Smith received 40 shares (see Ex. A).', 'review_normal', [(15, 17, '40', '40', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T169', 'Smith received 40 shares (approx.) in 2019.', 'review_normal', [(15, 17, '40', '40', True, False, None, 'independent literal quantity or legal identifier', []), (38, 42, '2019', '2019', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T170', 'The account had 2 owners (H and W).', 'review_normal', [(16, 17, '2', '2', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T171', 'The court awarded $5,000 (plus interest).', 'review_normal', [(18, 24, '5000', '5000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T172', 'Jones paid 50 (fifty) dollars.', 'review_normal', [(11, 13, '50', '50', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T173', 'Smith sold 10 shares on 3/4/2020 and 20 on 4/5/2020.', 'review_normal', [(11, 13, '10', '10', True, False, None, 'independent literal quantity or legal identifier', []), (28, 30, '20', '20', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T174', 'The lease runs 5 yrs (to 2025).', 'review_normal', [(15, 16, '5', '5', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T175', 'Total: 300 shares | 2019 |', 'review_normal', [(7, 10, '300', '300', True, False, None, 'independent literal quantity or legal identifier', []), (20, 24, '2019', '2019', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T176', '| Smith | 300 | Jones |', 'review_normal', [(10, 13, '300', '300', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T177', 'Smith bought 5,000 shares on 3/4/2020.', 'review_normal', [(13, 18, '5000', '5000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T178', 'Smith sold 5,000 shares at $3.20 per share on March 3, 2020.', 'review_normal', [(11, 16, '5000', '5000', True, False, None, 'independent literal quantity or legal identifier', []), (27, 32, '3.20', '3.20', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T179', '$1.2 million and $3.4 million', 'review_normal', [(0, 12, '1200000', '1200000', True, False, None, 'independent literal quantity or legal identifier', []), (17, 29, '3400000', '3400000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T180', 'between $5 million and $6 million', 'review_normal', [(8, 18, '5000000', '5000000', True, False, None, 'independent literal quantity or legal identifier', []), (23, 33, '6000000', '6000000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T181', 'The vote was 7 (yes) to 2 (no).', 'review_cost', [(13, 14, '7', '7', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T182', 'The court awarded $5,000 (P) and $2,000 (D).', 'review_cost', [(18, 24, '5000', '5000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T183', '| Smith | 300 | n/a |', 'review_cost', [(10, 13, '300', '300', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T184', '| Smith | 300 | TBD |', 'review_cost', [(10, 13, '300', '300', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T185', '| Smith | 300 | Yes |', 'review_cost', [(10, 13, '300', '300', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T186', '| Smith | 300 | CA |', 'review_cost', [(10, 13, '300', '300', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T187', 'Smith (CEO) sold 300 (A) shares.', 'review_cost', [(17, 20, '300', '300', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T188', 'Total: 1&#44;000 shares.', 'review_contract', [(7, 8, '1', '1', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', []), (13, 16, '000', '000', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T189', 'Total: 5<b>000</b> shares.', 'review_contract', [(7, 8, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', []), (11, 14, '000', '000', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T190', 'Total: <b>5</b>,000 shares.', 'review_contract', [(10, 11, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', []), (16, 19, '000', '000', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T191', 'Total: 5<wbr>000 shares.', 'review_contract', [(7, 8, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', []), (13, 16, '000', '000', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T192', 'Total: $<b>5</b> million.', 'review_contract', [(11, 12, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T193', 'Total: 12<sup>th</sup> lot.', 'review_contract', [(7, 9, '12', '12', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T194', 'Total: -<b>5</b> shares.', 'review_contract', [(11, 12, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T195', 'Total: &minus;5 shares.', 'review_contract', [(14, 15, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T196', 'Total: (<b>300</b>) shares.', 'review_contract', [(11, 14, '300', '300', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T197', 'Total: &lpar;300&rpar; shares.', 'review_contract', [(13, 16, '300', '300', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T198', 'Total: 5&shy;million shares.', 'review_contract', [(7, 8, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T199', 'Total: \uff15 shares.', 'review_contract', [(7, 8, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T200', 'Total: 5\uff10 shares.', 'review_contract', [(7, 8, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T201', 'Total: 5\u0332 shares.', 'review_contract', [(7, 8, '5', '5', False, False, None, 'partial, decorated, or non-ASCII source seed outside whole-token contract', [])], []),
        ('T202', 'Total: \u2460 shares, 5 more.', 'review_contract', [(17, 18, '5', '5', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T203', 'The total is 10.4 \\cdot 1000 units.', 'review_recheck', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T204', '5 x 1 x 1000', 'review_recheck', [(0, 1, '5', '5000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T205', '10.4 x 2e3', 'review_recheck', [(0, 4, '10.4', '20800', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T206', '10.4), x 10^3', 'review_recheck', [(0, 4, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T207', '5, million', 'review_recheck', [(0, 1, '5', '5000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T208', '5. Million Corp', 'review_recheck', [(0, 1, '5', '5', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T209', '10.4.) x 10^3', 'review_recheck', [(0, 4, '10.4', '10.4', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T210', 'in 2019, $5 million', 'review_recheck', [(3, 7, '2019', '2019', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T211', '5\nmillion', 'review_recheck', [(0, 1, '5', '5', False, False, None, 'incomplete strict token or unsupported attached/repeated scale', [])], []),
        ('T212', '5\u200b million', 'review_recheck', [(0, 1, '5', '5', False, False, None, 'incomplete strict token or unsupported attached/repeated scale', [])], []),
        ('T213', '5\xad million', 'review_recheck', [(0, 1, '5', '5', False, False, None, 'incomplete strict token or unsupported attached/repeated scale', [])], []),
        ('T214', '10.4 hundred thousand', 'review_recheck', [(0, 4, '10.4', '10.4', False, False, None, 'incomplete strict token or unsupported attached/repeated scale', [])], []),
        ('T215', '2 thousand million', 'review_recheck', [(0, 1, '2', '2', False, False, None, 'incomplete strict token or unsupported attached/repeated scale', [])], []),
        ('T216', '12-month', 'review_recheck', [(0, 2, '12', '12', False, False, None, 'incomplete strict token or unsupported attached/repeated scale', [])], []),
        ('T217', '5\nmillion', 'review_recheck', [(0, 9, '5000000', '5000000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T218', 'Reported amount: 7 \u25c6 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T219', 'Reported amount: 7 \u2605 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T220', 'Reported amount: 7 \u2055 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T221', 'Reported amount: 7 \u203b 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T222', 'Reported amount: 7 \u2e2e 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T223', 'Reported amount: 7 \u2042 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T224', 'Reported amount: 7 \u29eb 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T225', 'Reported amount: 7 \u2051 1000.', 'own_symbol', [(17, 18, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T226', 'Estimated reserves: 7 or 8 \u25c6 1000.', 'own_symbol', [(20, 21, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T227', 'Reserve 7 (CAD k) is held.', 'own_bracket', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T228', 'Reserve 7 (m CHF) is held.', 'own_bracket', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T229', 'Reserve 7 (AUD; mn) is held.', 'own_bracket', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T230', 'Reserve 7 [JPY k] is held.', 'own_bracket', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T231', 'Reserve 7 (USD / m) is held.', 'own_bracket', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T232', 'Reserve 7 (m, EUR) is held.', 'own_bracket', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T233', 'Reserve 7 (\xa3; bn) is held.', 'own_bracket', [(8, 9, '7', '7000000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T234', 'Reserve 7 (k: USD) is held.', 'own_bracket', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T235', 'Reserve 7 (GBP\u2060mn) is held.', 'own_bracket', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T236', 'Reserve 7 (m\u200bUSD) is held.', 'own_bracket', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T237', 'Reserve 7 <w:run>x 1000</w:run> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T238', 'Reserve 7 <mso:span data-id="a">x 1000</mso:span> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T239', 'Reserve 7 <office:money currency="USD">(m)</office:money> units.', 'own_markup', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T240', 'Reserve 7 <span style="color:red">x 1000</span> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T241', 'Reserve 7 <font face="Times">x 1000</font> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T242', 'Reserve 7 <math:math><math:mo>\u2716</math:mo><math:mn>1000</math:mn></math:math> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T243', 'Reserve 7 <!--[if mso]>x 1000<![endif]--> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T244', 'Reserve 7 <![if !mso]>x 1000<![endif]> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T245', 'Reserve 7 <custom-widget title="a > b">x 1000</custom-widget> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T246', 'Reserve 7 <w:p><w:r>x 1000</w:r></w:p> units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T247', 'Reserve 7 <mso:p/> x 1000 units.', 'own_markup', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T248', 'Reserve 7 <foreign>m</foreign> units.', 'own_markup', [(8, 9, '7', '7000000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T249', 'Reserve 7 <w:p>x 1000', 'own_markup_malformed', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T250', 'Reserve 7 <a><b>x 1000</a></b>', 'own_markup_malformed', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T251', 'Reserve 7 <unknown attr="', 'own_markup_malformed', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T252', 'Reserve 7 <!-- x 1000', 'own_markup_malformed', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T253', 'Reserve 7 <![if mso]>x 1000', 'own_markup_malformed', [(8, 9, '7', '7000', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T254', 'Total 7\u200c123 shares.', 'own_grouping', [(6, 7, '7', '7123', False, False, None, 'format character inside digit run; components not whole inputs', []), (8, 11, '123', '7123', False, False, None, 'format character inside digit run; components not whole inputs', [])], []),
        ('T255', 'Total 7\u200d123 shares.', 'own_grouping', [(6, 7, '7', '7123', False, False, None, 'format character inside digit run; components not whole inputs', []), (8, 11, '123', '7123', False, False, None, 'format character inside digit run; components not whole inputs', [])], []),
        ('T256', 'Total 7\u2060123 shares.', 'own_grouping', [(6, 7, '7', '7123', False, False, None, 'format character inside digit run; components not whole inputs', []), (8, 11, '123', '7123', False, False, None, 'format character inside digit run; components not whole inputs', [])], []),
        ('T257', 'Total 7\ufeff123 shares.', 'own_grouping', [(6, 7, '7', '7123', False, False, None, 'format character inside digit run; components not whole inputs', []), (8, 11, '123', '7123', False, False, None, 'format character inside digit run; components not whole inputs', [])], []),
        ('T258', 'Total 7\u2063123 shares.', 'own_grouping', [(6, 7, '7', '7123', False, False, None, 'format character inside digit run; components not whole inputs', []), (8, 11, '123', '7123', False, False, None, 'format character inside digit run; components not whole inputs', [])], []),
        ('T259', 'Total 7\u2064123 shares.', 'own_grouping', [(6, 7, '7', '7123', False, False, None, 'format character inside digit run; components not whole inputs', []), (8, 11, '123', '7123', False, False, None, 'format character inside digit run; components not whole inputs', [])], []),
        ('T260', 'Counsel filed 14 exhibits on May 8.', 'own_normal', [(14, 16, '14', '14', True, False, None, 'independent literal quantity or legal identifier', []), (33, 34, '8', '8', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T261', 'The trust paid $875.50 for 25 units.', 'own_normal', [(15, 22, '875.50', '875.50', True, False, None, 'independent literal quantity or legal identifier', []), (27, 29, '25', '25', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T262', 'The witness identified 6 accounts.', 'own_normal', [(23, 24, '6', '6', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T263', 'Order 7(a)(3) governs.', 'own_normal', [(6, 7, '7', '7', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T264', 'The parties stipulated to 18(d).', 'own_normal', [(26, 28, '18', '18', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T265', 'The exhibit lists 42 pages and 3 attachments.', 'own_normal', [(18, 20, '42', '42', True, False, None, 'independent literal quantity or legal identifier', []), (31, 32, '3', '3', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T266', 'The ratio changed from 12% to 15%.', 'own_normal', [(23, 26, '12', '12', True, False, None, 'independent literal quantity or legal identifier', []), (30, 33, '15', '15', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T267', 'The trust held $2 million and $7 million.', 'own_normal', [(15, 25, '2000000', '2000000', True, False, None, 'independent literal quantity or legal identifier', []), (30, 40, '7000000', '7000000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T268', 'During 2022, 80 shares were sold.', 'own_normal', [(7, 11, '2022', '2022', True, False, None, 'independent literal quantity or legal identifier', []), (13, 15, '80', '80', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T269', 'The account received 900 shares on 6/7/2022.', 'own_normal', [(21, 24, '900', '900', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T270', 'The order cites Rule 10b-5 and awards $700.', 'own_normal', [(38, 42, '700', '700', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T271', 'Records 7 through 9 describe the sale.', 'own_normal', [(8, 9, '7', '7', True, False, None, 'independent literal quantity or legal identifier', []), (18, 19, '9', '9', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T272', 'The clerk counted 11 folders; 4 were sealed.', 'own_normal', [(18, 20, '11', '11', True, False, None, 'independent literal quantity or legal identifier', []), (30, 31, '4', '4', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T273', '| Owner | 750 | shares |', 'own_normal', [(10, 13, '750', '750', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T274', 'The ledger shows 16 (kg) of goods.', 'own_normal', [(17, 19, '16', '16', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T275', 'Payment <w:r>42</w:r> dollars was made.', 'own_normal', [(13, 15, '42', '42', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T276', 'Shares <span style="color:blue">75</span> were sold.', 'own_normal', [(32, 34, '75', '75', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T277', 'There are 8 (USD) per unit.', 'own_normal', [(10, 11, '8', '8', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T278', '5 \xb6 1000', 'own_cost', [(0, 1, '5', '5', True, False, None, 'independent literal quantity or legal identifier', []), (4, 8, '1000', '1000', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T279', 'Items 1 * 2', 'own_cost', [(6, 7, '1', '1', True, False, None, 'independent literal quantity or legal identifier', []), (10, 11, '2', '2', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T280', '10 : 20', 'own_cost', [(0, 2, '10', '10', True, False, None, 'independent literal quantity or legal identifier', []), (5, 7, '20', '20', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T281', '5 / 6', 'own_cost', [(0, 1, '5', '5', True, False, None, 'independent literal quantity or legal identifier', []), (4, 5, '6', '6', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T282', '| item | 5 | 6 |', 'own_cost', [(9, 10, '5', '5', True, False, None, 'independent literal quantity or legal identifier', []), (13, 14, '6', '6', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T283', 'Rule 10b-5', 'own_cost', [(9, 10, '5', '5', False, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T284', 'Pages 5 - 6', 'own_cost', [(6, 7, '5', '5', True, False, None, 'independent literal quantity or legal identifier', []), (10, 11, '6', '6', True, False, None, 'independent literal quantity or legal identifier', [])], []),
        ('T285', "Price: 10.4 ('000)", 'review_break_residual', [(7, 11, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
        ('T286', 'The total of 10.4 (in 10^3) was booked.', 'review_break_residual', [(13, 17, '10.4', '10400', True, True, None, 'external magnitude changes selected coefficient', [])], []),
    ],
    'r11': [
        ('U001', 'Net debt was 1,234 (EUR million) at year end.', 'p2_scale', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U002', 'Net debt was 1,234 (EUR million)', 'p2_scale', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U003', 'Net debt was 1,234 (EUR billion).', 'p2_scale', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U004', 'Net debt was 1,234 (USD thousand).', 'p2_scale', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U005', 'Net debt was 1,234 (in USD thousand).', 'p2_scale', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U006', 'Net debt was 1,234 (in EUR million).', 'p2_scale', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U007', 'The fund holds 5 (CHF million) in notes.', 'p2_scale', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U008', 'The total is 10.4 (in million).', 'p2_scale', [(13, 17, '10.4', '10400000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U009', 'The total is 10.4 (in thousand) units.', 'p2_scale', [(13, 17, '10.4', '10400', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U010', '| Net debt | 1,234 | EUR million |', 'p2_scale', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U011', '| Net debt | 1,234 | USD thousand |', 'p2_scale', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U012', '<td>1,234</td><td>EUR million</td>', 'p2_scale', [(4, 9, '1234', '1234000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U013', '<p>Net debt 1,234 <i>(EUR million)</i></p>', 'p2_scale', [(12, 17, '1234', '1234000000', True, True, 'REFUSED', 'known singular scale modifies coefficient', [])], []),
        ('U014', 'Net debt was 1,234 (EUR m) at year end.', 'refuse_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U015', 'Net debt was 1,234 (USD k).', 'refuse_control', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U016', 'Net debt was 1,234 (GBP mn).', 'refuse_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U017', 'Net debt was 1,234 (million EUR).', 'refuse_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U018', 'Net debt was 1,234 (million).', 'refuse_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U019', 'Net debt was 1,234 (thousand).', 'refuse_control', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U020', '| Net debt | 1,234 | EUR mn |', 'refuse_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U021', 'The fund holds 5 (EUR bln) in notes.', 'refuse_control', [(15, 16, '5', '5000000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U022', 'The fund holds 5 (EUR Mio.) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U023', 'The fund holds 5 (EUR, mn) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U024', '| Net debt | 1,234 | \u20ac million |', 'refuse_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U025', 'The reserve was 10.4 \u2179 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U026', 'The reserve was 10.4 \U0001d465 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U027', 'The reserve was 10.4 \u2717 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U028', 'The reserve was 10.4 &#10007; 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U029', 'The reserve was 10.4 \xd7\xa0\xa01,000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U030', 'The reserve was 10.4 \xd7 ten to the third.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U031', 'The reserve was 10.4 \xd7 $1,000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U032', 'The reserve was 10.4 x $1,000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U033', 'The reserve was 10.4 x -1000.', 'refuse_control', [(16, 20, '10.4', '-10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U034', 'The reserve was 10.4 x (1000).', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U035', 'The reserve was 10.4 x \u2212 1000.', 'refuse_control', [(16, 20, '10.4', '-10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U036', 'The reserve was 10.4 \u2715\u2009103.', 'refuse_control', [(16, 20, '10.4', '5356/5', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U037', 'The reserve was 10.4 x 10<sup>3</sup>.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U038', 'The reserve was 10.4 * 10e3.', 'refuse_control', [(16, 20, '10.4', '104000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U039', 'The reserve was 10.4 \u2219\u2219 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U040', 'The reserve was 10.4 x x 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U041', 'The reserve was 10.4 \u2715 \u20ac1,000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U042', 'The reserve was 10.4 x \u20ac 1,000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U043', 'The reserve was 10.4 x one thousand.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U044', 'The reserve was 10.4 \xd7\u200b1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U045', 'The reserve was 10.4 \xd7\u200e1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U046', 'The reserve was 10.4 x\u2060 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U047', 'The reserve was 10.4 \xd7\xad1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U048', 'The reserve was 10.4 \u2715\u200b10^3.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U049', 'The reserve was 10.4 x<br>1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U050', 'The reserve was 10.4 \\times10^{3}.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U051', 'The reserve was 10.4 \xb710\xb3.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U052', 'The reserve was 10.4 [x 10^3].', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U053', 'The reserve was 10.4 10^3.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U054', 'The reserve was 10.4\u200e \xd7 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U055', 'Reserves: 5, 6, and 7 \xd7 10^6.', 'refuse_control', [(10, 11, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U056', 'Reserves: 5; 6 and 7 million.', 'refuse_control', [(13, 14, '6', '6000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U057', '5 & 6 million', 'refuse_control', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U058', '5 and/or 6 million', 'refuse_control', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U059', '5 or 6 MM', 'refuse_control', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U060', '5 \u2014 or 6 million', 'refuse_control', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U061', '5 (or 6) million', 'refuse_control', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U062', '5 and 6 \u2715 1,000', 'refuse_control', [(0, 1, '5', '5000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U063', '5 \u2013 6 mn', 'refuse_control', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U064', '5 \u2026 6 million', 'refuse_control', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U065', 'Reserves: 5\u2019000.', 'grouping', [(10, 15, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (10, 11, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (12, 15, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U066', 'Reserves: CHF 5\u2019000\u2019000.', 'grouping', [(14, 23, '5000000', '5000000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (14, 15, '5', '5000000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (16, 19, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (20, 23, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U067', "Reserves: 5'000 shares.", 'grouping', [(10, 15, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (10, 11, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (12, 15, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U068', 'Reserves: 5\u02bc000.', 'grouping', [(10, 15, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (10, 11, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (12, 15, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U069', '5\xa0000\xa0000', 'grouping', [(0, 9, '5000000', '5000000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (0, 1, '5', '5000000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (2, 5, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (6, 9, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U070', '5\u202f000', 'grouping', [(0, 5, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (0, 1, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (2, 5, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U071', '5\u2009000', 'grouping', [(0, 5, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (0, 1, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (2, 5, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U072', '5\xb7000', 'grouping', [(0, 5, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (0, 1, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (2, 5, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U073', 'Total: 5&#8203;000 shares.', 'grouping', [(7, 18, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (7, 8, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (15, 18, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U074', 'Reserves: 5\u201d000.', 'grouping', [(10, 15, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (10, 11, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (12, 15, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U075', 'Reserves: 5\u2018000.', 'grouping', [(10, 15, '5000', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (10, 11, '5', '5000', False, False, 'REFUSED', 'unsupported grouped numeral or component', []), (12, 15, '0', '0', False, False, 'REFUSED', 'unsupported grouped numeral or component', [])], []),
        ('U076', 'The fund holds 5 (EUR bln) in notes.', 'refuse_control', [(15, 16, '5', '5000000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U077', 'The fund holds 5 (EUR Mio.) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U078', 'The fund holds 5 (EUR, mn) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U079', 'The fund holds 5 (all in m) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U080', 'The fund holds 5 {m} in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U081', 'The fund holds 5 <m> in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U082', 'The fund holds 5 &lt;m&gt; in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U083', 'The fund holds 5 (\xa0m\xa0) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U084', 'The fund holds 5 (\u200bm) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U085', 'The fund holds 5 (M\u0332) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U086', 'The fund holds 5 \u200e(m) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U087', 'The fund holds 5 \u207d\u1d50\u207e in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U088', 'The fund holds 5 \xa0\xa0(m) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U089', 'The fund holds 5 (mm) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U090', 'The fund holds 5 (m)\u2014net in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U091', 'The fund holds 5 (Cr) in notes.', 'refuse_control', [(15, 16, '5', '50000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U092', 'The fund holds 5 (tsd) in notes.', 'refuse_control', [(15, 16, '5', '5000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U093', 'The fund holds 5 (Mrd.) in notes.', 'refuse_control', [(15, 16, '5', '5000000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U094', 'The fund holds 5 (million EUR) in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U095', 'The fund holds 5 | \u20acm | in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U096', 'The fund holds 5 | \u20ac million | in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U097', 'The fund holds 5 | \xa3000 | in notes.', 'refuse_control', [(15, 16, '5', '5000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U098', 'The fund holds 5 | EUR mn | in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U099', 'The fund holds 5 | 5 | (m) | in notes.', 'refuse_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U100', 'The reserve was 10.4 <span class="x">\xd7</span>10<span>3</span>.', 'refuse_control', [(16, 20, '10.4', '5356/5', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U101', 'The reserve was 10.4 <o:p>&nbsp;</o:p>x<o:p></o:p>1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U102', 'The reserve was 10.4 <w:t> m</w:t>.', 'refuse_control', [(16, 20, '10.4', '10400000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U103', 'The reserve was 10.4 <!--[endif]--> x 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U104', 'The reserve was 10.4 <![CDATA[x]]> 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U105', 'The reserve was 10.4 <i>times</i> 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U106', 'The reserve was 10.4 <b>m</b>.', 'refuse_control', [(16, 20, '10.4', '10400000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U107', 'The reserve was 10.4 (<b>m</b>).', 'refuse_control', [(16, 20, '10.4', '10400000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U108', 'The reserve was 10.4 &#40;m&#41;.', 'refuse_control', [(16, 20, '10.4', '10400000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U109', 'The reserve was 10.4 &amp;times; 1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U110', 'The reserve was 10.4 <st1:metricconverter ...>x 1000</st1:metricconverter>.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U111', 'The reserve was 10.4 <span>\u2716</span>1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U112', 'The reserve was 10.4 &times;&#8203;1000.', 'refuse_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U113', 'The reserve was 10.4<b>m</b>.', 'refuse_control', [(16, 20, '10.4', '10400000', True, True, 'REFUSED', 'review refusal control', [])], []),
        ('U114', 'Section 5(m) of the Act applies to 300 shares.', 'refuse_control', [(8, 9, '5', '5', True, False, 'REFUSED', 'review refusal control', [])], []),
        ('U115', 'Section 5(m) of the Act applies to 300 shares.', 'normal', [(35, 38, '300', '300', True, False, 'BOUND 300', 'ordinary independent quantity', [])], []),
        ('U116', 'Smith paid $1,250 on 3/4/2020 for 50 shares.', 'normal', [(11, 17, '1250', '1250', True, False, 'BOUND 1250', 'ordinary independent quantity', []), (34, 36, '50', '50', True, False, 'BOUND 50', 'ordinary independent quantity', [])], []),
        ('U117', 'Smith sold 1,000 shares - 10% of the class.', 'normal', [(11, 16, '1000', '1000', True, False, 'BOUND 1000', 'ordinary independent quantity', [])], []),
        ('U118', 'Smith sold 1,000 shares + 200 options.', 'normal', [(11, 16, '1000', '1000', True, False, 'BOUND 1000', 'ordinary independent quantity', [])], []),
        ('U119', 'The vote was 7-2 in favor; 3 abstained.', 'normal', [(27, 28, '3', '3', True, False, 'BOUND 3', 'ordinary independent quantity', [])], []),
        ('U120', 'Smith owns 40% (4,000 of 10,000 shares).', 'normal', [(11, 14, '40', '40', True, False, 'BOUND 40', 'ordinary independent quantity', [])], []),
        ('U121', 'Smith bought 500 shares @ $12.50.', 'normal', [(13, 16, '500', '500', True, False, 'BOUND 500', 'ordinary independent quantity', []), (26, 32, '25/2', '25/2', True, False, 'BOUND 25/2', 'ordinary independent quantity', [])], []),
        ('U122', 'Item 7. 100 shares were sold.', 'normal', [(5, 6, '7', '7', True, False, 'BOUND 7', 'ordinary independent quantity', []), (8, 11, '100', '100', True, False, 'BOUND 100', 'ordinary independent quantity', [])], []),
        ('U123', 'The fee was $250 / hour.', 'normal', [(12, 16, '250', '250', True, False, 'BOUND 250', 'ordinary independent quantity', [])], []),
        ('U124', 'Account no. 12 held 300 shares.', 'normal', [(12, 14, '12', '12', True, False, 'BOUND 12', 'ordinary independent quantity', []), (20, 23, '300', '300', True, False, 'BOUND 300', 'ordinary independent quantity', [])], []),
        ('U125', 'See Ex. 4 \xb6 12 for 300 shares.', 'normal', [(12, 14, '12', '12', True, False, 'BOUND 12', 'ordinary independent quantity', [])], []),
        ('U126', 'See \xa7 5 for 300 shares.', 'normal', [(6, 7, '5', '5', True, False, 'BOUND 5', 'ordinary independent quantity', []), (12, 15, '300', '300', True, False, 'BOUND 300', 'ordinary independent quantity', [])], []),
        ('U127', 'He was 25 years old; she was 30.', 'normal', [(7, 9, '25', '25', True, False, 'BOUND 25', 'ordinary independent quantity', []), (29, 31, '30', '30', True, False, 'BOUND 30', 'ordinary independent quantity', [])], []),
        ('U128', 'Rule 12(b)(6) applies to 3 counts.', 'normal', [(5, 7, '12', '12', True, False, 'BOUND 12', 'ordinary independent quantity', []), (25, 26, '3', '3', True, False, 'BOUND 3', 'ordinary independent quantity', [])], []),
        ('U129', 'The plan was a 401(k).', 'normal', [(15, 18, '401', '401', True, False, 'BOUND 401', 'ordinary independent quantity', [])], []),
        ('U130', 'On May 2, 2019, Jones wired $45,000 to Smith.', 'normal', [(7, 8, '2', '2', True, False, 'BOUND 2', 'ordinary independent quantity', []), (10, 14, '2019', '2019', True, False, 'BOUND 2019', 'ordinary independent quantity', []), (28, 35, '45000', '45000', True, False, 'BOUND 45000', 'ordinary independent quantity', [])], []),
        ('U131', 'Smith held 12.5% of the class as of 2020.', 'normal', [(11, 16, '25/2', '25/2', True, False, 'BOUND 25/2', 'ordinary independent quantity', []), (36, 40, '2020', '2020', True, False, 'BOUND 2020', 'ordinary independent quantity', [])], []),
        ('U132', 'The trust sold 2,500 shares for $1.2 million.', 'normal', [(15, 20, '2500', '2500', True, False, 'BOUND 2500', 'ordinary independent quantity', []), (32, 44, '1200000', '1200000', True, False, 'BOUND 1200000', 'ordinary independent quantity', [])], []),
        ('U133', 'Exhibit 3 & 4 show 200 trades.', 'normal', [(8, 9, '3', '3', True, False, 'BOUND 3', 'ordinary independent quantity', []), (19, 22, '200', '200', True, False, 'BOUND 200', 'ordinary independent quantity', [])], []),
        ('U134', 'The price was $12.50 (per share) on 300 shares.', 'normal', [(14, 20, '25/2', '25/2', True, False, 'BOUND 25/2', 'ordinary independent quantity', []), (36, 39, '300', '300', True, False, 'BOUND 300', 'ordinary independent quantity', [])], []),
        ('U135', 'He paid 300 (three hundred) dollars.', 'normal', [(8, 11, '300', '300', True, False, 'BOUND 300', 'ordinary independent quantity', [])], []),
        ('U136', 'Smith (age 45) sold 1,000 shares.', 'normal', [(11, 13, '45', '45', True, False, 'BOUND 45', 'ordinary independent quantity', []), (20, 25, '1000', '1000', True, False, 'BOUND 1000', 'ordinary independent quantity', [])], []),
        ('U137', 'The defendant made 3 trips (Jan., Feb. and Mar.).', 'normal', [(19, 20, '3', '3', True, False, 'BOUND 3', 'ordinary independent quantity', [])], []),
        ('U138', 'The reserve was 10.4 \u03c7 1000.', 'homoglyph', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'letter operator multiplies coefficient', [])], []),
        ('U139', 'The reserve was 10.4 x\u0304 1000.', 'homoglyph', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'letter operator multiplies coefficient', [])], []),
        ('U140', 'The reserve was 10.4 \u0445 1000.', 'homoglyph', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'letter operator multiplies coefficient', [])], []),
        ('U141', 'The reserve was 10.4 \u0425 1000.', 'homoglyph', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'letter operator multiplies coefficient', [])], []),
        ('U142', 'Net debt was 1,234 EUR million.', 'disclosed', [(13, 18, '1234', '1234000000', True, True, 'BOUND 1234', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U143', 'Net debt was 1,234 in EUR million.', 'disclosed', [(13, 18, '1234', '1234000000', True, True, 'BOUND 1234', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U144', 'Net debt was 1,234 (USD millions) at year end.', 'disclosed', [(13, 18, '1234', '1234000000', True, True, 'BOUND 1234', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U145', 'Net debt was 1,234 (EUR in millions).', 'disclosed', [(13, 18, '1234', '1234000000', True, True, 'BOUND 1234', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U146', 'Net debt was 1,234 (millions of EUR).', 'disclosed', [(13, 18, '1234', '1234000000', True, True, 'BOUND 1234', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U147', 'The fund holds 5 (amounts in m) in notes.', 'disclosed', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'meaning exceeds local reader scope', [])], []),
        ('U148', "| Net debt | 1,234 | EUR '000 |", 'disclosed', [(13, 18, '1234', '1234000', True, True, 'BOUND 1234', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U149', '| Net debt | 1,234 | EUR000 |', 'disclosed', [(13, 18, '1234', '1234000', True, True, 'BOUND 1234', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U150', 'The reserve was 10.4 into 1000.', 'disclosed', [(16, 20, '52/5', '10400', True, True, 'BOUND 52/5', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U151', 'The reserve was 10.4 mal 1000.', 'disclosed', [(16, 20, '52/5', '10400', True, True, 'BOUND 52/5', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U152', 'Total 10.4 <span hidden>none</span> x 1000', 'disclosed', [(6, 10, '52/5', '10400', True, True, 'BOUND 52/5', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U153', 'The fund holds 5(M), net.', 'disclosed', [(15, 16, '5', '5000000', True, True, 'BOUND 5', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U154', 'The fund holds 5(m)', 'disclosed', [(15, 16, '5', '5000000', True, True, 'BOUND 5', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
        ('U155', '| Amount (m) | 5 |', 'disclosed', [(15, 16, '5', '5000000', True, True, 'BOUND 5', 'disclosed exposure: meaning exceeds local reader scope', [])], []),
    ],
    'r12': [
        ('V001', 'Net debt was 1,234 thou\xadsand.', 'p2_interruption', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V002', 'Net debt was 1,234 thou&shy;sand.', 'p2_interruption', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V003', 'Net debt was 1,234 bil\xadlion.', 'p2_interruption', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V004', 'Net debt was 1,234 bil&shy;lion.', 'p2_interruption', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V005', '<p>The account held 5 bil&#173;lion dollars.</p>', 'p2_interruption', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V006', '<p>The account held 5 bil&#xAD;lion dollars.</p>', 'p2_interruption', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V007', 'Net debt was 1,234 hun\xaddred.', 'p2_interruption', [(13, 18, '1234', '123400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V008', 'Net debt was 1,234 tril\xadlion.', 'p2_interruption', [(13, 18, '1234', '1234000000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V009', 'Net debt was 1,234 mi\xadllion.', 'p2_interruption', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V010', 'The account held $5 bil\xadlion in cash.', 'p2_interruption', [(17, 19, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V011', 'Net debt was 1,234 thou\u200bsand.', 'p2_interruption', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V012', 'Net debt was 1,234 bil\u2060lion.', 'p2_interruption', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V013', 'The fund holds 5 thou\u200dsand dollars.', 'p2_interruption', [(15, 16, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V014', 'The account held 5 thou\ufeffsand.', 'p2_interruption', [(17, 18, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V015', 'The account held 5 bil\u200elion.', 'p2_interruption', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V016', 'The account held 5 cr\xadore.', 'p2_interruption', [(17, 18, '5', '50000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V017', 'Reserves were 5, 6 and 7 thou\xadsand.', 'p2_interruption', [(14, 15, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V018', 'Net debt was 1,234 (EUR mi\xadllion).', 'p2_interruption', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V019', 'Net debt was 1,234 (EUR mi\u200bllion).', 'p2_interruption', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V020', '| Net debt | 1,234 | EUR mi\xadllion |', 'p2_interruption', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V021', 'The reserve was 10.4 ti\xadmes 1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V022', 'The reserve was 10.4 mul\xadtiplied by 1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V023', 'The account held 5 bil-\nlion dollars.', 'p2_interruption', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V024', 'The account held 5 thou-\nsand dollars.', 'p2_interruption', [(17, 18, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V025', 'The account held 5 bil- lion dollars.', 'p2_interruption', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V026', 'The reserve was 10.4 \u2716\ufe0f 1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V027', 'The reserve was 10.4 \u2716\ufe0f1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V028', 'The reserve was 10.4\u2716\ufe0f1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V029', 'The reserve was 10.4 &#x2716;&#xFE0F; 1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V030', 'The reserve was 10.4 \u2716\ufe0f $1,000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V031', 'The reserve was 5 \u2716\ufe0f 1,000,000.', 'p2_interruption', [(16, 17, '5', '5000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V032', 'The reserve was 10.4 \u2715\ufe0f 1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V033', 'The reserve was 10.4 \u2716\u200d 1000.', 'p2_interruption', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V034', 'The reserve was 10.4 \u2797\ufe0f 1000.', 'p2_interruption', [(16, 20, '10.4', '13/1250', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V035', 'Net debt was 1,234 mil\xadlion.', 'p2_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V036', 'The fund holds 5 mil\u200clion dollars.', 'p2_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V037', 'The account held 5 mil-\nlion dollars.', 'p2_control', [(17, 18, '5', '5000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V038', 'Net debt was 1,234 thou<wbr>sand.', 'p2_control', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V039', 'Net debt was 1,234 bil<span>lion</span>.', 'p2_control', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V040', 'Net debt was 1,234 (EUR bil\xadlion).', 'p2_control', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V041', 'The reserve was 10.4 \u2716 1000.', 'p2_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V042', 'The reserve was 10.4 \xd7\ufe0f 1000.', 'p2_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V043', 'The reserve was 10.4 *\ufe0f 1000.', 'p2_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V044', 'The reserve was 10.4 x\ufe0f 1000.', 'p2_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V045', 'The reserve was 10.4 \u2716\ufe0f 10^3.', 'p2_control', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V046', 'The fund holds 5\u200b million.', 'p2_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V047', 'The fund holds 5\xadmillion.', 'p2_control', [(15, 16, '5', '5000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V048', 'The reserve was 10.4 \uab53 1000.', 'p3_lookalike', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V049', 'The reserve was 10.4 \ua7b3 1000.', 'p3_lookalike', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V050', 'The reserve was 10.4 \uab57 1000.', 'p3_lookalike', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V051', 'The reserve was 10.4 \uab59 1000.', 'p3_lookalike', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V052', 'The reserve was 10.4 \u0445\u200d 1000.', 'p3_lookalike', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V053', 'The reserve was 10.4% \u0445 1000.', 'p3_lookalike', [(16, 21, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V054', 'The reserve was [10.4] \u0445 1000.', 'p3_lookalike', [(17, 21, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V055', 'The reserve was 10.4 (\u0445 1000).', 'p3_lookalike', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V056', 'Reserves: 5\xb4000 shares.', 'p3_separator', [(10, 11, '5', '5000', True, True, 'REFUSED', 'grouped whole value 5000; coefficient must not bind literally', []), (12, 15, '0', '0', False, False, 'REFUSED', 'component of unsupported grouped numeral, not independent zero', [])], []),
        ('V057', 'Reserves: 5`000 shares.', 'p3_separator', [(10, 11, '5', '5000', True, True, 'REFUSED', 'grouped whole value 5000; coefficient must not bind literally', []), (12, 15, '0', '0', False, False, 'REFUSED', 'component of unsupported grouped numeral, not independent zero', [])], []),
        ('V058', 'Reserves: 5\u02d9000 shares.', 'p3_separator', [(10, 11, '5', '5000', True, True, 'REFUSED', 'grouped whole value 5000; coefficient must not bind literally', []), (12, 15, '0', '0', False, False, 'REFUSED', 'component of unsupported grouped numeral, not independent zero', [])], []),
        ('V059', 'Smith owned 3\u20444 of the lots.', 'p3_separator', [(14, 15, '4', '4', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', [])], []),
        ('V060', 'Smith owned 1\u20442 of the lots.', 'p3_separator', [(14, 15, '2', '2', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', [])], []),
        ('V061', 'Total: 5\u2215000 shares.', 'p3_separator', [(9, 12, '0', '0', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', [])], []),
        ('V062', 'Total: \u202e21\u202c shares.', 'p3_bidi', [(8, 10, '21', '12', True, False, 'REFUSED', 'override displays 12 while stored digits spell 21; refuse display ambiguity', [])], []),
        ('V063', 'The reserve was 10.4 \u1e8b 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V064', 'The reserve was 10.4 \u1d61 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V065', 'The reserve was 10.4 \u04b3 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V066', 'The reserve was 10.4 \u2cad 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V067', 'The reserve was 10.4 \u157d 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V068', 'The reserve was 10.4 \U0001d6a1 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V069', 'The reserve was 10.4 \u2179 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V070', 'The reserve was 10.4 \u03c7 10^3.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V071', 'The reserve was 10.4 \u03c7 10\xb3.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V072', 'The reserve was 10.4 \u03c7\xa01000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V073', 'The reserve was $10.4 \u0445 $1,000.', 'refusal_operator', [(16, 21, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V074', 'The reserve was 10.4 \u2a09 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V075', 'The reserve was 10.4 \u2573 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V076', 'The reserve was 10.4 \u02df 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V077', 'The reserve was 10.4 \u166d 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V078', 'The reserve was 10.4 \U0001f7ae 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V079', 'The reserve was 10.4 &#x2A2F; 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V080', 'The reserve was 10.4 &Cross; 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V081', 'The reserve was 10.4 &times;&nbsp;10<sup>3</sup>.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V082', "The reserve was 10.4 <span style='x'>&#215;</span> 1000.", 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V083', 'The reserve was 10.4 \xd7\ufe0e 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V084', 'The reserve was 10.4 \xd7\u200d 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V085', 'The reserve was 10.4 \xd7\u200b1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V086', 'The reserve was 10.4 \xd7\u0301 1000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V087', 'The reserve was 10.4 X 1,000.', 'refusal_operator', [(16, 20, '10.4', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V088', 'Total: 5 \u2716\ufe0f 3 = 15 shares.', 'refusal_operator', [(7, 8, '5', '15', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V089', 'Net debt was 1,234 (EUR Million).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V090', 'Net debt was 1,234 (EUR MILLION).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V091', 'Net debt was 1,234 [EUR million].', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V092', 'Net debt was 1,234 \uff08EUR million\uff09.', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V093', 'Net debt was 1,234 (EUR&nbsp;million).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V094', 'Net debt was 1,234 (EUR <b>million</b>).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V095', 'Net debt was 1,234 (amounts in EUR million, unaudited).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V096', 'Net debt was 1,234 (EUR million; restated).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V097', 'Net debt was 1,234 (EUR million) (2023).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V098', 'Net debt was 1,234 (INR crore).', 'refusal_bracket', [(13, 18, '1234', '12340000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V099', 'Net debt was 1,234 (Rs. lakh).', 'refusal_bracket', [(13, 18, '1234', '123400000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V100', 'Net debt was 1,234 (EUR hundred).', 'refusal_bracket', [(13, 18, '1234', '123400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V101', 'Net debt was 1,234 (EUR-million).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V102', 'Net debt was 1,234 (EUR/million).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V103', 'Net debt was 1,234 | (EUR million) |.', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V104', 'Net debt was 1,234 |  | EUR million |.', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V105', 'Net debt was 1,234 <th>EUR million</th>.', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V106', 'Net debt was 1,234 <td><span>EUR</span> <b>million</b></td>.', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V107', 'Net debt was 1,234 (EUR\u200bmillion).', 'refusal_bracket', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V108', '5(q)', 'refusal_marker', [(0, 1, '5', '5', False, False, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V109', '5 (zl)', 'refusal_marker', [(0, 1, '5', '5', False, False, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V110', '5 (Tsd.)', 'refusal_marker', [(0, 1, '5', '5000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V111', '5 (TEUR)', 'refusal_marker', [(0, 1, '5', '5', False, False, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V112', '5 (kEUR)', 'refusal_marker', [(0, 1, '5', '5000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V113', '5 (MEUR)', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V114', '5 | TEUR |', 'refusal_marker', [(0, 1, '5', '5', False, False, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V115', '5 | kUSD |', 'refusal_marker', [(0, 1, '5', '5000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V116', '5 (\u20acm)', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V117', '5(m)\u2014net', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V118', '5(m) net', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V119', '5(m)/yr', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V120', '5(b) and 6(c)', 'refusal_marker', [(0, 1, '5', '5', False, False, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V121', '5, 6, or 7 billion', 'refusal_marker', [(0, 1, '5', '5000000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V122', '5 to 7 billion', 'refusal_marker', [(0, 1, '5', '5000000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V123', '5 and 6 crore', 'refusal_marker', [(0, 1, '5', '50000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V124', '5 and 6 lakh', 'refusal_marker', [(0, 1, '5', '500000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V125', '5 and/or 6 bn', 'refusal_marker', [(0, 1, '5', '5000000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V126', '5 (and 6) million', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V127', '5 \u2014 and 6 \u2014 million', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V128', '5 or 6 MM', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V129', '5 or 6 Mio.', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V130', '5 (000s)', 'refusal_marker', [(0, 1, '5', '5000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V131', '5 ($ million)', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V132', '5 ($M)', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V133', '5 ($MM)', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V134', '5 m USD', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V135', '5 Mn USD', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V136', '5 mn', 'refusal_marker', [(0, 1, '5', '5000000', True, True, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', [])], []),
        ('V137', '5\u2032000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V138', '5\u2019000\u2019000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (6, 9, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V139', 'CHF 1\u2019234.50', 'refusal_grouping', [(4, 5, '1', '1', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (6, 12, '234.50', '234.50', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V140', '5\u2e31000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V141', '5\u05f3000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V142', '5\xa0000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V143', '5\u2007000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V144', '5\u3000000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V145', '5_000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V146', '5~000', 'refusal_grouping', [(0, 1, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (2, 5, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V147', "Smith's 5'000 shares.", 'refusal_grouping', [(8, 9, '5', '5', False, False, 'REFUSED', 'unsupported grouped numeral component', []), (10, 13, '000', '000', False, False, 'REFUSED', 'unsupported grouped numeral component', [])], []),
        ('V148', '5&#48;', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V149', '&#53;00', 'refusal_offsets', [(5, 7, '00', '00', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V150', '5<b></b>0', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V151', '5\uff10', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V152', '5\u2080', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V153', '5\xb2', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V154', '5&sup2;', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V155', '5&#x2c;000', 'refusal_offsets', [(0, 10, '5000', '5000', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V156', '1&#46;5', 'refusal_offsets', [(0, 7, '1.5', '1.5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V157', '5&nbsp;million', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', []), (0, 14, '5000000', '5000000', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V158', '5<span> million</span>', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', []), (0, 22, '5000000', '5000000', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V159', '$<b>5</b>', 'refusal_offsets', [(4, 5, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V160', '-<b>5</b>', 'refusal_offsets', [(4, 5, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V161', '(<b>5</b>)', 'refusal_offsets', [(4, 5, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V162', '\u2212<i>5</i>', 'refusal_offsets', [(4, 5, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V163', '5<i>.</i>5', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V164', '5<i>,</i>000', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V165', '5<!---->,000', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V166', '5<i></i>000', 'refusal_offsets', [(8, 11, '000', '000', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V167', '5\ufb01', 'refusal_offsets', [(0, 1, '5', '5', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V168', '1\ufe50000', 'refusal_offsets', [(0, 1, '1', '1', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V169', '1\uff0c000', 'refusal_offsets', [(0, 1, '1', '1', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V170', '12\u20245', 'refusal_offsets', [(0, 2, '12', '12', False, False, 'REFUSED', 'raw span is not exactly one contract numeric token in rendered context', [])], []),
        ('V171', 'The well-\nknown 5 shares were sold.', 'normal', [(16, 17, '5', '5', True, False, 'BOUND 5', 'ordinary independent count; letter rejoin must not join digits', [])], []),
        ('V172', 'Smith sold 5 shares to a long-\ntime friend.', 'normal', [(11, 12, '5', '5', True, False, 'BOUND 5', 'ordinary independent count; letter rejoin must not join digits', [])], []),
        ('V173', 'Smith sold 5 well- known shares.', 'normal', [(11, 12, '5', '5', True, False, 'BOUND 5', 'ordinary independent count; letter rejoin must not join digits', [])], []),
        ('V174', '5 files - 10 files', 'normal', [(0, 1, '5', '5', True, False, 'BOUND 5', 'ordinary independent count; letter rejoin must not join digits', [])], []),
        ('V175', '- 5 files', 'contract_sign_control', [(2, 3, '5', '-5', False, False, 'REFUSED', 'bare component after detached sign is not a whole contract token', [])], []),
        ('V176', '5 files with ordinary\tspacing.', 'normal', [(0, 1, '5', '5', True, False, 'BOUND 5', 'ordinary independent count; letter rejoin must not join digits', [])], []),
        ('V177', 'The account held 5 bil-<br>lion dollars.', 'gate_extension', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V178', 'The account held 5 bil\u2010\nlion dollars.', 'gate_extension', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', [])], []),
        ('V179', 'The fund holds 5(q) in notes.', 'refusal_marker', [(15, 16, '5', '5', False, False, 'REFUSED', 'unknown code followed by content has no subsection ownership', [])], []),
    ],
    'r13': [
        ('W01', 'The account held 5 bil\xad\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'soft hyphen + LF', ['W01', 'Q001'])], ['W01', 'Q001']),
        ('W02', 'Net debt was 1,234 thou\xad\nsand.', 'diagnosis_R', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'soft hyphen + LF', ['W02', 'Q008'])], ['W02', 'Q008']),
        ('W03', 'Revenue of 3.5 bil\xad\r\nlion was booked.', 'diagnosis_R', [(11, 14, '7/2', '3500000000', True, True, 'REFUSED', 'soft hyphen + CRLF', ['W03'])], ['W03']),
        ('W04', 'Revenue of 3.5 bil\xad lion was booked.', 'diagnosis_R', [(11, 14, '7/2', '3500000000', True, True, 'REFUSED', 'soft hyphen + space', ['W04'])], ['W04']),
        ('W05', '<p>Revenue of 3.5 bil&shy;<br/>lion was booked.</p>', 'diagnosis_R', [(14, 17, '7/2', '3500000000', True, True, 'REFUSED', '&shy; + br', ['W05'])], ['W05']),
        ('W06', '<p>Revenue of 3.5 bil&#173;\nlion was booked.</p>', 'diagnosis_R', [(14, 17, '7/2', '3500000000', True, True, 'REFUSED', '&#173; + LF', ['W06'])], ['W06']),
        ('W07', '<div>Revenue of 3.5 bil\xad</div>\n<div>lion was booked.</div>', 'diagnosis_R', [(16, 19, '7/2', '3500000000', True, True, 'REFUSED', 'soft hyphen + div', ['W07'])], ['W07']),
        ('W08', 'The loss was 7 hun\xad\ndred dollars.', 'diagnosis_R', [(13, 14, '7', '700', True, True, 'REFUSED', 'soft hyphen + LF, hundred', ['W08'])], ['W08']),
        ('W09', 'The reserve was 10.4 mul\xad\ntiplied by 1000.', 'diagnosis_R', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'soft hyphen + LF, operator', ['W09', 'Q013'])], ['W09', 'Q013']),
        ('W10', 'Payments of 2 tril\xad\nlion yen were made.', 'diagnosis_R', [(12, 13, '2', '2000000000000', True, True, 'REFUSED', 'soft hyphen + LF, trillion', ['W10'])], ['W10']),
        ('W11', 'The account held 5 bil\xad\x0clion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'soft hyphen + FF', ['W11', 'Q007'])], ['W11', 'Q007']),
        ('W12', 'THE PURCHASE PRICE IS $5 BIL-\nLION.', 'diagnosis_R', [(22, 24, '5', '5000000000', True, True, 'REFUSED', 'UPPER Pd + LF', ['W12', 'Q019'])], ['W12', 'Q019']),
        ('W13', 'THE SELLER SHALL PAY 250 THOU-\nSAND DOLLARS.', 'diagnosis_R', [(21, 24, '250', '250000', True, True, 'REFUSED', 'UPPER Pd + LF', ['W13'])], ['W13']),
        ('W14', 'TOTAL CONSIDERATION OF 1.2 BIL\xad\nLION DOLLARS.', 'diagnosis_R', [(23, 26, '6/5', '1200000000', True, True, 'REFUSED', 'UPPER soft hyphen + LF', ['W14'])], ['W14']),
        ('W15', 'THE FEE IS 3 TI-\nMES 1,000.', 'diagnosis_R', [(11, 12, '3', '3000', True, True, 'REFUSED', 'UPPER operator', ['W15'])], ['W15']),
        ('W16', 'Reserves: 5 or 6 THOU-\nSAND.', 'diagnosis_R', [(10, 11, '5', '5000', True, True, 'REFUSED', 'UPPER coordination', ['W16', 'Q020'])], ['W16', 'Q020']),
        ('W17', 'The Account Held 5 Bil-\nlion Dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Title case, lower continuation', ['W17'])], ['W17']),
        ('W18', 'The account held 5 bil-\nLion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'OCR case slip (lower then Upper)', ['W18'])], ['W18']),
        ('W19', '<div>The account held 5 bil-</div><div>lion dollars.</div>', 'diagnosis_R', [(22, 23, '5', '5000000000', True, True, 'REFUSED', 'div / div', ['W19', 'Q021'])], ['W19', 'Q021']),
        ('W20', '<p>The account held 5 bil-</p>\n<p>lion dollars.</p>', 'diagnosis_R', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'p / p + LF', ['W20', 'Q022'])], ['W20', 'Q022']),
        ('W21', '<span>The account held 5 bil-</span><span>lion dollars.</span>', 'diagnosis_R', [(23, 24, '5', '5000000000', True, True, 'REFUSED', 'span / span', ['W21', 'Q023'])], ['W21', 'Q023']),
        ('W22', '<div class="t m0 x1 h2 y3">Revenue of 3.5 bil-</div><div class="t m0 x1 h2 y4">lion was booked.</div>', 'diagnosis_R', [(38, 41, '7/2', '3500000000', True, True, 'REFUSED', 'pdf2htmlEX-style divs', ['W22'])], ['W22']),
        ('W23', '<p style="top:120px">Net debt of 1,234 thou-</p><p style="top:134px">sand.</p>', 'diagnosis_R', [(33, 38, '1234', '1234000', True, True, 'REFUSED', 'p with style', ['W23'])], ['W23']),
        ('W24', '<font size=2>Net debt of 1,234 thou-</font>\n<font size=2>sand.</font>', 'diagnosis_R', [(25, 30, '1234', '1234000', True, True, 'REFUSED', 'font / font (Word HTML)', ['W24'])], ['W24']),
        ('W25', 'Net debt of 1,234 thou-<!-- p4 -->sand.', 'diagnosis_R', [(12, 17, '1234', '1234000', True, True, 'REFUSED', 'HTML comment between halves', ['W25'])], ['W25']),
        ('W26', '<span>The account held 5 bil-<br></span><span>lion dollars.</span>', 'diagnosis_R', [(23, 24, '5', '5000000000', True, True, 'REFUSED', 'span + br', ['W26'])], ['W26']),
        ('W27', 'The account held 5 bil-lion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Pd, no break (reflow)', ['W27', 'Q024'])], ['W27', 'Q024']),
        ('W28', 'The account held 5 thou-sand dollars.', 'diagnosis_R', [(17, 18, '5', '5000', True, True, 'REFUSED', 'Pd, no break (reflow)', ['W28', 'Q025'])], ['W28', 'Q025']),
        ('W29', 'The reserve was 10.4 multi-plied by 1000.', 'diagnosis_R', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'Pd, no break, operator', ['W29'])], ['W29']),
        ('W30', 'NET DEBT WAS 1,234 THOU-SAND.', 'diagnosis_R', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'Pd, no break, UPPER', ['W30'])], ['W30']),
        ('W31', 'The account held 5 bil- \nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Pd + space + LF (OCR)', ['W31'])], ['W31']),
        ('W32', 'The account held 5 bil -\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'space + Pd + LF (OCR)', ['W32'])], ['W32']),
        ('W33', 'The account held 5 bil\u2010\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'U+2010 + LF', ['W33', 'C005'])], ['W33', 'C005']),
        ('W34', 'The account held 5 bil\u2011\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'U+2011 + LF', ['W34', 'C006'])], ['W34', 'C006']),
        ('W35', 'The account held 5 bil-\r\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Pd + CRLF', ['W35', 'C001'])], ['W35', 'C001']),
        ('W36', 'The account held 5 bil-\x0clion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Pd + FF', ['W36', 'C004'])], ['W36', 'C004']),
        ('W37', 'The account held 5 bil-\u2028lion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Pd + U+2028', ['W37'])], ['W37']),
        ('W38', 'The account held 5 bil-\xa0lion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Pd + NBSP', ['W38'])], ['W38']),
        ('W39', 'Net debt was 1,234 (EUR bil\xad\nlion).', 'diagnosis_R', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'bracket, soft hyphen + LF', ['W39', 'C013'])], ['W39', 'C013']),
        ('W40', '| Net debt | 1,234 | EUR thou-\nsand |', 'diagnosis_R', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'cell, Pd + LF', ['W40'])], ['W40']),
        ('W41', '<table><tr><td>Net debt</td><td>1,234</td><td>EUR bil-<br>lion</td></tr></table>', 'diagnosis_R', [(32, 37, '1234', '1234000000000', True, True, 'REFUSED', 'td + br', ['W41'])], ['W41']),
        ('W42', 'Reserves were 5 and 6 bil\xad\nlion dollars.', 'diagnosis_R', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'coordination, soft hyphen + LF', ['W42', 'Q012'])], ['W42', 'Q012']),
        ('W43', 'Reserves were 5, 6 or 7 THOU-\nSAND.', 'diagnosis_R', [(14, 15, '5', '5000', True, True, 'REFUSED', 'coordination, UPPER', ['W43'])], ['W43']),
        ('W44', 'A 5 million-\ndollar loss was booked.', 'diagnosis_R', [(2, 3, '5', '5000000', True, True, 'REFUSED', 'scale-word compound split at its own hyphen', ['W44'])], ['W44']),
        ('W45', 'Amounts in EUR thou-\nsand: 5 and 6.', 'diagnosis_R', [(27, 28, '5', '5000', True, True, 'REFUSED', 'left cluster (header before number)', ['W45'])], ['W45']),
        ('X01', 'The account held 5 bil-\n\x0cAcme Corp. 10-K Page 4\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'page break + running header', ['X01'])], ['X01']),
        ('X02', 'The account held 5 bil-\nConfidential\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'running footer between halves', ['X02'])], ['X02']),
        ('X03', 'The account held 5 bil\nlion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'bare LF, mark dropped', ['X03'])], ['X03']),
        ('X04', 'Net debt was 1,234 thou\nsand.', 'diagnosis_R', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'bare LF, mark dropped', ['X04'])], ['X04']),
        ('X05', 'The account held 5 mil-\nlions of dollars.', 'diagnosis_R', [(17, 18, '5', '5000000', True, True, 'REFUSED', 'plural, Pd + LF', ['X05'])], ['X05']),
        ('X06', 'Net debt was 1,234 (EUR bil-\nlions).', 'diagnosis_R', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'plural, bracket', ['X06'])], ['X06']),
        ('X07', 'The account held 5 millions of dollars.', 'diagnosis_R', [(17, 18, '5', '5000000', True, True, 'REFUSED', 'plural unbroken (baseline)', ['X07'])], ['X07']),
        ('X08', 'The account held 5 bil lion dollars.', 'diagnosis_R', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'OCR space split, no mark', ['X08'])], ['X08']),
        ('S01', 'CHF 1\xb4250\xb4000', 'diagnosis_R', [(4, 5, '1', '1250000', True, True, 'REFUSED', 'U+00B4 grouping', ['S01', 'K01']), (6, 9, '250', '250', False, False, 'REFUSED', 'U+00B4 grouping, component', ['S02', 'K02'])], ['S01', 'S02', 'K01', 'K02']),
        ('S03', 'Betrag: CHF 12\xb4500.00 per Rechnung.', 'diagnosis_R', [(12, 14, '12', '12500', True, True, 'REFUSED', 'U+00B4 grouping', ['S03'])], ['S03']),
        ('S04', 'Saldo per 31.12.2019: CHF 3\xb4400\xb4000.', 'diagnosis_R', [(26, 27, '3', '3400000', True, True, 'REFUSED', 'U+00B4 grouping', ['S04'])], ['S04']),
        ('S05', 'Smith\xb4s 5 shares were sold.', 'diagnosis_R', [(8, 9, '5', '5', True, False, 'BOUND 5', 'U+00B4 as apostrophe (control)', ['S05'])], ['S05']),
        ('S06', 'Reserves: 5`000 shares.', 'diagnosis_R', [(10, 11, '5', '5000', True, True, 'REFUSED', 'U+0060 grave grouping', ['S06', 'hard_V057_10']), (12, 15, '0', '0', False, False, 'REFUSED', 'U+0060 grave grouping, component', ['S07', 'K05', 'hard_V057_12'])], ['S06', 'S07', 'K05', 'hard_V057_10', 'hard_V057_12']),
        ('A01', 'The account held 5 \uff42\uff49\uff4c\xad\uff4c\uff49\uff4f\uff4e dollars.', 'diagnosis_A', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'fullwidth + soft hyphen', ['A01'])], ['A01']),
        ('A02', 'The account held 5 bil\xad-\nlion dollars.', 'diagnosis_A', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'stacked soft hyphen + Pd', ['A02'])], ['A02']),
        ('A03', 'The account held 5 bil-\u200b\nlion dollars.', 'diagnosis_A', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'stacked Pd + ZWSP', ['A03'])], ['A03']),
        ('A04', 'The account held 5 bil\xac\nlion dollars.', 'diagnosis_A', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'U+00AC as hyphen (one OCR export)', ['A04'])], ['A04']),
        ('A05', 'The account held 5 bil\u2027\nlion dollars.', 'diagnosis_A', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'U+2027 hyphenation point', ['A05'])], ['A05']),
        ('A06', 'The reserve was 10.4 mul-\nti-\nplied by 1000.', 'diagnosis_A', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'three-piece split', ['A06'])], ['A06']),
        ('A07', 'The account held 5 bil-\nli\u043en dollars.', 'diagnosis_A', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Cyrillic o inside the joined word', ['A07'])], ['A07']),
        ('N01', 'The well-\nknown 5 shares were sold.', 'diagnosis_R', [(16, 17, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N01'])], ['N01']),
        ('N02', 'Smith sold 5 shares to a long-\ntime friend.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N02'])], ['N02']),
        ('N03', 'Smith sold 5 well- known shares.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N03'])], ['N03']),
        ('N04', 'Smith sold 5 x-ray images.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N04'])], ['N04']),
        ('N05', 'Smith sold 5 shares to his co-owner.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N05'])], ['N05']),
        ('N06', 'Smith sold 5 bil-\nliard tables.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N06'])], ['N06']),
        ('N07', 'The 5 non-\nvoting shares were sold.', 'diagnosis_R', [(4, 5, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N07'])], ['N07']),
        ('N08', 'Smith sold 5 shares to his co-\nowner.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N08'])], ['N08']),
        ('N09', 'THE BUYER SHALL DELIVER 5 CER-\nTIFICATES.', 'diagnosis_R', [(24, 25, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N09'])], ['N09']),
        ('N10', 'The defendant made 14 de-\nposits of $9,500 each.', 'diagnosis_R', [(19, 21, '14', '14', True, False, 'BOUND 14', 'normal hyphenation', ['N10'])], ['N10']),
        ('N11', '<div>Smith sold 5 sha-</div><div>res to Jones.</div>', 'diagnosis_R', [(16, 17, '5', '5', True, False, 'BOUND 5', 'normal hyphenation', ['N11'])], ['N11']),
        ('N12', 'Smith sold 5 in-\ndependent appraisals.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation (unit fragment in)', ['N12'])], ['N12']),
        ('N13', 'Smith filed 5 or-\nders with the court.', 'diagnosis_R', [(12, 13, '5', '5', True, False, 'BOUND 5', 'normal hyphenation (coordinator fragment or)', ['N13'])], ['N13']),
        ('N14', 'Smith sold 5 shares\xad\nto Jones.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'soft hyphen after an ordinary word', ['N14'])], ['N14']),
        ('N15', 'Smith sold 5 mil-\nitary vehicles.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation (scale-abbrev fragment mil)', ['N15'])], ['N15']),
        ('N16', 'Smith wired $12,500 to Jones on May 3, 2019.', 'diagnosis_R', [(12, 19, '12500', '12500', True, False, 'BOUND 12500', 'normal', ['N16'])], ['N16']),
        ('N17', 'The 2019 plan paid 5 bil\xad\nlion.', 'diagnosis_R', [(4, 8, '2019', '2019', True, False, 'BOUND 2019', 'date control with later split scale', ['N17', 'Q015'])], ['N17', 'Q015']),
        ('N18', 'Smith paid $5,000 to the co-\nowner.', 'diagnosis_R', [(11, 17, '5000', '5000', True, False, 'BOUND 5000', 'normal hyphenation', ['N18'])], ['N18']),
        ('N19', '<p>Smith sold 5 shares.</p><p>Re-</p><p>ceived on May 3.</p>', 'diagnosis_R', [(14, 15, '5', '5', True, False, 'BOUND 5', 'normal, next block', ['N19'])], ['N19']),
        ('N20', 'Smith sold 5 multi-\nyear leases.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation (multi- prefix)', ['N20'])], ['N20']),
        ('N21', 'Smith sold 5 time-\nshares to Jones.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation (time- prefix)', ['N21'])], ['N21']),
        ('N22', 'THE 5 NON-\nVOTING SHARES WERE SOLD.', 'diagnosis_R', [(4, 5, '5', '5', True, False, 'BOUND 5', 'normal hyphenation, UPPER', ['N22'])], ['N22']),
        ('N23', '<p>Smith sold 5 well&shy;known shares.</p>', 'diagnosis_R', [(14, 15, '5', '5', True, False, 'BOUND 5', 'normal hyphenation, entity', ['N23'])], ['N23']),
        ('N24', 'Smith sold 5 ten-\nant leases.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'normal hyphenation (ten- prefix)', ['N24'])], ['N24']),
        ('N25', 'Smith sent 5 e-\nmails.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'fragment is a witness (e-)', ['N25'])], ['N25']),
        ('N26', 'Smith made 5 ten-\nder offers.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'fragment is a witness (ten-)', ['N26'])], ['N26']),
        ('N27', 'Smith bought 5 mil-\nligrams.', 'diagnosis_R', [(13, 14, '5', '5', True, False, 'BOUND 5', 'fragment is a witness (mil-)', ['N27'])], ['N27']),
        ('N28', 'Smith sold 5 sha-\n\x0cAcme Corp. Page 4\nres to Jones.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'dangling fragment at corridor stop', ['N28'])], ['N28']),
        ('N29', 'The 5 non-\nVoting shares were sold.', 'diagnosis_R', [(4, 5, '5', '5', True, False, 'BOUND 5', 'dangling fragment, Title continuation', ['N29'])], ['N29']),
        ('N30', 'Smith sold 5 shares to a long-\n\x0cPage 4\ntime friend.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'dangling fragment, not at stop', ['N30'])], ['N30']),
        ('N31', 'Smith sold 5 shares\nto Jones.', 'diagnosis_R', [(11, 12, '5', '5', True, False, 'BOUND 5', 'bare line break between ordinary words', ['N31'])], ['N31']),
        ('N32', '<p>Smith sold 5</p><p>shares to Jones.</p>', 'diagnosis_R', [(14, 15, '5', '5', True, False, 'BOUND 5', 'bare line break, ordinary words', ['N32'])], ['N32']),
        ('Z01', 'The account held 5 bil-\nlion dollars.', 'diagnosis_R', [(17, 28, '5', '5000000000', False, False, 'REFUSED', 'whole raw split span (contract)', ['Z01']), (17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V023'])], ['Z01', 'old_V023']),
        ('Z02', 'The account held 5 thou\xad\nsand dollars.', 'diagnosis_R', [(17, 29, '5', '5000', False, False, 'REFUSED', 'whole raw split span (contract)', ['Z02']), (17, 18, '5', '5000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q002'])], ['Z02', 'Q002']),
        ('Z03', 'The account held 5 million dollars.', 'diagnosis_R', [(17, 26, '5000000', '5000000', True, False, 'BOUND 5000000', 'whole plain span (control)', ['Z03'])], ['Z03']),
        ('Q003', 'The account held 5 bil\xad\r\nlion dollars.', 'review_p2_1', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q003'])], ['Q003']),
        ('Q004', 'The account held 5 bil\xad lion dollars.', 'review_p2_1', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q004'])], ['Q004']),
        ('Q005', 'The account held 5 bil\xad\xa0lion dollars.', 'review_p2_1', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q005'])], ['Q005']),
        ('Q006', 'The account held 5 bil\xad\u2028lion dollars.', 'review_p2_1', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q006'])], ['Q006']),
        ('Q009', '<p>The account held 5 bil&shy;<br>lion dollars.</p>', 'review_p2_1', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q009'])], ['Q009']),
        ('Q010', '<p>The account held 5 bil&shy;\nlion dollars.</p>', 'review_p2_1', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q010'])], ['Q010']),
        ('Q011', '<div>The account held 5 bil\xad</div><div>lion dollars.</div>', 'review_p2_1', [(22, 23, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q011'])], ['Q011']),
        ('Q014', 'The reserve was 10.4 ti\xad\nmes 1000.', 'review_p2_1', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q014'])], ['Q014']),
        ('Q016', 'THE ACCOUNT HELD 5 BIL-\nLION DOLLARS.', 'review_p2_1', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q016'])], ['Q016']),
        ('Q017', 'THE ACCOUNT HELD 5 THOU-\nSAND DOLLARS.', 'review_p2_1', [(17, 18, '5', '5000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q017'])], ['Q017']),
        ('Q018', 'NET DEBT WAS 1,234 BIL-\nLION.', 'review_p2_1', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'r13 P2-1 table literal', ['Q018'])], ['Q018']),
        ('K03', 'Reserves: 5\xb4000 shares.', 'review_p2_2', [(10, 11, '5', '5000', True, True, 'REFUSED', 'raw single Sk grouping, unsupported components', ['K03', 'hard_V056_10']), (12, 15, '0', '0', False, False, 'REFUSED', 'raw single Sk grouping, unsupported components', ['K04', 'hard_V056_12'])], ['K03', 'K04', 'hard_V056_10', 'hard_V056_12']),
        ('K06', 'CHF 1&acute;250&acute;000', 'review_p2_2', [(4, 5, '1', '1250000', True, True, 'REFUSED', 'raw single Sk grouping, unsupported components', ['K06']), (12, 15, '250', '250', False, False, 'REFUSED', 'raw single Sk grouping, unsupported components', ['K07']), (22, 25, '0', '0', False, False, 'REFUSED', 'raw single Sk grouping, unsupported components', ['K08'])], ['K06', 'K07', 'K08']),
        ('K09', 'CHF 1&#180;250&#xB4;000', 'review_p2_2', [(4, 5, '1', '1250000', True, True, 'REFUSED', 'raw single Sk grouping, unsupported components', ['K09']), (20, 23, '0', '0', False, False, 'REFUSED', 'raw single Sk grouping, unsupported components', ['K10'])], ['K09', 'K10']),
        ('old_V001', 'Net debt was 1,234 thou\xadsand.', 'r12_control', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V001'])], ['old_V001']),
        ('old_V002', 'Net debt was 1,234 thou&shy;sand.', 'r12_control', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V002'])], ['old_V002']),
        ('old_V003', 'Net debt was 1,234 bil\xadlion.', 'r12_control', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V003'])], ['old_V003']),
        ('old_V004', 'Net debt was 1,234 bil&shy;lion.', 'r12_control', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V004'])], ['old_V004']),
        ('old_V005', '<p>The account held 5 bil&#173;lion dollars.</p>', 'r12_control', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V005'])], ['old_V005']),
        ('old_V006', '<p>The account held 5 bil&#xAD;lion dollars.</p>', 'r12_control', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V006'])], ['old_V006']),
        ('old_V007', 'Net debt was 1,234 hun\xaddred.', 'r12_control', [(13, 18, '1234', '123400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V007'])], ['old_V007']),
        ('old_V008', 'Net debt was 1,234 tril\xadlion.', 'r12_control', [(13, 18, '1234', '1234000000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V008'])], ['old_V008']),
        ('old_V009', 'Net debt was 1,234 mi\xadllion.', 'r12_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V009'])], ['old_V009']),
        ('old_V010', 'The account held $5 bil\xadlion in cash.', 'r12_control', [(17, 19, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V010'])], ['old_V010']),
        ('old_V011', 'Net debt was 1,234 thou\u200bsand.', 'r12_control', [(13, 18, '1234', '1234000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V011'])], ['old_V011']),
        ('old_V012', 'Net debt was 1,234 bil\u2060lion.', 'r12_control', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V012'])], ['old_V012']),
        ('old_V013', 'The fund holds 5 thou\u200dsand dollars.', 'r12_control', [(15, 16, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V013'])], ['old_V013']),
        ('old_V014', 'The account held 5 thou\ufeffsand.', 'r12_control', [(17, 18, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V014'])], ['old_V014']),
        ('old_V015', 'The account held 5 bil\u200elion.', 'r12_control', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V015'])], ['old_V015']),
        ('old_V016', 'The account held 5 cr\xadore.', 'r12_control', [(17, 18, '5', '50000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V016'])], ['old_V016']),
        ('old_V017', 'Reserves were 5, 6 and 7 thou\xadsand.', 'r12_control', [(14, 15, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V017'])], ['old_V017']),
        ('old_V018', 'Net debt was 1,234 (EUR mi\xadllion).', 'r12_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V018'])], ['old_V018']),
        ('old_V019', 'Net debt was 1,234 (EUR mi\u200bllion).', 'r12_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V019'])], ['old_V019']),
        ('old_V020', '| Net debt | 1,234 | EUR mi\xadllion |', 'r12_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V020'])], ['old_V020']),
        ('old_V021', 'The reserve was 10.4 ti\xadmes 1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V021'])], ['old_V021']),
        ('old_V022', 'The reserve was 10.4 mul\xadtiplied by 1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V022'])], ['old_V022']),
        ('old_V024', 'The account held 5 thou-\nsand dollars.', 'r12_control', [(17, 18, '5', '5000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V024'])], ['old_V024']),
        ('old_V025', 'The account held 5 bil- lion dollars.', 'r12_control', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V025'])], ['old_V025']),
        ('old_V026', 'The reserve was 10.4 \u2716\ufe0f 1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V026'])], ['old_V026']),
        ('old_V027', 'The reserve was 10.4 \u2716\ufe0f1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V027'])], ['old_V027']),
        ('old_V028', 'The reserve was 10.4\u2716\ufe0f1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V028'])], ['old_V028']),
        ('old_V029', 'The reserve was 10.4 &#x2716;&#xFE0F; 1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V029'])], ['old_V029']),
        ('old_V030', 'The reserve was 10.4 \u2716\ufe0f $1,000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V030'])], ['old_V030']),
        ('old_V031', 'The reserve was 5 \u2716\ufe0f 1,000,000.', 'r12_control', [(16, 17, '5', '5000000', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V031'])], ['old_V031']),
        ('old_V032', 'The reserve was 10.4 \u2715\ufe0f 1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V032'])], ['old_V032']),
        ('old_V033', 'The reserve was 10.4 \u2716\u200d 1000.', 'r12_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V033'])], ['old_V033']),
        ('old_V034', 'The reserve was 10.4 \u2797\ufe0f 1000.', 'r12_control', [(16, 20, '52/5', '13/1250', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['old_V034'])], ['old_V034']),
        ('C002', 'The account held 5 bil-\tlion dollars.', 'review_control', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C002'])], ['C002']),
        ('C003', 'The account held 5 bil-  \n  lion dollars.', 'review_control', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C003'])], ['C003']),
        ('C007', 'The account held 5 bil-<BR CLEAR="all">lion dollars.', 'review_control', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C007'])], ['C007']),
        ('C008', 'The account held 5 Bil-\nlion dollars.', 'review_control', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C008'])], ['C008']),
        ('C009', 'The account held 5 BIL\xadLION dollars.', 'review_control', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C009'])], ['C009']),
        ('C010', 'The account held 5 mil\xad\nlion dollars.', 'review_control', [(17, 18, '5', '5000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C010'])], ['C010']),
        ('C011', 'Net debt was 1,234 hun-\ndred.', 'review_control', [(13, 18, '1234', '123400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C011'])], ['C011']),
        ('C012', 'THE ACCOUNT HELD 5 MIL-\nLION DOLLARS.', 'review_control', [(17, 18, '5', '5000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C012'])], ['C012']),
        ('C014', 'Net debt was 1,234 (EUR bil\xad lion).', 'review_control', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C014'])], ['C014']),
        ('C015', '| Net debt | 1,234 | EUR bil\xad\nlion |', 'review_control', [(13, 18, '1234', '1234000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C015'])], ['C015']),
        ('C016', '<td>5</td><td>bil\xad\nlion</td>', 'review_control', [(4, 5, '5', '5000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C016'])], ['C016']),
        ('C017', '<td>1,234</td><td>EUR mil&shy;lion</td>', 'review_control', [(4, 9, '1234', '1234000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C017'])], ['C017']),
        ('C018', 'The reserve was 10.4 x\xad\n1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C018'])], ['C018']),
        ('C019', 'The reserve was 10.4 times\xad\n1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C019'])], ['C019']),
        ('C020', 'The reserve was 10.4 &times;&#xFE0F; 1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C020'])], ['C020']),
        ('C021', 'The reserve was 10.4 \u2716\ufe0e 1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C021'])], ['C021']),
        ('C022', 'The reserve was 10.4 \u200e\xd7 1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C022'])], ['C022']),
        ('C023', 'The reserve was 10.4 \u2715\ufe0f\xa01000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C023'])], ['C023']),
        ('C024', 'The reserve was 10.4 \xd7&#xFE0F;1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C024'])], ['C024']),
        ('C025', 'The reserve was 10.4 x \ufe0f 1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C025'])], ['C025']),
        ('C026', 'The reserve was 10.4 x\n\xad1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C026'])], ['C026']),
        ('C027', 'The reserve was 10.4 \xa0\xd7\xa01000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C027'])], ['C027']),
        ('C028', 'The reserve was 10.4 \xd7\n1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C028'])], ['C028']),
        ('C029', 'The reserve was 10.4 mul-\ntiplied by 1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C029'])], ['C029']),
        ('C030', 'The reserve was 10.4 multi-\nplied by 1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C030'])], ['C030']),
        ('C031', 'The reserve was 10.4 ti-\nmes 1000.', 'review_control', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C031'])], ['C031']),
        ('C032', 'The fee was 5 \ufe0f\u20e3 million.', 'review_control', [(12, 13, '5', '5000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C032'])], ['C032']),
        ('C033', 'Net debt was 1,234 (EUR mill\xadion).', 'review_control', [(13, 18, '1234', '1234000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C033'])], ['C033']),
        ('C034', 'Reserves were 5, 6 and 7 bil-\nlion.', 'review_control', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C034'])], ['C034']),
        ('C035', 'Reserves: 5 or 6 thou-\nsand.', 'review_control', [(10, 11, '5', '5000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C035'])], ['C035']),
        ('C036', 'The reserve was 10.4 (EUR mil-\nlion).', 'review_control', [(16, 20, '52/5', '10400000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C036'])], ['C036']),
        ('C037', 'The reserve was 10.4 (EUR bil-\nlion).', 'review_control', [(16, 20, '52/5', '10400000000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C037'])], ['C037']),
        ('C038', 'The reserve was 10.4 (EUR mil\xad\nlion).', 'review_control', [(16, 20, '52/5', '10400000', True, True, 'REFUSED', 'r13 literal refusal control; fragments explicitly expanded', ['C038'])], ['C038']),
        ('U01', 'The fund holds 5 (zq) in notes.', 'review_control', [(15, 16, '5', '5', True, False, 'BOUND 5', 'unknown code: no magnitude inferred; contract validity independent of refusal', ['U01'])], ['U01']),
        ('U02', 'The fund holds 5 (qzv) in notes.', 'review_control', [(15, 16, '5', '5', True, False, 'BOUND 5', 'unknown code: no magnitude inferred; contract validity independent of refusal', ['U02'])], ['U02']),
        ('U03', 'The fund holds 5qzv in notes.', 'review_control', [(15, 16, '5', '5', False, False, 'REFUSED', 'unknown code: no magnitude inferred; contract validity independent of refusal', ['U03'])], ['U03']),
        ('U04', '| Amount | 5 | qzv |', 'review_control', [(11, 12, '5', '5', True, False, 'BOUND 5', 'unknown code: no magnitude inferred; contract validity independent of refusal', ['U04'])], ['U04']),
        ('B01', '5 thou-\nsand', 'review_control', [(0, 12, '5', '5000', False, False, 'REFUSED', 'whole split span outside contract', ['B01'])], ['B01']),
        ('B02', '5 mil-\nlion', 'review_control', [(0, 11, '5', '5000000', False, False, 'REFUSED', 'whole split span outside contract', ['B02'])], ['B02']),
        ('B03', '5 bil-\nlion', 'review_control', [(0, 11, '5', '5000000000', False, False, 'REFUSED', 'whole split span outside contract', ['B03'])], ['B03']),
        ('B04', '5\xa0million', 'review_control', [(0, 9, '5000000', '5000000', True, False, 'BOUND 5000000', 'whole contract scale with ordinary Unicode space', ['B04'])], ['B04']),
        ('D01', '5 (zl)', 'label_dispute', [(0, 1, '5', '5', True, False, 'BOUND 5', 'r13 P3-1 corrected reading; earlier r12 pin stays unchanged', ['D01'])], ['D01']),
        ('D02', '5(b) and 6(c)', 'label_dispute', [(0, 1, '5', '5', True, False, 'BOUND 5', 'r13 P3-1 corrected reading; earlier r12 pin stays unchanged', ['D02'])], ['D02']),
        ('D03', '5 (TEUR)', 'label_dispute', [(0, 1, '5', '5000', True, True, 'REFUSED', 'r13 P3-1 corrected reading; earlier r12 pin stays unchanged', ['D03'])], ['D03']),
        ('D04', '5 | TEUR |', 'label_dispute', [(0, 1, '5', '5000', True, True, 'REFUSED', 'r13 P3-1 corrected reading; earlier r12 pin stays unchanged', ['D04'])], ['D04']),
        ('L01', '\ufeff5,000 shares were sold.', 'review_control', [(1, 6, '5000', '5000', True, False, 'BOUND 5000', 'r13 P3-2 realistic accepted refusal family', ['L01'])], ['L01']),
        ('L02', 'Paid to Jose\u0301 500 dollars.', 'review_control', [(14, 17, '500', '500', True, False, 'BOUND 500', 'r13 P3-2 realistic accepted refusal family', ['L02'])], ['L02']),
        ('L03', '\u200f500 \u05e9\u05e7\u05dc', 'review_control', [(1, 4, '500', '500', True, False, 'BOUND 500', 'r13 P3-2 realistic accepted refusal family', ['L03'])], ['L03']),
        ('L04', 'Smith paid $1,250\u200b on 3/4/2020.', 'review_control', [(11, 17, '1250', '1250', True, False, 'BOUND 1250', 'r13 P3-2 realistic accepted refusal family', ['L04'])], ['L04']),
        ('L05', 'Total\xad 500 shares.', 'review_control', [(7, 10, '500', '500', True, False, 'BOUND 500', 'r13 P3-2 realistic accepted refusal family', ['L05'])], ['L05']),
        ('L06', '<p>5</p><p>\xad</p>', 'review_control', [(3, 4, '5', '5', True, False, 'BOUND 5', 'r13 P3-2 realistic accepted refusal family', ['L06'])], ['L06']),
        ('L07', 'Smith sold 5\xad\nshares.', 'review_control', [(11, 12, '5', '5', True, False, 'BOUND 5', 'r13 P3-2 realistic accepted refusal family', ['L07'])], ['L07']),
        ('L08', 'The account held 5 million-\ndollar notes.', 'review_control', [(17, 26, '5000000', '5000000', True, False, 'BOUND 5000000', 'r13 P3-2 realistic accepted refusal family', ['L08'])], ['L08']),
        ('L09', 'The account held $5 million-\ndollar note.', 'review_control', [(17, 27, '5000000', '5000000', True, False, 'BOUND 5000000', 'r13 P3-2 realistic accepted refusal family', ['L09'])], ['L09']),
        ('P01', 'Smith sold 5 thou-\nsandwiches.', 'review_control', [(11, 12, '5', '5', True, False, 'BOUND 5', 'r13 normal phrasing control', ['P01'])], ['P01']),
        ('P02', 'Smith sold 5 sha-\nres to Jones.', 'review_control', [(11, 12, '5', '5', True, False, 'BOUND 5', 'r13 normal phrasing control', ['P02'])], ['P02']),
        ('P03', 'Smith sold 5 shares on 3/4/2020.\u200b', 'review_control', [(11, 12, '5', '5', True, False, 'BOUND 5', 'r13 normal phrasing control', ['P03'])], ['P03']),
        ('P04', 'Smith sold 5 shares\u200e to Jones.', 'review_control', [(11, 12, '5', '5', True, False, 'BOUND 5', 'r13 normal phrasing control', ['P04'])], ['P04']),
        ('P05', '| Shares | 500 |\u200b', 'review_control', [(11, 14, '500', '500', True, False, 'BOUND 500', 'r13 normal phrasing control', ['P05'])], ['P05']),
        ('P06', '\u200b| Shares | 500 |', 'review_control', [(12, 15, '500', '500', True, False, 'BOUND 500', 'r13 normal phrasing control', ['P06'])], ['P06']),
        ('P07', 'Smith-\nson paid $5,000.', 'review_control', [(16, 22, '5000', '5000', True, False, 'BOUND 5000', 'r13 normal phrasing control', ['P07'])], ['P07']),
        ('P08', 'A co-\nowner paid 5 shares.', 'review_control', [(17, 18, '5', '5', True, False, 'BOUND 5', 'r13 normal phrasing control', ['P08'])], ['P08']),
        ('P09', 'The defendant made 14 deposits of $9,500 each.\x0cPage 2', 'review_control', [(34, 40, '9500', '9500', True, False, 'BOUND 9500', 'r13 normal phrasing control', ['P09'])], ['P09']),
        ('P10', 'Smith wired $12,500\xa0to Jones on May 3.', 'review_control', [(12, 19, '12500', '12500', True, False, 'BOUND 12500', 'r13 normal phrasing control', ['P10'])], ['P10']),
        ('P11', 'The ledger shows:\r\n| Fees | $1,250 |', 'review_control', [(28, 34, '1250', '1250', True, False, 'BOUND 1250', 'r13 normal phrasing control', ['P11'])], ['P11']),
        ('P12', '<td>500</td><td>&nbsp;</td><td>shares</td>', 'review_control', [(4, 7, '500', '500', True, False, 'BOUND 500', 'r13 normal phrasing control', ['P12'])], ['P12']),
        ('P13', 'Smith held 1,200 shares (Ex.\xa07).', 'review_control', [(11, 16, '1200', '1200', True, False, 'BOUND 1200', 'r13 normal phrasing control', ['P13'])], ['P13']),
        ('P14', 'The account held 5 thou\xad\nsand dollars and 6 shares.', 'review_control', [(42, 43, '6', '6', True, False, 'BOUND 6', 'r13 normal phrasing control', ['P14'])], ['P14']),
        ('P15', 'Smith paid $1,250 to Jose\u0301 50 times.', 'review_control', [(11, 17, '1250', '1250', True, False, 'BOUND 1250', 'r13 normal phrasing control', ['P15'])], ['P15']),
        ('P16', '5<!-- \xad -->', 'review_control', [(0, 1, '5', '5', True, False, 'BOUND 5', 'r13 normal phrasing control', ['P16'])], ['P16']),
        ('P17', "5 <i data-x='a&shy;b'>shares</i>", 'review_control', [(0, 1, '5', '5', True, False, 'BOUND 5', 'r13 normal phrasing control', ['P17'])], ['P17']),
        ('hard_V048_16', 'The reserve was 10.4 \uab53 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V048_16'])], ['hard_V048_16']),
        ('hard_V049_16', 'The reserve was 10.4 \ua7b3 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V049_16'])], ['hard_V049_16']),
        ('hard_V050_16', 'The reserve was 10.4 \uab57 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V050_16'])], ['hard_V050_16']),
        ('hard_V051_16', 'The reserve was 10.4 \uab59 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V051_16'])], ['hard_V051_16']),
        ('hard_V052_16', 'The reserve was 10.4 \u0445\u200d 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V052_16'])], ['hard_V052_16']),
        ('hard_V053_16', 'The reserve was 10.4% \u0445 1000.', 'hardening', [(16, 21, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V053_16'])], ['hard_V053_16']),
        ('hard_V054_17', 'The reserve was [10.4] \u0445 1000.', 'hardening', [(17, 21, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V054_17'])], ['hard_V054_17']),
        ('hard_V055_16', 'The reserve was 10.4 (\u0445 1000).', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['hard_V055_16'])], ['hard_V055_16']),
        ('hard_V058_10', 'Reserves: 5\u02d9000 shares.', 'hardening', [(10, 11, '5', '5000', True, True, 'REFUSED', 'grouped whole value 5000; coefficient must not bind literally', ['hard_V058_10']), (12, 15, '0', '0', False, False, 'REFUSED', 'component of unsupported grouped numeral, not independent zero', ['hard_V058_12'])], ['hard_V058_10', 'hard_V058_12']),
        ('hard_V059_14', 'Smith owned 3\u20444 of the lots.', 'hardening', [(14, 15, '4', '4', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', ['hard_V059_14'])], ['hard_V059_14']),
        ('hard_V060_14', 'Smith owned 1\u20442 of the lots.', 'hardening', [(14, 15, '2', '2', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', ['hard_V060_14'])], ['hard_V060_14']),
        ('hard_V061_9', 'Total: 5\u2215000 shares.', 'hardening', [(9, 12, '0', '0', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', ['hard_V061_9'])], ['hard_V061_9']),
        ('hard_V062_8', 'Total: \u202e21\u202c shares.', 'hardening', [(8, 10, '21', '12', True, False, 'REFUSED', 'override displays 12 while stored digits spell 21; refuse display ambiguity', ['hard_V062_8'])], ['hard_V062_8']),
        ('hard_V108_0', '5(q)', 'hardening', [(0, 1, '5', '5', False, False, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', ['hard_V108_0'])], ['hard_V108_0']),
    ],
    'r14': [
        ('P01', 'The growth factor was 1.05 to the pow-\ner of 10.', 'review_p2_1', [(22, 26, '21/20', '16679880978201/10240000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P01']), (45, 47, '10', '10', False, False, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P05'])], ['P01', 'P05']),
        ('P02', 'The growth factor was 1.05 to the pow\xader of 10.', 'review_p2_1', [(22, 26, '21/20', '16679880978201/10240000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P02'])], ['P02']),
        ('P03', '<p>The growth factor was 1.05 to the pow-</p><p>er of 10.</p>', 'review_p2_1', [(25, 29, '21/20', '16679880978201/10240000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P03'])], ['P03']),
        ('P04', 'THE FACTOR IS 1.05 TO THE POW-\nER OF 10.', 'review_p2_1', [(14, 18, '21/20', '16679880978201/10240000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P04'])], ['P04']),
        ('P06', 'The reserve was 10 to the po-\nwer of 3.', 'review_p2_1', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P06', 'J_hyphen_lf_power_early']), (37, 38, '3', '3', False, False, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P12'])], ['P06', 'P12', 'J_hyphen_lf_power_early']),
        ('P07', 'The reserve was 10 to the pow-\ner of 3.', 'review_p2_1', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P07', 'J_hyphen_lf_power']), (37, 38, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / hyphen_lf', ['J_hyphen_lf_exponent_component'])], ['P07', 'J_hyphen_lf_power', 'J_hyphen_lf_exponent_component']),
        ('P08', 'The reserve was 10 to the pow&shy;er of 3.', 'review_p2_1', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P08', 'J_entity_power']), (40, 41, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / entity', ['J_entity_exponent_component'])], ['P08', 'J_entity_power', 'J_entity_exponent_component']),
        ('P09', 'The reserve was 10 to the pow\xad\ner of 3.', 'review_p2_1', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P09', 'J_soft_lf_power']), (37, 38, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / soft_lf', ['J_soft_lf_exponent_component'])], ['P09', 'J_soft_lf_power', 'J_soft_lf_exponent_component']),
        ('P10', 'The reserve was 10 to the POW-\nER OF 3.', 'review_p2_1', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P10'])], ['P10']),
        ('P11', 'The reserve was 10 to the pow-\ner 3.', 'review_p2_1', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P11'])], ['P11']),
        ('P13', 'The sample held 2.5 x 10 to the pow-\ner of 6 copies.', 'review_p2_1', [(22, 24, '10', '10', False, False, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['P13'])], ['P13']),
        ('C01', 'The growth factor was 1.05 to the power of 10.', 'review_held', [(22, 26, '21/20', '16679880978201/10240000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C01']), (43, 45, '10', '10', False, False, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C02'])], ['C01', 'C02']),
        ('C03', 'The reserve was 10 to the power of 3.', 'review_held', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C03']), (35, 36, '3', '3', False, False, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C04'])], ['C03', 'C04']),
        ('C05', 'The lab measured 2.5 x 10 to the power of 6 copies.', 'review_held', [(23, 25, '10', '10', False, False, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C05'])], ['C05']),
        ('C06', 'The reserve was 10.4 raised to the pow-\ner of 3.', 'review_held', [(16, 20, '52/5', '140608/125', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C06'])], ['C06']),
        ('C07', 'The reserve was 10.4 times 10 to the po-\nwer of 3.', 'review_held', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C07'])], ['C07']),
        ('C08', 'The reserve was 10 to the 3rd po-\nwer.', 'review_held', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C08'])], ['C08']),
        ('C09', 'The reserve was 10.4 ex-\nponent 3.', 'review_held', [(16, 20, '52/5', '140608/125', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C09'])], ['C09']),
        ('C10', 'The reserve was 10 to the\npower of 3.', 'review_held', [(16, 18, '10', '1000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['C10'])], ['C10']),
        ('N01', 'Smith sold 10 shares to the power company.', 'normal', [(11, 13, '10', '10', True, False, 'BOUND 10', 'ordinary company prose; ideal BOUND 10', ['N01'])], ['N01']),
        ('N02', 'Smith sold 10 shares to the pow-\ner company.', 'normal', [(11, 13, '10', '10', True, False, 'BOUND 10', 'ordinary company prose; ideal BOUND 10', ['N02'])], ['N02']),
        ('N03', 'Smith sold 10 to the pow-\ner company.', 'normal', [(11, 13, '10', '10', True, False, 'BOUND 10', 'ordinary company prose; ideal BOUND 10', ['N03'])], ['N03']),
        ('J_hyphen_lf_to', 'The reserve was 10 t-\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / hyphen_lf', ['J_hyphen_lf_to'])], ['J_hyphen_lf_to']),
        ('J_hyphen_lf_the', 'The reserve was 10 to t-\nhe power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / hyphen_lf', ['J_hyphen_lf_the'])], ['J_hyphen_lf_the']),
        ('J_hyphen_lf_of', 'The reserve was 10 to the power o-\nf 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / hyphen_lf', ['J_hyphen_lf_of'])], ['J_hyphen_lf_of']),
        ('J_hyphen_lf_exponent_word', 'The reserve was 10 ex-\nponent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / hyphen_lf', ['J_hyphen_lf_exponent_word'])], ['J_hyphen_lf_exponent_word']),
        ('J_hyphen_lf_raised', 'The reserve was 10 rai-\nsed to the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / hyphen_lf', ['J_hyphen_lf_raised'])], ['J_hyphen_lf_raised']),
        ('J_hyphen_lf_raised_to', 'The reserve was 10 raised t-\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / hyphen_lf', ['J_hyphen_lf_raised_to'])], ['J_hyphen_lf_raised_to']),
        ('J_hyphen_lf_multiplied', 'The reserve was 10 multi-\nplied by 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / hyphen_lf', ['J_hyphen_lf_multiplied'])], ['J_hyphen_lf_multiplied']),
        ('J_hyphen_lf_by', 'The reserve was 10 multiplied b-\ny 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / hyphen_lf', ['J_hyphen_lf_by'])], ['J_hyphen_lf_by']),
        ('J_hyphen_lf_times', 'The reserve was 10 ti-\nmes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / hyphen_lf', ['J_hyphen_lf_times'])], ['J_hyphen_lf_times']),
        ('J_hyphen_lf_multiplied_base', 'The sample held 2.5 ti-\nmes 10 to the 6 copies.', 'joint_guard', [(28, 30, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / hyphen_lf', ['J_hyphen_lf_multiplied_base']), (38, 39, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / hyphen_lf', ['J_hyphen_lf_multiplied_exponent'])], ['J_hyphen_lf_multiplied_base', 'J_hyphen_lf_multiplied_exponent']),
        ('J_hyphen_lf_later_power', 'The sample held 2.5 10 to the pow-\ner of 6 copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / hyphen_lf', ['J_hyphen_lf_later_power'])], ['J_hyphen_lf_later_power']),
        ('J_hyphen_lf_scale', 'The account held 5 bil-\nlion dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / hyphen_lf', ['J_hyphen_lf_scale'])], ['J_hyphen_lf_scale']),
        ('J_hyphen_lf_bracket', 'Net debt was 5 (EUR bil-\nlion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / hyphen_lf', ['J_hyphen_lf_bracket'])], ['J_hyphen_lf_bracket']),
        ('J_hyphen_lf_cell', '| Net debt | 5 | EUR bil-\nlion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / hyphen_lf', ['J_hyphen_lf_cell'])], ['J_hyphen_lf_cell']),
        ('J_hyphen_lf_left_cluster', 'EUR bil-\nlion 5.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / hyphen_lf', ['J_hyphen_lf_left_cluster'])], ['J_hyphen_lf_left_cluster']),
        ('J_hyphen_lf_short_code', 'The account held 5 (b-\nn).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / hyphen_lf', ['J_hyphen_lf_short_code'])], ['J_hyphen_lf_short_code']),
        ('J_hyphen_lf_unit', 'The account held 5 (EU-\nR).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / hyphen_lf', ['J_hyphen_lf_unit'])], ['J_hyphen_lf_unit']),
        ('J_hyphen_lf_coordinator', 'Reserves were 5 a-\nnd 6 billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / hyphen_lf', ['J_hyphen_lf_coordinator'])], ['J_hyphen_lf_coordinator']),
        ('J_soft_power', 'The reserve was 10 to the pow\xader of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power / soft', ['J_soft_power']), (36, 37, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / soft', ['J_soft_exponent_component'])], ['J_soft_power', 'J_soft_exponent_component']),
        ('J_soft_power_early', 'The reserve was 10 to the po\xadwer of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / soft', ['J_soft_power_early'])], ['J_soft_power_early']),
        ('J_soft_to', 'The reserve was 10 t\xado the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / soft', ['J_soft_to'])], ['J_soft_to']),
        ('J_soft_the', 'The reserve was 10 to t\xadhe power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / soft', ['J_soft_the'])], ['J_soft_the']),
        ('J_soft_of', 'The reserve was 10 to the power o\xadf 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / soft', ['J_soft_of'])], ['J_soft_of']),
        ('J_soft_exponent_word', 'The reserve was 10 ex\xadponent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / soft', ['J_soft_exponent_word'])], ['J_soft_exponent_word']),
        ('J_soft_raised', 'The reserve was 10 rai\xadsed to the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / soft', ['J_soft_raised'])], ['J_soft_raised']),
        ('J_soft_raised_to', 'The reserve was 10 raised t\xado the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / soft', ['J_soft_raised_to'])], ['J_soft_raised_to']),
        ('J_soft_multiplied', 'The reserve was 10 multi\xadplied by 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / soft', ['J_soft_multiplied'])], ['J_soft_multiplied']),
        ('J_soft_by', 'The reserve was 10 multiplied b\xady 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / soft', ['J_soft_by'])], ['J_soft_by']),
        ('J_soft_times', 'The reserve was 10 ti\xadmes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / soft', ['J_soft_times'])], ['J_soft_times']),
        ('J_soft_multiplied_base', 'The sample held 2.5 ti\xadmes 10 to the 6 copies.', 'joint_guard', [(27, 29, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / soft', ['J_soft_multiplied_base']), (37, 38, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / soft', ['J_soft_multiplied_exponent'])], ['J_soft_multiplied_base', 'J_soft_multiplied_exponent']),
        ('J_soft_later_power', 'The sample held 2.5 10 to the pow\xader of 6 copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / soft', ['J_soft_later_power'])], ['J_soft_later_power']),
        ('J_soft_scale', 'The account held 5 bil\xadlion dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / soft', ['J_soft_scale'])], ['J_soft_scale']),
        ('J_soft_bracket', 'Net debt was 5 (EUR bil\xadlion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / soft', ['J_soft_bracket'])], ['J_soft_bracket']),
        ('J_soft_cell', '| Net debt | 5 | EUR bil\xadlion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / soft', ['J_soft_cell'])], ['J_soft_cell']),
        ('J_soft_left_cluster', 'EUR bil\xadlion 5.', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / soft', ['J_soft_left_cluster'])], ['J_soft_left_cluster']),
        ('J_soft_short_code', 'The account held 5 (b\xadn).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / soft', ['J_soft_short_code'])], ['J_soft_short_code']),
        ('J_soft_unit', 'The account held 5 (EU\xadR).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / soft', ['J_soft_unit'])], ['J_soft_unit']),
        ('J_soft_coordinator', 'Reserves were 5 a\xadnd 6 billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / soft', ['J_soft_coordinator'])], ['J_soft_coordinator']),
        ('J_soft_lf_power_early', 'The reserve was 10 to the po\xad\nwer of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / soft_lf', ['J_soft_lf_power_early'])], ['J_soft_lf_power_early']),
        ('J_soft_lf_to', 'The reserve was 10 t\xad\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / soft_lf', ['J_soft_lf_to'])], ['J_soft_lf_to']),
        ('J_soft_lf_the', 'The reserve was 10 to t\xad\nhe power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / soft_lf', ['J_soft_lf_the'])], ['J_soft_lf_the']),
        ('J_soft_lf_of', 'The reserve was 10 to the power o\xad\nf 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / soft_lf', ['J_soft_lf_of'])], ['J_soft_lf_of']),
        ('J_soft_lf_exponent_word', 'The reserve was 10 ex\xad\nponent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / soft_lf', ['J_soft_lf_exponent_word'])], ['J_soft_lf_exponent_word']),
        ('J_soft_lf_raised', 'The reserve was 10 rai\xad\nsed to the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / soft_lf', ['J_soft_lf_raised'])], ['J_soft_lf_raised']),
        ('J_soft_lf_raised_to', 'The reserve was 10 raised t\xad\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / soft_lf', ['J_soft_lf_raised_to'])], ['J_soft_lf_raised_to']),
        ('J_soft_lf_multiplied', 'The reserve was 10 multi\xad\nplied by 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / soft_lf', ['J_soft_lf_multiplied'])], ['J_soft_lf_multiplied']),
        ('J_soft_lf_by', 'The reserve was 10 multiplied b\xad\ny 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / soft_lf', ['J_soft_lf_by'])], ['J_soft_lf_by']),
        ('J_soft_lf_times', 'The reserve was 10 ti\xad\nmes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / soft_lf', ['J_soft_lf_times'])], ['J_soft_lf_times']),
        ('J_soft_lf_multiplied_base', 'The sample held 2.5 ti\xad\nmes 10 to the 6 copies.', 'joint_guard', [(28, 30, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / soft_lf', ['J_soft_lf_multiplied_base']), (38, 39, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / soft_lf', ['J_soft_lf_multiplied_exponent'])], ['J_soft_lf_multiplied_base', 'J_soft_lf_multiplied_exponent']),
        ('J_soft_lf_later_power', 'The sample held 2.5 10 to the pow\xad\ner of 6 copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / soft_lf', ['J_soft_lf_later_power'])], ['J_soft_lf_later_power']),
        ('J_soft_lf_scale', 'The account held 5 bil\xad\nlion dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / soft_lf', ['J_soft_lf_scale'])], ['J_soft_lf_scale']),
        ('J_soft_lf_bracket', 'Net debt was 5 (EUR bil\xad\nlion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / soft_lf', ['J_soft_lf_bracket'])], ['J_soft_lf_bracket']),
        ('J_soft_lf_cell', '| Net debt | 5 | EUR bil\xad\nlion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / soft_lf', ['J_soft_lf_cell'])], ['J_soft_lf_cell']),
        ('J_soft_lf_left_cluster', 'EUR bil\xad\nlion 5.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / soft_lf', ['J_soft_lf_left_cluster'])], ['J_soft_lf_left_cluster']),
        ('J_soft_lf_short_code', 'The account held 5 (b\xad\nn).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / soft_lf', ['J_soft_lf_short_code'])], ['J_soft_lf_short_code']),
        ('J_soft_lf_unit', 'The account held 5 (EU\xad\nR).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / soft_lf', ['J_soft_lf_unit'])], ['J_soft_lf_unit']),
        ('J_soft_lf_coordinator', 'Reserves were 5 a\xad\nnd 6 billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / soft_lf', ['J_soft_lf_coordinator'])], ['J_soft_lf_coordinator']),
        ('J_entity_power_early', 'The reserve was 10 to the po&shy;wer of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / entity', ['J_entity_power_early'])], ['J_entity_power_early']),
        ('J_entity_to', 'The reserve was 10 t&shy;o the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / entity', ['J_entity_to'])], ['J_entity_to']),
        ('J_entity_the', 'The reserve was 10 to t&shy;he power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / entity', ['J_entity_the'])], ['J_entity_the']),
        ('J_entity_of', 'The reserve was 10 to the power o&shy;f 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / entity', ['J_entity_of'])], ['J_entity_of']),
        ('J_entity_exponent_word', 'The reserve was 10 ex&shy;ponent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / entity', ['J_entity_exponent_word'])], ['J_entity_exponent_word']),
        ('J_entity_raised', 'The reserve was 10 rai&shy;sed to the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / entity', ['J_entity_raised'])], ['J_entity_raised']),
        ('J_entity_raised_to', 'The reserve was 10 raised t&shy;o the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / entity', ['J_entity_raised_to'])], ['J_entity_raised_to']),
        ('J_entity_multiplied', 'The reserve was 10 multi&shy;plied by 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / entity', ['J_entity_multiplied'])], ['J_entity_multiplied']),
        ('J_entity_by', 'The reserve was 10 multiplied b&shy;y 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / entity', ['J_entity_by'])], ['J_entity_by']),
        ('J_entity_times', 'The reserve was 10 ti&shy;mes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / entity', ['J_entity_times'])], ['J_entity_times']),
        ('J_entity_multiplied_base', 'The sample held 2.5 ti&shy;mes 10 to the 6 copies.', 'joint_guard', [(31, 33, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / entity', ['J_entity_multiplied_base']), (41, 42, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / entity', ['J_entity_multiplied_exponent'])], ['J_entity_multiplied_base', 'J_entity_multiplied_exponent']),
        ('J_entity_later_power', 'The sample held 2.5 10 to the pow&shy;er of 6 copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / entity', ['J_entity_later_power'])], ['J_entity_later_power']),
        ('J_entity_scale', 'The account held 5 bil&shy;lion dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / entity', ['J_entity_scale'])], ['J_entity_scale']),
        ('J_entity_bracket', 'Net debt was 5 (EUR bil&shy;lion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / entity', ['J_entity_bracket'])], ['J_entity_bracket']),
        ('J_entity_cell', '| Net debt | 5 | EUR bil&shy;lion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / entity', ['J_entity_cell'])], ['J_entity_cell']),
        ('J_entity_left_cluster', 'EUR bil&shy;lion 5.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / entity', ['J_entity_left_cluster'])], ['J_entity_left_cluster']),
        ('J_entity_short_code', 'The account held 5 (b&shy;n).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / entity', ['J_entity_short_code'])], ['J_entity_short_code']),
        ('J_entity_unit', 'The account held 5 (EU&shy;R).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / entity', ['J_entity_unit'])], ['J_entity_unit']),
        ('J_entity_coordinator', 'Reserves were 5 a&shy;nd 6 billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / entity', ['J_entity_coordinator'])], ['J_entity_coordinator']),
        ('J_bare_power', 'The reserve was 10 to the pow\ner of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power / bare', ['J_bare_power']), (36, 37, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / bare', ['J_bare_exponent_component'])], ['J_bare_power', 'J_bare_exponent_component']),
        ('J_bare_power_early', 'The reserve was 10 to the po\nwer of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / bare', ['J_bare_power_early'])], ['J_bare_power_early']),
        ('J_bare_to', 'The reserve was 10 t\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / bare', ['J_bare_to'])], ['J_bare_to']),
        ('J_bare_the', 'The reserve was 10 to t\nhe power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / bare', ['J_bare_the'])], ['J_bare_the']),
        ('J_bare_of', 'The reserve was 10 to the power o\nf 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / bare', ['J_bare_of'])], ['J_bare_of']),
        ('J_bare_exponent_word', 'The reserve was 10 ex\nponent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / bare', ['J_bare_exponent_word'])], ['J_bare_exponent_word']),
        ('J_bare_raised', 'The reserve was 10 rai\nsed to the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / bare', ['J_bare_raised'])], ['J_bare_raised']),
        ('J_bare_raised_to', 'The reserve was 10 raised t\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / bare', ['J_bare_raised_to'])], ['J_bare_raised_to']),
        ('J_bare_multiplied', 'The reserve was 10 multi\nplied by 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / bare', ['J_bare_multiplied'])], ['J_bare_multiplied']),
        ('J_bare_by', 'The reserve was 10 multiplied b\ny 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / bare', ['J_bare_by'])], ['J_bare_by']),
        ('J_bare_times', 'The reserve was 10 ti\nmes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / bare', ['J_bare_times'])], ['J_bare_times']),
        ('J_bare_multiplied_base', 'The sample held 2.5 ti\nmes 10 to the 6 copies.', 'joint_guard', [(27, 29, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / bare', ['J_bare_multiplied_base']), (37, 38, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / bare', ['J_bare_multiplied_exponent'])], ['J_bare_multiplied_base', 'J_bare_multiplied_exponent']),
        ('J_bare_later_power', 'The sample held 2.5 10 to the pow\ner of 6 copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / bare', ['J_bare_later_power'])], ['J_bare_later_power']),
        ('J_bare_scale', 'The account held 5 bil\nlion dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / bare', ['J_bare_scale'])], ['J_bare_scale']),
        ('J_bare_bracket', 'Net debt was 5 (EUR bil\nlion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / bare', ['J_bare_bracket'])], ['J_bare_bracket']),
        ('J_bare_cell', '| Net debt | 5 | EUR bil\nlion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / bare', ['J_bare_cell'])], ['J_bare_cell']),
        ('J_bare_left_cluster', 'EUR bil\nlion 5.', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / bare', ['J_bare_left_cluster'])], ['J_bare_left_cluster']),
        ('J_bare_short_code', 'The account held 5 (b\nn).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / bare', ['J_bare_short_code'])], ['J_bare_short_code']),
        ('J_bare_unit', 'The account held 5 (EU\nR).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / bare', ['J_bare_unit'])], ['J_bare_unit']),
        ('J_bare_coordinator', 'Reserves were 5 a\nnd 6 billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / bare', ['J_bare_coordinator'])], ['J_bare_coordinator']),
        ('J_element_power', '<p>The reserve was 10 to the pow-</p><p>er of 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for power / element', ['J_element_power']), (46, 47, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / element', ['J_element_exponent_component'])], ['J_element_power', 'J_element_exponent_component']),
        ('J_element_power_early', '<p>The reserve was 10 to the po-</p><p>wer of 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / element', ['J_element_power_early'])], ['J_element_power_early']),
        ('J_element_to', '<p>The reserve was 10 t-</p><p>o the power of 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for to / element', ['J_element_to'])], ['J_element_to']),
        ('J_element_the', '<p>The reserve was 10 to t-</p><p>he power of 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for the / element', ['J_element_the'])], ['J_element_the']),
        ('J_element_of', '<p>The reserve was 10 to the power o-</p><p>f 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for of / element', ['J_element_of'])], ['J_element_of']),
        ('J_element_exponent_word', '<p>The reserve was 10 ex-</p><p>ponent 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / element', ['J_element_exponent_word'])], ['J_element_exponent_word']),
        ('J_element_raised', '<p>The reserve was 10 rai-</p><p>sed to the power of 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for raised / element', ['J_element_raised'])], ['J_element_raised']),
        ('J_element_raised_to', '<p>The reserve was 10 raised t-</p><p>o the power of 3.</p>', 'joint_guard', [(19, 21, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / element', ['J_element_raised_to'])], ['J_element_raised_to']),
        ('J_element_multiplied', '<p>The reserve was 10 multi-</p><p>plied by 1000.</p>', 'joint_guard', [(19, 21, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / element', ['J_element_multiplied'])], ['J_element_multiplied']),
        ('J_element_by', '<p>The reserve was 10 multiplied b-</p><p>y 1000.</p>', 'joint_guard', [(19, 21, '10', '10000', True, True, 'REFUSED', 'hand label for by / element', ['J_element_by'])], ['J_element_by']),
        ('J_element_times', '<p>The reserve was 10 ti-</p><p>mes 1000.</p>', 'joint_guard', [(19, 21, '10', '10000', True, True, 'REFUSED', 'hand label for times / element', ['J_element_times'])], ['J_element_times']),
        ('J_element_multiplied_base', '<p>The sample held 2.5 ti-</p><p>mes 10 to the 6 copies.</p>', 'joint_guard', [(37, 39, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / element', ['J_element_multiplied_base']), (47, 48, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / element', ['J_element_multiplied_exponent'])], ['J_element_multiplied_base', 'J_element_multiplied_exponent']),
        ('J_element_later_power', '<p>The sample held 2.5 10 to the pow-</p><p>er of 6 copies.</p>', 'joint_guard', [(19, 22, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / element', ['J_element_later_power'])], ['J_element_later_power']),
        ('J_element_scale', '<p>The account held 5 bil-</p><p>lion dollars.</p>', 'joint_guard', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / element', ['J_element_scale'])], ['J_element_scale']),
        ('J_element_bracket', '<p>Net debt was 5 (EUR bil-</p><p>lion).</p>', 'joint_guard', [(16, 17, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / element', ['J_element_bracket'])], ['J_element_bracket']),
        ('J_element_cell', '<p>| Net debt | 5 | EUR bil-</p><p>lion |</p>', 'joint_guard', [(16, 17, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / element', ['J_element_cell'])], ['J_element_cell']),
        ('J_element_left_cluster', '<p>EUR bil-</p><p>lion 5.</p>', 'joint_guard', [(23, 24, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / element', ['J_element_left_cluster'])], ['J_element_left_cluster']),
        ('J_element_short_code', '<p>The account held 5 (b-</p><p>n).</p>', 'joint_guard', [(20, 21, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / element', ['J_element_short_code'])], ['J_element_short_code']),
        ('J_element_unit', '<p>The account held 5 (EU-</p><p>R).</p>', 'joint_guard', [(20, 21, '5', '5', True, False, 'BOUND 5', 'hand label for unit / element', ['J_element_unit'])], ['J_element_unit']),
        ('J_element_coordinator', '<p>Reserves were 5 a-</p><p>nd 6 billion.</p>', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / element', ['J_element_coordinator'])], ['J_element_coordinator']),
        ('J_br_power', 'The reserve was 10 to the pow-<br>er of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power / br', ['J_br_power']), (40, 41, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / br', ['J_br_exponent_component'])], ['J_br_power', 'J_br_exponent_component']),
        ('J_br_power_early', 'The reserve was 10 to the po-<br>wer of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / br', ['J_br_power_early'])], ['J_br_power_early']),
        ('J_br_to', 'The reserve was 10 t-<br>o the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / br', ['J_br_to'])], ['J_br_to']),
        ('J_br_the', 'The reserve was 10 to t-<br>he power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / br', ['J_br_the'])], ['J_br_the']),
        ('J_br_of', 'The reserve was 10 to the power o-<br>f 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / br', ['J_br_of'])], ['J_br_of']),
        ('J_br_exponent_word', 'The reserve was 10 ex-<br>ponent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / br', ['J_br_exponent_word'])], ['J_br_exponent_word']),
        ('J_br_raised', 'The reserve was 10 rai-<br>sed to the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / br', ['J_br_raised'])], ['J_br_raised']),
        ('J_br_raised_to', 'The reserve was 10 raised t-<br>o the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / br', ['J_br_raised_to'])], ['J_br_raised_to']),
        ('J_br_multiplied', 'The reserve was 10 multi-<br>plied by 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / br', ['J_br_multiplied'])], ['J_br_multiplied']),
        ('J_br_by', 'The reserve was 10 multiplied b-<br>y 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / br', ['J_br_by'])], ['J_br_by']),
        ('J_br_times', 'The reserve was 10 ti-<br>mes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / br', ['J_br_times'])], ['J_br_times']),
        ('J_br_multiplied_base', 'The sample held 2.5 ti-<br>mes 10 to the 6 copies.', 'joint_guard', [(31, 33, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / br', ['J_br_multiplied_base']), (41, 42, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / br', ['J_br_multiplied_exponent'])], ['J_br_multiplied_base', 'J_br_multiplied_exponent']),
        ('J_br_later_power', 'The sample held 2.5 10 to the pow-<br>er of 6 copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / br', ['J_br_later_power'])], ['J_br_later_power']),
        ('J_br_scale', 'The account held 5 bil-<br>lion dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / br', ['J_br_scale'])], ['J_br_scale']),
        ('J_br_bracket', 'Net debt was 5 (EUR bil-<br>lion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / br', ['J_br_bracket'])], ['J_br_bracket']),
        ('J_br_cell', '| Net debt | 5 | EUR bil-<br>lion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / br', ['J_br_cell'])], ['J_br_cell']),
        ('J_br_left_cluster', 'EUR bil-<br>lion 5.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / br', ['J_br_left_cluster'])], ['J_br_left_cluster']),
        ('J_br_short_code', 'The account held 5 (b-<br>n).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / br', ['J_br_short_code'])], ['J_br_short_code']),
        ('J_br_unit', 'The account held 5 (EU-<br>R).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / br', ['J_br_unit'])], ['J_br_unit']),
        ('J_br_coordinator', 'Reserves were 5 a-<br>nd 6 billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / br', ['J_br_coordinator'])], ['J_br_coordinator']),
        ('J_crlf_power', 'The reserve was 10 to the pow-\r\ner of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power / crlf', ['J_crlf_power']), (38, 39, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / crlf', ['J_crlf_exponent_component'])], ['J_crlf_power', 'J_crlf_exponent_component']),
        ('J_crlf_power_early', 'The reserve was 10 to the po-\r\nwer of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / crlf', ['J_crlf_power_early'])], ['J_crlf_power_early']),
        ('J_crlf_to', 'The reserve was 10 t-\r\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / crlf', ['J_crlf_to'])], ['J_crlf_to']),
        ('J_crlf_the', 'The reserve was 10 to t-\r\nhe power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / crlf', ['J_crlf_the'])], ['J_crlf_the']),
        ('J_crlf_of', 'The reserve was 10 to the power o-\r\nf 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / crlf', ['J_crlf_of'])], ['J_crlf_of']),
        ('J_crlf_exponent_word', 'The reserve was 10 ex-\r\nponent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / crlf', ['J_crlf_exponent_word'])], ['J_crlf_exponent_word']),
        ('J_crlf_raised', 'The reserve was 10 rai-\r\nsed to the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / crlf', ['J_crlf_raised'])], ['J_crlf_raised']),
        ('J_crlf_raised_to', 'The reserve was 10 raised t-\r\no the power of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / crlf', ['J_crlf_raised_to'])], ['J_crlf_raised_to']),
        ('J_crlf_multiplied', 'The reserve was 10 multi-\r\nplied by 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / crlf', ['J_crlf_multiplied'])], ['J_crlf_multiplied']),
        ('J_crlf_by', 'The reserve was 10 multiplied b-\r\ny 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / crlf', ['J_crlf_by'])], ['J_crlf_by']),
        ('J_crlf_times', 'The reserve was 10 ti-\r\nmes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / crlf', ['J_crlf_times'])], ['J_crlf_times']),
        ('J_crlf_multiplied_base', 'The sample held 2.5 ti-\r\nmes 10 to the 6 copies.', 'joint_guard', [(29, 31, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / crlf', ['J_crlf_multiplied_base']), (39, 40, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / crlf', ['J_crlf_multiplied_exponent'])], ['J_crlf_multiplied_base', 'J_crlf_multiplied_exponent']),
        ('J_crlf_later_power', 'The sample held 2.5 10 to the pow-\r\ner of 6 copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / crlf', ['J_crlf_later_power'])], ['J_crlf_later_power']),
        ('J_crlf_scale', 'The account held 5 bil-\r\nlion dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / crlf', ['J_crlf_scale'])], ['J_crlf_scale']),
        ('J_crlf_bracket', 'Net debt was 5 (EUR bil-\r\nlion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / crlf', ['J_crlf_bracket'])], ['J_crlf_bracket']),
        ('J_crlf_cell', '| Net debt | 5 | EUR bil-\r\nlion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / crlf', ['J_crlf_cell'])], ['J_crlf_cell']),
        ('J_crlf_left_cluster', 'EUR bil-\r\nlion 5.', 'joint_guard', [(15, 16, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / crlf', ['J_crlf_left_cluster'])], ['J_crlf_left_cluster']),
        ('J_crlf_short_code', 'The account held 5 (b-\r\nn).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / crlf', ['J_crlf_short_code'])], ['J_crlf_short_code']),
        ('J_crlf_unit', 'The account held 5 (EU-\r\nR).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / crlf', ['J_crlf_unit'])], ['J_crlf_unit']),
        ('J_crlf_coordinator', 'Reserves were 5 a-\r\nnd 6 billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / crlf', ['J_crlf_coordinator'])], ['J_crlf_coordinator']),
        ('J_upper_power', 'THE RESERVE WAS 10 TO THE POW-\nER OF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power / upper', ['J_upper_power']), (37, 38, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / upper', ['J_upper_exponent_component'])], ['J_upper_power', 'J_upper_exponent_component']),
        ('J_upper_power_early', 'THE RESERVE WAS 10 TO THE PO-\nWER OF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / upper', ['J_upper_power_early'])], ['J_upper_power_early']),
        ('J_upper_to', 'THE RESERVE WAS 10 T-\nO THE POWER OF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / upper', ['J_upper_to'])], ['J_upper_to']),
        ('J_upper_the', 'THE RESERVE WAS 10 TO T-\nHE POWER OF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / upper', ['J_upper_the'])], ['J_upper_the']),
        ('J_upper_of', 'THE RESERVE WAS 10 TO THE POWER O-\nF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / upper', ['J_upper_of'])], ['J_upper_of']),
        ('J_upper_exponent_word', 'THE RESERVE WAS 10 EX-\nPONENT 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / upper', ['J_upper_exponent_word'])], ['J_upper_exponent_word']),
        ('J_upper_raised', 'THE RESERVE WAS 10 RAI-\nSED TO THE POWER OF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / upper', ['J_upper_raised'])], ['J_upper_raised']),
        ('J_upper_raised_to', 'THE RESERVE WAS 10 RAISED T-\nO THE POWER OF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / upper', ['J_upper_raised_to'])], ['J_upper_raised_to']),
        ('J_upper_multiplied', 'THE RESERVE WAS 10 MULTI-\nPLIED BY 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / upper', ['J_upper_multiplied'])], ['J_upper_multiplied']),
        ('J_upper_by', 'THE RESERVE WAS 10 MULTIPLIED B-\nY 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / upper', ['J_upper_by'])], ['J_upper_by']),
        ('J_upper_times', 'THE RESERVE WAS 10 TI-\nMES 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / upper', ['J_upper_times'])], ['J_upper_times']),
        ('J_upper_multiplied_base', 'THE SAMPLE HELD 2.5 TI-\nMES 10 TO THE 6 COPIES.', 'joint_guard', [(28, 30, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / upper', ['J_upper_multiplied_base']), (38, 39, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / upper', ['J_upper_multiplied_exponent'])], ['J_upper_multiplied_base', 'J_upper_multiplied_exponent']),
        ('J_upper_later_power', 'THE SAMPLE HELD 2.5 10 TO THE POW-\nER OF 6 COPIES.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / upper', ['J_upper_later_power'])], ['J_upper_later_power']),
        ('J_upper_scale', 'THE ACCOUNT HELD 5 BIL-\nLION DOLLARS.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / upper', ['J_upper_scale'])], ['J_upper_scale']),
        ('J_upper_bracket', 'NET DEBT WAS 5 (EUR BIL-\nLION).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / upper', ['J_upper_bracket'])], ['J_upper_bracket']),
        ('J_upper_cell', '| NET DEBT | 5 | EUR BIL-\nLION |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / upper', ['J_upper_cell'])], ['J_upper_cell']),
        ('J_upper_left_cluster', 'EUR BIL-\nLION 5.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / upper', ['J_upper_left_cluster'])], ['J_upper_left_cluster']),
        ('J_upper_short_code', 'THE ACCOUNT HELD 5 (B-\nN).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / upper', ['J_upper_short_code'])], ['J_upper_short_code']),
        ('J_upper_unit', 'THE ACCOUNT HELD 5 (EU-\nR).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / upper', ['J_upper_unit'])], ['J_upper_unit']),
        ('J_upper_coordinator', 'RESERVES WERE 5 A-\nND 6 BILLION.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / upper', ['J_upper_coordinator'])], ['J_upper_coordinator']),
        ('J_title_power', 'The Reserve Was 10 To The Pow-\nEr Of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power / title', ['J_title_power']), (37, 38, '3', '3', False, False, 'REFUSED', 'hand label for exponent_component / title', ['J_title_exponent_component'])], ['J_title_power', 'J_title_exponent_component']),
        ('J_title_power_early', 'The Reserve Was 10 To The Po-\nWer Of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for power_early / title', ['J_title_power_early'])], ['J_title_power_early']),
        ('J_title_to', 'The Reserve Was 10 T-\nO The Power Of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for to / title', ['J_title_to'])], ['J_title_to']),
        ('J_title_the', 'The Reserve Was 10 To T-\nHe Power Of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for the / title', ['J_title_the'])], ['J_title_the']),
        ('J_title_of', 'The Reserve Was 10 To The Power O-\nF 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for of / title', ['J_title_of'])], ['J_title_of']),
        ('J_title_exponent_word', 'The Reserve Was 10 Ex-\nPonent 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for exponent_word / title', ['J_title_exponent_word'])], ['J_title_exponent_word']),
        ('J_title_raised', 'The Reserve Was 10 Rai-\nSed To The Power Of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised / title', ['J_title_raised'])], ['J_title_raised']),
        ('J_title_raised_to', 'The Reserve Was 10 Raised T-\nO The Power Of 3.', 'joint_guard', [(16, 18, '10', '1000', True, True, 'REFUSED', 'hand label for raised_to / title', ['J_title_raised_to'])], ['J_title_raised_to']),
        ('J_title_multiplied', 'The Reserve Was 10 Multi-\nPlied By 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for multiplied / title', ['J_title_multiplied'])], ['J_title_multiplied']),
        ('J_title_by', 'The Reserve Was 10 Multiplied B-\nY 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for by / title', ['J_title_by'])], ['J_title_by']),
        ('J_title_times', 'The Reserve Was 10 Ti-\nMes 1000.', 'joint_guard', [(16, 18, '10', '10000', True, True, 'REFUSED', 'hand label for times / title', ['J_title_times'])], ['J_title_times']),
        ('J_title_multiplied_base', 'The Sample Held 2.5 Ti-\nMes 10 To The 6 Copies.', 'joint_guard', [(28, 30, '10', '10', False, False, 'REFUSED', 'hand label for multiplied_base / title', ['J_title_multiplied_base']), (38, 39, '6', '6', False, False, 'REFUSED', 'hand label for multiplied_exponent / title', ['J_title_multiplied_exponent'])], ['J_title_multiplied_base', 'J_title_multiplied_exponent']),
        ('J_title_later_power', 'The Sample Held 2.5 10 To The Pow-\nEr Of 6 Copies.', 'joint_guard', [(16, 19, '5/2', '2500000', True, True, 'REFUSED', 'hand label for later_power / title', ['J_title_later_power'])], ['J_title_later_power']),
        ('J_title_scale', 'The Account Held 5 Bil-\nLion Dollars.', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for scale / title', ['J_title_scale'])], ['J_title_scale']),
        ('J_title_bracket', 'Net Debt Was 5 (Eur Bil-\nLion).', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for bracket / title', ['J_title_bracket'])], ['J_title_bracket']),
        ('J_title_cell', '| Net Debt | 5 | Eur Bil-\nLion |', 'joint_guard', [(13, 14, '5', '5000000000', True, True, 'REFUSED', 'hand label for cell / title', ['J_title_cell'])], ['J_title_cell']),
        ('J_title_left_cluster', 'Eur Bil-\nLion 5.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for left_cluster / title', ['J_title_left_cluster'])], ['J_title_left_cluster']),
        ('J_title_short_code', 'The Account Held 5 (B-\nN).', 'joint_guard', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'hand label for short_code / title', ['J_title_short_code'])], ['J_title_short_code']),
        ('J_title_unit', 'The Account Held 5 (Eu-\nR).', 'joint_guard', [(17, 18, '5', '5', True, False, 'BOUND 5', 'hand label for unit / title', ['J_title_unit'])], ['J_title_unit']),
        ('J_title_coordinator', 'Reserves Were 5 A-\nNd 6 Billion.', 'joint_guard', [(14, 15, '5', '5000000000', True, True, 'REFUSED', 'hand label for coordinator / title', ['J_title_coordinator'])], ['J_title_coordinator']),
        ('D01', 'Net debt\t1234\tEUR billion', 'delimited_currency_cells', [(9, 13, '1234', '1234000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['D01'])], ['D01']),
        ('D02', 'Net debt,1234,EUR billion', 'delimited_currency_cells', [(9, 13, '1234', '1234000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['D02'])], ['D02']),
        ('D03', 'Net debt;1234;EUR billion', 'delimited_currency_cells', [(9, 13, '1234', '1234000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['D03'])], ['D03']),
        ('D04', 'Net debt\t1234\tEUR mn', 'delimited_currency_cells', [(9, 13, '1234', '1234000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['D04'])], ['D04']),
        ('D05', 'Net debt,1234,EUR m', 'delimited_currency_cells', [(9, 13, '1234', '1234000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['D05'])], ['D05']),
        ('D06', 'Revenue,3.5,USD bn', 'delimited_currency_cells', [(8, 11, '7/2', '3500000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['D06'])], ['D06']),
        ('D07', 'Net debt,1234,"EUR bil-\nlion"', 'delimited_currency_cells', [(9, 13, '1234', '1234000000000', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['D07'])], ['D07']),
        ('F01', 'Smith owed 5 thousandths of a share.', 'fraction_word_suffixes', [(11, 12, '5', '1/200', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['F01'])], ['F01']),
        ('F02', 'Smith owed 5 thou-\nsandths of a share.', 'fraction_word_suffixes', [(11, 12, '5', '1/200', True, True, 'REFUSED', 'external power or scale; refuse the selected coefficient', ['F02'])], ['F02']),
        ('N_suffix_1', 'Smith sold 5 billionaire-owned shares.', 'normal', [(11, 12, '5', '5', True, False, 'BOUND 5', 'ordinary scale-prefix content word; ideal BOUND 5', ['N_suffix_1'])], ['N_suffix_1']),
        ('N_suffix_2', 'Smith sold 5 bil-\nlionaire-owned shares.', 'normal', [(11, 12, '5', '5', True, False, 'BOUND 5', 'ordinary scale-prefix content word; ideal BOUND 5', ['N_suffix_2'])], ['N_suffix_2']),
        ('N_suffix_3', 'Smith sold 5 millionaires tickets.', 'normal', [(11, 12, '5', '5', True, False, 'BOUND 5', 'ordinary scale-prefix content word; ideal BOUND 5', ['N_suffix_3'])], ['N_suffix_3']),
        ('N_cell_1', 'Net debt,1234,EUR', 'normal', [(9, 13, '1234', '1234', True, False, 'BOUND 1234', 'unit code without scale; ideal literal value', ['N_cell_1'])], ['N_cell_1']),
        ('N_cell_2', 'Net debt\t1234\tEUR', 'normal', [(9, 13, '1234', '1234', True, False, 'BOUND 1234', 'unit code without scale; ideal literal value', ['N_cell_2'])], ['N_cell_2']),
        ('N_cell_3', 'Net debt;1234;EUR', 'normal', [(9, 13, '1234', '1234', True, False, 'BOUND 1234', 'unit code without scale; ideal literal value', ['N_cell_3'])], ['N_cell_3']),
        ('N_cell_4', 'Smith sold 5 shares; EUR was a currency code.', 'normal', [(11, 12, '5', '5', True, False, 'BOUND 5', 'unit code without scale; ideal literal value', ['N_cell_4'])], ['N_cell_4']),
        ('H01', 'The account held 5 bil-<sup>1</sup>\nlion dollars.', 'hardening', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'standing-rule PR3 observation; ideal refusal', ['H01'])], ['H01']),
        ('H02', 'The account held 5 bil-<!-- a > b -->lion dollars.', 'hardening', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'standing-rule PR3 observation; ideal refusal', ['H02'])], ['H02']),
        ('H03', 'The balance was $1,250,\n000.', 'hardening', [(16, 22, '1250', '1250000', True, True, 'REFUSED', 'standing-rule PR3 observation; ideal refusal', ['H03'])], ['H03']),
        ('H04', 'The balance was $1,25-\n0,000.', 'hardening', [(23, 28, '0', '0', False, False, 'REFUSED', 'standing-rule PR3 observation; ideal refusal', ['H04'])], ['H04']),
        ('H05', 'Total 5<br>000 shares.', 'hardening', [(6, 7, '5', '5000', True, True, 'REFUSED', 'standing-rule PR3 observation; ideal refusal', ['H05']), (11, 14, '0', '0', False, False, 'REFUSED', 'standing-rule PR3 observation; ideal refusal', ['H06'])], ['H05', 'H06']),
        ('H_A07', 'The account held 5 bil-\nli\u043en dollars.', 'hardening', [(17, 18, '5', '5000000000', True, True, 'REFUSED', 'Cyrillic o inside the joined word', ['H_A07'])], ['H_A07']),
        ('H_hard_V048_16', 'The reserve was 10.4 \uab53 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['H_hard_V048_16'])], ['H_hard_V048_16']),
        ('H_hard_V049_16', 'The reserve was 10.4 \ua7b3 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['H_hard_V049_16'])], ['H_hard_V049_16']),
        ('H_hard_V050_16', 'The reserve was 10.4 \uab57 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['H_hard_V050_16'])], ['H_hard_V050_16']),
        ('H_hard_V051_16', 'The reserve was 10.4 \uab59 1000.', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['H_hard_V051_16'])], ['H_hard_V051_16']),
        ('H_hard_V053_16', 'The reserve was 10.4% \u0445 1000.', 'hardening', [(16, 21, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['H_hard_V053_16'])], ['H_hard_V053_16']),
        ('H_hard_V054_17', 'The reserve was [10.4] \u0445 1000.', 'hardening', [(17, 21, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['H_hard_V054_17'])], ['H_hard_V054_17']),
        ('H_hard_V055_16', 'The reserve was 10.4 (\u0445 1000).', 'hardening', [(16, 20, '52/5', '10400', True, True, 'REFUSED', 'external magnitude or interrupted witness; refuse without guessing', ['H_hard_V055_16'])], ['H_hard_V055_16']),
        ('H_hard_V059_14', 'Smith owned 3\u20444 of the lots.', 'hardening', [(14, 15, '4', '4', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', ['H_hard_V059_14'])], ['H_hard_V059_14']),
        ('H_hard_V060_14', 'Smith owned 1\u20442 of the lots.', 'hardening', [(14, 15, '2', '2', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', ['H_hard_V060_14'])], ['H_hard_V060_14']),
        ('H_hard_V061_9', 'Total: 5\u2215000 shares.', 'hardening', [(9, 12, '0', '0', False, False, 'REFUSED', 'component of fraction or grouped numeral; no independent value', ['H_hard_V061_9'])], ['H_hard_V061_9']),
        ('H_hard_V108_0', '5(q)', 'hardening', [(0, 1, '5', '5', False, False, 'REFUSED', 'unknown marker must refuse; chosen conventional scales only where specified', ['H_hard_V108_0'])], ['H_hard_V108_0']),
    ],
}
FROZEN_NUMERIC_SETS["r9_new"] = FROZEN_NUMERIC_SETS["probe"][104:]

# BC, E, RC, L, RD, denominator, D5 E, leaks, independently pinned study totals.
FROZEN_TOTALS = {
    'main': (58, 5, 43, 4, 21, 62, 0, 0),
    'stress': (23, 4, 39, 5, 25, 28, 0, 0),
    'probe': (209, 8, 163, 53, 143, 262, 0, 0),
    'r9_new': (102, 6, 104, 36, 95, 138, 0, 0),
    'repro': (91, 6, 77, 43, 87, 131, 1, 3),
    'cites': (25, 0, 8, 11, 0, 36, 0, 0),
    'r10': (94, 23, 45, 15, 159, 109, 23, 0),
    'r11': (36, 12, 35, 2, 108, 38, 12, 0),
    'r12': (9, 7, 57, 0, 123, 5, 7, 4),
    'r13': (50, 11, 13, 20, 147, 66, 11, 4),
    'r14': (13, 11, 36, 13, 203, 20, 11, 6),
}


def frozen_measurements(name):
    counts = [0] * 8
    results = []
    for identifier, raw, category, labels, source_ids in FROZEN_NUMERIC_SETS[name]:
        for start, end, printed, meant, valid, d5, ideal, why, sources in labels:
            printed, meant = Fraction(printed), Fraction(meant)
            try:
                item = bind_input('x', printed, 'S1', (start, end), raw[start:end],
                                  stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
                value, reason = Fraction(item.value), ''
            except CalculationError as error:
                value, reason = None, str(error)
            kind = (0 if value == meant else 1) if value is not None else (
                2 if not valid else 3 if printed == meant else 4)
            counts[kind] += 1
            counts[5] += valid and printed == meant
            counts[6] += d5 and kind == 1
            counts[7] += value is not None and not valid
            results.append((identifier, raw, start, end, str(value), str(meant), kind,
                            reason, valid, d5, ideal, category, source_ids, sources, why))
    return tuple(counts), results


@pytest.mark.parametrize('name', FROZEN_TOTALS)
def test_a_eleven_sets_fixed_ceilings(name):
    counts, results = frozen_measurements(name)
    assert counts == FROZEN_TOTALS[name], (name, counts, FROZEN_TOTALS[name])


def grounded_calculations(evidence, context, expressions):
    raw = "Combined holdings were 1 shares and 0 adjustments."
    texts = {key: item.excerpt for key, item in evidence.items()}
    texts['S1'] = raw
    evidence['S1'] = replace(evidence['S1'], excerpt=raw)
    calculator = Calculator(stored_text=texts.__getitem__, opened_ids=set(texts))
    records = []
    for expression in expressions:
        literal = '0' if expression == '0' else '1'
        start = raw.index(literal)
        item = bind_input('x', Decimal(literal), 'S1', (start, start + 1), literal,
                          stored_text=texts.__getitem__, opened_ids=set(texts))
        records.append(calculator.calculate('x' if literal == '0' else f'x * ({expression})', (item,)))
    return dict(context, calculations=tuple(records), stored_text=texts.__getitem__, opened_ids=set(texts))


@pytest.mark.parametrize('raw,number,value', [
    ('$1,250.00', '$1,250.00', '1250'), ('(300)', '(300)', '-300'),
    ('12.5%', '12.5%', '12.5'), ('5\nmillion', '5\nmillion', '5000000'),
    ('<span class="count">42</span> files', '42', '42'),
    ('4 cafe\u0301 receipts accompany 5 million records.', '4', '4'),
    ('5 well-known items', '5', '5'), ('5 x-ray images', '5', '5'),
    ('12(b)(6)', '12', '12'), ('401(k).', '401', '401'),
    ('| 5 | $3.20 |', '5', '5'), ('5 shares at $3.20', '5', '5'),
    ('5. Million Corp', '5', '5'), ('5.000', '5.000', '5'),
    ('Smith\xb4s 5 shares were sold.', '5', '5'),
])
def test_shared_lexer_mandatory_controls(raw, number, value):
    from case_intelligence.exact_arithmetic import numeric_tokens
    start = raw.index(number)
    item = bind_input('x', Fraction(value), 'S1', (start, start + len(number)), number,
                      stored_text={'S1': raw}.__getitem__, opened_ids=frozenset({'S1'}))
    assert item.value == Fraction(value)
    token, = numeric_tokens(raw, span=item.span)
    assert (token.start, token.end, token.value) == (*item.span, Fraction(value))


@pytest.mark.parametrize('suffix', ['x 1', 'x 1000', '\u2716 1000', '(qzrv)', '[EUR billion]',
                                  '\tEUR billion', ',"EUR billion"', 'thousandths of a share',
                                  'thou-\nsandths of a share', 'to the pow-\ner of 3',
                                  '\u2063 1000', 'mil<!-- > -->lion shares'])
def test_shared_refusal_bind_forged_rebind_and_printed_path_b(suffix):
    from case_intelligence.exact_arithmetic import CalculationRecord, verify_claim_numbers
    from case_intelligence.generation import _NUMBER
    raw = '5 ' + suffix
    item = BoundInput('x', Decimal(5), 'S1', (0, 1), '5')
    with pytest.raises(CalculationError):
        bind_input('x', Decimal(5), 'S1', (0, 1), '5', stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
    record = CalculationRecord('C1', 'x', Decimal(5), '5', (item,))
    assert not verify_calculations((record,), stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
    # The record is valid in a separate opened source; printing the core cannot bypass context.
    assert not verify_claim_numbers(raw, set(_NUMBER.findall(raw)), _NUMBER,
                                    calculation_ids=['C1'], records=(record,), cited_ids={'S1'},
                                    stored_text={'S1': '5 shares'}.__getitem__, opened_ids={'S1'})


@pytest.mark.parametrize('number,value,accepted', [
    ('10.4', '10.4', True), ('10.400', '10.4', True), ('10.4kg', '10.4', True),
    ('10.4e3', '10.4', False), ('10.4m', '10.4', False), ('999m', '10.4', False),
    ('10.4 thousand', '10400', True), ('10.4k', '10400', True), ('1.04e4', '10400', True),
    ('$10,400', '10400', True), ('\u2212 10.4', '-10.4', True),
    ('\u2212 $10.4', '-10.4', True), ('(10.4)', '-10.4', True),
    ('\u2013 10.4', '10.4', False), ('~$10.4', '10.4', False), ('10.4qzrv', '10.4', False),
    ('1/3', '1/3', True), ('0.3333', '1/3', False), ('1/3%', '1/3', False),
])
def test_claim_number_forms(calculation_case, number, value, accepted):
    from case_intelligence.exact_arithmetic import verify_claim_numbers
    from case_intelligence.generation import _NUMBER
    evidence, context = calculation_case
    context = grounded_calculations(evidence, context, (value,))
    assert verify_claim_numbers(number + ' shares', set(), _NUMBER,
                                calculation_ids=['C1'], records=context['calculations'],
                                cited_ids={'S1'}, stored_text=context['stored_text'],
                                opened_ids=context['opened_ids']) is accepted


def test_no_calculation_path_never_reads_records_or_lexer(monkeypatch):
    import case_intelligence.exact_arithmetic as arithmetic
    from case_intelligence.generation import _NUMBER
    def forbidden(*args, **kwargs):
        pytest.fail('Path A called the computed path')
    monkeypatch.setattr(arithmetic, 'numeric_tokens', forbidden)
    monkeypatch.setattr(arithmetic, 'verify_record', forbidden)
    for ids in ((), []):
        assert arithmetic.verify_claim_numbers('5 million shares', {'5'}, _NUMBER,
                                              calculation_ids=ids, records=(object(),))
        assert not arithmetic.verify_claim_numbers('6 shares', {'5'}, _NUMBER, calculation_ids=ids)


def test_record_isolation_memo_and_citation_check(calculation_case, monkeypatch):
    import case_intelligence.exact_arithmetic as arithmetic
    from case_intelligence.generation import _NUMBER
    evidence, context = calculation_case
    record, = context['calculations']
    calls = []
    original = arithmetic.verify_record
    def counted(*args, **kwargs):
        calls.append(args[0].calculation_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(arithmetic, 'verify_record', counted)
    memo = {}
    records = (record, replace(record, calculation_id='C2', expression='999'))
    args = dict(calculation_ids=['C1'], records=records, stored_text=context['stored_text'],
                opened_ids=context['opened_ids'], verified=memo)
    assert arithmetic.verify_claim_numbers('10.4 shares', set(), _NUMBER, cited_ids={'S1', 'S2'}, **args)
    assert not arithmetic.verify_claim_numbers('10.4 shares', set(), _NUMBER, cited_ids={'S1'}, **args)
    assert calls == ['C1']
    assert not arithmetic.verify_claim_numbers('10.4 shares', set(), _NUMBER,
                                               **dict(args, records=(record, record)))
    assert not arithmetic.verify_calculations(records, stored_text=context['stored_text'], opened_ids={'S1', 'S2'})
    assert ANSWER_SCHEMA['properties']['claims']['items']['properties']['calculation_ids']['maxItems'] == 8
    assert 'uniqueItems' not in ANSWER_SCHEMA['properties']['claims']['items']['properties']['calculation_ids']


@pytest.mark.parametrize('expression', ['x * 2', 'x / 3', 'round(x, 2)', 'percent(x, 100)',
                                       'x * (x / x * 1000000)', 'x - y', 'min(x, max(y,z))'])
def test_semantic_grounding_positive(expression):
    from case_intelligence.exact_arithmetic import _ground
    values = {name: Fraction(5) for name in 'xyz' if name in expression}
    assert _ground(expression, values) == _evaluate(expression, values)


@pytest.mark.parametrize('expression', ['x + 2', '2 - x', 'sum(x, 2 * 500000)', 'min(x, 1 * 5)',
                                       'percent(5 * 1, x)', 'round(x, y)', 'x - x',
                                       'max(x, x/x*5)', 'x + percent(x,x)*10000'])
def test_semantic_constant_contexts_and_d1_costs(expression):
    from case_intelligence.exact_arithmetic import _ground
    with pytest.raises(CalculationError):
        _ground(expression, {name: Fraction(5) for name in 'xy' if name in expression})


def test_grounding_cost_and_pinned_bases(monkeypatch):
    import case_intelligence.exact_arithmetic as arithmetic
    from itertools import islice
    bases = list(islice(arithmetic._probe_bases(['a', 'b', 'c']), 2))
    assert bases[0] == {'a': Fraction(7, 3), 'b': Fraction(16, 5), 'c': Fraction(36, 7)}
    assert bases[1] == {'a': Fraction(16, 5), 'b': Fraction(36, 7), 'c': Fraction(7, 3)}
    calls = []
    original = arithmetic._probe_point
    def counted(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(arithmetic, '_probe_point', counted)
    values = {f'x{i}': Fraction(i + 1) for i in range(128)}
    expression = '+'.join(values)
    assert arithmetic._ground(expression, values)[1] == 8256
    assert len(calls) == 257
    calls.clear()
    with pytest.raises(CalculationError):
        arithmetic._ground('x + x/x * 1000000', {'x': Fraction(5)})
    assert 3 < len(calls) <= 4096
    with pytest.raises(CalculationError):
        arithmetic._ground(expression, dict(values, extra=Fraction(1)))


def test_all_name_orders_min_max():
    from itertools import permutations, product
    from case_intelligence.exact_arithmetic import _ground
    def shapes(names):
        if len(names) == 1:
            yield names[0]
        else:
            for split in range(1, len(names)):
                for left, right, op in product(shapes(names[:split]), shapes(names[split:]), ('min', 'max')):
                    yield f'{op}({left},{right})'
    counts = []
    for alphabet in ('abc', 'abcd'):
        count = 0
        for names in permutations(alphabet):
            values = {name: Fraction(i+1) for i, name in enumerate(names)}
            for expression in shapes(names):
                assert _ground(expression, values) == _evaluate(expression, values)
                count += 1
        counts.append(count)
    assert counts == [48, 960]


def test_normalization_maps_and_one_projection(monkeypatch):
    import case_intelligence.exact_arithmetic as arithmetic
    raw = '<span>42</span> files'
    calls = []
    original = arithmetic.inspect_once
    def counted(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(arithmetic, 'inspect_once', counted)
    view = arithmetic.a_normalize_once(raw, 6, 8)
    assert view.text == '42 files'
    assert view.origins[:2] == ((6, 7), (7, 8))
    assert view.to_normal(6, 8) == (0, 2)
    assert view.to_source(0, 2) == (6, 8)
    assert calls == [1]
    with pytest.raises(ValueError, match='offset-map'):
        view.to_normal(0, 8)
    decoded = list('5 well-known items')
    origins = [(i, i+1) for i in range(len(decoded))]
    assert arithmetic.a_pre_read_gate(decoded, origins, 0, 1) == (decoded, origins)


@pytest.mark.parametrize('raw', ['5 &times; 1000', '5 &#215; 1000', '5 &#xD7; 1000',
                                '5 <sup class="x">3</sup>', '5 &lt;sup&gt;3&lt;/sup&gt;',
                                '5 \u00b3', '5 \u2026 6 million', '5 <b>mil-</b><br>lion',
                                '5 <!--[if true]>million<![endif]-->', '<span>5</div>',
                                '5 (EU-\nR)', '5\u2063 1000', '5\u0301 1000'])
def test_normalized_marker_and_map_refusals(raw):
    start = raw.index('5')
    with pytest.raises(CalculationError):
        bind_input('x', Decimal(5), 'S1', (start, start+1), '5',
                   stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})


@pytest.mark.parametrize('raw,token,meant', [
    ('Smith owed 5 ten-thousandths of a share.', '5', Fraction(1, 2000)),
    ('Smith owed 5 one-thousandth shares.', '5', Fraction(1, 200)),
    ('Smith owed 5 ten-millionths of a share.', '5', Fraction(1, 2000000)),
    ('<td>The reserve was 10.4 ti-</td><td>mes 1,000.</td>', '10.4', Fraction(10400)),
    ("Smith owed 5 thousandth's of a share.", '5', Fraction(1, 200)),
])
def test_r14_p3_families_explicitly_unsafe_residuals(raw, token, meant):
    start = raw.index(token)
    item = bind_input('x', Fraction(token), 'S1', (start, start+len(token)), token,
                      stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
    assert item.value == Fraction(token)
    assert item.value != meant  # Ideal REFUSED; this is a disclosed residual, not semantic correctness.


@pytest.mark.parametrize('raw', ['Smith paid 500 Lakhani Holdings shares.', 'Smith sold 5 Hundredweight of grain.'])
def test_scale_prefix_additional_recovery_costs(raw):
    number = '500' if '500' in raw else '5'
    start = raw.index(number)
    with pytest.raises(CalculationError):
        bind_input('x', Fraction(number), 'S1', (start, start+len(number)), number,
                   stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})


def test_provider_exceptions_and_abstract_opened_sets():
    def broken(identifier):
        raise RuntimeError('synthetic provider failure')
    with pytest.raises(CalculationError, match='unavailable'):
        bind_input('x', Decimal(5), 'S1', (0, 1), '5', stored_text=broken, opened_ids=frozenset({'S1'}))


def test_single_converter_static():
    import ast
    import inspect
    import case_intelligence.exact_arithmetic as arithmetic
    tree = ast.parse(inspect.getsource(arithmetic))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {'bind_input', '_rebind', 'verify_claim_numbers', 'strict_lex', 'read_atom'}:
            assert not any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                           and call.func.id == 'Decimal' for call in ast.walk(node))
    assert 'numeric_tokens' in inspect.getsource(arithmetic.bind_input)
    assert 'numeric_tokens' in inspect.getsource(arithmetic.verify_claim_numbers)
    assert 'bind_input' in inspect.getsource(arithmetic._rebind)


def test_only_token_value_builds_numerals_from_text():
    import ast
    import inspect
    import case_intelligence.exact_arithmetic as arithmetic
    allowed = {'_token_value', '_fraction', '_result', 'expression', 'function', '_probe_ladder'}
    tree = ast.parse(inspect.getsource(arithmetic))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name in allowed:
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in {'Fraction', 'Decimal', 'int'} and call.args
                    and not isinstance(call.args[0], ast.Constant)):
                pytest.fail(f'{node.name}: use _token_value for numeral conversion')


def test_generation_memo_rebinds_once_per_answer(calculation_case):
    evidence, context = calculation_case
    calls = []
    original = context['stored_text']
    def counted(identifier):
        calls.append(identifier)
        return original(identifier)
    context['stored_text'] = counted
    claims = [calculation_claim(f'Combined holdings were {number}%.', calculation_ids=['C1'])
              for number in ('10.4', '10.40', '10.400')]
    answer = verify_answer(claims, evidence, **context)
    assert answer.omitted_claims == 0
    assert [c.text for c in answer.claims] == [c['text'] for c in claims]
    assert calls == ['S1', 'S2']


@pytest.mark.parametrize('size,indices', [(3, [1, 2, 0]), (6, [2, 4, 5, 3, 0, 1]),
                                         (8, [6, 4, 5, 1, 7, 3, 0, 2])])
def test_probe_base_literal_permutations(size, indices):
    from case_intelligence.exact_arithmetic import _probe_bases
    from itertools import islice
    ladder = [Fraction(7, 3), Fraction(16, 5), Fraction(36, 7), Fraction(78, 11),
              Fraction(144, 13), Fraction(222, 17), Fraction(324, 19), Fraction(438, 23)]
    names = [f'x{i}' for i in range(size)]
    first, shuffled = list(islice(_probe_bases(names), 2))
    assert list(first.values()) == ladder[:size]
    assert list(shuffled.values()) == [ladder[i] for i in indices]


def test_rewrite_families_hidden_constants():
    from case_intelligence.exact_arithmetic import _ground
    constants = ['x/x', 'x-x', 'percent(x,x)', '2*500000', 'x*(1000000/x)',
                 'x/(x+y)+y/(x+y)']
    contexts = ['x+({c})', 'x-({c})', 'sum(x,({c}))', 'min(x,({c}))',
                'max(x,({c}))', 'percent(({c}),x)', 'round(({c}),0)']
    for constant in constants:
        for context in contexts:
            expression = context.format(c=constant)
            values = {'x': Fraction(5), 'y': Fraction(7)} if 'y' in expression else {'x': Fraction(5)}
            with pytest.raises(CalculationError):
                _ground(expression, values)


@pytest.mark.parametrize('expression', ['1+rate', '1-rate', 'new/old-1', '100-pct',
                                       'shares*(1+r)', 'end-start+1', 'max(a-b,0)',
                                       'min(x,100)', 'total-1000'])
def test_all_d1_idiom_costs(expression):
    import re
    from case_intelligence.exact_arithmetic import _ground
    names = set(re.findall(r'[A-Za-z]+', expression)) - {'min', 'max'}
    with pytest.raises(CalculationError):
        _ground(expression, {name: Fraction(5) for name in names})


def test_unprobeable_and_deep_nests_are_deterministic():
    from case_intelligence.exact_arithmetic import _ground
    with pytest.raises(CalculationError):
        _ground('*'.join(['x'] * 40), {'x': Fraction(1)})
    expression = 'h'
    for i, name in enumerate(reversed('abcdefg')):
        expression = f'{"min" if i%2 else "max"}({name},{expression})'
    values = {name: Fraction(i+1) for i, name in enumerate('abcdefgh')}
    outcomes = []
    for _ in range(2):
        try:
            outcomes.append(_ground(expression, values))
        except CalculationError as error:
            outcomes.append(str(error))
    assert outcomes[0] == outcomes[1]
    if isinstance(outcomes[0], tuple):
        assert outcomes[0] == _evaluate(expression, values)


def test_normalization_raw_bounds_and_fail_closed_types():
    from case_intelligence.exact_arithmetic import numeric_tokens
    for raw, span in [(None, (0, 1)), ('5', (False, 1)), ('5', (0, 2)),
                      ('5'*129, (0, 129)), ('5', (1, 0)), ('5', [0]), ('5', (0, 0))]:
        assert all(t.ambiguous and t.value is None for t in numeric_tokens(raw, span=span))
    for tail in (' '*129 + 'million', ' and '*30 + '5 million'):
        assert numeric_tokens('5'+tail, span=(0, 1))[0].ambiguous
    assert numeric_tokens('5 files'+' '*150, span=(0, 1))[0].value == 5


@pytest.mark.parametrize('raw,span,extents', [
    ('125 shares', (0, 2), [(0, 3)]),
    ('125 shares', (1, 3), [(0, 3)]),
    ('1,250.50 dollars', (2, 8), [(0, 8)]),
    ('5 million shares', (0, 1), [(0, 9)]),
    ('5 shares and 6 files', (0, 14), [(0, 1), (13, 14)]),
])
def test_span_selects_canonical_tokens_without_setting_boundaries(raw, span, extents):
    from case_intelligence.exact_arithmetic import numeric_tokens
    selected = numeric_tokens(raw, span=span)
    assert [(t.start, t.end) for t in selected] == extents
    assert all(not t.ambiguous for t in selected)
    for value in (Fraction(12), selected[0].value):
        with pytest.raises(CalculationError):
            bind_input('x', value, 'S1', span, raw[slice(*span)],
                       stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})


@pytest.mark.parametrize('raw', [
    '\uff15 \u00d7 \uff11\uff10\uff10\uff10',
    '\uff15 x 1000', '\uff15 (qzrv)', '\uff15\u2063 1000',
    '\u2464 x 1000', '\uff15 &times; \uff11\uff10\uff10\uff10',
])
def test_normalized_numerals_cannot_disappear_before_path_b_precheck(raw):
    from case_intelligence.exact_arithmetic import CalculationRecord, numeric_tokens, verify_claim_numbers
    from case_intelligence.generation import _NUMBER
    item = BoundInput('x', Decimal(5), 'S1', (0, 1), '5')
    record = CalculationRecord('C1', 'x', Decimal(5), '5', (item,))
    assert any(t.context_ambiguous for t in numeric_tokens(raw))
    assert not verify_claim_numbers(raw, set(_NUMBER.findall(raw)), _NUMBER,
                                    calculation_ids=['C1'], records=(record,), cited_ids={'S1'},
                                    stored_text={'S1': '5 shares'}.__getitem__, opened_ids={'S1'})


@pytest.mark.parametrize('raw,span,value', [
    ('<span class="count">42</span> files', (20, 22), 42),
    ('\uff11\uff12\uff15 shares', (0, 3), 125),
    ('\u2464 shares', (0, 1), 5),
])
def test_claim_discovery_preserves_raw_offsets_and_values(raw, span, value):
    from case_intelligence.exact_arithmetic import numeric_tokens
    token, = numeric_tokens(raw)
    assert not token.ambiguous
    assert (token.start, token.end, token.core_start, token.core_end) == (*span, *span)
    assert token.value == value
    if not raw.isascii():
        with pytest.raises(CalculationError):
            bind_input('x', Fraction(value), 'S1', span, raw[slice(*span)],
                       stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})


def test_non_numeral_unicode_words_are_not_numeric_tokens():
    from case_intelligence.exact_arithmetic import numeric_tokens
    assert numeric_tokens('\u4e00\u4e8c\u4e09') == ()


def test_r11_all_required_refusals_use_three_paths(calculation_case):
    from case_intelligence.exact_arithmetic import CalculationRecord, verify_claim_numbers
    from case_intelligence.generation import _NUMBER
    evidence, context = calculation_case
    for identifier, raw, category, labels, sources in FROZEN_NUMERIC_SETS['r11']:
        index = int(identifier[1:])
        if not (index <= 114 or index in {135, 138, 139, 140, 141, 146, 147}):
            continue
        for start, end, printed, meant, valid, d5, ideal, why, ids in labels:
            with pytest.raises(CalculationError):
                bind_input('x', Fraction(printed), 'S1', (start, end), raw[start:end],
                           stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
            item = BoundInput('x', Fraction(printed), 'S1', (start, end), raw[start:end])
            _, exact, display = _evaluate('x', {'x': Fraction(printed)})
            record = CalculationRecord('C1', 'x', exact, display, (item,))
            assert not verify_calculations((record,), stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
            if valid and d5:
                assert not verify_claim_numbers(raw, set(_NUMBER.findall(raw)), _NUMBER,
                                                calculation_ids=['C1'], records=context['calculations'],
                                                stored_text=context['stored_text'], opened_ids=context['opened_ids'],
                                                cited_ids={'S1', 'S2'}), identifier


# Historical rev14 measurement positions, separate from hand-authored meanings.
REV14_POSITIONS = {
    'main': {'LOST': [('M06', 20, 21), ('M08', 17, 19), ('M16', 38, 42), ('M41', 43, 48)], 'EXPOSED': [('M21', 30, 42), ('M33', 18, 19), ('M42', 56, 57), ('M42', 60, 61), ('M42', 64, 66)], 'leak': []},
    'stress': {'LOST': [('S15', 18, 20), ('S17', 18, 19), ('S19', 22, 27), ('S21', 19, 20), ('S22', 18, 19)], 'EXPOSED': [('S18', 35, 47), ('S31', 16, 17), ('S32', 39, 40), ('S32', 43, 44)], 'leak': []},
    'probe': {'LOST': [('P013', 25, 27), ('P019', 14, 17), ('P020', 2, 3), ('P021', 12, 13), ('P022', 18, 19), ('P044', 22, 23), ('P057', 0, 2), ('P063', 13, 17), ('P064', 6, 7), ('P065', 6, 8), ('P079', 8, 10), ('P081', 11, 12), ('P082', 11, 12), ('P082', 15, 16), ('P084', 16, 17), ('P098', 7, 8), ('P102', 9, 10), ('P124', 13, 16), ('P124', 18, 23), ('P125', 7, 8), ('P125', 17, 18), ('P126', 21, 22), ('P126', 26, 28), ('P128', 5, 6), ('P129', 5, 6), ('P139', 5, 14), ('P157', 4, 5), ('P158', 4, 5), ('P160', 4, 5), ('P161', 5, 6), ('P162', 0, 1), ('P162', 4, 5), ('P163', 0, 1), ('P172', 4, 13), ('P181', 8, 10), ('P182', 5, 9), ('P193', 6, 8), ('P199', 4, 8), ('P200', 7, 11), ('P201', 0, 5), ('P201', 8, 13), ('P202', 13, 16), ('P209', 16, 17), ('P216', 20, 22), ('P241', 8, 10), ('P242', 5, 7), ('P243', 7, 8), ('P244', 8, 10), ('P245', 10, 12), ('P247', 8, 10), ('P253', 21, 24), ('P253', 26, 29), ('P255', 5, 6)], 'EXPOSED': [('P023', 20, 24), ('P024', 20, 24), ('P042', 5, 6), ('P203', 16, 17), ('P204', 24, 25), ('P205', 16, 17), ('P206', 16, 17), ('P207', 16, 17), ('P210', 36, 41)], 'leak': []},
    'r9_new': {'LOST': [('P124', 13, 16), ('P124', 18, 23), ('P125', 7, 8), ('P125', 17, 18), ('P126', 21, 22), ('P126', 26, 28), ('P128', 5, 6), ('P129', 5, 6), ('P139', 5, 14), ('P157', 4, 5), ('P158', 4, 5), ('P160', 4, 5), ('P161', 5, 6), ('P162', 0, 1), ('P162', 4, 5), ('P163', 0, 1), ('P172', 4, 13), ('P181', 8, 10), ('P182', 5, 9), ('P193', 6, 8), ('P199', 4, 8), ('P200', 7, 11), ('P201', 0, 5), ('P201', 8, 13), ('P202', 13, 16), ('P209', 16, 17), ('P216', 20, 22), ('P241', 8, 10), ('P242', 5, 7), ('P243', 7, 8), ('P244', 8, 10), ('P245', 10, 12), ('P247', 8, 10), ('P253', 21, 24), ('P253', 26, 29), ('P255', 5, 6)], 'EXPOSED': [('P203', 16, 17), ('P204', 24, 25), ('P205', 16, 17), ('P206', 16, 17), ('P207', 16, 17), ('P210', 36, 41)], 'leak': []},
    'repro': {'LOST': [('R024', 13, 16), ('R024', 18, 23), ('R025', 21, 22), ('R025', 26, 28), ('R026', 21, 24), ('R026', 26, 29), ('R027', 17, 19), ('R030', 32, 34), ('R031', 28, 30), ('R032', 16, 18), ('R034', 13, 16), ('R035', 25, 28), ('R036', 9, 10), ('R037', 10, 11), ('R038', 9, 10), ('R038', 11, 14), ('R039', 6, 9), ('R040', 5, 6), ('R040', 7, 10), ('R041', 11, 13), ('R042', 36, 40), ('R090', 10, 15), ('R091', 13, 16), ('R091', 18, 21), ('R092', 11, 15), ('R092', 17, 21), ('R093', 30, 34), ('R094', 11, 15), ('R095', 12, 13), ('R097', 14, 15), ('R098', 12, 13), ('R099', 6, 7), ('R100', 5, 6), ('R136', 11, 12), ('R137', 15, 18), ('R137', 20, 23), ('R138', 21, 22), ('R138', 26, 28), ('R139', 13, 16), ('R142', 21, 23), ('R144', 18, 21), ('R145', 8, 9), ('R145', 10, 13)], 'EXPOSED': [('R022', 9, 10), ('R079', 0, 4), ('R081', 0, 1), ('R083', 13, 17), ('R084', 16, 17), ('R085', 20, 24)], 'leak': [('R077', 7, 9), ('R078', 7, 9), ('R086', 14, 17)]},
    'cites': {'LOST': [('C01', 18, 20), ('C03', 15, 17), ('C04', 4, 6), ('C05', 2, 5), ('C07', 0, 1), ('C09', 23, 24), ('C12', 16, 18), ('C15', 18, 20), ('C16', 21, 22), ('C20', 16, 18), ('C20', 33, 36)], 'EXPOSED': [], 'leak': []},
    'r10': {'LOST': [('T092', 0, 1), ('T093', 0, 1), ('T173', 28, 30), ('T181', 13, 14), ('T182', 18, 24), ('T183', 10, 13), ('T184', 10, 13), ('T185', 10, 13), ('T186', 10, 13), ('T187', 17, 20), ('T278', 0, 1), ('T279', 6, 7), ('T280', 0, 2), ('T281', 0, 1), ('T284', 6, 7)], 'EXPOSED': [('T066', 15, 16), ('T067', 15, 16), ('T068', 15, 16), ('T069', 8, 9), ('T070', 15, 16), ('T071', 9, 10), ('T072', 9, 10), ('T073', 9, 10), ('T074', 9, 10), ('T075', 9, 10), ('T076', 9, 10), ('T077', 9, 10), ('T078', 9, 10), ('T079', 15, 16), ('T080', 15, 16), ('T081', 15, 16), ('T082', 15, 16), ('T083', 15, 16), ('T088', 13, 17), ('T089', 13, 17), ('T090', 13, 18), ('T123', 23, 24), ('T286', 13, 17)], 'leak': []},
    'r11': {'LOST': [('U114', 8, 9), ('U135', 8, 11)], 'EXPOSED': [('U142', 13, 18), ('U143', 13, 18), ('U144', 13, 18), ('U145', 13, 18), ('U146', 13, 18), ('U148', 13, 18), ('U149', 13, 18), ('U150', 16, 20), ('U151', 16, 20), ('U152', 6, 10), ('U153', 15, 16), ('U154', 15, 16), ('U155', 15, 16)], 'leak': []},
    'r12': {'LOST': [], 'EXPOSED': [('V048', 16, 20), ('V049', 16, 20), ('V050', 16, 20), ('V051', 16, 20), ('V053', 16, 21), ('V054', 17, 21), ('V055', 16, 20)], 'leak': [('V059', 14, 15), ('V060', 14, 15), ('V061', 9, 12), ('V108', 0, 1)]},
    'r13': {'LOST': [('N15', 11, 12), ('N24', 11, 12), ('N25', 11, 12), ('N26', 11, 12), ('N27', 13, 14), ('N28', 11, 12), ('N29', 4, 5), ('U01', 15, 16), ('U02', 15, 16), ('U04', 11, 12), ('D01', 0, 1), ('D02', 0, 1), ('L01', 1, 6), ('L02', 14, 17), ('L03', 1, 4), ('L04', 11, 17), ('L05', 7, 10), ('L06', 3, 4), ('L07', 11, 12)], 'EXPOSED': [('W45', 27, 28), ('X06', 13, 18), ('X07', 17, 18), ('X08', 17, 18), ('A07', 17, 18), ('hard_V048_16', 16, 20), ('hard_V049_16', 16, 20), ('hard_V050_16', 16, 20), ('hard_V051_16', 16, 20), ('hard_V053_16', 16, 21), ('hard_V054_17', 17, 21), ('hard_V055_16', 16, 20)], 'leak': [('hard_V059_14', 14, 15), ('hard_V060_14', 14, 15), ('hard_V061_9', 9, 12), ('hard_V108_0', 0, 1)]},
    'r14': {'LOST': [('J_hyphen_lf_unit', 17, 18), ('J_soft_unit', 17, 18), ('J_soft_lf_unit', 17, 18), ('J_entity_unit', 17, 18), ('J_bare_unit', 17, 18), ('J_element_unit', 20, 21), ('J_br_unit', 17, 18), ('J_crlf_unit', 17, 18), ('J_upper_unit', 17, 18), ('J_title_unit', 17, 18)], 'EXPOSED': [('P01', 22, 26), ('P02', 22, 26), ('P03', 25, 29), ('P04', 14, 18), ('P06', 16, 18), ('P07', 16, 18), ('P08', 16, 18), ('P09', 16, 18), ('P10', 16, 18), ('P11', 16, 18), ('J_hyphen_lf_the', 16, 18), ('J_hyphen_lf_of', 16, 18), ('J_hyphen_lf_later_power', 16, 19), ('J_soft_power', 16, 18), ('J_soft_power_early', 16, 18), ('J_soft_the', 16, 18), ('J_soft_of', 16, 18), ('J_soft_later_power', 16, 19), ('J_soft_lf_power_early', 16, 18), ('J_soft_lf_the', 16, 18), ('J_soft_lf_of', 16, 18), ('J_soft_lf_later_power', 16, 19), ('J_entity_power_early', 16, 18), ('J_entity_the', 16, 18), ('J_entity_of', 16, 18), ('J_entity_later_power', 16, 19), ('J_bare_power', 16, 18), ('J_bare_power_early', 16, 18), ('J_bare_the', 16, 18), ('J_bare_of', 16, 18), ('J_bare_later_power', 16, 19), ('J_element_power', 19, 21), ('J_element_power_early', 19, 21), ('J_element_the', 19, 21), ('J_element_of', 19, 21), ('J_element_later_power', 19, 22), ('J_br_power', 16, 18), ('J_br_power_early', 16, 18), ('J_br_the', 16, 18), ('J_br_of', 16, 18), ('J_br_later_power', 16, 19), ('J_crlf_power', 16, 18), ('J_crlf_power_early', 16, 18), ('J_crlf_the', 16, 18), ('J_crlf_of', 16, 18), ('J_crlf_later_power', 16, 19), ('J_upper_power', 16, 18), ('J_upper_power_early', 16, 18), ('J_upper_the', 16, 18), ('J_upper_of', 16, 18), ('J_upper_later_power', 16, 19), ('J_title_power', 16, 18), ('J_title_power_early', 16, 18), ('J_title_the', 16, 18), ('J_title_of', 16, 18), ('J_title_later_power', 16, 19), ('D01', 9, 13), ('D02', 9, 13), ('D03', 9, 13), ('D04', 9, 13), ('D05', 9, 13), ('D06', 8, 11), ('D07', 9, 13), ('F01', 11, 12), ('F02', 11, 12), ('H01', 17, 18), ('H02', 17, 18), ('H03', 16, 22), ('H05', 6, 7), ('H_A07', 17, 18), ('H_hard_V048_16', 16, 20), ('H_hard_V049_16', 16, 20), ('H_hard_V050_16', 16, 20), ('H_hard_V051_16', 16, 20), ('H_hard_V053_16', 16, 21), ('H_hard_V054_17', 17, 21), ('H_hard_V055_16', 16, 20)], 'leak': [('P01', 45, 47), ('P06', 37, 38), ('P07', 37, 38), ('P08', 40, 41), ('P09', 37, 38), ('P13', 22, 24), ('J_hyphen_lf_multiplied_base', 38, 39), ('J_soft_power', 36, 37), ('J_soft_multiplied_base', 37, 38), ('J_soft_lf_multiplied_base', 38, 39), ('J_entity_multiplied_base', 41, 42), ('J_bare_power', 36, 37), ('J_bare_multiplied_base', 37, 38), ('J_element_power', 46, 47), ('J_element_multiplied_base', 47, 48), ('J_br_power', 40, 41), ('J_br_multiplied_base', 41, 42), ('J_crlf_power', 38, 39), ('J_crlf_multiplied_base', 39, 40), ('J_upper_power', 37, 38), ('J_upper_multiplied_base', 38, 39), ('J_title_power', 37, 38), ('J_title_multiplied_base', 38, 39), ('H04', 23, 28), ('H05', 11, 14), ('H_hard_V059_14', 14, 15), ('H_hard_V060_14', 14, 15), ('H_hard_V061_9', 9, 12), ('H_hard_V108_0', 0, 1)]},
}


@pytest.mark.parametrize('name', FROZEN_TOTALS)
def test_exact_historical_loss_exposure_and_leak_deltas(name):
    counts, rows = frozen_measurements(name)
    lost = {(r[0], r[2], r[3]) for r in rows if r[6] == 3}
    exposed = {(r[0], r[2], r[3]) for r in rows if r[6] == 1}
    leaks = {(r[0], r[2], r[3]) for r in rows if not r[8] and r[6] in (0, 1)}
    added = ({('P01', 11, 12)} if name == 'r13' else
             {('N_suffix_1', 11, 12), ('N_suffix_2', 11, 12), ('N_suffix_3', 11, 12)} if name == 'r14' else set())
    assert lost - set(REV14_POSITIONS[name]['LOST']) == added
    assert set(REV14_POSITIONS[name]['LOST']) <= lost
    assert exposed <= set(REV14_POSITIONS[name]['EXPOSED'])
    assert leaks <= set(REV14_POSITIONS[name]['leak'])


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_sampled_five_and_six_input_min_max(seed):
    import random
    from case_intelligence.exact_arithmetic import _ground
    rng = random.Random(seed)
    for size in (5, 6):
        for _ in range(64):
            names = [f'x{i}' for i in range(size)]
            rng.shuffle(names)
            nodes = list(names)
            while len(nodes) > 1:
                index = rng.randrange(len(nodes)-1)
                nodes[index:index+2] = [f'{rng.choice(("min", "max"))}({nodes[index]},{nodes[index+1]})']
            values = {name: Fraction(i+1) for i, name in enumerate(names)}
            assert _ground(nodes[0], values) == _evaluate(nodes[0], values)


@pytest.mark.parametrize('expression,values', [
    ('x*y', {'x': Fraction(0), 'y': Fraction(5)}),
    ('a*(b-c)', {'a': Fraction(7), 'b': Fraction(5), 'c': Fraction(5)}),
    ('percent(a,b)*c', {'a': Fraction(3), 'b': Fraction(5), 'c': Fraction(0)}),
])
def test_zero_actual_values_do_not_hide_generic_sensitivity(expression, values):
    from case_intelligence.exact_arithmetic import _ground
    assert _ground(expression, values)[1] == 0


def test_d1_supported_replacements_and_disclosed_instance_residual():
    from case_intelligence.exact_arithmetic import _ground
    for expression, values in [
        ('percent(new-old,old)', {'new': Fraction(8), 'old': Fraction(5)}),
        ('shares+shares*rate/100', {'shares': Fraction(5), 'rate': Fraction(2)}),
        ('shares+one*1000000', {'shares': Fraction(5), 'one': Fraction(1)}),
    ]:
        assert _ground(expression, values) == _evaluate(expression, values)


def test_offset_map_partial_units_and_markup_geometry():
    from case_intelligence.exact_arithmetic import AOffsetView, a_normalize_once
    view = AOffsetView('ffi', ((0, 1), (0, 1), (0, 1)), False, False)
    assert view.to_normal(0, 1) == (0, 3)
    with pytest.raises(ValueError, match='offset-map'):
        view.to_source(0, 1)
    for raw, expected in [('5 files <sup>3</sup>', '5 files ^(3)'),
                          ('5 files \u207b\u00b3', '5 files ^(-3)'),
                          ('5 files <sub/>', '5 files _()'),
                          ('<td>5 files</td><th>other</th>', '|5 files||other|'),
                          ('5 files &amp;times;', '5 files &times;')]:
        start = raw.index('5')
        view = a_normalize_once(raw, start, start+1)
        assert view.text == expected
        assert view.to_source(*view.to_normal(start, start+1)) == (start, start+1)


@pytest.mark.parametrize('glyph', ['\u00b4', '&acute;', '&#180;', '&#xB4;', "'", '\u2019', '\u02bc'])
def test_grouped_numeral_every_component(glyph):
    raw = f'CHF 1{glyph}250{glyph}000'
    for start, end in [(4, 5), (5+len(glyph), 8+len(glyph)),
                       (8+2*len(glyph), 11+2*len(glyph))]:
        with pytest.raises(CalculationError):
            bind_input('x', Fraction(raw[start:end]), 'S1', (start, end), raw[start:end],
                       stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})


def test_clause_stop_and_content_stop_preserve_normal_shapes():
    from case_intelligence.exact_arithmetic import numeric_tokens, a_structural_marker
    for glyph in '[](){}<>"\u2018\u2019\u201c\u201d.:;!?,&|\u3008\u3009':
        assert not a_structural_marker(glyph + ' 1000', 0)
        assert numeric_tokens('5 files ' + glyph + ' 1000', span=(0, 1))[0].value == 5


def test_all_currency_unit_exemptions_and_unknown_categories():
    from case_intelligence.exact_arithmetic import A_UNITS, numeric_tokens
    assert set(A_UNITS) == set('usd eur gbp cad aud chf jpy % pct pp ft in yd mi km cm kg g lb lbs oz mg ml hr hrs min sec yr yrs mo wk day days st nd rd th no sq inc llc ltd co'.split())
    for unit in A_UNITS.keys() - {'%'}:
        raw = f'5 ({unit})'
        assert numeric_tokens(raw, span=(0, 1))[0].value == 5
    for code in ['q', 'qz', 'qzr', 'qzrv']:
        for raw in [f'5{code}', f'5 ({code})', f'| 5 | {code} |', f'5 (EUR {code})']:
            start = raw.index('5')
            assert numeric_tokens(raw, span=(start, start+1))[0].ambiguous


def test_independent_p256_meaning_is_retained_separately():
    row = next(r for r in FROZEN_NUMERIC_SETS['probe'] if r[0] == 'P256')
    assert any(n[3] == '3500000' for n in row[3])
    raw = 'The reserve was 7.5 x 10^6 units.'
    with pytest.raises(CalculationError):
        bind_input('x', Fraction(7500000), 'S1', (16, 19), '7.5',
                   stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})


@pytest.mark.parametrize('name,identifier', [('probe', 'P023'), ('r11', 'U146'), ('r13', 'X07')])
def test_rev15_named_improvements(name, identifier):
    _, rows = frozen_measurements(name)
    selected = [r for r in rows if r[0] == identifier]
    assert selected
    assert all(r[4] == 'None' for r in selected)


def test_r14_review_components_and_company_controls():
    _, rows = frozen_measurements('r14')
    targets = {f'P{i:02}' for i in range(1, 14)}
    found = set()
    for row in rows:
        ids = set(row[13]) | {row[0]}
        if ids & targets:
            assert row[4] == 'None', row[:7]
            found.update(ids & targets)
        if row[0] in {'N01', 'N02', 'N03'}:
            assert row[4] == '10'
            assert row[6] == 0
    assert found == targets


def test_shared_gate_never_calls_value_readers(monkeypatch):
    import case_intelligence.exact_arithmetic as arithmetic
    def forbidden(*args, **kwargs):
        pytest.fail('Inspection called a value reader')
    monkeypatch.setattr(arithmetic, 'read_atom', forbidden)
    monkeypatch.setattr(arithmetic, '_token_value', forbidden)
    monkeypatch.setattr(arithmetic, 'power', forbidden)
    raw = '5 well-known items'
    origins = [(i, i+1) for i in range(len(raw))]
    assert arithmetic.a_pre_read_gate(list(raw), origins, 0, 1) == (list(raw), origins)
    raw = '5 mil-\nlion shares'
    origins = [(i, i+1) for i in range(len(raw))]
    with pytest.raises(ValueError, match='category gate'):
        arithmetic.a_pre_read_gate(list(raw), origins, 0, 1)


@pytest.mark.parametrize('name', ['r13', 'r14'])
def test_joint_fixtures_bind_rebind_and_printed_claim_precheck(name, calculation_case):
    from case_intelligence.exact_arithmetic import CalculationRecord, verify_claim_numbers
    from case_intelligence.generation import _NUMBER
    _, context = calculation_case
    checked = 0
    for identifier, raw, category, labels, _ in FROZEN_NUMERIC_SETS[name]:
        if category in {'hardening', 'diagnosis_A', 'label_dispute'}:
            continue
        disclosed_binding_residuals = {('r13', 'W45'), ('r13', 'X06'), ('r13', 'X08')}
        if (name, identifier) in disclosed_binding_residuals:
            continue
        for start, end, printed, meant, valid, d5, ideal, why, sources in labels:
            if ideal != 'REFUSED':
                continue
            with pytest.raises(CalculationError):
                bind_input('x', Fraction(printed), 'S1', (start, end), raw[start:end],
                           stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
            item = BoundInput('x', Fraction(printed), 'S1', (start, end), raw[start:end])
            _, exact, display = _evaluate('x', {'x': Fraction(printed)})
            forged = CalculationRecord('C1', 'x', exact, display, (item,))
            assert not verify_calculations((forged,), stored_text={'S1': raw}.__getitem__, opened_ids={'S1'})
            printed_complete_scale_residual = (name, identifier) == ('r13', 'W44')
            assert verify_claim_numbers(raw, set(_NUMBER.findall(raw)), _NUMBER,
                                        calculation_ids=['C1'], records=context['calculations'],
                                        stored_text=context['stored_text'], opened_ids=context['opened_ids'],
                                        cited_ids={'S1', 'S2'}) is printed_complete_scale_residual, (name, identifier, start, end)
            checked += 1
    assert checked >= 100


@pytest.mark.parametrize('raw,ambiguous', [('A 5 million-\ndollar loss was booked.', False),
                                         ('The account held 5 mil-\nlions of dollars.', True)])
def test_joined_scale_word_boundary_refuses_printed_claim(raw, ambiguous, calculation_case):
    from case_intelligence.exact_arithmetic import numeric_tokens, verify_claim_numbers
    from case_intelligence.generation import _NUMBER
    _, context = calculation_case
    token, = numeric_tokens(raw)
    assert token.context_ambiguous is ambiguous
    assert token.value == (None if ambiguous else Fraction(5000000))
    args = dict(calculation_ids=['C1'], records=context['calculations'],
                stored_text=context['stored_text'], opened_ids=context['opened_ids'],
                cited_ids={'S1', 'S2'})
    assert not verify_claim_numbers(raw, set(), _NUMBER, **args)
    assert verify_claim_numbers(raw, {'5'}, _NUMBER, **args) is not ambiguous
    for complete_scale in ('5 million dollars', '5 mil dollars'):
        token, = numeric_tokens(complete_scale)
        assert token.value == 5000000 and not token.context_ambiguous
        assert verify_claim_numbers(complete_scale, {'5'}, _NUMBER, **args)


def test_unresolved_grounding_stops_at_point_budget(monkeypatch):
    import case_intelligence.exact_arithmetic as arithmetic
    calls = 0
    original = arithmetic._probe_point
    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)
    monkeypatch.setattr(arithmetic, '_probe_point', counted)
    values = {f'x{i}': Fraction(i+1) for i in range(128)}
    expression = '+'.join(f'{name}/{name}' for name in values)
    with pytest.raises(CalculationError, match='point budget'):
        arithmetic._ground(expression, values)
    assert calls == 3855
    assert calls <= arithmetic.MAX_POINTS == 4096
