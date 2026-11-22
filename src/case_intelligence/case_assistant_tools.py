"""Matter-scoped, read-only discovery tools and deterministic calculation receipts."""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import io
import math
import re
import time
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

ALLOWED_TOOLS = frozenset({
    "list_sources", "search_discovery", "read_source", "get_case_knowledge",
    "calculate", "query_table", "read_email_thread", "read_transcript_window",
})
ALLOWED_CALC_OPS = frozenset({"sum", "subtract", "multiply", "divide", "percent", "days_between", "running_balance"})
ALLOWED_TABLE_AGG_OPS = frozenset({"count", "sum", "min", "max"})
ALLOWED_KNOWLEDGE_SECTIONS = frozenset({"all", "entities", "assertions", "timeline", "case_theory", "notes", "purpose"})
MAX_LIST_LIMIT, MAX_SEARCH_LIMIT, MAX_EXCERPT_CHARS, MAX_CALC_INPUTS = 100, 50, 4_000, 500
_CALC_PRECISION = 64
_MAX_CALC_EXPONENT = 24
_COMMA_NUMBER_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


class AssistantToolError(ValueError):
    """Structured, fail-closed error for invalid tool names or arguments."""

class AssistantAccessError(PermissionError):
    """Matter authorization or document-scope violation."""

@dataclass(frozen=True)
class SourceRef:
    document_id: str; source_name: str; location: str
    source_version_id: str = ""; excerpt_digest: str = ""

@dataclass(frozen=True)
class SourceSummary:
    document_id: str; source_name: str; kind: str; status: str
    page_count: int = 0; path: str = ""; bates: str = ""; status_detail: str = ""

@dataclass(frozen=True)
class SearchHit:
    document_id: str; source_name: str; location: str; excerpt: str
    score: float = 0.0; source_version_id: str = ""; excerpt_digest: str = ""; evidence_kind: str = "document"

@dataclass(frozen=True)
class SearchResponse:
    hits: tuple[SearchHit, ...]
    total_matches: int
    offset: int = 0
    has_more: bool = False
    excluded_unavailable_count: int = 0

@dataclass(frozen=True)
class SourcePage:
    page_number: int; location: str; text: str
    source_version_id: str = ""; excerpt_digest: str = ""

@dataclass(frozen=True)
class SourceReadResult:
    document_id: str; source_name: str; total_pages: int
    pages: tuple[SourcePage, ...]; truncated: bool = False

@dataclass(frozen=True)
class CalculationInput:
    label: str; value: str; source_refs: tuple[SourceRef, ...] = ()

@dataclass(frozen=True)
class CalculationReceipt:
    receipt_id: str; operation: str; expression: str
    result: str; unit: str; inputs: tuple[CalculationInput, ...]

@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str; tool_name: str; arguments: Mapping[str, object]; status: str; elapsed_ms: int
    cited_refs: tuple[SourceRef, ...] = (); receipt: CalculationReceipt | None = None; error: str = ""


class MatterToolBackend(Protocol):
    def authorize(self, matter_id: str, actor_id: str) -> bool: ...
    def list_sources(self, matter_id: str, *, query: str = "", kind: str = "", status: str = "",
                     offset: int = 0, limit: int = 50,
                     document_ids: frozenset[str] | None = None) -> tuple[Sequence[SourceSummary], int]: ...
    def search_discovery(self, matter_id: str, query: str, *, mode: str = "hybrid",
                         limit: int = 20, offset: int = 0,
                         document_ids: frozenset[str] | None = None) -> SearchResponse | Sequence[SearchHit]: ...
    def read_source(self, matter_id: str, document_id: str, *, start_page: int = 1,
                    end_page: int | None = None) -> SourceReadResult | None: ...
    def get_case_knowledge(self, matter_id: str, *, section: str = "all", limit: int = 50,
                           document_ids: frozenset[str] | None = None) -> Mapping[str, object]: ...


def _tool_spec(name: str, description: str, properties: dict[str, object], required: Sequence[str] = ()) -> dict[str, object]:
    params: dict[str, object] = {"type": "object", "additionalProperties": False, "properties": properties}
    if required:
        params["required"] = list(required)
    return {"name": name, "description": description, "parameters": params}


TOOL_DEFINITIONS: tuple[dict[str, object], ...] = (
    _tool_spec("list_sources", "List matter discovery sources, kinds, Bates IDs, and unavailable/failed files.", {
        "query": {"type": "string", "maxLength": 200}, "kind": {"type": "string", "maxLength": 64},
        "status": {"type": "string", "maxLength": 32}, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT}}),
    _tool_spec("search_discovery", "Search authorized matter discovery using hybrid or exact Boolean/phrase queries.", {
        "query": {"type": "string", "minLength": 1, "maxLength": 512}, "mode": {"type": "string", "enum": ["hybrid", "exact"]},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
        "document_ids": {"type": "array", "items": {"type": "string"}}}, required=("query",)),
    _tool_spec("read_source", "Read full pages or units from one authorized matter source.", {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "start_page": {"type": "integer", "minimum": 1}, "end_page": {"type": "integer", "minimum": 1},
        "char_offset": {"type": "integer", "minimum": 0}}, required=("document_id",)),
    _tool_spec("get_case_knowledge", "Read saved entities, assertions, timeline events, case theory, or matter notes.", {
        "section": {"type": "string", "enum": sorted(ALLOWED_KNOWLEDGE_SECTIONS)}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _tool_spec("calculate", "Run exact deterministic arithmetic or date math over labeled, source-cited inputs.", {
        "operation": {"type": "string", "enum": sorted(ALLOWED_CALC_OPS)},
        "inputs": {"type": "array", "minItems": 1, "maxItems": MAX_CALC_INPUTS, "items": {
            "type": "object", "additionalProperties": False, "required": ["label", "value"],
            "properties": {"label": {"type": "string", "minLength": 1, "maxLength": 120},
                           "value": {"type": "string", "minLength": 1, "maxLength": 64},
                           "source_refs": {"type": "array", "items": {"type": "object"}}}}},
        "unit": {"type": "string", "maxLength": 32}, "precision": {"type": "integer", "minimum": 0, "maximum": 12}}, required=("operation", "inputs")),
    _tool_spec("query_table", "Filter CSV/TSV source rows and optionally compute exact Decimal column aggregates.", {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "filters": {"type": "object"}, "aggregate_column": {"type": "string", "maxLength": 120},
        "aggregate_op": {"type": "string", "enum": sorted(ALLOWED_TABLE_AGG_OPS)},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT}}, required=("document_id",)),
    _tool_spec("read_email_thread", "Read the linked email thread containing an authorized email source with pagination.", {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, required=("document_id",)),
    _tool_spec("read_transcript_window", "Read timestamped transcript cues within a start/end second range.", {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "start_seconds": {"type": "number", "minimum": 0}, "end_seconds": {"type": "number", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT}}, required=("document_id",)),
)

def openai_tool_schemas() -> list[dict[str, object]]:
    return [{"type": "function", "function": dict(defn)} for defn in TOOL_DEFINITIONS]

def ollama_tool_schemas() -> list[dict[str, object]]:
    return openai_tool_schemas()

def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(v) for v in value)
    return value

def _int_arg(args: Mapping[str, object], key: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = args.get(key, default)
    if type(raw) is not int or raw < minimum or (maximum is not None and raw > maximum):
        raise AssistantToolError(f"'{key}' must be an integer in range.")
    return raw

def _parse_decimal(raw: object, label: str) -> Decimal:
    text = str(raw).strip()
    if not text:
        raise AssistantToolError(f"Invalid numeric value for '{label}'.")
    if "," in text:
        if not _COMMA_NUMBER_RE.fullmatch(text):
            raise AssistantToolError(f"Malformed comma grouping in numeric value for '{label}'.")
        text = text.replace(",", "")
    try:
        with localcontext() as ctx:
            ctx.prec = _CALC_PRECISION
            value = Decimal(text)
    except InvalidOperation as exc:
        raise AssistantToolError(f"Invalid numeric value for '{label}'.") from exc
    if not value.is_finite():
        raise AssistantToolError(f"Non-finite numeric value for '{label}'.")
    if value != 0 and abs(value.adjusted()) > _MAX_CALC_EXPONENT + 12:
        raise AssistantToolError(f"Numeric value out of supported range for '{label}'.")
    return value

def _format_decimal(value: Decimal, precision: int) -> str:
    quant = Decimal(1) if precision == 0 else Decimal("1." + "0" * precision)
    try:
        with localcontext() as ctx:
            ctx.prec = _CALC_PRECISION
            if not value.is_finite():
                raise AssistantToolError("Calculation produced a non-finite result.")
            quantized = value.quantize(quant, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError) as exc:
        raise AssistantToolError("Calculation result is outside the supported precision range.") from exc
    text = format(quantized, "f")
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text in {"-0", ""} else text

def _parse_ref(raw: Mapping[str, object]) -> SourceRef:
    doc_id, source_name, location = (str(raw.get(k, "")).strip() for k in ("document_id", "source_name", "location"))
    if not (doc_id and source_name and location):
        raise AssistantToolError("Source reference requires document_id, source_name, and location.")
    return SourceRef(doc_id, source_name, location, str(raw.get("source_version_id", "")).strip(), str(raw.get("excerpt_digest", "")).strip())

def _parse_calc_inputs(raw_inputs: object) -> tuple[CalculationInput, ...]:
    if not isinstance(raw_inputs, Sequence) or isinstance(raw_inputs, (str, bytes)) or not 1 <= len(raw_inputs) <= MAX_CALC_INPUTS:
        raise AssistantToolError(f"Calculation requires between 1 and {MAX_CALC_INPUTS} inputs.")
    parsed: list[CalculationInput] = []
    for item in raw_inputs:
        if not isinstance(item, Mapping):
            raise AssistantToolError("Each calculation input must be an object.")
        label, value = str(item.get("label", "")).strip(), str(item.get("value", "")).strip()
        raw_refs = item.get("source_refs")
        if raw_refs is None:
            raw_refs = ()
        if not (label and value) or not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
            raise AssistantToolError("Each calculation input requires label, value, and optional source_refs list.")
        refs: list[SourceRef] = []
        for entry in raw_refs:
            if not isinstance(entry, Mapping):
                raise AssistantToolError("Each source_ref must be an object with document_id, source_name, and location.")
            refs.append(_parse_ref(entry))
        parsed.append(CalculationInput(label, value, tuple(refs)))
    return tuple(parsed)


def run_calculation(operation: str, *, inputs: Sequence[Mapping[str, object]],
                    unit: str = "", precision: int = 6, receipt_id: str = "calc-1") -> CalculationReceipt:
    if operation not in ALLOWED_CALC_OPS:
        raise AssistantToolError(f"Unsupported calculation operation: {operation!r}.")
    if type(precision) is not int or not 0 <= precision <= 12:
        raise AssistantToolError("precision must be an integer between 0 and 12.")
    parsed = _parse_calc_inputs(inputs)
    if operation == "days_between":
        if len(parsed) != 2:
            raise AssistantToolError("days_between requires exactly 2 ISO date inputs.")
        try:
            delta = (date.fromisoformat(parsed[1].value) - date.fromisoformat(parsed[0].value)).days
        except ValueError as exc:
            raise AssistantToolError("days_between inputs must be ISO YYYY-MM-DD dates.") from exc
        return CalculationReceipt(receipt_id, operation, f"{parsed[1].value} - {parsed[0].value} = {delta} days",
                                  str(delta), unit or "days", parsed)
    numbers = [_parse_decimal(item.value, item.label) for item in parsed]
    try:
        with localcontext() as ctx:
            ctx.prec = _CALC_PRECISION
            if operation == "sum":
                result = Decimal(0)
                for value in numbers:
                    result += value
                expr = " + ".join(f"{i.label}({i.value})" for i in parsed)
            elif operation == "subtract":
                if len(numbers) < 2:
                    raise AssistantToolError("subtract requires at least 2 inputs.")
                subtrahend = Decimal(0)
                for value in numbers[1:]:
                    subtrahend += value
                result = numbers[0] - subtrahend
                expr = f"{parsed[0].label}({parsed[0].value}) - " + " - ".join(f"{i.label}({i.value})" for i in parsed[1:])
            elif operation == "multiply":
                if not 2 <= len(numbers) <= 50:
                    raise AssistantToolError("multiply requires between 2 and 50 inputs.")
                result = Decimal(1)
                for value in numbers:
                    result *= value
                expr = " * ".join(f"{i.label}({i.value})" for i in parsed)
            elif operation in {"divide", "percent"}:
                if len(numbers) != 2 or numbers[1] == 0:
                    raise AssistantToolError(f"{operation} requires 2 inputs with a non-zero denominator.")
                result = (numbers[0] / numbers[1]) * (Decimal(100) if operation == "percent" else Decimal(1))
                expr = f"({parsed[0].label}({parsed[0].value}) / {parsed[1].label}({parsed[1].value}))" + (" * 100" if operation == "percent" else "")
                unit = unit or ("%" if operation == "percent" else "")
            else:
                running, steps = Decimal(0), []
                for item, value in zip(parsed, numbers):
                    running += value
                    steps.append(f"{item.label}({item.value})->{_format_decimal(running, precision)}")
                result, expr = running, "; ".join(steps)
    except (InvalidOperation, ArithmeticError) as exc:
        raise AssistantToolError("Calculation exceeded supported numeric bounds.") from exc
    formatted = _format_decimal(result, precision)
    return CalculationReceipt(receipt_id, operation, f"{expr} = {formatted}", formatted, unit, parsed)


_EXTRACTED_ROW_RE = re.compile(r"^Row\s+\d+:\s+(.*)$")
_CELL_COORD_RE = re.compile(r"^([A-Za-z0-9]+):\s*(.*)$")


def _format_exact_decimal(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = _CALC_PRECISION
        if not value.is_finite():
            raise AssistantToolError("Calculation produced a non-finite result.")
        if value == 0:
            return "0"
        text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_extracted_cells(line: str) -> list[tuple[str, str]] | None:
    match = _EXTRACTED_ROW_RE.match(line)
    if not match:
        return None
    body = match.group(1)
    cells: list[tuple[str, str]] = []
    for raw_cell in body.split(" | "):
        cm = _CELL_COORD_RE.match(raw_cell.strip())
        if not cm:
            return None
        cells.append((cm.group(1).strip(), cm.group(2).strip()))
    return cells if cells else None


def _parse_table_sections(
    sections: Sequence[tuple[str, str, str]],
) -> tuple[list[str], list[tuple[dict[str, str], str, str]]]:
    tagged_lines: list[tuple[str, str, str]] = []
    for loc, ver, text in sections:
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped:
                tagged_lines.append((stripped, loc, ver))
    if not tagged_lines:
        return [], []
    first_line = tagged_lines[0][0]
    header_cells = _parse_extracted_cells(first_line)
    if header_cells is not None:
        coord_headers = [(coord, col) for coord, col in header_cells if col]
        headers = [col for _, col in coord_headers]
        rows: list[tuple[dict[str, str], str, str]] = []
        for line, loc, ver in tagged_lines[1:]:
            parsed = _parse_extracted_cells(line)
            if parsed is None:
                raise AssistantToolError(
                    "Extracted table row contains ambiguous cell delimiters and cannot be aggregated reliably."
                )
            by_coord = dict(parsed)
            rows.append(({col: by_coord.get(coord, "") for coord, col in coord_headers}, loc, ver))
        return headers, rows
    delim = "\t" if first_line.count("\t") > first_line.count(",") else ","
    header_reader = csv.reader(io.StringIO(first_line), delimiter=delim)
    headers = [h.strip() for h in next(header_reader, []) if h.strip()]
    rows = []
    for line, loc, ver in tagged_lines[1:]:
        vals = next(csv.reader(io.StringIO(line), delimiter=delim), [])
        row = {col: (vals[idx].strip() if idx < len(vals) else "") for idx, col in enumerate(headers)}
        rows.append((row, loc, ver))
    return headers, rows


def _parse_table_rows(text: str) -> tuple[list[str], list[dict[str, str]]]:
    cols, tagged = _parse_table_sections([("Line 1", "", text)])
    return cols, [row for row, _, _ in tagged]


def query_table_rows(
    document_id: str, source_name: str, text: str = "", *, filters: Mapping[str, object] | None = None,
    aggregate_column: str = "", aggregate_op: str = "", limit: int = 50,
    source_version_id: str = "", location: str = "Line 1", receipt_id: str = "calc-1",
    sections: Sequence[tuple[str, str, str]] | None = None,
) -> tuple[dict[str, object], CalculationReceipt | None]:
    """Filter CSV/TSV/extracted spreadsheet rows and compute exact Decimal aggregates with per-section provenance."""
    if aggregate_op and aggregate_op not in ALLOWED_TABLE_AGG_OPS:
        raise AssistantToolError(f"Unsupported table aggregate operation: {aggregate_op!r}.")
    section_list = list(sections) if sections is not None else [(location, source_version_id, text)]
    columns, all_rows = _parse_table_sections(section_list)
    if not columns:
        raise AssistantToolError("Source does not contain a tabular header row.")
    matched: list[tuple[dict[str, str], str, str]] = []
    filter_map = dict(filters or {})
    for row, row_loc, row_ver in all_rows:
        keep = True
        for col, cond in filter_map.items():
            cell = str(row.get(col, "")).strip()
            if isinstance(cond, Mapping):
                num = _parse_decimal(cell, col) if cell else None
                if num is None:
                    keep = False; break
                if "min" in cond and num < _parse_decimal(cond["min"], f"{col}.min"):
                    keep = False; break
                if "max" in cond and num > _parse_decimal(cond["max"], f"{col}.max"):
                    keep = False; break
            elif str(cond).strip().casefold() not in cell.casefold():
                keep = False; break
        if keep:
            cleaned = {k: str(v or "").strip() for k, v in row.items() if k is not None}
            matched.append((cleaned, row_loc, row_ver))
    receipt: CalculationReceipt | None = None
    if aggregate_op:
        if aggregate_op == "count":
            if len(matched) > MAX_CALC_INPUTS:
                raise AssistantToolError(
                    f"Row count over {len(matched)} matched rows exceeds auditable receipt limit ({MAX_CALC_INPUTS}); narrow filters."
                )
            val = str(len(matched))
            count_inputs = tuple(
                CalculationInput(f"row-{i}", "1", (SourceRef(document_id, source_name, rloc, rver),))
                for i, (_, rloc, rver) in enumerate(matched, 1)
            )
            if not count_inputs:
                first_loc, first_ver = section_list[0][0], section_list[0][1]
                count_inputs = (CalculationInput("matched_rows", "0", (SourceRef(document_id, source_name, first_loc, first_ver),)),)
            receipt = CalculationReceipt(receipt_id, "count", f"count({source_name}) = {val}", val, "rows", count_inputs)
        else:
            if not aggregate_column or aggregate_column not in columns:
                raise AssistantToolError(f"Aggregate column {aggregate_column!r} not found in table.")
            val_rows = [
                (_parse_decimal(r.get(aggregate_column, ""), aggregate_column), rloc, rver)
                for r, rloc, rver in matched if r.get(aggregate_column, "")
            ]
            if not val_rows:
                raise AssistantToolError(f"No numeric values matched for column {aggregate_column!r}.")
            if len(val_rows) > MAX_CALC_INPUTS:
                raise AssistantToolError(
                    f"Aggregate over {len(val_rows)} values exceeds auditable receipt limit ({MAX_CALC_INPUTS}); narrow filters."
                )
            with localcontext() as ctx:
                ctx.prec = _CALC_PRECISION
                if aggregate_op == "sum":
                    agg = Decimal(0)
                    for v, _, _ in val_rows:
                        agg += v
                elif aggregate_op == "min":
                    agg = min(v for v, _, _ in val_rows)
                else:
                    agg = max(v for v, _, _ in val_rows)
            formatted = _format_exact_decimal(agg)
            receipt = CalculationReceipt(
                receipt_id, aggregate_op, f"{aggregate_op}({aggregate_column} over {len(val_rows)} rows) = {formatted}",
                formatted, "", tuple(
                    CalculationInput(f"row-{i}", _format_exact_decimal(v), (SourceRef(document_id, source_name, rloc, rver),))
                    for i, (v, rloc, rver) in enumerate(val_rows, 1)
                ),
            )
    primary_loc = matched[0][1] if matched else section_list[0][0]
    primary_ver = matched[0][2] if matched else section_list[0][1]
    matched_locations = list(dict.fromkeys(rloc for _, rloc, _ in matched))
    out_rows = [{**row, "_location": rloc, "_source_version_id": rver} for row, rloc, rver in matched[:limit]]
    payload: dict[str, object] = {
        "document_id": document_id, "source_name": source_name, "location": primary_loc,
        "source_version_id": primary_ver, "locations": matched_locations,
        "columns": columns, "total_rows": len(all_rows), "matched_rows": len(matched),
        "rows": out_rows, "truncated": len(matched) > limit,
    }
    if receipt is not None:
        payload["calculation"] = asdict(receipt)
    return payload, receipt


def _extract_knowledge_doc_ids(entry: object) -> frozenset[str] | None:
    """Return all supporting document IDs declared on a knowledge entry, or None if unprovable."""
    if not isinstance(entry, Mapping):
        return None
    ids: set[str] = set()
    for key in ("source_document_ids", "document_ids"):
        raw = entry.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    ids.add(item.strip())
    raw_cites = entry.get("citations")
    if isinstance(raw_cites, Sequence) and not isinstance(raw_cites, (str, bytes)):
        for cite in raw_cites:
            if isinstance(cite, Mapping) and isinstance(cite.get("document_id"), str) and cite["document_id"].strip():
                ids.add(cite["document_id"].strip())
    return frozenset(ids) if ids else None


def _validate_scoped_knowledge(payload: Mapping[str, object], allowed_ids: frozenset[str]) -> dict[str, object]:
    """Fail closed if any returned case-knowledge item lacks complete in-scope document support."""
    validated: dict[str, object] = {}
    for key, value in payload.items():
        if value is None or value == "" or value == [] or value == ():
            validated[key] = value
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for entry in value:
                doc_ids = _extract_knowledge_doc_ids(entry)
                if not doc_ids or not doc_ids.issubset(allowed_ids):
                    raise AssistantAccessError("Case knowledge entry is outside the authorized document scope.")
            validated[key] = list(value)
        elif isinstance(value, Mapping):
            doc_ids = _extract_knowledge_doc_ids(value)
            if not doc_ids or not doc_ids.issubset(allowed_ids):
                raise AssistantAccessError("Case knowledge entry is outside the authorized document scope.")
            validated[key] = dict(value)
        else:
            raise AssistantAccessError("Unscoped case knowledge cannot be returned under a document-restricted runtime.")
    return validated


class MatterToolRuntime:
    """Authorize, bound, invoke, and record read-only Case Assistant tool calls."""

    def __init__(self, matter_id: str, actor_id: str, backend: MatterToolBackend, *,
                 allowed_document_ids: frozenset[str] | None = None, max_calls: int = 40, max_read_chars: int = 24_000) -> None:
        self.matter_id, self.actor_id, self.backend = matter_id, actor_id, backend
        self.allowed_document_ids = allowed_document_ids
        self.max_calls, self.max_read_chars = max(1, int(max_calls)), max(1_000, int(max_read_chars))
        self._trace: list[ToolCallRecord] = []
        self._receipts: dict[str, CalculationReceipt] = {}
        self._cited: dict[tuple[str, str, str], SourceRef] = {}
        self._inspected: dict[tuple[str, str, str], SourceRef] = {}

    @property
    def trace(self) -> tuple[ToolCallRecord, ...]: return tuple(self._trace)
    @property
    def receipts(self) -> Mapping[str, CalculationReceipt]: return dict(self._receipts)
    @property
    def cited_refs(self) -> tuple[SourceRef, ...]: return tuple(self._cited.values())
    @property
    def inspected_refs(self) -> tuple[SourceRef, ...]: return tuple(self._inspected.values())

    def _record_refs(self, refs: Sequence[SourceRef], *, inspected: bool) -> tuple[SourceRef, ...]:
        unique: list[SourceRef] = []
        for ref in refs:
            key = (ref.document_id, ref.location, ref.source_version_id)
            self._cited.setdefault(key, ref)
            if inspected:
                self._inspected.setdefault(key, ref)
            if ref not in unique:
                unique.append(ref)
        return tuple(unique)

    def invoke(self, tool_name: str, arguments: Mapping[str, object] | None = None, *, call_id: str = "") -> dict[str, object]:
        started = time.monotonic()
        if arguments is not None and not isinstance(arguments, Mapping):
            raise AssistantToolError("Tool arguments must be an object.")
        frozen_args = _freeze_value(arguments or {})
        cid = call_id or f"call-{len(self._trace) + 1}"
        if len(self._trace) >= self.max_calls:
            raise AssistantToolError("Assistant tool call budget reached.")
        if not self.backend.authorize(self.matter_id, self.actor_id):
            self._trace.append(ToolCallRecord(cid, tool_name, frozen_args, "denied", 0, error="Matter access denied."))
            raise AssistantAccessError("Matter access denied.")
        if tool_name not in ALLOWED_TOOLS:
            self._trace.append(ToolCallRecord(cid, tool_name, frozen_args, "error", 0, error="Unsupported tool."))
            raise AssistantToolError(f"Unsupported assistant tool: {tool_name!r}.")
        try:
            payload, refs, receipt = self._dispatch(tool_name, frozen_args)
        except (AssistantAccessError, AssistantToolError) as exc:
            self._trace.append(ToolCallRecord(cid, tool_name, frozen_args, "error", round((time.monotonic() - started) * 1_000), error=str(exc)))
            raise
        self._trace.append(ToolCallRecord(cid, tool_name, frozen_args, "ok", round((time.monotonic() - started) * 1_000),
                                          self._record_refs(refs, inspected=(tool_name != "calculate")), receipt))
        return payload

    def _check_doc_scope(self, document_id: str) -> str:
        doc_id = str(document_id or "").strip()
        if not doc_id:
            raise AssistantToolError("document_id is required.")
        if self.allowed_document_ids is not None and doc_id not in self.allowed_document_ids:
            raise AssistantAccessError("Requested source is outside the authorized scope.")
        return doc_id

    @staticmethod
    def _parse_seconds_arg(args: Mapping[str, object], key: str, default: float | None) -> float | None:
        if key not in args:
            return default
        raw = args.get(key)
        if raw is None:
            if default is None:
                return None
            raise AssistantToolError(f"'{key}' must be a finite non-negative number.")
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise AssistantToolError(f"'{key}' must be a finite non-negative number.")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise AssistantToolError(f"'{key}' must be a finite non-negative number.") from exc
        if not math.isfinite(value) or value < 0:
            raise AssistantToolError(f"'{key}' must be a finite non-negative number.")
        return value

    def _parse_document_ids_filter(self, args: Mapping[str, object]) -> frozenset[str] | None:
        if "document_ids" not in args or args.get("document_ids") is None:
            return self.allowed_document_ids
        requested = args.get("document_ids")
        if not isinstance(requested, (list, tuple)):
            raise AssistantToolError("'document_ids' must be an array of non-empty document ID strings.")
        cleaned: list[str] = []
        for item in requested:
            if not isinstance(item, str) or not item.strip():
                raise AssistantToolError("'document_ids' entries must be non-empty strings.")
            cleaned.append(item.strip())
        req_set = frozenset(cleaned)
        return req_set if self.allowed_document_ids is None else (self.allowed_document_ids & req_set)

    def _dispatch(self, tool_name: str, args: Mapping[str, object]):
        if tool_name == "list_sources":
            offset, limit = _int_arg(args, "offset", 0, minimum=0), _int_arg(args, "limit", 50, minimum=1, maximum=MAX_LIST_LIMIT)
            if self.allowed_document_ids is not None and not self.allowed_document_ids:
                return ({"total": 0, "offset": offset, "limit": limit, "sources": [],
                         "unavailable_count": 0, "unavailable_sources": []}, (), None)
            items, total = self.backend.list_sources(
                self.matter_id, query=str(args.get("query", "")), kind=str(args.get("kind", "")),
                status=str(args.get("status", "")), offset=offset, limit=limit,
                document_ids=self.allowed_document_ids)
            unavail, unavail_total = self.backend.list_sources(
                self.matter_id, status="failed", offset=0, limit=25,
                document_ids=self.allowed_document_ids)
            if self.allowed_document_ids is not None:
                items = [row for row in items if row.document_id in self.allowed_document_ids]
                unavail = [row for row in unavail if row.document_id in self.allowed_document_ids]
                total = min(total, len(self.allowed_document_ids))
                unavail_total = min(unavail_total, len(self.allowed_document_ids))
            return ({"total": total, "offset": offset, "limit": limit, "sources": [asdict(r) for r in items],
                     "unavailable_count": unavail_total, "unavailable_sources": [asdict(r) for r in unavail]}, (), None)
        if tool_name == "search_discovery":
            query, mode = str(args.get("query", "")).strip(), str(args.get("mode", "hybrid")).strip()
            if not query or mode not in {"hybrid", "exact"}:
                raise AssistantToolError("search_discovery requires non-empty query and mode 'hybrid' or 'exact'.")
            offset = _int_arg(args, "offset", 0, minimum=0)
            limit = _int_arg(args, "limit", 20, minimum=1, maximum=MAX_SEARCH_LIMIT)
            scope = self._parse_document_ids_filter(args)
            if scope is not None and not scope:
                return ({"query": query, "mode": mode, "offset": offset, "limit": limit, "count": 0,
                         "total_matches": 0, "has_more": False, "next_offset": None, "truncated": False,
                         "excluded_unavailable_count": 0, "hits": []}, (), None)
            raw_search = self.backend.search_discovery(
                self.matter_id, query, mode=mode, limit=limit, offset=offset, document_ids=scope)
            if isinstance(raw_search, SearchResponse):
                hits = tuple(raw_search.hits)
                total_matches = raw_search.total_matches
                has_more = raw_search.has_more
                excluded_unavailable = raw_search.excluded_unavailable_count
            else:
                hits = tuple(raw_search)
                total_matches = offset + len(hits)
                has_more = False
                excluded_unavailable = 0
            if scope is not None:
                hits = tuple(h for h in hits if h.document_id in scope)
            next_offset = offset + len(hits) if has_more else None
            refs = [SourceRef(h.document_id, h.source_name, h.location, h.source_version_id, h.excerpt_digest) for h in hits]
            return ({"query": query, "mode": mode, "offset": offset, "limit": limit, "count": len(hits),
                     "total_matches": total_matches, "has_more": has_more, "next_offset": next_offset,
                     "truncated": has_more, "excluded_unavailable_count": excluded_unavailable,
                     "hits": [{**asdict(h), "excerpt": h.excerpt[:MAX_EXCERPT_CHARS], "truncated": len(h.excerpt) > MAX_EXCERPT_CHARS} for h in hits]},
                    refs, None)
        if tool_name == "read_source":
            document_id, start_page = self._check_doc_scope(str(args.get("document_id", ""))), _int_arg(args, "start_page", 1, minimum=1)
            end_page = None if args.get("end_page") is None else _int_arg(args, "end_page", start_page, minimum=start_page)
            char_offset = _int_arg(args, "char_offset", 0, minimum=0)
            result = self.backend.read_source(self.matter_id, document_id, start_page=start_page, end_page=end_page)
            if result is None:
                raise AssistantAccessError("Source not found in this matter.")
            skip_left, remaining, omitted, emitted, pages, refs = char_offset, self.max_read_chars, 0, 0, [], []
            for page in result.pages:
                text = page.text
                if skip_left > 0 and skip_left >= len(text):
                    skip_left -= len(text)
                    continue
                sliced = text[skip_left:]
                page_start_offset = skip_left
                skip_left = 0
                if remaining <= 0:
                    omitted += len(sliced)
                    continue
                chunk = sliced[:remaining]
                omitted += len(sliced) - len(chunk)
                remaining -= len(chunk)
                emitted += len(chunk)
                pages.append({**asdict(page), "text": chunk, "char_offset": page_start_offset, "total_chars": len(text)})
                refs.append(SourceRef(result.document_id, result.source_name, page.location, page.source_version_id, page.excerpt_digest))
            truncated = result.truncated or omitted > 0
            next_char_offset = char_offset + emitted if omitted > 0 else None
            return ({"document_id": result.document_id, "source_name": result.source_name, "total_pages": result.total_pages,
                     "char_offset": char_offset, "next_char_offset": next_char_offset,
                     "pages": pages, "truncated": truncated, "omitted_chars": omitted}, refs, None)
        if tool_name == "get_case_knowledge":
            section = str(args.get("section", "all")).strip()
            if section not in ALLOWED_KNOWLEDGE_SECTIONS:
                raise AssistantToolError(f"Unsupported case knowledge section: {section!r}.")
            limit = _int_arg(args, "limit", 50, minimum=1, maximum=100)
            raw_knowledge = dict(self.backend.get_case_knowledge(
                self.matter_id, section=section, limit=limit, document_ids=self.allowed_document_ids))
            if self.allowed_document_ids is not None:
                raw_knowledge = _validate_scoped_knowledge(raw_knowledge, self.allowed_document_ids)
            return raw_knowledge, (), None
        if tool_name == "query_table":
            document_id = self._check_doc_scope(str(args.get("document_id", "")))
            limit = _int_arg(args, "limit", 50, minimum=1, maximum=MAX_LIST_LIMIT)
            result = self.backend.read_source(self.matter_id, document_id, start_page=1, end_page=None)
            if result is None or not result.pages:
                raise AssistantAccessError("Table source not found in this matter.")
            if result.total_pages > 500 or result.truncated or len(result.pages) < result.total_pages:
                raise AssistantToolError("Table source exceeds complete-aggregation coverage bounds.")
            sections = [(p.location, p.source_version_id, p.text) for p in result.pages]
            receipt_id = f"calc-{len(self._receipts) + 1}"
            payload, receipt = query_table_rows(
                result.document_id, result.source_name,
                filters=args.get("filters") if isinstance(args.get("filters"), Mapping) else None,
                aggregate_column=str(args.get("aggregate_column", "")).strip(),
                aggregate_op=str(args.get("aggregate_op", "")).strip(), limit=limit,
                receipt_id=receipt_id, sections=sections,
            )
            if receipt is not None:
                self._receipts[receipt_id] = receipt
            refs = [SourceRef(result.document_id, result.source_name, p.location, p.source_version_id, p.excerpt_digest) for p in result.pages]
            return payload, refs, receipt
        if tool_name == "read_email_thread":
            document_id = self._check_doc_scope(str(args.get("document_id", "")))
            offset = None if "offset" not in args or args.get("offset") is None else _int_arg(args, "offset", 0, minimum=0)
            limit = _int_arg(args, "limit", 25, minimum=1, maximum=50)
            handler = getattr(self.backend, "read_email_thread", None)
            if not callable(handler):
                raise AssistantToolError("Email thread traversal is not supported by this backend.")
            data = handler(
                self.matter_id, document_id, limit=limit, offset=offset,
                allowed_document_ids=self.allowed_document_ids,
            )
            if data is None:
                raise AssistantAccessError("Email source not found in this matter.")
            refs: list[SourceRef] = []
            for m in data.get("messages", ()):
                if not isinstance(m, Mapping):
                    continue
                spans = m.get("body_spans")
                if isinstance(spans, Sequence) and not isinstance(spans, (str, bytes)) and spans:
                    for span in spans:
                        if isinstance(span, Mapping):
                            refs.append(SourceRef(
                                m["document_id"], m["source_name"], str(span["location"]),
                                str(span.get("source_version_id") or m.get("source_version_id", "")),
                                str(span.get("excerpt_digest", "")),
                            ))
                else:
                    refs.append(SourceRef(m["document_id"], m["source_name"], m["location"], m.get("source_version_id", "")))
            return dict(data), refs, None
        if tool_name == "read_transcript_window":
            document_id = self._check_doc_scope(str(args.get("document_id", "")))
            start_s = self._parse_seconds_arg(args, "start_seconds", 0.0)
            end_s = self._parse_seconds_arg(args, "end_seconds", None)
            assert start_s is not None
            if end_s is not None and end_s < start_s:
                raise AssistantToolError("start_seconds must be >= 0 and end_seconds >= start_seconds.")
            limit = _int_arg(args, "limit", 50, minimum=1, maximum=MAX_LIST_LIMIT)
            handler = getattr(self.backend, "read_transcript_window", None)
            if not callable(handler):
                raise AssistantToolError("Transcript window reading is not supported by this backend.")
            data = handler(self.matter_id, document_id, start_seconds=start_s, end_seconds=end_s, limit=limit)
            if data is None:
                raise AssistantAccessError("Transcript source not found in this matter.")
            refs = [SourceRef(data["document_id"], data["source_name"], c["location"], c.get("source_version_id", ""))
                    for c in data.get("cues", ()) if isinstance(c, Mapping)]
            return dict(data), refs, None
        receipt_id = f"calc-{len(self._receipts) + 1}"
        precision = _int_arg(args, "precision", 6, minimum=0, maximum=12)
        receipt = run_calculation(str(args.get("operation", "")), inputs=args.get("inputs", ()),
                                  unit=str(args.get("unit", "")), precision=precision, receipt_id=receipt_id)
        calc_refs = [ref for item in receipt.inputs for ref in item.source_refs]
        if self.allowed_document_ids is not None:
            for ref in calc_refs:
                if ref.document_id not in self.allowed_document_ids:
                    raise AssistantAccessError("Calculation references a source outside the authorized document scope.")
        self._receipts[receipt_id] = receipt
        return asdict(receipt), calc_refs, receipt
