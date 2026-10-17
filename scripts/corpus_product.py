"""Observe synthetic corpus inputs through ingestion, search and cited answers.

This adapter never reads expected outputs. Fast runs ingest native PDFs and
explicit prepared transcript projections; they do not test OCR, ASR or media intake.
Full runs ingest scanned PDFs and original audio with independently observed ASR
segments. Both use the real workbench router and a deterministic extractive
generator, not a language-model quality evaluation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid
import wave

from case_intelligence.ask_router import classify
from case_intelligence.answer_evidence import _terms
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.pilot_uploads import PilotDocument, canonical_media_type
from case_intelligence.review_bench import Citation, ExtractiveAnswerProvider
from case_intelligence.review_bench_v2 import DeterministicEmbeddingAdapter, DeterministicReranker
from case_intelligence.workbench import CaseIntelligenceWorkbench


class CorpusExtractiveGenerator:
    """Deterministic lexical selection followed by the product extractive provider.

    This intentionally small fixture generator is not a semantic model: retain
    only passages with the highest positive current-question word overlap,
    using the product's evidence-window filler vocabulary. Quoted topic text
    restricts those words when present. It never consults corpus expectations.
    """

    available = True

    def generate(self, *, question, evidence, **kwargs):
        quoted = re.findall(r'"([^"\n]+)"', question)
        terms = _terms(" ".join(quoted) if quoted else question)
        scores = [len(terms & _terms(item.excerpt)) for item in evidence]
        maximum = max(scores, default=0)
        evidence = tuple(item for item, score in zip(evidence, scores) if score == maximum and score > 0)
        citations = tuple(Citation(item.source_name, item.location, item.excerpt, "")
                          for item in evidence)
        selected = ExtractiveAnswerProvider().answer(question, citations).citations
        return {
            "answerable": bool(selected),
            "claims": [{"text": (
                            "The machine transcript appears to say that "
                            if evidence[citations.index(citation)].evidence_kind == "transcript" else ""
                        ) + citation.excerpt,
                        "evidence_ids": [evidence[citations.index(citation)].evidence_id]}
                       for citation in selected],
            "limitation": None,
            "missing_information": "" if selected else "No supporting corpus passage.",
        }


@contextmanager
def _environment(**values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _input_path(root, relative):
    path = root / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Corpus inputs must be regular files inside the corpus root")
    return path


def _prepared_media(store, path):
    """Seed immutable WAV bytes for fast transcript integration, without probing.

    This matches the product's transcript projection regression fixture seam.
    Full mode must use store_stream instead, including its real media preflight.
    """
    original = path.read_bytes()
    with wave.open(str(path), "rb") as audio:
        duration_ms = round(audio.getnframes() / audio.getframerate() * 1000)
    identifier = uuid.uuid4().hex
    document = PilotDocument(
        identifier, path.name, identifier + ".wav", "audio/wav", len(original),
        "queued", "Prepared synthetic transcript fixture", [],
        digest=hashlib.sha256(original).hexdigest(), version_id=uuid.uuid4().hex,
        duration_ms=duration_ms,
    )
    (store.files / document.stored_name).write_bytes(original)
    document._units_loader, document._units_iterator = store._load_units, store._iter_units
    with store.mutation_guard():
        store.documents[identifier] = document
        store._save((identifier,))
    return document


def _import_transcript(bench, matter, document, segments, actor):
    job = bench.workspace.queue_media_job(
        matter.matter_id, document.document_id, document.version_id, actor,
        source_sha256=document.digest, byte_size=document.size,
        media_type=document.media_type, duration_ms=document.duration_ms)
    claimed = bench.workspace.claim_media_job("synthetic-corpus-worker")
    if claimed is None or claimed.media_job_id != job.media_job_id:
        raise RuntimeError("Corpus transcript job could not be claimed")
    bench.workspace.import_media_transcript(job.media_job_id, segments=[{
        "external_segment_id": f"corpus-segment-{index}",
        "start_ms": round(segment["start"] * 1000),
        "end_ms": round(segment["end"] * 1000),
        "speaker_cluster": segment.get("speaker", "UNKNOWN"),
        "model_text": segment["text"],
    } for index, segment in enumerate(segments, 1)], warnings=[], quality={}, provenance={})
    bench.workspace.finish_media_job(job.media_job_id, degraded=False, message="Synthetic corpus projection")
    bench._project_media_transcript(matter, document, (), degraded=False)
    return bench.source_store(matter).get(document.document_id)


def _citation(bench, matter, citation, aliases):
    """Resolve every persisted token against the current source and exact span."""
    value = asdict(citation) if not isinstance(citation, dict) else citation
    support = bench.support(matter, value["support_token"])
    document = bench.source_store(matter).get(value["document_id"])
    unit = next(unit for unit in document.parsed_units()
                if unit.number == value["unit_number"])
    excerpt = unit.text
    valid = (document.state == "ready" and document.version_id == value["source_version_id"]
             and unit.excerpt_digest == value["excerpt_digest"]
             and support.lines == tuple(excerpt.splitlines())
             and support.location == value["location"]
             and value.get("excerpt", excerpt) == excerpt)
    return {"document_id": aliases[document.document_id], "location": support.location,
            "excerpt": excerpt, "start": 0, "end": len(excerpt),
            "unit": unit.number, "start_ms": unit.start_ms, "end_ms": unit.end_ms,
            "source_valid": valid, "evidence_kind": value["evidence_kind"]}


def observe(root: Path, manifest: dict, full: bool = False, *,
            observed_transcripts: dict | None = None) -> dict:
    """Return stable case IDs and actual product observations, without grading.

    ``observed_transcripts`` maps audio IDs to ``{start,end,text,speaker?}``
    segments, with times in seconds. Full mode requires this separate inference
    output and never falls back to ``fast_transcripts``.
    """
    if manifest.get("synthetic") is not True:
        raise ValueError("Only explicitly synthetic corpora are supported")
    # Never attach this disposable run to an operator's storage or database.
    if any(os.getenv(name) for name in (
        "CASE_INTELLIGENCE_POSTGRES_DSN", "CASE_INTELLIGENCE_POSTGRES_DSN_FILE",
        "CASE_INTELLIGENCE_MANAGED_STORAGE_ROOT", "CASE_INTELLIGENCE_SOURCE_LOCATIONS_FILE",
        "CASE_INTELLIGENCE_CLAMAV_HOST",
    )):
        raise ValueError("Corpus observation requires an isolated environment without configured storage or database")
    if full and manifest.get("audio") and observed_transcripts is None:
        raise ValueError("Full observation requires independently observed transcripts")
    results, aliases = {}, {}
    transcripts = observed_transcripts if full else manifest.get("fast_transcripts", {})
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="synthetic-corpus-") as temporary, _environment(
        CASE_INTELLIGENCE_TEST_MODE="1", CASE_INTELLIGENCE_MAINTENANCE_ENABLED="0",
        CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY="0",
    ):
        bench = CaseIntelligenceWorkbench(
            Path(temporary).resolve(), generator=CorpusExtractiveGenerator(),
            embedding=DeterministicEmbeddingAdapter(), reranker=DeterministicReranker(),
            learned_retrieval=False, storage_policy=StoragePolicy(reserve_bytes=0),
            answer_workers=1,
        )
        try:
            # This synchronous check owns its jobs; do not contend with workers.
            for name in ("answers", "research", "full_review", "media", "playback"):
                coordinator = getattr(bench, name)
                if coordinator is not None:
                    coordinator.close()
                    setattr(bench, name, None)
            principal = bench.workspace.upsert_principal(
                "test", "synthetic-corpus", "Synthetic corpus reviewer", "corpus@example.test")
            matter = bench.create_matter("Synthetic discovery corpus", "Generated fixtures only", principal.principal_id)
            store = bench.source_store(matter)
            for item in manifest.get("documents", []):
                if item.get("ocr") and not full:
                    continue
                path = _input_path(root, item["path"])
                with _environment(CASE_INTELLIGENCE_OCR_MODE="expanded" if item.get("ocr") else "disabled",
                                  CASE_INTELLIGENCE_OCR_LANGUAGE=item.get("language", "eng")), path.open("rb") as stream:
                    document, _ = store.store_stream(path.name, canonical_media_type(path.suffix), stream)
                aliases[document.document_id] = item["id"]
                recognized = re.search(r"OCR returned text: (\d+)", document.message)
                results[item["id"]] = {"text": "\n".join(unit.text for unit in document.parsed_units()),
                                       "state": document.state, "pages": document.page_count,
                                       "ocr_pages": int(recognized.group(1)) if recognized else 0,
                                       "processing_message": document.message,
                                       "mode": "ocr" if item.get("ocr") else "native-pdf"}
            for item in manifest.get("audio", []):
                segments = transcripts[item["id"]]
                path = _input_path(root, item["path"])
                if full:
                    with path.open("rb") as stream:
                        document, _ = store.store_stream(path.name, canonical_media_type(path.suffix), stream)
                else:
                    document = _prepared_media(store, path)
                document = _import_transcript(bench, matter, document, segments, principal.principal_id)
                aliases[document.document_id] = item["id"]
                results[item["id"]] = {"text": "\n".join(unit.text for unit in document.parsed_units()),
                                       "state": document.state,
                                       "mode": "observed-asr" if full else "prepared-transcript-fixture"}
            bench._sync_source_catalog(matter, tuple(store.documents.values()))
            for item in manifest.get("search", []):
                citations = [_citation(bench, matter, row, aliases)
                             for row in bench.search(matter, item["query"], limit=item.get("top_k", 5))]
                results[item["id"]] = {"document_ids": list(dict.fromkeys(row["document_id"] for row in citations)),
                                       "citations": citations}
            for item in manifest.get("answers", []):
                question = item["question"]
                route = classify(question).kind
                conversation = bench.workspace.create_conversation(matter.matter_id, item["id"])
                answer = bench.ask(matter, conversation, question)
                citations = [_citation(bench, matter, citation, aliases)
                             for claim in answer.payload["claims"] for citation in claim["citations"]]
                results[item["id"]] = {"text": " ".join(claim["text"] for claim in answer.payload["claims"]),
                                       "answerable": answer.payload["answerable"], "question": question,
                                       "citations": citations, "provider": "CorpusExtractiveGenerator",
                                       "route": route}
            return results
        finally:
            bench.close()
