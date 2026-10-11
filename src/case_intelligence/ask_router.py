"""Pure Ask intent classification; no I/O, search execution or model calls."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal as Kind

from .exact_search import And, Expression, Literal, Not, Or, Proximity, QuerySyntaxError, parse_query


@dataclass(frozen=True)
class RouteDecision:
    kind: Kind["exact", "question", "every_source"]
    reason: str


# Consume complete quoted phrases and their escapes before matching operator tokens.
_BOOLEAN_OR_PHRASE = re.compile(r'"(?:[^"\\]|\\["\\])*"|(?<![^\s()])(?P<operator>AND|OR|NOT)(?![^\s()])')
# These are natural-language retrieval cues, not an exact-search grammar.
_FORMAT = r"\.?(?:pdf|docx|txt|jpg|jpeg|png|tif|tiff|eml|csv|tsv|xlsx|wav|mp3|m4a|ogg|opus|mp4|mov|webm)"
_RECORD = (
    r"(?:documents?|records?|sources?|files?|e-?mails?|messages?|texts?|reports?|"
    r"transcripts?|statements?|interviews?|recordings?|notes?|memos?|exhibits?|"
    r"attachments?|references?|mentions?|occurrences?|matches|results|evidence|items?|"
    rf"{_FORMAT}s?|videos?|images?|photos?|photographs?|pictures?|spreadsheets?|workbooks?|audio)"
)
_QUANTITY = r"(?:all(?:\s+of)?(?:\s+the)?|every(?:\s+single)?|each(?:\s+and\s+every)?)"
_MEDIA = rf"(?:{_FORMAT}|video|audio|image|photo|spreadsheet|text|excel|word)"
_POPULATION = rf"{_QUANTITY}\s+(?:(?:matching|relevant|available)\s+)?(?:{_MEDIA}\s+)?{_RECORD}\b"
_RETRIEVE = (r"(?:find|list|show|locate|identify|retrieve|return|collect|get|check|review|"
             r"enumerate|pull(?:\s+up)?|search\s+for|give\s+me)")
_REQUEST = (r"(?:please\s+)?(?:(?:(?:can|could|would|will)\s+you|"
            r"i\s+(?:need|want)\s+(?:you\s+)?to)\s+(?:please\s+)?)?")
# Filtered populations require a positive request, not an ambiguous subject.
_EVERY_SOURCE = re.compile(
    rf"^(?:{_REQUEST}{_RETRIEVE}\s+(?:me\s+)?"
    rf"(?:{_POPULATION}|{_QUANTITY}\s*[.!?]*$)"
    rf"|i\s+(?:need|want)\s+{_POPULATION}\s*[.!?]*$"
    rf"|(?:please\s+)?{_POPULATION}\s*[.!?]*$)", re.IGNORECASE)
# Quoted topics in prose are not instructions to AND every prose word.
_NATURAL_REQUEST = re.compile(
    rf"^(?:{_REQUEST}(?:who|what|where|when|why|how|which|whose|"
    rf"is|are|was|were|do|does|did|has|have|had|can|could|would|will|should|"
    rf"explain|summarize|describe|compare|know|understand|tell\s+me|help\s+me|{_RETRIEVE})"
    rf"|i\s+(?:need|want)\s+(?:an?\s+)?(?:explanation|summary|comparison|description))\b", re.IGNORECASE)
# "Show all reports are consistent" asks about a proposition, not a population.
_POPULATION_STATEMENT = re.compile(r"^\s+(?:is|are|was|were|has|have|had|seems?|agree|agrees)\b", re.IGNORECASE)
# Only unquoted corrections/conditions can withhold a positive request.
_WITHHELD_REQUEST = re.compile(
    r"(?:[.!?;]\s*no\b|\bonly\s+(?:if|when|after)\b|\bbut\s+(?:do\s+not|don't)\b)", re.IGNORECASE)


def _exact_reason(node: Expression, *, uppercase_boolean: bool, phrases: bool = True) -> str | None:
    if isinstance(node, Literal):
        return "Chose exact search because your words are in quotes." if node.phrase and phrases else None
    if isinstance(node, Proximity):
        return "Chose exact search because you specified how close words must be."
    if uppercase_boolean and (isinstance(node, (Not, Or)) or isinstance(node, And) and node.explicit):
        return "Chose exact search because you used a Boolean operator."
    operands = (node.operand,) if isinstance(node, Not) else node.operands
    for operand in operands:
        if reason := _exact_reason(operand, uppercase_boolean=uppercase_boolean, phrases=phrases):
            return reason
    return None


def classify(text: str) -> RouteDecision:
    """Require valid exact grammar; otherwise use enumeration cues or question fallback."""
    text = text.strip()
    if not text:
        return RouteDecision("question", "Nothing was typed, so there is no question to answer yet.")

    population = _EVERY_SOURCE.search(text)
    unquoted = _BOOLEAN_OR_PHRASE.sub(lambda match: match[0] if match["operator"] else " ", text)
    enumerates = bool(population and not _POPULATION_STATEMENT.search(text[population.end():])
                      and not _WITHHELD_REQUEST.search(unquoted))
    natural_request = bool(_NATURAL_REQUEST.search(text)) and not enumerates
    try:
        parsed = parse_query(text)
    except QuerySyntaxError:
        pass
    else:
        uppercase = any(match["operator"] for match in _BOOLEAN_OR_PHRASE.finditer(text))
        if reason := _exact_reason(parsed.expression, uppercase_boolean=uppercase, phrases=not natural_request):
            return RouteDecision("exact", reason)

    if enumerates:
        return RouteDecision("every_source", "Chose every source because you asked for all matching records.")
    if natural_request:
        return RouteDecision("question", "Chose a question because you asked about the records in ordinary language.")
    return RouteDecision("question", "Chose a question because no search instructions were recognized.")
