"""Versioned exact-search grammar and document-level reference semantics.

This is independent of ranked answer retrieval. Callers must authorize and scope
documents before evaluating them; a query does not grant access to a population.
"""


from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from collections import deque
from dataclasses import dataclass, field
import re
import unicodedata
from typing import Callable, Iterable

GRAMMAR_VERSION = f"{LEGACY_STEM}-exact-v2"
SUPPORTED_GRAMMAR_VERSIONS = {f"{LEGACY_STEM}-exact-v1", GRAMMAR_VERSION}
TOKENIZER_VERSION = f"{LEGACY_STEM}-words-v1"
MAX_PROXIMITY_GAP = 100
MAX_QUERY_CHARS = 512
MAX_QUERY_TOKENS = 128
MAX_QUERY_DEPTH = 16
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})
_PROXIMITY = re.compile(r"^(NEAR|BEFORE)/([0-9]+)$", re.I)
_UNSUPPORTED_V1 = re.compile(r"^(?:NEAR|WITHIN|ADJ|W/\d+|PRE/\d+)(?:/\d+)?$", re.I)
_UNSUPPORTED = re.compile(r"^(?:NEAR|BEFORE|WITHIN|ADJ|W|PRE)(?:/.*)?$", re.I)


class QuerySyntaxError(ValueError):
    """Safe, content-free explanation with a zero-based source position."""

    def __init__(self, message: str, position: int) -> None:
        self.message = message
        self.position = position
        super().__init__(f"{message} (character {position + 1}).")


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold()).translate(_APOSTROPHES)


def tokenize_text(text: str, *, budget_check: Callable[[], None] | None = None) -> tuple[str, ...]:
    """NFC/casefold words; retain internal apostrophes and ASCII hyphens.

    Other punctuation separates words. No stemming or stop-word removal occurs.
    A phrase is consecutive tokens within one extracted unit, never across units.
    """
    normalized = _normalize_text(text)
    words: list[str] = []
    word: list[str] = []
    for index, char in enumerate(normalized):
        if budget_check is not None and index % 4096 == 0:
            budget_check()
        attached_mark = bool(word) and unicodedata.category(char).startswith("M")
        internal_joiner = (bool(word) and char in "'-" and index + 1 < len(normalized)
                           and normalized[index + 1].isalnum())
        if char.isalnum() or attached_mark or internal_joiner:
            word.append(char)
        elif word:
            words.append("".join(word))
            word = []
    if word:
        words.append("".join(word))
    return tuple(words)


@dataclass(frozen=True)
class Literal:
    words: tuple[str, ...]
    phrase: bool = False


@dataclass(frozen=True)
class Proximity:
    left: Literal
    right: Literal
    gap: int
    ordered: bool = False


@dataclass(frozen=True)
class Not:
    operand: Expression


@dataclass(frozen=True)
class And:
    operands: tuple[Expression, ...]
    # Syntax provenance only: explicit and implicit AND have identical semantics.
    explicit: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class Or:
    operands: tuple[Expression, ...]


Expression = Literal | Proximity | Not | And | Or
Positional = Literal | Proximity


def _plan(node: Expression) -> dict[str, object]:
    if isinstance(node, Literal):
        return {"operator": "phrase" if node.phrase else "term", "words": list(node.words)}
    if isinstance(node, Proximity):
        return {"operator": "before" if node.ordered else "near", "max_intervening_words": node.gap,
                "left": _plan(node.left), "right": _plan(node.right), "boundary": "unit"}
    if isinstance(node, Not):
        return {"operator": "not", "operand": _plan(node.operand)}
    return {
        "operator": "and" if isinstance(node, And) else "or",
        "operands": [_plan(item) for item in node.operands],
    }


def _normalized(node: Expression, parent_precedence: int = 0) -> str:
    if isinstance(node, Literal):
        value = " ".join(node.words)
        return f'"{value}"' if node.phrase else value
    if isinstance(node, Proximity):
        return f"{_normalized(node.left)} {'BEFORE' if node.ordered else 'NEAR'}/{node.gap} {_normalized(node.right)}"
    if isinstance(node, Not):
        precedence = 3
        value = f"NOT {_normalized(node.operand, precedence)}"
    else:
        precedence = 2 if isinstance(node, And) else 1
        operator = " " if isinstance(node, And) else " OR "
        value = operator.join(_normalized(item, precedence) for item in node.operands)
    return f"({value})" if precedence < parent_precedence else value


@dataclass(frozen=True)
class ParsedQuery:
    original: str
    expression: Expression
    grammar_version: str = GRAMMAR_VERSION

    @property
    def normalized(self) -> str:
        return _normalized(self.expression)

    def to_dict(self) -> dict[str, object]:
        """Serializable backend contract; this is not SQL or an executable query."""
        return {
            "grammar_version": self.grammar_version,
            "tokenizer_version": TOKENIZER_VERSION,
            "original": self.original,
            "normalized": self.normalized,
            "expression": _plan(self.expression),
        }

    def matches_units(self, units: Iterable[str], *, budget_check: Callable[[], None] | None = None) -> bool:
        return self.explain_units(units, budget_check=budget_check) is not None

    def explain_units(self, units: Iterable[str], *, budget_check: Callable[[], None] | None = None) -> tuple[Positional, ...] | None:
        """Reference matching for one eligible, already-authorized document.

        AND/OR/NOT apply to the entire document. Positive term/phrase occurrences
        may be in different units, but an individual phrase cannot span units.
        Empty/unextracted documents never count as matches, including under NOT.
        """
        literals: set[Positional] = set()

        def collect(node: Expression) -> None:
            if isinstance(node, (Literal, Proximity)):
                literals.add(node)
            elif isinstance(node, Not):
                collect(node.operand)
            else:
                for child in node.operands:
                    collect(child)

        collect(self.expression)
        found: set[Positional] = set()
        has_text = False
        for unit in units:
            tokens = tokenize_text(unit, budget_check=budget_check)
            if not tokens:
                continue
            has_text = True
            found.update(match_positionals(tokens, literals - found, budget_check=budget_check))

        def witness(node: Expression, desired: bool = True) -> tuple[Positional, ...] | None:
            if isinstance(node, (Literal, Proximity)):
                return ((node,) if desired else ()) if (node in found) == desired else None
            if isinstance(node, Not):
                return witness(node.operand, not desired)
            # An AND is true only through every child; an OR is false only
            # through every child. The opposite cases need one satisfied branch.
            require_all = isinstance(node, And) == desired
            selected = []
            for child in node.operands:
                proof = witness(child, desired)
                if proof is None:
                    if require_all:
                        return None
                elif not require_all:
                    return proof
                else:
                    selected.extend(proof)
            return tuple(dict.fromkeys(selected)) if require_all else None

        return witness(self.expression) if has_text else None



def match_positionals(tokens: tuple[str, ...], nodes: Iterable[Positional], *, budget_check=None):
    """Match simple terms with one shared set; scan positions only when needed."""
    nodes = tuple(nodes)
    words = set(tokens) if any(isinstance(node, Literal) and len(node.words) == 1 for node in nodes) else None
    for node in nodes:
        if budget_check is not None:
            budget_check()
        if isinstance(node, Literal) and len(node.words) == 1:
            if node.words[0] in words:
                yield node
        elif next(matching_spans(tokens, node, budget_check=budget_check), None) is not None:
            yield node


def matching_spans(tokens: tuple[str, ...], node: Positional, *, budget_check=None):
    """Yield half-open token spans; proximity operands never overlap.

    A gap is the number of tokenizer words between the two full operand spans.
    Pairing uses sorted positions, avoiding a Cartesian product for repetition.
    """
    if isinstance(node, Literal):
        width = len(node.words)
        for index in range(len(tokens) - width + 1):
            if budget_check is not None and index % 4096 == 0:
                budget_check()
            if tokens[index:index + width] == node.words:
                yield index, index + width
        return
    # Discover both orientations together. Advancing a separate occurrence
    # iterator can exhaust a large tail before an early reverse match is tried.
    # Only recent completed occurrences can qualify, keeping memory bounded by
    # the query's maximum operand width and gap rather than source length.
    widths = (len(node.left.words), len(node.right.words))
    recent = (deque(), deque())
    for end in range(1, len(tokens) + 1):
        if budget_check is not None and (end - 1) % 4096 == 0:
            budget_check()
        found = tuple(end >= width and tokens[end - width:end] == literal.words
                      for width, literal in zip(widths, (node.left, node.right)))
        for spans in recent:
            while spans and spans[0][1] < end - max(widths) - node.gap:
                spans.popleft()
        for side in (1,) if node.ordered else (0, 1):
            if not found[side]:
                continue
            start = end - widths[side]
            earlier = next((span for span in reversed(recent[1 - side]) if span[1] <= start), None)
            if earlier is not None and start - earlier[1] <= node.gap:
                yield earlier[0], end
        for side in (0, 1):
            if found[side]:
                recent[side].append((end - widths[side], end))



@dataclass(frozen=True)
class _Token:
    kind: str
    position: int
    literal: Literal | None = None
    gap: int = 0
    ordered: bool = False


def _lex(query: str, grammar_version: str) -> tuple[_Token, ...]:
    result: list[_Token] = []
    index = 0
    while index < len(query):
        if query[index].isspace():
            index += 1
            continue
        start = index
        char = query[index]
        if char in "()":
            result.append(_Token(char, start))
            index += 1
        elif char == '"':
            index += 1
            value: list[str] = []
            while index < len(query) and query[index] != '"':
                if query[index] == "\\":
                    index += 1
                    if index >= len(query) or query[index] not in {'"', "\\"}:
                        raise QuerySyntaxError('Escape only a quote or backslash inside a phrase', index - 1)
                value.append(query[index])
                index += 1
            if index == len(query):
                raise QuerySyntaxError("Close the quoted phrase", start)
            index += 1
            words = tokenize_text("".join(value))
            if not words:
                raise QuerySyntaxError("A phrase needs at least one word", start)
            result.append(_Token("literal", start, Literal(words, phrase=True)))
            if index < len(query) and not query[index].isspace() and query[index] not in "()":
                raise QuerySyntaxError("Separate the phrase from the next term", index)
        else:
            while index < len(query) and not query[index].isspace() and query[index] not in '()"':
                index += 1
            value = query[start:index]
            if index < len(query) and query[index] == '"':
                raise QuerySyntaxError("Separate the term from the quoted phrase", index)
            if value.upper() in {"AND", "OR", "NOT"}:
                result.append(_Token(value.upper(), start))
            elif proximity := _PROXIMITY.fullmatch(value):
                if grammar_version == f"{LEGACY_STEM}-exact-v1":
                    raise QuerySyntaxError(f"Proximity requires the {LEGACY_STEM}-exact-v2 grammar", start)
                gap = int(proximity[2])
                if gap > MAX_PROXIMITY_GAP:
                    raise QuerySyntaxError("Use a proximity distance from 0 to 100 intervening words", start)
                result.append(_Token("proximity", start, gap=gap, ordered=proximity[1].upper() == "BEFORE"))
            else:
                unsupported = _UNSUPPORTED_V1 if grammar_version == f"{LEGACY_STEM}-exact-v1" else _UNSUPPORTED
                if unsupported.fullmatch(value):
                    raise QuerySyntaxError("Use NEAR/n or BEFORE/n with 0 to 100 intervening words", start)
                if any(mark in value for mark in "*?~:"):
                    raise QuerySyntaxError("Wildcards, fuzzy search, and field operators are not supported yet", start)
                normalized = _normalize_text(value)
                if tokenize_text(normalized) != (normalized,):
                    raise QuerySyntaxError("Use a word, a quoted phrase, or AND, OR, NOT and parentheses", start)
                result.append(_Token("literal", start, Literal((normalized,))))
        if len(result) > MAX_QUERY_TOKENS:
            raise QuerySyntaxError("Use fewer terms and operators", start)
    return tuple(result)


class _Parser:
    def __init__(self, tokens: tuple[_Token, ...], end: int) -> None:
        self.tokens = tokens
        self.index = 0
        self.end = end

    @property
    def kind(self) -> str:
        return self.tokens[self.index].kind if self.index < len(self.tokens) else "end"

    @property
    def position(self) -> int:
        return self.tokens[self.index].position if self.index < len(self.tokens) else self.end

    def disjunction(self, depth: int = 0) -> Expression:
        nodes = [self.conjunction(depth)]
        while self.kind == "OR":
            self.index += 1
            nodes.append(self.conjunction(depth))
        return nodes[0] if len(nodes) == 1 else Or(tuple(nodes))

    def conjunction(self, depth: int) -> Expression:
        nodes = [self.unary(depth)]
        explicit = False
        while self.kind in {"AND", "NOT", "literal", "("}:
            if self.kind == "AND":
                explicit = True
                self.index += 1
            nodes.append(self.unary(depth))
        return nodes[0] if len(nodes) == 1 else And(tuple(nodes), explicit=explicit)

    def unary(self, depth: int) -> Expression:
        if depth > MAX_QUERY_DEPTH:
            raise QuerySyntaxError("Use fewer nested parentheses or NOT operators", self.position)
        if self.kind == "NOT":
            self.index += 1
            return Not(self.unary(depth + 1))
        if self.kind == "(":
            start = self.position
            self.index += 1
            node = self.disjunction(depth + 1)
            if self.kind != ")":
                raise QuerySyntaxError("Close the parenthesized expression", start)
            self.index += 1
            return node
        if self.kind == "literal":
            token = self.tokens[self.index]
            self.index += 1
            assert token.literal is not None
            if self.kind == "proximity":
                proximity = self.tokens[self.index]
                self.index += 1
                if self.kind != "literal":
                    raise QuerySyntaxError("Put a word or quoted phrase after the proximity operator", self.position)
                right = self.tokens[self.index].literal
                self.index += 1
                assert right is not None
                if self.kind == "proximity":
                    raise QuerySyntaxError("Use separate proximity pairs joined with AND or OR", self.position)
                return Proximity(token.literal, right, proximity.gap, proximity.ordered)
            return token.literal
        raise QuerySyntaxError("Add a search term or phrase here", self.position)


def parse_query(query: str, *, grammar_version: str = GRAMMAR_VERSION) -> ParsedQuery:
    if grammar_version not in SUPPORTED_GRAMMAR_VERSIONS:
        raise QuerySyntaxError("Choose a supported exact-search grammar version", 0)
    if not isinstance(query, str):
        raise QuerySyntaxError("Enter a text query", 0)
    if len(query) > MAX_QUERY_CHARS:
        raise QuerySyntaxError(f"Keep the query within {MAX_QUERY_CHARS} characters", MAX_QUERY_CHARS)
    for index, char in enumerate(query):
        if unicodedata.category(char).startswith("C"):
            raise QuerySyntaxError("Remove control or hidden formatting characters", index)
    tokens = _lex(query, grammar_version)
    if not tokens:
        raise QuerySyntaxError("Enter a search term or phrase", 0)
    parser = _Parser(tokens, len(query))
    expression = parser.disjunction()
    if parser.kind == "proximity":
        raise QuerySyntaxError("Proximity requires two words or quoted phrases", parser.position)
    if parser.kind != "end":
        raise QuerySyntaxError("Remove the unmatched closing parenthesis", parser.position)
    parsed = ParsedQuery(query, expression, grammar_version)
    if len(parsed.normalized) > MAX_QUERY_CHARS:
        raise QuerySyntaxError(
            f"Keep the normalized query within {MAX_QUERY_CHARS} characters", len(query)
        )
    return parsed
