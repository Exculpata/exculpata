"""Synthetic contracts for the standalone calculator and source-bound records."""

from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from case_intelligence.exact_arithmetic import (
    BoundInput, CalculationError, Calculator, MAX_DEPTH, MAX_LENGTH,
    bind_input, verify_calculations,
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
