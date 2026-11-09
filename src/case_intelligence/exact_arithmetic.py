"""Exact, bounded arithmetic and records grounded in opened raw source strings.

Decimal literals, declared names, unary signs, + - * /, parentheses and the five
functions below form the entire grammar. round uses ties-to-even. Terminating
results display as fixed decimals without trailing zeros; other results display
as reduced numerator/denominator. No ambient Decimal context affects arithmetic.
Source callbacks must resolve the authorized raw text for each evidence identity.
"""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import re
from typing import Callable

MAX_LENGTH = 4096
MAX_DEPTH = 32
_MAX_BITS = 4096
_NUMBER = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_NAME = r"[A-Za-z_][A-Za-z_0-9]*"
_TOKEN = re.compile(rf"{_NUMBER}|{_NAME}|[+*/(),-]")
_FUNCTIONS = {"sum", "min", "max", "round", "percent"}
Exact = Decimal | Fraction
StoredText = Callable[[str], str]


class CalculationError(ValueError):
    """Unsupported arithmetic or unverifiable source support."""


def _bounded(value: Fraction) -> Fraction:
    if max(value.numerator.bit_length(), value.denominator.bit_length()) > _MAX_BITS:
        raise CalculationError("Arithmetic size limit exceeded")
    return value


def _fraction(value: Exact) -> Fraction:
    if type(value) is Decimal:
        if (not value.is_finite() or len(value.as_tuple().digits) > MAX_LENGTH
                or abs(value.as_tuple().exponent) > MAX_LENGTH):
            raise CalculationError("Expected a bounded finite Decimal")
    elif type(value) is not Fraction:
        raise CalculationError("Expected Decimal or Fraction, never float")
    return _bounded(Fraction(value))


def _result(value: Fraction) -> tuple[Exact, str]:
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return value, f"{value.numerator}/{value.denominator}"
    places = max(twos, fives)
    scaled = abs(value.numerator) * 2 ** (places - twos) * 5 ** (places - fives)
    # Decimal tuple construction is exact even under a low-precision context.
    exact = Decimal((int(value < 0), tuple(map(int, str(scaled))), -places))
    display = format(exact, "f")
    return exact, display.rstrip("0").rstrip(".") if "." in display else display


class _Parser:
    def __init__(self, expression: str, inputs: dict[str, Fraction]):
        if not isinstance(expression, str) or not 0 < len(expression) <= MAX_LENGTH:
            raise CalculationError("Expression length limit or empty expression")
        self.tokens = []
        position = 0
        while position < len(expression):
            if expression[position].isspace():
                position += 1
                continue
            match = _TOKEN.match(expression, position)
            if match is None:
                raise CalculationError("Unsupported syntax; use calculator expressions")
            self.tokens.append(match.group())
            position = match.end()
        self.index = 0
        self.inputs = inputs

    def peek(self) -> str:
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def take(self, token: str) -> None:
        if self.peek() != token or not token:
            raise CalculationError("Incomplete or unsupported expression")
        self.index += 1

    def expression(self, depth: int = 0, minimum: int = 0) -> Fraction:
        if depth > MAX_DEPTH:
            raise CalculationError("Expression depth limit exceeded")
        token = self.peek()
        self.take(token)
        if token in {"+", "-"}:
            value = self.expression(depth + 1, 3) * (1 if token == "+" else -1)
        elif token == "(":
            value = self.expression(depth + 1)
            self.take(")")
        elif re.fullmatch(_NUMBER, token):
            value = _fraction(Decimal(token))
        elif token in _FUNCTIONS:
            self.take("(")
            args = [self.expression(depth + 1)]
            while self.peek() == ",":
                self.take(",")
                args.append(self.expression(depth + 1))
            self.take(")")
            value = self.function(token, args)
        elif token in self.inputs:
            value = self.inputs[token]
        else:
            raise CalculationError("Undeclared input or unsupported syntax")
        precedence = {"+": 1, "-": 1, "*": 2, "/": 2}
        while self.peek() in precedence and precedence[self.peek()] >= minimum:
            operator = self.peek()
            self.take(operator)
            right = self.expression(depth + 1, precedence[operator] + 1)
            if operator == "+":
                value += right
            elif operator == "-":
                value -= right
            elif operator == "*":
                value *= right
            else:
                value /= right
            value = _bounded(value)
        return _bounded(value)

    @staticmethod
    def function(name: str, args: list[Fraction]) -> Fraction:
        if name == "sum":
            return sum(args, Fraction())
        if name == "min":
            return min(args)
        if name == "max":
            return max(args)
        if len(args) != 2:
            raise CalculationError("round and percent require two arguments")
        if name == "percent":
            return args[0] / args[1] * 100
        places = args[1]
        if places.denominator != 1 or abs(places) > 1024:
            raise CalculationError("round precision must be an integer within 1024")
        scale = Fraction(10) ** int(places)
        return Fraction(round(args[0] * scale)) / scale


def _evaluate(expression: str, inputs: dict[str, Fraction]) -> tuple[str, Exact, str]:
    parser = _Parser(expression, inputs)
    try:
        value = parser.expression()
    except ZeroDivisionError as error:
        raise CalculationError("Division by zero") from error
    if parser.peek():
        raise CalculationError("Trailing unsupported syntax")
    exact, display = _result(value)
    return "".join(parser.tokens), exact, display


@dataclass(frozen=True)
class BoundInput:
    name: str
    value: Exact
    evidence_id: str
    span: tuple[int, int]
    text: str


@dataclass(frozen=True)
class CalculationRecord:
    calculation_id: str
    expression: str
    exact_value: Exact
    display_value: str
    inputs: tuple[BoundInput, ...]


def bind_input(name: str, value: Exact, evidence_id: str, span: tuple[int, int],
               text: str, *, stored_text: StoredText, opened_ids: set[str]) -> BoundInput:
    """Bind an entire numeric span using zero-based, end-exclusive code points."""
    if (not isinstance(name, str) or not re.fullmatch(_NAME, name)
            or name in _FUNCTIONS or len(name) > MAX_LENGTH):
        raise CalculationError("Invalid or reserved input name")
    if (not callable(stored_text) or not isinstance(opened_ids, set)
            or not isinstance(evidence_id, str) or evidence_id not in opened_ids):
        raise CalculationError("Source must have been opened")
    if (type(span) is not tuple or len(span) != 2
            or any(type(offset) is not int for offset in span)):
        raise CalculationError("Span requires two integer code-point offsets")
    try:
        raw = stored_text(evidence_id)
    except (LookupError, OSError) as error:
        raise CalculationError("Stored source is unavailable") from error
    start, end = span
    if (not isinstance(raw, str) or not 0 <= start < end <= len(raw)
            or not isinstance(text, str) or raw[start:end] != text):
        raise CalculationError("Span does not match this stored source")
    if len(text) > MAX_LENGTH or not re.fullmatch(rf"[+-]?{_NUMBER}", text):
        raise CalculationError("Span must contain one complete decimal number")
    if _fraction(value) != _fraction(Decimal(text)):
        raise CalculationError("Input value does not match its source span")
    return BoundInput(name, value, evidence_id, span, text)


def _rebind(inputs: tuple[BoundInput, ...], stored_text: StoredText | None,
            opened_ids: set[str] | None) -> dict[str, Fraction]:
    values = {}
    for item in inputs:
        if not isinstance(item, BoundInput):
            raise CalculationError("Expected bound inputs")
        rebound = bind_input(item.name, item.value, item.evidence_id, item.span,
                             item.text, stored_text=stored_text, opened_ids=opened_ids)
        if rebound.name in values:
            raise CalculationError("Duplicate input name")
        values[rebound.name] = _fraction(rebound.value)
    return values


class Calculator:
    """Allocate C1, C2, ... within one calculation session; failures consume no ID."""

    def __init__(self, *, stored_text: StoredText | None = None,
                 opened_ids: set[str] | None = None):
        self.stored_text = stored_text
        self.opened_ids = opened_ids
        self._count = 0

    def calculate(self, expression: str,
                  inputs: tuple[BoundInput, ...] = ()) -> CalculationRecord:
        inputs = tuple(inputs)
        values = _rebind(inputs, self.stored_text, self.opened_ids)
        canonical, exact, display = _evaluate(expression, values)
        self._count += 1
        return CalculationRecord(f"C{self._count}", canonical, exact, display, inputs)


def verify_calculations(records: tuple[CalculationRecord, ...], *,
                        stored_text: StoredText | None = None,
                        opened_ids: set[str] | None = None) -> bool:
    """Rebind and recompute every record; nonempty input requires source context."""
    if records and (not callable(stored_text) or not isinstance(opened_ids, set)):
        return False
    seen = set()
    try:
        for record in records:
            if (not isinstance(record, CalculationRecord) or type(record.inputs) is not tuple
                    or not isinstance(record.calculation_id, str)
                    or not re.fullmatch(r"C[1-9][0-9]*", record.calculation_id)
                    or record.calculation_id in seen):
                return False
            seen.add(record.calculation_id)
            values = _rebind(record.inputs, stored_text, opened_ids)
            expression, exact, display = _evaluate(record.expression, values)
            if (expression != record.expression or type(record.exact_value) is not type(exact)
                    or _fraction(record.exact_value) != Fraction(exact)
                    or record.display_value != display):
                return False
    except (CalculationError, TypeError, AttributeError):
        return False
    return True


def verify_claim_numbers(text: str, source_numbers: set[str], number_pattern: re.Pattern[str], *,
                         calculation_ids: object = (), records: tuple[CalculationRecord, ...] = (),
                         cited_ids: frozenset[str] = frozenset(),
                         stored_text: StoredText | None = None,
                         opened_ids: set[str] | None = None) -> bool:
    """Extend printed-number support only through linked, rechecked calculations."""
    if (not isinstance(calculation_ids, (list, tuple))
            or any(not isinstance(identifier, str) for identifier in calculation_ids)
            or len(set(calculation_ids)) != len(calculation_ids)
            or not verify_calculations(records, stored_text=stored_text, opened_ids=opened_ids)):
        return False
    by_id = {record.calculation_id: record for record in records}
    results = set()
    for identifier in calculation_ids:
        record = by_id.get(identifier)
        if record is None or any(item.evidence_id not in cited_ids for item in record.inputs):
            return False
        results.add(Fraction(record.exact_value))
    whole_numbers = tuple(re.finditer(r"[+-]?(?:\.[0-9]+|[0-9][0-9,.:/-]*[0-9]|[0-9])", text))
    for match in number_pattern.finditer(text):
        number = match.group()
        if number in source_numbers:
            continue
        number = next((whole.group() for whole in whole_numbers
                       if whole.start() <= match.start() and whole.end() >= match.end()), "")
        if not re.fullmatch(rf"[+-]?(?:{_NUMBER}|[0-9]+/[0-9]+|"
                            r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)", number):
            return False
        try:
            if Fraction(number.replace(",", "")) not in results:
                return False
        except (ValueError, ZeroDivisionError):
            return False
    return True
