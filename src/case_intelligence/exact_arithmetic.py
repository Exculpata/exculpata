"""Exact, bounded arithmetic and records grounded in opened raw source strings.

Decimal literals, declared names, unary signs, + - * /, parentheses and the five
functions below form the entire grammar. round uses ties-to-even. Terminating
results display as fixed decimals without trailing zeros; other results display
as reduced numerator/denominator. No ambient Decimal context affects arithmetic.
Source callbacks must resolve the authorized raw text for each evidence identity.
"""

from dataclasses import dataclass, replace
from collections.abc import Set
from html.parser import HTMLParser
import html
import random
import unicodedata
from decimal import Decimal
from fractions import Fraction
import re
from typing import Callable

MAX_LENGTH = 4096
MAX_DEPTH = 32
MAX_INPUTS = 128
R = 64
MAX_POINTS = 4096
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
        self.nodes = []

    def peek(self) -> str:
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def take(self, token: str) -> None:
        if self.peek() != token or not token:
            raise CalculationError("Incomplete or unsupported expression")
        self.index += 1

    def node(self, op, children=(), literal=None):
        refs = frozenset({literal}) if op == 'name' else frozenset().union(
            *(self.nodes[child][3] for child in children))
        self.nodes.append((op, children, literal, refs))
        return len(self.nodes) - 1

    def expression(self, depth: int = 0, minimum: int = 0) -> int:
        if depth > MAX_DEPTH:
            raise CalculationError("Expression depth limit exceeded")
        token = self.peek()
        self.take(token)
        if token in {"+", "-"}:
            value = self.node('pos' if token == '+' else 'neg', (self.expression(depth + 1, 3),))
        elif token == "(":
            value = self.expression(depth + 1)
            self.take(")")
        elif re.fullmatch(_NUMBER, token):
            value = self.node('literal', literal=_fraction(Decimal(token)))
        elif token in _FUNCTIONS:
            self.take("(")
            args = [self.expression(depth + 1)]
            while self.peek() == ",":
                self.take(",")
                args.append(self.expression(depth + 1))
            self.take(")")
            value = self.node(token, tuple(args))
        elif token in self.inputs:
            value = self.node('name', literal=token)
        else:
            raise CalculationError("Undeclared input or unsupported syntax")
        precedence = {"+": 1, "-": 1, "*": 2, "/": 2}
        while self.peek() in precedence and precedence[self.peek()] >= minimum:
            operator = self.peek()
            self.take(operator)
            right = self.expression(depth + 1, precedence[operator] + 1)
            value = self.node(operator, (value, right))
        return value

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


def _parse(expression, inputs):
    parser = _Parser(expression, inputs)
    parser.expression()
    if parser.peek():
        raise CalculationError("Trailing unsupported syntax")
    return "".join(parser.tokens), parser.nodes


def _point(nodes, values):
    results = []
    for op, children, literal, refs in nodes:
        args = [results[i] for i in children]
        try:
            if any(v is None for v in args):
                raise CalculationError("Undefined child")
            if op == 'literal': value = literal
            elif op == 'name': value = values[literal]
            elif op == 'pos': value = args[0]
            elif op == 'neg': value = -args[0]
            elif op == '+': value = args[0] + args[1]
            elif op == '-': value = args[0] - args[1]
            elif op == '*': value = args[0] * args[1]
            elif op == '/': value = args[0] / args[1]
            else: value = _Parser.function(op, args)
            results.append(_bounded(value))
        except (CalculationError, ZeroDivisionError, OverflowError):
            results.append(None)
    return results


def _evaluate(expression: str, inputs: dict[str, Fraction]) -> tuple[str, Exact, str]:
    canonical, nodes = _parse(expression, inputs)
    value = _point(nodes, inputs)[-1]
    if value is None:
        raise CalculationError("Undefined or unbounded arithmetic")
    return canonical, *_result(value)


def _probe_ladder(names):
    primes, candidate = [], 2
    while len(primes) <= len(names):
        if all(candidate % p for p in primes if p * p <= candidate):
            primes.append(candidate)
        candidate += 1
    ladder = [Fraction(primes[i]) + Fraction(1, primes[i + 1]) for i in range(len(names))]
    return ladder


def _probe_bases(names):
    ladder = _probe_ladder(names)
    yield dict(zip(names, ladder))
    rng = random.Random(20261109)
    for _ in range(1, R):
        perm = list(ladder)
        rng.shuffle(perm)
        yield dict(zip(names, perm))


def _probe_point(nodes, values):
    return _point(nodes, values)


def _check_constant_contexts(nodes, sensitive):
    for index, (op, children, literal, refs) in enumerate(nodes):
        if not sensitive[index]:
            continue
        for position, child in enumerate(children):
            if (not sensitive[child] and op not in {'*', '/'}
                    and not (op in {'percent', 'round'} and position == 1)):
                raise CalculationError("Ungrounded constant quantity")


def _ground(expression, values):
    if not 1 <= len(values) <= MAX_INPUTS:
        raise CalculationError("Expected 1 to 128 bound inputs")
    canonical, nodes = _parse(expression, values)
    sensitive = [set() for _ in nodes]
    names = sorted(values)
    unresolved = set(names)
    points = 0
    for base in _probe_bases(names):
        if not unresolved:
            break
        if points + 1 + 2 * len(unresolved) > MAX_POINTS:
            raise CalculationError("Grounding point budget exceeded")
        baseline = _probe_point(nodes, base)
        points += 1
        for name in sorted(unresolved):
            reference = list(baseline)
            for extreme in (Fraction(2) ** 128, -(Fraction(2) ** 128)):
                probe = _probe_point(nodes, dict(base, **{name: extreme}))
                points += 1
                for i, value in enumerate(probe):
                    if value is not None and reference[i] is not None and value != reference[i]:
                        sensitive[i].add(name)
                    if reference[i] is None:
                        reference[i] = value
        unresolved = set(names) - sensitive[-1]
        for i, node in enumerate(nodes):
            if not sensitive[i]:
                unresolved.update(node[3])
    if set(names) != sensitive[-1]:
        raise CalculationError("Every input must affect the result")
    _check_constant_contexts(nodes, sensitive)
    value = _point(nodes, values)[-1]
    if value is None:
        raise CalculationError("Undefined or unbounded arithmetic")
    return canonical, *_result(value)


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
    if (not callable(stored_text) or not isinstance(opened_ids, Set)
            or not isinstance(evidence_id, str) or evidence_id not in opened_ids):
        raise CalculationError("Source must have been opened")
    if (type(span) is not tuple or len(span) != 2
            or any(type(offset) is not int for offset in span)):
        raise CalculationError("Span requires two integer code-point offsets")
    try:
        raw = stored_text(evidence_id)
    except Exception as error:
        raise CalculationError("Stored source is unavailable") from error
    start, end = span
    if (not isinstance(raw, str) or not 0 <= start < end <= len(raw)
            or not isinstance(text, str) or raw[start:end] != text):
        raise CalculationError("Span does not match this stored source")
    tokens = numeric_tokens(raw, span=span)
    if len(tokens) != 1 or (tokens[0].start, tokens[0].end) != span:
        raise CalculationError('Span must match one complete numeric token')
    if tokens[0].ambiguous or tokens[0].value is None:
        raise CalculationError(tokens[0].reason if tokens else "No complete numeric token")
    if _fraction(value) != tokens[0].value:
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
        canonical, exact, display = _ground(expression, values)
        self._count += 1
        return CalculationRecord(f"C{self._count}", canonical, exact, display, inputs)


def verify_record(record, *, stored_text=None, opened_ids=None):
    try:
        if (not isinstance(record, CalculationRecord) or type(record.inputs) is not tuple
                or not isinstance(record.calculation_id, str)
                or not re.fullmatch(r"C[1-9][0-9]*", record.calculation_id)):
            return False
        values = _rebind(record.inputs, stored_text, opened_ids)
        expression, exact, display = _ground(record.expression, values)
        return (expression == record.expression and type(exact) is type(record.exact_value)
                and _fraction(record.exact_value) == _fraction(exact) and record.display_value == display)
    except Exception:
        return False


def verify_calculations(records, *, stored_text=None, opened_ids=None):
    seen = set()
    try:
        for record in records:
            if not verify_record(record, stored_text=stored_text, opened_ids=opened_ids):
                return False
            if record.calculation_id in seen:
                return False
            seen.add(record.calculation_id)
    except Exception:
        return False
    return True


def verify_claim_numbers(text, source_numbers, number_pattern, *, calculation_ids=(), records=(),
                         cited_ids=frozenset(), stored_text=None, opened_ids=None, verified=None):
    if (not isinstance(calculation_ids, (tuple, list)) or len(calculation_ids) > 8
            or any(not isinstance(i, str) for i in calculation_ids)
            or len(set(calculation_ids)) != len(calculation_ids)):
        return False
    matches = tuple(number_pattern.finditer(text))
    if not calculation_ids:
        return all(m.group() in source_numbers for m in matches)
    results = set()
    for identifier in calculation_ids:
        selected = [r for r in records if isinstance(r, CalculationRecord) and r.calculation_id == identifier]
        if len(selected) != 1:
            return False
        record = selected[0]
        good = verified.get(identifier) if verified is not None else None
        if good is None:
            good = verify_record(record, stored_text=stored_text, opened_ids=opened_ids)
            if verified is not None:
                verified[identifier] = good
        if not good or any(item.evidence_id not in cited_ids for item in record.inputs):
            return False
        results.add(_fraction(record.exact_value))
    tokens = numeric_tokens(text)
    if any(t.context_ambiguous for t in tokens):
        return False
    for match in matches:
        if match.group() in source_numbers:
            continue
        token = next((t for t in tokens if t.core_start <= match.start() and t.core_end >= match.end()), None)
        if (token is None or token.attached_prefix or token.ambiguous or token.form == 'compound'
                or token.form == 'fraction' and (token.exponent or token.scale or token.percent)
                or token.value not in results):
            return False
    for token in tokens:
        if any(m.start() < token.end and m.end() > token.start for m in matches):
            continue
        if token.ambiguous:
            return False
        if not token.attached_prefix and text[token.core_start:token.core_end] not in source_numbers and token.value not in results:
            return False
    return True


@dataclass(frozen=True)
class NumericToken:
    start: int
    end: int
    value: Fraction | None = None
    scaled: bool = False
    percent: bool = False
    core_start: int = 0
    core_end: int = 0
    form: str = 'plain'
    signed: bool = False
    paren_wrapped: bool = False
    currency: str = ''
    exponent: bool = False
    scale: str = ''
    attached_prefix: bool = False
    attached_suffix: bool = False
    context_ambiguous: bool = False
    ambiguous: bool = False
    reason: str = ''


def _token_value(core):
    return _bounded(Fraction(core.replace(',', '')))


WINDOW = 128
MEMBERS = 32
CONTRACT_SCALE = {'thousand': 1000, 'million': 1000000}
SCALES = dict(CONTRACT_SCALE, hundred=100, billion=10 ** 9, trillion=10 ** 12, mn=10 ** 6, mil=10 ** 6, mio=10 ** 6, crore=10 ** 7, lakh=10 ** 5, k=1000, m=10 ** 6, mm=10 ** 6, b=10 ** 9, bn=10 ** 9, tn=10 ** 12, t=10 ** 12)
MULTIPLIERS = {'x', 'times', 'multiplied', 'raised', 'power', 'exponent', 'e'}
SHORT_SCALES = {'k', 'm', 'b', 't', 'mn', 'bn', 'tn', 'mm', 'mil', 'mio'}
COORDINATORS = {'and', 'or', 'through', 'to', 'maybe', 'perhaps'}
DASHES = '-' + ''.join((chr(i) for i in range(8208, 8214))) + '\u2212\ufe63\uff0d'
MARKS = '~\u2248<>\u2264\u2265\u00b1^'
CORE = '(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\\.[0-9]+)?|[0-9]+(?:\\.[0-9]+)?)'
SEP = '[\\s\\-\\u2010\\u2011]{0,8}'
STRICT = re.compile(f'(?P<pre>\\$\\(|\\(\\$|\\$|\\(|)(?P<core>{CORE})(?P<pct>%?)(?P<scale>{SEP}(?:thousand|million))?(?P<close>\\)?)', re.I)
ATOM = re.compile(f'(?P<currency>\\$?)(?P<core>{CORE})(?P<exponent>[eE][+-]?[0-9]+)?(?P<pct>%?)')
SCALE_RE = re.compile(SEP + '(' + '|'.join(sorted(SCALES, key=len, reverse=True)) + ')(?!\\w)', re.I)
SINGLE_SCALES = {'k', 'm', 'b', 't'}
BARE_SCALES = set(SCALES) - SINGLE_SCALES
MONTHS = {'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'}
SUPERSCRIPTS = '\u00b2\u00b3\u00b9' + ''.join((chr(i) for i in range(8304, 8314))) + '\u207b'

def context_word(s, pos):
    """Maximal L run plus attached M, with punctuation only between letters."""
    if pos >= len(s) or not unicodedata.category(s[pos]).startswith('L'):
        return None
    end = pos + 1
    while end < len(s):
        category = unicodedata.category(s[end])
        if category.startswith(('L', 'M')):
            end += 1
        elif s[end] in "'\u2019.-" and end + 1 < len(s) and unicodedata.category(s[end + 1]).startswith('L'):
            end += 2
        else:
            break
    return re.compile(re.escape(s[pos:end])).match(s, pos)

def temporal_position(text, token):
    """Explicit year/date position owns a date role, not a later amount's scale."""
    raw = text[token.start:token.end]
    if not re.fullmatch('[0-9]+', raw):
        return False
    left = text[max(0, token.start - WINDOW):token.start]
    month = '(?:' + '|'.join(sorted(MONTHS)) + ')'
    if len(raw) == 4 and re.search('\\b(?:in|during)\\s+$', left, re.I):
        return True
    if len(raw) <= 2 and re.search('\\b' + month + '\\s+$', left, re.I):
        return True
    return bool(len(raw) == 4 and re.search('\\b' + month + '\\s+[0-9]{1,2},\\s+$', left, re.I))

def strict_lex(text, start, end):
    """The only contract lexer; refuses partial or unsupported source tokens."""
    if not isinstance(text, str) or type(start) is not int or type(end) is not int:
        return (None, 'contract: invalid argument types')
    if not 0 <= start < end <= len(text) or end - start > WINDOW:
        return (None, 'contract: invalid span or token length bound')
    raw = text[start:end]
    m = STRICT.fullmatch(raw)
    if not m or raw[-1:].isspace():
        return (None, 'contract: unsupported whole-token form')
    pre, close = (m['pre'], m['close'])
    if ('(' in pre) != bool(close):
        return (None, 'contract: parentheses must surround the complete token')
    if m['scale'] and sum((c in '-\u2010\u2011' for c in m['scale'])) > 1:
        return (None, 'contract: nonplain scale separator')
    if m['scale'] and m['scale'][0].isalpha():
        return (None, 'contract: glued multiplier word is not a trailing word')
    left = text[max(0, start - WINDOW):start]
    right = text[end:end + WINDOW]
    if left and (left[-1].isalnum() or left[-1] in '_$' or unicodedata.category(left[-1]) == 'Sc'):
        return (None, 'contract: omitted prefix or attached identifier')
    if right and (right[0].isalnum() or right[0] in '_%$'):
        return (None, 'contract: omitted suffix, exponent, or attached unit')
    connectors = '.,:/_' + DASHES
    if len(left) >= 2 and left[-1] in connectors and left[-2].isdigit():
        return (None, 'contract: subspan of a compound numeric run')
    if len(right) >= 2 and right[0] in connectors and right[1].isdigit():
        return (None, 'contract: subspan of a compound numeric run')
    if re.match('[' + re.escape(connectors) + ']{2,}[0-9]', right):
        return (None, 'contract: subspan before malformed compound connectors')
    if re.search('[0-9][' + re.escape(connectors) + ']{2,}$', left):
        return (None, 'contract: subspan after malformed compound connectors')
    if re.match('[-\\u2010-\\u2015][^\\W\\d_]', right):
        return (None, 'contract: glued unit or identifier after hyphen')
    if re.match('\\s*(?:\\^|\\*\\*)\\s*[({\\[]*\\s*[+-]?[0-9]', right):
        return (None, 'contract: base subspan of symbolic exponent')
    if re.search('(?:\\^|\\*\\*)\\s*[({\\[]*\\s*$', left):
        return (None, 'contract: exponent subspan')
    if left.endswith('(') and right.startswith(')'):
        return (None, 'contract: omitted accounting parentheses')
    prefix = re.search('([^\\s])\\s{0,8}$', left)
    if prefix and prefix[1] in DASHES + '+' + MARKS:
        ch = prefix[1]
        before = left[:prefix.start(1)].rstrip()
        detached_range = ch in DASHES and prefix.end(1) < len(left) and before and (before[-1] in '0123456789%)')
        if not detached_range:
            return (None, 'contract: sign or approximation mark before token')
    word_reason = a_word_guard(text, start, end)
    if word_reason:
        return (None, word_reason)
    if (right[:1] in SUPERSCRIPTS and bool(right) or bool(re.match(r'<sup>', html.unescape(right), re.I))):
        return (None, 'contract: power base component outside whole-token grammar')
    if re.search('&(?:#[xX]?[0-9A-Fa-f]*|[A-Za-z][A-Za-z0-9]*)?$', left) and re.match('[A-Za-z0-9]*;', right):
        return (None, 'contract: numeral inside markup entity')
    if re.fullmatch('[0-9]{1,3}', raw) and re.match(' +[0-9]{3}(?![0-9])', right) or (re.fullmatch('[0-9]{3}', raw) and re.search('(?<![0-9])[0-9]{1,3} +$', left)):
        return (None, 'contract: component of unsupported space-grouped numeral')
    if re.match('[\\s\\-\\u2010\\u2011]{9}', right):
        return (None, 'contract: scale separator length bound')
    value = _token_value(m['core'])
    if m['scale']:
        word = re.search('[A-Za-z]+$', m['scale'])[0].lower()
        value *= CONTRACT_SCALE[word]
    if close:
        value = -value
    return (NumericToken(start, end, value, bool(m['scale']), bool(m['pct'])), '')

@dataclass(frozen=True)
class Atom:
    end: int
    value: Fraction
    factor: Fraction
    marked: bool

def atom_syntax(s, pos):
    """Shared lexical atom extent, no numeral conversion or scale evaluation."""
    m = ATOM.match(s, pos)
    if not m:
        return None
    end, scale_name = (m.end(), None)
    scale = SCALE_RE.match(s, end)
    if scale:
        scale_name, end = (scale[1].lower(), scale.end())
    else:
        bracket = re.match('\\s*(?:\\(\\s*([mbkt])\\s*\\)|\\[\\s*([mbkt])\\s*\\]|\\{\\s*([mbkt])\\s*\\})', s[end:], re.I)
        if bracket:
            scale_name = next((group for group in bracket.groups() if group is not None)).lower()
            end += bracket.end()
    return (m, end, scale_name)

def witness_atom(s, pos):
    syntax = atom_syntax(s, pos)
    if syntax is None:
        return None
    match, end, scale_name = syntax
    return Atom(end, Fraction(0), Fraction(1), bool(match['exponent'] or scale_name))

def read_atom(s, pos):
    syntax = atom_syntax(s, pos)
    if syntax is None:
        return None
    m, end, scale_name = syntax
    factor, marked = (Fraction(1), False)
    if m['exponent']:
        exp = _token_value(m['exponent'][1:])
        if abs(exp) > 64:
            raise ValueError('context exponent bound')
        factor, marked = (Fraction(10) ** exp, True)
    if scale_name is not None:
        factor *= SCALES[scale_name]
        marked = True
    return Atom(end, _bounded(_token_value(m['core']) * factor), factor, marked)

def wrappers(s, pos):
    m = re.match('[\\s\\[\\](){}"\\u2018\\u2019\\u201c\\u201d]*', s[pos:])
    return pos + m.end()

def power_syntax(s, pos):
    q = wrappers(s, pos)
    m = re.match('(?:\\^|\\*\\*)\\s*[({\\[]*\\s*([+-]?[0-9]+)\\b|to\\s+the\\s+(?:power\\s+(?:of\\s+)?([+-]?[0-9]+)\\b|([+-]?[0-9]+)(?:st|nd|rd|th)\\b)', s[q:], re.I)
    return (m, q)

def a_word_guard(text, start, end):
    """Shared lexical D5 word guards; no value conversion or span mutation."""
    left = text[max(0, start - WINDOW):start]
    right = text[end:end + WINDOW]
    seed_scale = SCALE_RE.search(text[start:end])
    if (seed_scale and seed_scale.end() == end - start
            and seed_scale[1].casefold() in SHORT_SCALES and right[:1].isalpha()):
        return 'contract: abbreviated scale ends inside a magnitude word'
    if power_syntax(text, end)[0]:
        return 'contract: power base component outside whole-token grammar'
    if re.search('to\\s+the\\s+power\\s+(?:of\\s+)?$', left, re.I):
        return 'contract: verbal exponent component'
    multiplied_base = re.search('(?:\\bx|\\btimes|[\u00d7\u00b7\u22c5*])\\s*$', left, re.I)
    if multiplied_base and re.match('\\s+to\\s+the\\s+[+-]?[0-9]+\\b', right, re.I):
        return 'contract: base component of multiplied verbal power'
    if re.search('(?:\\bx|\\btimes|[\u00d7\u00b7\u22c5*])\\s*[0-9]+\\s+to\\s+the\\s+$', left, re.I):
        return 'contract: exponent component of multiplied verbal power'
    extra = SCALE_RE.match(right)
    if extra:
        return 'contract: omitted or repeated scale word'
    gap_end = 0
    while gap_end < min(8, len(right)) and (not (right[gap_end].isalpha() or right[gap_end].isdecimal())):
        gap_end += 1
    if gap_end and (not re.match('[.,;:!?](?:\\s|$)', right)):
        word = context_word(right, gap_end)
        if word and word[0].lower() in set(SCALES) - SHORT_SCALES:
            return 'contract: nonplain scale separator'
    return ''

def power(s, pos, base):
    """Only explicit numeric powers; no conversion of arbitrary prose."""
    m, q = power_syntax(s, pos)
    if not m:
        return None
    exponent = _token_value(next((group for group in m.groups() if group is not None)))
    if abs(exponent) > 64 or abs(base) > 10 ** 12:
        raise ValueError('context power bound')
    if max(base.numerator.bit_length(), base.denominator.bit_length()) * abs(exponent) > 4096:
        raise ValueError('context power bit budget')
    return (_bounded(base ** exponent), q + m.end())

def direct_operator(s, pos):
    return re.match('(?:x\\b|times\\b|multiplied\\s+by\\b|[\u00d7\u00b7\u22c5*]|[.:](?=\\s))', s[pos:], re.I)

def direct(s, pos, value):
    """Read multiplication, division, implicit powers, and a power of self."""
    own_power = power(s, pos, value)
    if own_power:
        return (own_power[0], own_power[1], True, False)
    q = wrappers(s, pos)
    if q < len(s) and s[q] == ';' and (q > pos):
        q = wrappers(s, q + 1)
    op = direct_operator(s, q)
    word = context_word(s, q)
    if op and word and (op[0].lower() in {'x', 'times'}) and (word[0].lower() != op[0].lower()):
        op = None
    if op and op[0] in '.:' and (q == pos):
        op = None
    operand_pos = wrappers(s, q + op.end()) if op else q
    atom = read_atom(s, operand_pos)
    if atom is None:
        return (value, pos, False, bool(op))
    raised = power(s, atom.end, atom.value)
    if not op and (not raised):
        return (value, pos, False, False)
    multiplier, finish = raised if raised else (atom.value, atom.end)
    if finish < len(s) and s[finish] in ':/' and (not raised):
        return (value, pos, False, bool(op))
    if op and op[0].startswith(':'):
        if not multiplier:
            raise ValueError('zero context divisor')
        return (_bounded(value / multiplier), finish, True, False)
    return (_bounded(value * multiplier), finish, True, False)

def bridge(s, pos):
    """A coordination edge has punctuation plus only coordination words."""
    q = wrappers(s, pos)
    start = q
    seen = False
    while q < len(s):
        if s[q] in ',&':
            seen, q = (True, wrappers(s, q + 1))
            continue
        w = context_word(s, q)
        if w and w[0].lower() in COORDINATORS:
            seen, q = (True, wrappers(s, w.end()))
            continue
        break
    return q if seen and q > start else None

def magnitude(s, initial, clipped=False, independent_role=False):
    """Maximal adjacent expression and list; explicit member scales own scope."""
    value, pos, marked, unresolved = direct(s, 0, initial.value)
    own = marked or initial.scaled or initial.percent or independent_role
    if marked and incomplete_after(s, pos, clipped):
        unresolved = True
    finish = pos
    for _ in range(MEMBERS):
        nxt = bridge(s, pos)
        atom = read_atom(s, nxt) if nxt is not None else None
        if atom is None:
            if clipped and nxt is not None and (nxt == len(s)):
                raise ValueError('context bridge reached window edge')
            return (value, finish, marked, unresolved)
        if clipped and atom.end == len(s):
            raise ValueError('context atom reached window edge')
        later, after, has_op, unknown = direct(s, atom.end, atom.value)
        if has_op and incomplete_after(s, after, clipped):
            unknown = True
        marker = atom.marked or has_op
        if marker and (not own):
            factor = later / _token_value(ATOM.match(s, nxt)['core']) if _token_value(ATOM.match(s, nxt)['core']) else atom.factor
            value = _bounded(initial.value * factor)
            finish, marked = (after if has_op else atom.end, True)
        unresolved = unresolved or (unknown and (not own))
        pos = after if has_op else atom.end
    raise ValueError('context list member bound')

def incomplete_after(s, pos, clipped=False):
    """Only called after a completed operation, never after no-match direct."""
    q = wrappers(s, pos)
    if q == len(s):
        return clipped
    if s[q] in '.:;!?' and (q == 0 or not s[q - 1].isspace()):
        return False
    word = context_word(s, q)
    if word:
        return clipped and word.end() == len(s) or word[0].lower() in MULTIPLIERS | BARE_SCALES | {'ten'} or bool(re.match('to\\s+the\\b', s[q:], re.I))
    if s[q] in ',&' and bridge(s, q) is not None:
        return False
    return True
A_UNITS = {'usd': 'US dollar code', 'eur': 'euro code', 'gbp': 'pound sterling code', 'cad': 'Canadian dollar code', 'aud': 'Australian dollar code', 'chf': 'Swiss franc code', 'jpy': 'Japanese yen code', '%': 'contract percent decoration', 'pct': 'percent as printed', 'pp': 'percentage-point label', 'ft': 'feet', 'in': 'inches', 'yd': 'yards', 'mi': 'miles', 'km': 'kilometres', 'cm': 'centimetres', 'kg': 'kilograms', 'g': 'grams', 'lb': 'pounds', 'lbs': 'pounds plural', 'oz': 'ounces', 'mg': 'milligrams', 'ml': 'millilitres', 'hr': 'hours', 'hrs': 'hours plural', 'min': 'minutes', 'sec': 'seconds', 'yr': 'years', 'yrs': 'years plural', 'mo': 'months', 'wk': 'weeks', 'day': 'days singular', 'days': 'days plural', 'st': 'ordinal suffix', 'nd': 'ordinal suffix', 'rd': 'ordinal suffix', 'th': 'ordinal suffix', 'no': 'number label', 'sq': 'square-unit qualifier', 'inc': 'incorporated suffix', 'llc': 'limited-liability suffix', 'ltd': 'limited-company suffix', 'co': 'company suffix'}
A_BLOCK = frozenset('address article aside blockquote body br caption center dd details div dl dt fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head header hr html li main nav ol p pre section summary table tbody tfoot thead tr ul'.split())
A_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
A_SUP_DIGITS = dict(zip('\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207b', '0123456789-'))

@dataclass(frozen=True)
class AOffsetView:
    text: str
    origins: tuple
    left_edge: bool
    right_edge: bool

    def to_normal(self, start, end):
        selected = [i for i, (a, b) in enumerate(self.origins) if a < end and b > start]
        if not selected:
            raise ValueError('offset-map: removed or empty span')
        lo, hi = (selected[0], selected[-1] + 1)
        if selected != list(range(lo, hi)):
            raise ValueError('offset-map: noncontiguous span')
        origins = self.origins[lo:hi]
        if any((a < start or b > end for a, b in origins)):
            raise ValueError('offset-map: span cuts normalization unit')
        if min((a for a, b in origins)) != start or max((b for a, b in origins)) != end:
            raise ValueError('offset-map: unmatched source boundary')
        if self.to_source(lo, hi) != (start, end):
            raise ValueError('offset-map: roundtrip failure')
        return (lo, hi)

    def to_source(self, start, end):
        if not 0 <= start < end <= len(self.origins):
            raise ValueError('offset-map: invalid normalized span')
        origins = self.origins[start:end]
        a, b = (min((x for x, y in origins)), max((y for x, y in origins)))
        if any((x < b and y > a for x, y in self.origins[:start] + self.origins[end:])):
            raise ValueError('offset-map: reverse span cuts normalization unit')
        return (a, b)

def a_tag_geometry(name, closing=False):
    if name in {'sup', 'sub'}:
        return ')' if closing else '^(' if name == 'sup' else '_('
    return '|' if name in {'td', 'th'} else '\n' if name in A_BLOCK else ''

class AHTMLText(HTMLParser):

    def __init__(self, text, origins):
        super().__init__(convert_charrefs=False)
        self.source, self.source_origins = (text, origins)
        self.lines = [0]
        self.lines.extend((i + 1 for i, ch in enumerate(text) if ch == '\n'))
        self.parts = []
        self.origins = []
        self.special = []
        self.tags = []

    def position(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def emit(self, value, begin, end, literal=False):
        if not value:
            return
        selected = self.source_origins[begin:end]
        if not selected:
            raise ValueError('offset-map: empty markup extent')
        self.parts.append(value)
        if literal and len(value) == end - begin:
            self.origins.extend(selected)
        else:
            origin = (min((a for a, b in selected)), max((b for a, b in selected)))
            self.origins.extend([origin] * len(value))

    def handle_data(self, data):
        pos = self.position()
        self.emit(data, pos, pos + len(data), True)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')

    def handle_starttag(self, tag, attrs):
        pos = self.position()
        end = pos + len(self.get_starttag_text())
        if tag not in A_VOID:
            self.tags.append(tag)
        if tag in {'sup', 'sub'}:
            self.special.append(tag)
        self.emit(a_tag_geometry(tag), pos, end)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in A_VOID:
            self.tags.pop()
        if tag in {'sup', 'sub'}:
            self.special.pop()
            pos = self.position()
            self.emit(a_tag_geometry(tag, True), pos, pos + len(self.get_starttag_text()))

    def handle_endtag(self, tag):
        pos = self.position()
        end = self.source.find('>', pos) + 1
        if end <= pos:
            raise ValueError('offset-map: unterminated tag')
        if not self.tags or self.tags.pop() != tag:
            raise ValueError('offset-map: unbalanced markup')
        if tag in {'sup', 'sub'}:
            if not self.special or self.special.pop() != tag:
                raise ValueError('offset-map: unmatched power/subscript tag')
        self.emit(a_tag_geometry(tag, True), pos, end)

    def handle_comment(self, data):
        if re.match('\\s*\\[if\\b', data, re.I):
            raise ValueError('offset-map: conditional markup requires rendering')

    def unknown_decl(self, data):
        raise ValueError('offset-map: unsupported conditional/declaration markup')

def a_gate_category(ch):
    category = unicodedata.category(ch)
    return category == 'Cf' or category.startswith('M') or (category == 'Cc' and (not ch.isspace()))
_TAG = re.compile('<!--[\\s\\S]*?-->|<(?:[^>\\\'\\"]|\\\'[^\\\']*\\\'|\\"[^\\"]*\\")*>')
_STRUCT = set('[](){}<>"\'.,:;!?&|/') | {chr(c) for c in (8216, 8217, 8220, 8221)}
_LINE = {chr(c) for c in (10, 13, 11, 12, 28, 29, 30, 133, 8232, 8233)}

def a_joint_mark(ch):
    """Hyphenation mark by category: Pd, or any non-grammar P*/S* character."""
    c = unicodedata.category(ch)
    return ch not in _STRUCT and c[0] in 'PS' and (c not in ('Ps', 'Pe', 'Pi', 'Pf'))

def inspect_once(decoded, origins):
    """ONE inspection projection over the once-decoded raw window.
    1 tag geometry (td/th '|', block '\\n', every other tag and comment zero-width)
    2 per-letter compatibility fold (fullwidth Latin), hazards flagged, never folded
    3 hazards removed -> `clean` (split reading)
    4 joints closed -> `joined`: letter, then a run of marks / hazards / whitespace
      (block tags already whitespace, inline tags already gone) with at least one
      mark or hazard (or a line break), then a letter
    Every output char keeps its scan index; scan keeps raw extents."""
    text = ''.join(decoded)
    scan = []
    spans = []
    i = 0
    dangling = set()
    while i < len(text):
        m = _TAG.match(text, i)
        if m:
            name = re.match('</?\\s*([\\w:]+)', m[0])
            name = name[1].lower() if name else ''
            sep = a_tag_geometry(name, m[0].startswith('</'))
            if name in {'sup', 'sub'} and m[0].rstrip().endswith('/>'):
                sep += a_tag_geometry(name, True)
            if sep:
                scan.extend(sep)
                spans.extend([(origins[i][0], origins[m.end() - 1][1])] * len(sep))
            i = m.end()
            continue
        scan.append(text[i])
        spans.append(origins[i])
        i += 1
    fold = []
    for k, ch in enumerate(scan):
        if a_gate_category(ch):
            fold.append((ch, k, True))
            continue
        piece = unicodedata.normalize('NFKC', ch)
        if not (len(piece) == 1 and piece.isalpha()):
            piece = ch
        fold.append((piece, k, False))
    clean = [(c, k) for c, k, h in fold if not h]
    joined = []
    joints = set()
    n = len(fold)
    p = 0
    while p < n:
        c, k, h = fold[p]
        if h:
            p += 1
            continue
        joined.append((c, k))
        if c.isalpha():
            q = p + 1
            marks = 0
            line = False
            while q < n:
                cq, kq, hq = fold[q]
                if hq or a_joint_mark(cq):
                    marks += 1
                elif cq.isspace():
                    line = line or cq in _LINE
                else:
                    break
                q += 1
            letter = q < n and fold[q][0].isalpha()
            case_ok = letter and (fold[q][0].islower() or (c.isupper() and fold[q][0].isupper()))
            if marks and line and (not case_ok):
                dangling.add(k)
            if q > p + 1 and letter and (marks or line):
                joints.add(len(joined))
                p = q
                continue
        p += 1
    return (scan, spans, clean, joined, joints, dangling)

def a_dangling_stop(s, kept, a, windows, dangling):
    """The corridor's first unconsumed word after the token is an unresolved fragment."""
    pos = max([a] + [f for b, f in windows])
    pos = wrappers(s, pos)
    w = context_word(s, pos)
    return bool(w) and kept[w.end() - 1] in dangling

def a_pre_read_gate(decoded, origins, start, end):
    """Refusal only on decoded raw characters; never mutate the value view."""
    scan, spans, clean, joined, joints, dangling = inspect_once(decoded, origins)
    text = ''.join(decoded)
    for m in re.finditer('(?=(?<![0-9])([0-9]{1,3})(.)([0-9]{3})(?![0-9]))', text):
        if unicodedata.category(m[2]) == 'Sk' and m[2] != '^' and (start < origins[m.end(3) - 1][1]) and (end > origins[m.start(1)][0]):
            raise ValueError('contract: component of unsupported punctuation-grouped numeral')
    sel = [i for i, (a, b) in enumerate(spans) if a < end and b > start]
    if not sel:
        raise ValueError('offset-map: gate removed selected span')
    left, right = (sel[0], sel[-1] + 1)

    def refuse():
        raise ValueError('category gate: adjacent Cf/Cc/M/VS')
    if any((a_gate_category(c) for c in scan[left:right])):
        refuse()
    i = left - 1
    while i >= 0 and scan[i].isspace():
        i -= 1
    if i >= 0 and a_gate_category(scan[i]):
        refuse()
    i = right
    while i < len(scan) and scan[i].isspace():
        i += 1
    if i < len(scan) and a_gate_category(scan[i]):
        refuse()
    for proj, jts in ((clean, set()), (joined, joints)):
        s = ''.join((c for c, k in proj))
        kept = [k for c, k in proj]
        a = sum((k < left for k in kept))
        b = sum((k < right for k in kept))
        if a >= b:
            continue
        if a_word_guard(s, a, b):
            refuse()
        seed = s[a:b]
        windows = []
        scaled = any((m.end() == len(seed) for m in SCALE_RE.finditer(seed)))
        a_marker_corridor(s, NumericToken(a, b, Fraction(0), scaled, '%' in seed), False, witnesses=windows)
        prev = re.search('[^\\s]+(?=\\s*$)', s[:a])
        if prev:
            w = prev[0].casefold()
            op = a_multiplication(s, prev.start())
            if w in SCALES or w in A_UNITS or w in MULTIPLIERS or (op == prev.end()) or (wrappers(s, prev.start() + 1) == a and a_structural_marker(s, prev.start(), witness_atom)):
                windows.append((prev.start(), prev.end()))
        if proj is clean and a_dangling_stop(s, kept, b, windows, dangling):
            refuse()
        for begin, finish in windows:
            if begin >= finish:
                continue
            if any((begin < j < finish for j in jts)):
                refuse()
            lo, hi = (kept[begin], kept[finish - 1] + 1)
            while lo > 0 and a_gate_category(scan[lo - 1]):
                lo -= 1
            while hi < len(scan) and a_gate_category(scan[hi]):
                hi += 1
            bf = lo - 1
            while bf >= 0 and scan[bf].isspace():
                bf -= 1
            af = hi
            while af < len(scan) and scan[af].isspace():
                af += 1
            if bf >= 0 and a_gate_category(scan[bf]):
                refuse()
            if af < len(scan) and a_gate_category(scan[af]):
                refuse()
            if any((a_gate_category(c) for c in scan[lo:hi])):
                refuse()
    return (list(''.join(decoded)), list(origins))

def a_normalize_once(text, start, end):
    """One bounded pipeline, with per-output-code-point raw half-open extents.

    Decode references once, parse rendered tags once, preserve superscript runs
    BEFORE compatibility normalization erases their role, then NFKC clusters.
    Assert cluster-wise NFKC equals whole-view NFKC, otherwise fail closed.
    Source boundaries inside an entity, ligature, combining cluster or removed
    tag cannot roundtrip and refuse. Synthetic ^( and ) inherit tag extents.
    """
    lo = max(0, start - WINDOW)
    hi = min(len(text), end + WINDOW)
    raw = text[lo:hi]
    decoded = []
    origins = []
    i = 0
    entity = re.compile('&(?:#[xX][0-9A-Fa-f]+;?|#[0-9]+;?|[A-Za-z][A-Za-z0-9]*;?)')
    while i < len(raw):
        match = entity.match(raw, i)
        if match:
            piece = html.unescape(match[0])
            if piece != match[0]:
                decoded.append(piece)
                origins.extend([(lo + i, lo + match.end())] * len(piece))
                i = match.end()
                continue
        decoded.append(raw[i])
        origins.append((lo + i, lo + i + 1))
        i += 1
    decoded, origins = a_pre_read_gate(decoded, origins, start, end)
    parser = AHTMLText(''.join(decoded), origins)
    parser.feed(parser.source)
    if parser.rawdata and '<' in parser.rawdata:
        raise ValueError('offset-map: clipped or malformed markup')
    parser.close()
    if parser.tags:
        raise ValueError('offset-map: unclosed or clipped markup')
    if parser.special:
        raise ValueError('offset-map: unclosed power/subscript tag')
    plain = ''.join(parser.parts)
    source = parser.origins
    expanded = []
    maps = []
    i = 0
    while i < len(plain):
        if plain[i] in A_SUP_DIGITS:
            j = i + 1
            while j < len(plain) and plain[j] in A_SUP_DIGITS:
                j += 1
            piece = '^(' + ''.join((A_SUP_DIGITS[ch] for ch in plain[i:j])) + ')'
            span = (min((a for a, b in source[i:j])), max((b for a, b in source[i:j])))
            expanded.append(piece)
            maps.extend([span] * len(piece))
            i = j
        else:
            expanded.append(plain[i])
            maps.append(source[i])
            i += 1
    before = ''.join(expanded)
    normalized = []
    final = []
    i = 0
    while i < len(before):
        j = i + 1
        while j < len(before) and unicodedata.combining(before[j]):
            j += 1
        piece = unicodedata.normalize('NFKC', before[i:j])
        origin = (min((a for a, b in maps[i:j])), max((b for a, b in maps[i:j])))
        normalized.append(piece)
        final.extend([origin] * len(piece))
        i = j
    result = ''.join(normalized)
    if result != unicodedata.normalize('NFKC', before) or len(result) > WINDOW * 12:
        raise ValueError('offset-map: unsupported normalization cluster or expansion bound')
    return AOffsetView(result, tuple(final), lo > 0, hi < len(text))

def a_multiplication(s, pos):
    command = re.match('\\\\(?:times|cdot)(?![A-Za-z])', s[pos:])
    if command:
        return pos + command.end()
    if pos >= len(s):
        return None
    ch = s[pos]
    if ch in 'xX':
        word = context_word(s, pos)
        return pos + 1 if word and word[0] in 'xX' else None
    if ch in '\u00d7\u2715\u2a2f\u2219\u2022\u00b7\u22c5*\u2062':
        return pos + 1
    return None

def a_structural_marker(s, pos, atom_reader=read_atom):
    ch = s[pos]
    owned = '[](){}<>"\u2018\u2019\u201c\u201d.:;!?,&|'
    category = unicodedata.category(ch)
    if atom_reader(s, pos) is not None:
        return False
    if category[0] not in {'S', 'P'} or category in {'Ps', 'Pe'} or ch in owned:
        return False
    following = wrappers(s, pos + 1)
    return atom_reader(s, following) is not None

def a_letter_operator(text, pos, atom_reader=read_atom):
    """One non-Latin letter cluster, or marked Latin x, between numeric atoms."""
    left = text[:pos].rstrip()
    if not left or not left[-1].isdigit():
        return False
    word = context_word(text, pos)
    if not word:
        return False
    cluster = unicodedata.normalize('NFD', word[0])
    if not unicodedata.category(cluster[0]).startswith('L') or any((not unicodedata.category(c).startswith('M') for c in cluster[1:])):
        return False
    non_latin = 'LATIN' not in unicodedata.name(cluster[0], '')
    marked_x = cluster[0].casefold() == 'x' and len(cluster) > 1
    return (non_latin or marked_x) and atom_reader(text, wrappers(text, word.end())) is not None

def a_grouped_digit_run(text, start, end):
    """Refuse overlapping quote/punctuation-grouped numerals and all components."""
    for match in re.finditer('(?=(?<![0-9])([0-9]{1,3})(.)([0-9]{3})(?![0-9]))', text):
        separator = match[2]
        if separator not in '.,' and unicodedata.category(separator) in {'Pi', 'Pf', 'Po', 'Lm'} and (start < match.end(3)) and (end > match.start(1)):
            return True
    return False

def a_short_word(s, pos):
    end = pos
    while end < len(s) and s[end].isalpha():
        end += 1
    if 1 <= end - pos <= 4:
        return (s[pos:end].casefold(), end)
    return None

def a_bracket(s, pos):
    if pos >= len(s):
        return None
    if unicodedata.category(s[pos]) != 'Ps' and s[pos] != '<':
        return None
    end = pos + 1
    depth = 1
    while end < len(s):
        category = unicodedata.category(s[end])
        if category == 'Ps' or s[end] == '<':
            depth += 1
        elif category == 'Pe' or s[end] == '>':
            depth -= 1
            if depth == 0:
                break
        end += 1
    if end >= len(s):
        return None
    inside = s[pos + 1:end]
    words = a_short_codes(inside)
    return (' '.join(words), end + 1, inside) if words else None

def a_short_codes(cell):
    words = re.findall('[^\\W\\d_]+', cell, re.UNICODE)
    if words and (not any((c.isdigit() for c in cell))) and (all((len(w) <= 4 for w in words)) or any((w.casefold() in SCALES for w in words))):
        return [w.casefold() for w in words]
    return None

def a_subsection(text, token, mode='structural'):
    """Primary structural subsection ownership."""
    raw = text[token.start:token.end]
    if not re.fullmatch('[0-9]+', raw):
        return False
    tail = text[token.end:]
    bracket = a_bracket(tail, 0)
    if not bracket:
        return False
    name, end, inside = bracket
    if len(name) != 1 or (inside != name and inside.casefold() != name):
        return False
    left = text[max(0, token.start - WINDOW):token.start]
    if '|' in left.rsplit('\n', 1)[-1] or any((unicodedata.category(ch) == 'Sc' for ch in inside)):
        return False
    after = tail[end:].lstrip()
    if after.startswith(('$', '%', '|')):
        return False
    return not after or after[0] in '(.!:;?,'

def a_delimited_code_cell(text, pos):
    """A delimited cell led by an existing currency/unit code, no new vocabulary."""
    if text[pos] not in '\t,;':
        return False
    word = context_word(text, wrappers(text, pos + 1))
    return bool(word and word[0].casefold() in A_UNITS)

def a_marker_corridor(text, token, clipped, mode='structural', expanded_punctuation=frozenset(), witnesses=None):
    tail = text[token.end:]
    q = 0
    crossed = False
    atom_reader = read_atom if witnesses is None else witness_atom

    def consume(begin, finish):
        if witnesses is not None:
            witnesses.append((token.end + begin, token.end + finish))
    independent = a_subsection(text, token, mode) or temporal_position(text, token)
    owned = token.scaled or token.percent or independent
    while q < len(tail):
        ch = tail[q]
        if q == len(tail) - len(tail.lstrip()) and a_subsection(text, token, mode):
            bracket = a_bracket(tail, q)
            if bracket:
                q = bracket[1]
                continue
        bracket = a_bracket(tail, q)
        if bracket:
            name, finish, inside = bracket
            consume(q, finish)
            if any((w not in A_UNITS for w in a_short_codes(inside))):
                return (True, 'context: class short bracket marker ' + name)
            q = finish
            continue
        if ch == '|' or a_delimited_code_cell(tail, q):
            crossed = True
            stop = re.search('[|\\t,;]', tail[q + 1:])
            end = q + 1 + stop.start() if stop else -1
            cell = tail[q + 1:end if end >= 0 else len(tail)].strip()
            words = a_short_codes(cell)
            if words:
                consume(q + 1, end if end >= 0 else len(tail))
            if words and any((w not in A_UNITS for w in words)):
                return (True, 'context: class short cell marker ' + ','.join(words))
            q += 1
            continue
        op = a_multiplication(tail, q)
        if op is not None:
            pos = wrappers(tail, op)
            atom = atom_reader(tail, pos)
            if atom:
                consume(q, op)
                return (True, 'context: class multiplication followed by numeric operand')
        if a_letter_operator(text, token.end + q, atom_reader):
            consume(q, context_word(tail, q).end())
            return (True, 'context: letter cluster between numeric atoms')
        if a_structural_marker(tail, q, atom_reader):
            consume(q, q + 1)
            return (True, 'context: structural symbol before numeric atom')
        if ch in '^' or tail.startswith('_(', q):
            consume(q, q + 1)
            return (True, 'context: normalized power or subscript syntax')
        if ch in '.:;!?':
            if token.end + q in expanded_punctuation:
                q += 1
                continue
            if q and tail[q - 1].isspace():
                q += 1
                continue
            return (False, '')
        if ch in ',&' and owned:
            return (False, '')
        atom = atom_reader(tail, q)
        if atom:
            consume(q, atom.end)
            if atom.marked:
                return (False, '') if crossed or independent else (True, 'context: later scaled atom')
            has_power = power(tail, atom.end, atom.value) if witnesses is None else power_syntax(tail, atom.end)[0]
            if has_power:
                match, begin = power_syntax(tail, atom.end)
                consume(atom.end, begin + match.end())
                return (True, 'context: normalized power atom')
            attached = a_short_word(tail, atom.end)
            if attached and attached[0] not in A_UNITS:
                consume(atom.end, attached[1])
                return (True, 'context: class short attached marker ' + attached[0])
            q = atom.end
            continue
        word = context_word(tail, q)
        if word:
            name = word[0].casefold()
            attached = q == 0 or (q > 0 and tail[q - 1].isdigit())
            if attached and 1 <= len(name) <= 4 and name.isalpha() and (name not in A_UNITS):
                consume(q, word.end())
                return (True, 'context: class short attached marker ' + name)
            if name.isalpha() and any((name.startswith(w) and len(name) > len(w) for w in set(SCALES) - SHORT_SCALES)):
                consume(q, word.end())
                return (True, 'context: scale-prefix letter run')
            if name in MULTIPLIERS or name in BARE_SCALES or name == 'ten':
                operator = direct_operator(tail, q)
                consume(q, max(word.end(), q + operator.end() if operator else word.end()))
                return (True, 'context: retained magnitude word ' + name)
            if name in A_UNITS:
                consume(q, word.end())
            if name not in COORDINATORS or owned:
                return (bool(clipped and word.end() == len(tail)), 'context: clipped word' if clipped else '')
            consume(q, word.end())
            q = word.end()
            continue
        command = re.match('\\\\[A-Za-z]+', tail[q:])
        if command:
            q += command.end()
            continue
        q += 1
    return (clipped, 'context: clipped live corridor' if clipped else '')

def a_class_context(text, token, clipped=False, mode='structural', expanded_punctuation=frozenset()):
    marker, reason = a_marker_corridor(text, token, clipped, mode, expanded_punctuation)
    if marker:
        return (False, reason)
    independent = a_subsection(text, token, mode) or temporal_position(text, token)
    tail = text[token.end:]
    group_start = len(tail) - len(tail.lstrip())
    if independent and a_bracket(tail, group_start):
        tail = tail[a_bracket(tail, group_start)[1]:]
    value, extent, marked, unknown = magnitude(tail, token, clipped, independent)
    if value != token.value:
        return (False, f'context: differential value {value} differs from {token.value}')
    if unknown:
        return (False, 'context: incomplete operation or continuation')
    if clipped and (extent >= len(tail) or (marked and wrappers(tail, extent) == len(tail))):
        return (False, 'context: structural corridor reached window edge')
    return (True, 'class rules, equal value or no reachable marker')


_CLAIM_CORE = re.compile(r'(?:\.\d+|\d(?:\d|[' + re.escape('.,:/_' + DASHES) + r'](?=\d))*)')


def _claim_token(text, match, *, rendered=True):
    a, b = match.span()
    core = match[0]
    if len(core) > WINDOW: raise CalculationError("Numeric token length bound")
    form = ('plain' if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', core) else
            'grouped' if re.fullmatch(CORE, core) else
            'fraction' if re.fullmatch(r'[0-9]+/[0-9]+', core) else
            'leading_decimal' if re.fullmatch(r'\.[0-9]+', core) else 'compound')
    start, end, signs, currency, paren, ambiguous = a, b, '', '', False, form == 'compound'
    while start > 0 and a - start < 3 and (text[start - 1] in '(' + DASHES + '+' or unicodedata.category(text[start - 1]) == 'Sc'):
        start -= 1
    prefix = text[start:a]
    signs = ''.join(ch for ch in prefix if ch in DASHES + '+')
    currency = ''.join(ch for ch in prefix if unicodedata.category(ch) == 'Sc')
    if start and (text[start - 1] in '(' + DASHES + '+' or unicodedata.category(text[start - 1]) == 'Sc'):
        ambiguous = True
    detached = re.search(r'([^\s])\s{0,8}$', text[max(0, start - WINDOW):start])
    if detached and detached[1] in DASHES + '+' + (MARKS if rendered else ''):
        offset = max(0, start - WINDOW) + detached.start(1)
        before = text[max(0, offset - WINDOW):offset].rstrip()
        is_range = detached[1] in DASHES and offset + 1 < start and before and before[-1] in '0123456789%)'
        if not is_range:
            start = offset
            if detached[1] in MARKS: ambiguous = True
            else: signs += detached[1]
    exponent = re.match(r'[eE]([+\-' + re.escape(DASHES) + r']?[0-9]+)', text[end:end + WINDOW + 1])
    factor = Fraction(1)
    if exponent:
        digits = exponent[1]
        exp = _token_value(digits.translate(str.maketrans({ch: '-' for ch in DASHES})))
        if abs(exp) > 64: ambiguous = True
        else: factor = Fraction(10) ** exp
        end += exponent.end()
    pct = text[end:end + 1] == '%'
    end += pct
    scale = SCALE_RE.match(text, end)
    scale_name = scale[1].casefold() if scale else ''
    if scale:
        factor *= SCALES[scale_name]
        end = scale.end()
    if '(' in prefix and text[end:end + 1] == ')':
        paren = True
        end += 1
    elif '(' in prefix:
        start += prefix.count('(')
    ambiguous |= len(signs) > 1 or paren and bool(signs) or len(currency) > 1
    attached_prefix = start > 0 and (text[start - 1].isalnum() or text[start - 1] == '_')
    attached_suffix = end < len(text) and (text[end].isalnum() or text[end] == '_')
    if end - start > WINDOW: raise CalculationError("Numeric token length bound")
    value = None if ambiguous else _token_value(core) * factor * (-1 if paren or signs and signs != '+' else 1)
    return NumericToken(start, end, value, bool(scale), pct, a, b, form, bool(signs), paren,
                        currency, bool(exponent), scale_name, attached_prefix, attached_suffix,
                        ambiguous=ambiguous)


def _expanded_punctuation(raw, view):
    return frozenset(i for i, (lo, hi) in enumerate(view.origins)
                     if view.text[i] in '.:;!?' and raw[lo:hi] not in '.:;!?'
                     and unicodedata.normalize('NFKC', raw[lo:hi]) == view.text[i] * len(unicodedata.normalize('NFKC', raw[lo:hi])))


def _numeric_span(raw, start, end, *, evidence):
    if (not isinstance(raw, str) or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(raw) or end - start > WINDOW):
        raise CalculationError('contract: invalid span or token length bound')
    view = a_normalize_once(raw, start, end)
    a, b = view.to_normal(start, end)
    if evidence:
        original, reason = strict_lex(raw[start:end], 0, end - start)
        if original is None: raise CalculationError(reason)
        token, reason = strict_lex(view.text, a, b)
        if token is None: raise CalculationError(reason)
        if token.value != original.value: raise CalculationError('offset-map: normalization changed token value')
        core = STRICT.fullmatch(view.text[a:b])
        token = replace(token, core_start=a + core.start('core'), core_end=a + core.end('core'),
                        form='grouped' if ',' in core['core'] else 'plain', currency='$' if '$' in core['pre'] else '',
                        paren_wrapped=bool(core['close']), scale=''.join(c for c in (core['scale'] or '') if c.isalpha()))
    else:
        match = _CLAIM_CORE.search(view.text, a, b)
        if match is None: raise CalculationError('offset-map: missing numeral')
        token = _claim_token(view.text, match)
        if (token.start, token.end) != (a, b): raise CalculationError('offset-map: changed token extent')
    if a_grouped_digit_run(view.text, a, b):
        raise CalculationError('contract: component of unsupported punctuation-grouped numeral')
    if view.left_edge and a == 0: raise CalculationError('offset-map: clipped left boundary')
    safe, reason = a_class_context(view.text, token, view.right_edge, expanded_punctuation=_expanded_punctuation(raw, view))
    if not safe: raise CalculationError(reason)
    core_start, core_end = view.to_source(token.core_start, token.core_end)
    return replace(token, start=start, end=end, core_start=core_start, core_end=core_end)


def numeric_tokens(text: str, *, span: tuple[int, int] | None = None) -> tuple[NumericToken, ...]:
    """One bounded numeric reader; original offsets and values never come from an inspection join."""
    tokens = []
    try:
        if not isinstance(text, str):
            raise CalculationError('contract: invalid text')
        lo, hi = 0, len(text)
        if span is not None:
            if (type(span) is not tuple or len(span) != 2
                    or any(type(n) is not int for n in span)
                    or not 0 <= span[0] < span[1] <= len(text)
                    or span[1] - span[0] > WINDOW):
                raise CalculationError('contract: invalid span or token length bound')
            lo, hi = max(0, span[0] - WINDOW), min(len(text), span[1] + WINDOW)
        cursor = lo
        while cursor < hi:
            match = _CLAIM_CORE.match(text, cursor, hi)
            if match is None and not unicodedata.category(text[cursor]).startswith('N'):
                cursor += 1
                continue
            start, end = match.span() if match else (cursor, cursor + 1)
            try:
                seed = _claim_token(text, match, rendered=False) if match else NumericToken(start, end)
                start, end = seed.start, seed.end
                if span is not None and not (start < span[1] and end > span[0]):
                    cursor = max(cursor + 1, end)
                    continue
                token = _numeric_span(text, start, end, evidence=span is not None)
            except Exception as error:
                token = NumericToken(start, end, core_start=start, core_end=end,
                                     ambiguous=True, context_ambiguous=True, reason=str(error))
            if span is None or start < span[1] and end > span[0]:
                tokens.append(token)
            cursor = max(cursor + 1, end)
        return tuple(tokens)
    except Exception as error:
        return (NumericToken(0, 0, ambiguous=True, context_ambiguous=True, reason=str(error)),)
