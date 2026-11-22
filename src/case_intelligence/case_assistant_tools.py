"""Matter-scoped, read-only discovery tools and deterministic calculation receipts."""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import time
from typing import Mapping, Protocol, Sequence

ALLOWED_TOOLS = frozenset({
    "list_sources", "search_discovery", "read_source", "get_case_knowledge",
    "calculate", "query_table", "read_email_thread", "read_transcript_window",
})
ALLOWED_CALC_OPS = frozenset({"sum", "subtract", "multiply", "divide", "percent", "days_between", "running_balance"})
ALLOWED_TABLE_AGG_OPS = frozenset({"count", "sum", "min", "max"})
ALLOWED_KNOWLEDGE_SECTIONS = frozenset({"all", "entities", "assertions", "timeline", "case_theory", "notes", "purpose"})
MAX_LIST_LIMIT, MAX_SEARCH_LIMIT, MAX_EXCERPT_CHARS, MAX_CALC_INPUTS = 100, 50, 4_000, 500


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
                     offset: int = 0, limit: int = 50) -> tuple[Sequence[SourceSummary], int]: ...
    def search_discovery(self, matter_id: str, query: str, *, mode: str = "hybrid",
                         limit: int = 20, document_ids: frozenset[str] | None = None) -> Sequence[SearchHit]: ...
    def read_source(self, matter_id: str, document_id: str, *, start_page: int = 1,
                    end_page: int | None = None) -> SourceReadResult | None: ...
    def get_case_knowledge(self, matter_id: str, *, section: str = "all", limit: int = 50) -> Mapping[str, object]: ...


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
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT}, "document_ids": {"type": "array", "items": {"type": "string"}}}, required=("query",)),
    _tool_spec("read_source", "Read full pages or units from one authorized matter source.", {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "start_page": {"type": "integer", "minimum": 1}, "end_page": {"type": "integer", "minimum": 1}}, required=("document_id",)),
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
    _tool_spec("read_email_thread", "Read the full linked email thread containing an authorized email source.", {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
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

def _int_arg(args: Mapping[str, object], key: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = args.get(key, default)
    if type(raw) is not int or raw < minimum or (maximum is not None and raw > maximum):
        raise AssistantToolError(f"'{key}' must be an integer in range.")
    return raw

def _parse_decimal(raw: object, label: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise AssistantToolError(f"Invalid numeric value for '{label}'.") from exc
    if not value.is_finite():
        raise AssistantToolError(f"Non-finite numeric value for '{label}'.")
    return value

def _format_decimal(value: Decimal, precision: int) -> str:
    quant = Decimal(1) if precision == 0 else Decimal("1." + "0" * precision)
    text = format(value.quantize(quant, rounding=ROUND_HALF_UP), "f")
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
        raw_refs = item.get("source_refs") or ()
        if not (label and value) or not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
            raise AssistantToolError("Each calculation input requires label, value, and optional source_refs list.")
        parsed.append(CalculationInput(label, value, tuple(_parse_ref(r) for r in raw_refs if isinstance(r, Mapping))))
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
    if operation == "sum":
        result, expr = sum(numbers, Decimal(0)), " + ".join(f"{i.label}({i.value})" for i in parsed)
    elif operation == "subtract":
        if len(numbers) < 2:
            raise AssistantToolError("subtract requires at least 2 inputs.")
        result = numbers[0] - sum(numbers[1:], Decimal(0))
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
    formatted = _format_decimal(result, precision)
    return CalculationReceipt(receipt_id, operation, f"{expr} = {formatted}", formatted, unit, parsed)


def _parse_table_rows(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return [], []
    if lines[0].startswith("Row ") and ": " in lines[0] and " | " in lines[0]:
        def split_extracted(line: str) -> list[str]:
            body = line.split(": ", 1)[1] if ": " in line else line
            return [cell.split(": ", 1)[1].strip() if ": " in cell else cell.strip() for cell in body.split(" | ")]
        headers = split_extracted(lines[0])
        rows = [dict(zip(headers, split_extracted(line))) for line in lines[1:]]
        return headers, rows
    delim = "\t" if lines[0].count("\t") > lines[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return list(reader.fieldnames or ()), [{k: str(v or "").strip() for k, v in r.items() if k is not None} for r in reader]


def query_table_rows(
    document_id: str, source_name: str, text: str, *, filters: Mapping[str, object] | None = None,
    aggregate_column: str = "", aggregate_op: str = "", limit: int = 50,
    source_version_id: str = "", location: str = "Line 1", receipt_id: str = "calc-1",
) -> tuple[dict[str, object], CalculationReceipt | None]:
    """Filter CSV/TSV/extracted spreadsheet rows and compute exact Decimal aggregates."""
    if aggregate_op and aggregate_op not in ALLOWED_TABLE_AGG_OPS:
        raise AssistantToolError(f"Unsupported table aggregate operation: {aggregate_op!r}.")
    columns, all_rows = _parse_table_rows(text)
    if not columns:
        raise AssistantToolError("Source does not contain a tabular header row.")
    matched: list[dict[str, str]] = []
    filter_map = dict(filters or {})
    for row in all_rows:
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
            matched.append({k: str(v or "").strip() for k, v in row.items() if k is not None})
    receipt: CalculationReceipt | None = None
    ref = SourceRef(document_id, source_name, location, source_version_id)
    if aggregate_op:
        if aggregate_op == "count":
            val = str(len(matched))
            receipt = CalculationReceipt(receipt_id, "count", f"count({source_name}) = {val}", val, "rows",
                                         (CalculationInput("matched_rows", val, (ref,)),))
        else:
            if not aggregate_column or aggregate_column not in columns:
                raise AssistantToolError(f"Aggregate column {aggregate_column!r} not found in table.")
            vals = [_parse_decimal(r.get(aggregate_column, ""), aggregate_column) for r in matched if r.get(aggregate_column, "")]
            if not vals:
                raise AssistantToolError(f"No numeric values matched for column {aggregate_column!r}.")
            agg = sum(vals, Decimal(0)) if aggregate_op == "sum" else (min(vals) if aggregate_op == "min" else max(vals))
            formatted = _format_decimal(agg, 6)
            receipt = CalculationReceipt(
                receipt_id, aggregate_op, f"{aggregate_op}({aggregate_column} over {len(vals)} rows) = {formatted}",
                formatted, "", tuple(CalculationInput(f"row-{i}", _format_decimal(v, 6), (ref,)) for i, v in enumerate(vals[:50], 1)),
            )
    payload: dict[str, object] = {
        "document_id": document_id, "source_name": source_name, "location": location,
        "columns": columns, "matched_rows": len(matched),
        "rows": matched[:limit], "truncated": len(matched) > limit,
    }
    if receipt is not None:
        payload["calculation"] = asdict(receipt)
    return payload, receipt


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

    @property
    def trace(self) -> tuple[ToolCallRecord, ...]: return tuple(self._trace)
    @property
    def receipts(self) -> Mapping[str, CalculationReceipt]: return dict(self._receipts)
    @property
    def cited_refs(self) -> tuple[SourceRef, ...]: return tuple(self._cited.values())

    def _record_refs(self, refs: Sequence[SourceRef]) -> tuple[SourceRef, ...]:
        unique: list[SourceRef] = []
        for ref in refs:
            self._cited.setdefault((ref.document_id, ref.location, ref.source_version_id), ref)
            if ref not in unique:
                unique.append(ref)
        return tuple(unique)

    def invoke(self, tool_name: str, arguments: Mapping[str, object] | None = None, *, call_id: str = "") -> dict[str, object]:
        started, args, cid = time.monotonic(), dict(arguments or {}), call_id or f"call-{len(self._trace) + 1}"
        if len(self._trace) >= self.max_calls:
            raise AssistantToolError("Assistant tool call budget reached.")
        if not self.backend.authorize(self.matter_id, self.actor_id):
            self._trace.append(ToolCallRecord(cid, tool_name, args, "denied", 0, error="Matter access denied."))
            raise AssistantAccessError("Matter access denied.")
        if tool_name not in ALLOWED_TOOLS:
            self._trace.append(ToolCallRecord(cid, tool_name, args, "error", 0, error="Unsupported tool."))
            raise AssistantToolError(f"Unsupported assistant tool: {tool_name!r}.")
        try:
            payload, refs, receipt = self._dispatch(tool_name, args)
        except (AssistantAccessError, AssistantToolError) as exc:
            self._trace.append(ToolCallRecord(cid, tool_name, args, "error", round((time.monotonic() - started) * 1_000), error=str(exc)))
            raise
        self._trace.append(ToolCallRecord(cid, tool_name, args, "ok", round((time.monotonic() - started) * 1_000),
                                          self._record_refs(refs), receipt))
        return payload

    def _check_doc_scope(self, document_id: str) -> str:
        doc_id = str(document_id or "").strip()
        if not doc_id:
            raise AssistantToolError("document_id is required.")
        if self.allowed_document_ids is not None and doc_id not in self.allowed_document_ids:
            raise AssistantAccessError("Requested source is outside the authorized scope.")
        return doc_id

    def _dispatch(self, tool_name: str, args: dict[str, object]):
        if tool_name == "list_sources":
            offset, limit = _int_arg(args, "offset", 0, minimum=0), _int_arg(args, "limit", 50, minimum=1, maximum=MAX_LIST_LIMIT)
            items, total = self.backend.list_sources(
                self.matter_id, query=str(args.get("query", "")), kind=str(args.get("kind", "")),
                status=str(args.get("status", "")), offset=offset, limit=limit)
            if self.allowed_document_ids is not None:
                items = [row for row in items if row.document_id in self.allowed_document_ids]
            unavail, unavail_total = self.backend.list_sources(self.matter_id, status="failed", offset=0, limit=25)
            return ({"total": total, "offset": offset, "limit": limit, "sources": [asdict(r) for r in items],
                     "unavailable_count": unavail_total, "unavailable_sources": [asdict(r) for r in unavail]}, (), None)
        if tool_name == "search_discovery":
            query, mode = str(args.get("query", "")).strip(), str(args.get("mode", "hybrid")).strip()
            if not query or mode not in {"hybrid", "exact"}:
                raise AssistantToolError("search_discovery requires non-empty query and mode 'hybrid' or 'exact'.")
            limit = _int_arg(args, "limit", 20, minimum=1, maximum=MAX_SEARCH_LIMIT)
            requested, scope = args.get("document_ids"), self.allowed_document_ids
            if isinstance(requested, list) and requested:
                req_set = frozenset(str(item) for item in requested)
                scope = req_set if scope is None else (scope & req_set)
            hits = self.backend.search_discovery(self.matter_id, query, mode=mode, limit=limit, document_ids=scope)
            refs = [SourceRef(h.document_id, h.source_name, h.location, h.source_version_id, h.excerpt_digest) for h in hits]
            return ({"query": query, "mode": mode, "count": len(hits),
                     "hits": [{**asdict(h), "excerpt": h.excerpt[:MAX_EXCERPT_CHARS], "truncated": len(h.excerpt) > MAX_EXCERPT_CHARS} for h in hits]},
                    refs, None)
        if tool_name == "read_source":
            document_id, start_page = self._check_doc_scope(str(args.get("document_id", ""))), _int_arg(args, "start_page", 1, minimum=1)
            end_page = None if args.get("end_page") is None else _int_arg(args, "end_page", start_page, minimum=start_page)
            result = self.backend.read_source(self.matter_id, document_id, start_page=start_page, end_page=end_page)
            if result is None:
                raise AssistantAccessError("Source not found in this matter.")
            remaining, omitted, pages, refs = self.max_read_chars, 0, [], []
            for page in result.pages:
                chunk = page.text[:remaining]
                omitted, remaining = omitted + len(page.text) - len(chunk), remaining - len(chunk)
                pages.append({**asdict(page), "text": chunk})
                refs.append(SourceRef(result.document_id, result.source_name, page.location, page.source_version_id, page.excerpt_digest))
            return ({"document_id": result.document_id, "source_name": result.source_name, "total_pages": result.total_pages,
                     "pages": pages, "truncated": result.truncated or omitted > 0, "omitted_chars": omitted}, refs, None)
        if tool_name == "get_case_knowledge":
            section = str(args.get("section", "all")).strip()
            if section not in ALLOWED_KNOWLEDGE_SECTIONS:
                raise AssistantToolError(f"Unsupported case knowledge section: {section!r}.")
            limit = _int_arg(args, "limit", 50, minimum=1, maximum=100)
            return dict(self.backend.get_case_knowledge(self.matter_id, section=section, limit=limit)), (), None
        if tool_name == "query_table":
            document_id = self._check_doc_scope(str(args.get("document_id", "")))
            limit = _int_arg(args, "limit", 50, minimum=1, maximum=MAX_LIST_LIMIT)
            result = self.backend.read_source(self.matter_id, document_id, start_page=1, end_page=20)
            if result is None or not result.pages:
                raise AssistantAccessError("Table source not found in this matter.")
            full_text = "\n".join(p.text for p in result.pages)
            first = result.pages[0]
            receipt_id = f"calc-{len(self._receipts) + 1}"
            payload, receipt = query_table_rows(
                result.document_id, result.source_name, full_text,
                filters=args.get("filters") if isinstance(args.get("filters"), Mapping) else None,
                aggregate_column=str(args.get("aggregate_column", "")).strip(),
                aggregate_op=str(args.get("aggregate_op", "")).strip(), limit=limit,
                source_version_id=first.source_version_id, location=first.location, receipt_id=receipt_id,
            )
            if receipt is not None:
                self._receipts[receipt_id] = receipt
            refs = [SourceRef(result.document_id, result.source_name, p.location, p.source_version_id, p.excerpt_digest) for p in result.pages]
            return payload, refs, receipt
        if tool_name == "read_email_thread":
            document_id = self._check_doc_scope(str(args.get("document_id", "")))
            limit = _int_arg(args, "limit", 25, minimum=1, maximum=50)
            handler = getattr(self.backend, "read_email_thread", None)
            if not callable(handler):
                raise AssistantToolError("Email thread traversal is not supported by this backend.")
            data = handler(self.matter_id, document_id, limit=limit, allowed_document_ids=self.allowed_document_ids)
            if data is None:
                raise AssistantAccessError("Email source not found in this matter.")
            refs = [SourceRef(m["document_id"], m["source_name"], m["location"], m.get("source_version_id", ""))
                    for m in data.get("messages", ()) if isinstance(m, Mapping)]
            return dict(data), refs, None
        if tool_name == "read_transcript_window":
            document_id = self._check_doc_scope(str(args.get("document_id", "")))
            start_s = float(args.get("start_seconds", 0.0))
            end_s = None if args.get("end_seconds") is None else float(args["end_seconds"])
            if start_s < 0 or (end_s is not None and end_s < start_s):
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
        self._receipts[receipt_id] = receipt
        return asdict(receipt), [ref for item in receipt.inputs for ref in item.source_refs], receipt
