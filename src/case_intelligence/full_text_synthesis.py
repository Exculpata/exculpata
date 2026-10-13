"""Bound one terminal full-text review to the existing shared synthesis engine."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import re

from .full_text_synthesis_repository import ADMISSION, VERSION, encoded_bytes
from .generation import TRANSCRIPT_EVIDENCE_KIND
from .hierarchical_synthesis import digest, run_synthesis, valid_claim
from .workspace_store import WorkspaceProblem


OUTCOME_REASONS = {"admitted", "unsupported_finding", "oversized_original", "finding_limit",
                   "input_byte_limit", "unsealed_or_invalidated"}


def _admitted_finding_text(candidate, source):
    token = source["support_token"]
    claim = {"text": candidate["text"], "support_tokens": [token], "parents": [str(candidate["cursor"])]}
    if valid_claim(claim, {token: source}):
        return claim["text"]
    # Classification saves an exact original span. Add presentation attribution
    # only for that strongest support path; never rewrite the saved rationale or
    # turn an unsupported transcript paraphrase into an admissible finding.
    text = " ".join(candidate["text"].split()) if isinstance(candidate["text"], str) else ""
    if (source["evidence_kind"] == TRANSCRIPT_EVIDENCE_KIND and text
            and text.casefold() in " ".join(source["excerpt"].split()).casefold()):
        claim["text"] = "The machine transcript appears to say that " + text
        if valid_claim(claim, {token: source}):
            return claim["text"]
    return None


def input_notice(receipt):
    """Plain coverage context kept separate from generated source-backed claims."""
    counts, coverage = receipt["counts"], receipt["coverage"]
    omitted = "; ".join(f"{value:,} {reason.replace('_', ' ')}" for reason, value in sorted(counts.items())
                        if reason not in {"candidate_findings", "admitted"} and value)
    ranges, units, sources = coverage["ranges"], coverage["units"], coverage["sources"]
    prefix = "Partial full-text synthesis input." if receipt["partial"] else "Bounded full-text synthesis input."
    return (f"{prefix} {counts.get('admitted', 0):,} of {counts['candidate_findings']:,} saved candidate findings admitted. "
            + (f"Omitted findings: {omitted}. " if omitted else "")
            + f"Review coverage retains {ranges.get('no_finding', 0):,} excluded/no-finding ranges, "
            f"{ranges.get('failed', 0):,} failed analysis ranges, {ranges.get('pending', 0):,} pending ranges, "
            f"{units.get('empty', 0):,} empty units, {sources.get('unavailable', 0):,} unavailable extraction sources, "
            f"and {sources.get('pending', 0):,} sources with unresolved inventory. "
            "Human decision revisions and counts are frozen context, separate from machine findings and original evidence. "
            "This receipt does not establish whole-run factual coverage or that every relevant fact was recognized.")


def _input_digest(prepared):
    receipt = {key: value for key, value in prepared["full_text_synthesis_input"].items() if key != "input_digest"}
    return digest({"receipt": receipt, "passes": prepared["passes"], "evidence": prepared["evidence"]})


def validate_prepared(prepared):
    """Validate persisted adapter inputs without a service or mutable database."""
    try:
        receipt, passes, evidence = (prepared["full_text_synthesis_input"], prepared["passes"], prepared["evidence"])
        if receipt["version"] != VERSION or receipt["admission"] != ADMISSION:
            raise ValueError("Unknown full-text synthesis input version or policy.")
        if receipt["human_decisions"]["mode"] != "frozen_context_not_evidence":
            raise ValueError("Unknown full-text human-decision basis.")
        if receipt["run_state"] not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("The full-text synthesis basis was not terminal.")
        for key in ("snapshot_digest", "criterion_digest", "decision_revision_digest", "input_digest"):
            if not isinstance(receipt[key], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[key]):
                raise ValueError("Invalid full-text snapshot digest.")
        if receipt["table_digests"]["decisions"] != receipt["decision_revision_digest"]:
            raise ValueError("The full-text decision revision receipt changed.")
        sources = receipt["sources"]
        if not isinstance(sources, list) or not sources or len(sources) > ADMISSION["sources"]:
            raise ValueError("Invalid full-text frozen population.")
        frozen = {source["document_id"]: source for source in sources}
        if len(frozen) != len(sources):
            raise ValueError("Repeated full-text frozen source.")
        if not isinstance(passes, list) or not isinstance(evidence, list) or len(passes) > ADMISSION["findings"] or len(evidence) > ADMISSION["findings"]:
            raise ValueError("Oversized full-text admitted inputs.")
        if not receipt["question"].strip() or len(receipt["question"]) > 2_000:
            raise ValueError("Invalid full-text criterion question.")
        outcomes = receipt["outcomes"]
        if not isinstance(outcomes, list) or len(outcomes) > ADMISSION["ranges"]:
            raise ValueError("Invalid full-text finding outcomes.")
        previous = 0
        for item in outcomes:
            if not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int or item[0] <= previous or item[1] not in OUTCOME_REASONS:
                raise ValueError("Invalid full-text finding identity or omission reason.")
            previous = item[0]
        counts = dict(Counter(item[1] for item in outcomes))
        if receipt["counts"] != {"candidate_findings": len(outcomes), **counts} or counts.get("admitted", 0) != len(passes):
            raise ValueError("Full-text candidates are not completely accounted for.")
        if receipt["partial"] != bool(any(reason != "admitted" for _, reason in outcomes) or receipt["coverage"]["has_gaps"] or not passes):
            raise ValueError("The full-text partial-coverage receipt is invalid.")
        ledger = {}
        for source in evidence:
            if (source["matter_id"] != receipt["matter_id"] or not source["excerpt"]
                or source["document_id"] not in frozen
                or source["source_version_id"] != frozen[source["document_id"]]["source_version_id"]
                or len(source["excerpt"]) > ADMISSION["original_characters"]
                or hashlib.sha256(source["excerpt"].encode()).hexdigest() != source["excerpt_digest"]
                or source["support_token"] in ledger):
                raise ValueError("Invalid full-text original evidence.")
            ledger[source["support_token"]] = source
        admitted = [cursor for cursor, reason in outcomes if reason == "admitted"]
        for step, cursor in zip(passes, admitted):
            claim = step["answer"]["claims"][0]
            tokens = [item["support_token"] for item in claim["citations"]]
            if (step["full_text_cursor"] != cursor or len(step["answer"]["claims"]) != 1
                or step["status"] != "supported" or step["text"] != claim["text"]
                or step["answer"]["answerable"] is not True
                or not valid_claim({"text": claim["text"], "support_tokens": tokens, "parents": [str(cursor)]}, ledger)):
                raise ValueError("A full-text finding lacks original support.")
            for citation in claim["citations"]:
                if any(citation[key] != ledger[citation["support_token"]][key] for key in ("source_name", "location", "evidence_kind")):
                    raise ValueError("A full-text finding changed its original citation.")
        oversized = receipt["oversized_originals"]
        if (not isinstance(oversized, list) or [item[0] for item in oversized] != [cursor for cursor, reason in outcomes if reason == "oversized_original"]
            or any(len(item) != 4 or item[1] not in frozen or type(item[2]) is not int or item[2] < 1
                   or type(item[3]) is not int or item[3] <= ADMISSION["original_characters"] for item in oversized)):
            raise ValueError("The oversized original omission receipt is invalid.")
        if receipt["input_digest"] != _input_digest(prepared):
            raise ValueError("The full-text synthesis input receipt changed.")
        if encoded_bytes(receipt) > ADMISSION["receipt_bytes"]:
            raise ValueError("The complete full-text input receipt exceeds its persistence byte limit.")
        if encoded_bytes({"full_text_synthesis_input": receipt, "passes": passes, "evidence": evidence}) > ADMISSION["input_bytes"]:
            raise ValueError("Full-text synthesis inputs exceed the persistence byte limit.")
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("The full-text synthesis input receipt is incomplete or invalid.") from exc
    return receipt


def validate_job_input(job, prepared=None):
    """Bind a queued, interrupted or completed job to its frozen input receipt."""
    receipt = validate_prepared(job.result if prepared is None else prepared)
    try:
        if (type(job.plan.get("full_text_synthesis_version")) is not int
                or job.plan["full_text_synthesis_version"] != VERSION
                or job.plan.get("input_digest") != receipt["input_digest"]
                or job.plan.get("run_id") != receipt["run_id"]
                or job.question != receipt["question"]
                or job.matter_id != receipt["matter_id"]
                or job.source_set_id != receipt["source_set_id"]):
            raise ValueError("The synthesis job no longer matches its frozen full-text input.")
    except (AttributeError, TypeError, KeyError) as exc:
        raise ValueError("The synthesis job has an invalid full-text input binding.") from exc
    return receipt


class FullTextSynthesisService:
    def __init__(self, *, repository, resolve_originals, generator=None):
        self.repository, self.resolve_originals, self.generator = repository, resolve_originals, generator

    def prepare(self, matter_id, actor_id, run_id):
        captured = self.repository.capture(matter_id, actor_id, run_id)
        receipt, candidates = captured["receipt"], captured["candidates"]
        unique = {}
        for item in candidates:
            unique.setdefault((item["document_id"], item["unit_ordinal"]), item["locator"])
        selected_documents = {key[0] for key in unique}
        source_basis = {item["document_id"]: item["source_basis_digest"] for item in receipt["sources"] if item["document_id"] in selected_documents}
        source_versions = {item["document_id"]: item["source_version_id"] for item in receipt["sources"] if item["document_id"] in selected_documents}
        originals = self.resolve_originals(list(unique.values()), source_basis_digests=source_basis, source_versions=source_versions) if unique else []
        if len(originals) != len(unique):
            raise WorkspaceProblem("One or more saved findings have no original source.")
        original_by_unit = dict(zip(unique, originals))
        prepared = {"full_text_synthesis_input": receipt, "passes": [], "evidence": []}
        outcomes = {cursor: [cursor, reason] for cursor, reason in receipt["outcomes"]}
        receipt["outcomes"] = list(outcomes.values())
        evidence_by_token = {}
        for candidate in candidates:
            source = original_by_unit[(candidate["document_id"], candidate["unit_ordinal"])]
            locator = candidate["locator"]
            if any(source.get(key) != locator.get(key) for key in ("matter_id", "document_id", "source_version_id", "support_token", "excerpt_digest", "chunk_id", "unit_number", "location")):
                raise WorkspaceProblem("A resolved original no longer matches its full-text locator.")
            if len(source["excerpt"]) != candidate["text_chars"] or len(source["excerpt"]) > ADMISSION["original_characters"]:
                raise WorkspaceProblem("An original exceeds the admitted full-text character limit or changed length.")
            if hashlib.sha256(source["excerpt"].encode()).hexdigest() != source["excerpt_digest"]:
                raise WorkspaceProblem("A resolved original has stale extraction digest metadata.")
            token, cursor = source["support_token"], candidate["cursor"]
            text = _admitted_finding_text(candidate, source)
            if text is None:
                outcomes[cursor][1] = "unsupported_finding"
                continue
            step = {"full_text_cursor": cursor, "finding_key": candidate["finding_key"],
                    "status": "supported", "text": text, "query": f"Saved full-text finding {cursor}",
                    "candidate_sources": 1,
                    "answer": {"answerable": True, "introduction": "", "missing_information": "", "limitation": None,
                               "claims": [{"text": text, "citations": [{key: source[key] for key in ("source_name", "location", "evidence_kind", "support_token")}]}]}}
            new_original = token not in evidence_by_token
            prepared["passes"].append(step)
            if new_original:
                prepared["evidence"].append(source)
            # Reserve room for fixed receipt fields added below. Whole findings
            # are omitted; source text and rationale are never silently sliced.
            if encoded_bytes(prepared) + 4_096 > ADMISSION["input_bytes"]:
                prepared["passes"].pop()
                if new_original:
                    prepared["evidence"].pop()
                outcomes[cursor][1] = "input_byte_limit"
            else:
                evidence_by_token[token] = source
                outcomes[cursor][1] = "admitted"
        receipt["counts"] = {"candidate_findings": len(outcomes), **dict(Counter(item[1] for item in outcomes.values()))}
        receipt["partial"] = bool(any(item[1] != "admitted" for item in outcomes.values()) or receipt["coverage"]["has_gaps"] or not prepared["passes"])
        receipt["input_digest"] = _input_digest(prepared)
        validate_prepared(prepared)
        return prepared

    def run(self, question, prepared, checkpoint, boundary, saved=None, **kwargs):
        receipt = validate_prepared(prepared)
        if question != receipt["question"]:
            raise WorkspaceProblem("Full-text synthesis must use the selected review's exact criterion.")
        if self.generator is None:
            raise RuntimeError("No synthesis generator was supplied.")
        boundary()
        return run_synthesis(question, prepared["passes"], prepared["evidence"], self.generator,
                             checkpoint, boundary, deepcopy(saved), **kwargs)
