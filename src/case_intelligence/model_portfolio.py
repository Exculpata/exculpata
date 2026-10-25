"""Operator-facing projection of Exculpata's configured local AI portfolio."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from typing import Mapping
from urllib.parse import urlparse

from .generation import (
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_ITEMS,
    GroundedGenerationService,
)
from .review_bench_v2 import DEFAULT_EMBEDDING_MODEL, DEFAULT_RERANKER_MODEL


PORTFOLIO_SCHEMA_VERSION = 1
PORTFOLIO_ID = f"{LEGACY_STEM}-local-ai-v1"
APPROVED_GENERATOR_MODEL = "exculpata-qwen35-4b"
APPROVED_GENERATOR_REPOSITORY = "Qwen/Qwen3.5-4B"
APPROVED_GENERATOR_REVISION = "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
_PORTABLE_PROFILE = {
    "profile": "portable",
    "repository": APPROVED_GENERATOR_REPOSITORY,
    "revision": APPROVED_GENERATOR_REVISION,
}
APPROVED_GENERATOR_PROFILES = {
    APPROVED_GENERATOR_MODEL: _PORTABLE_PROFILE,
    # Nodes installed before the portable id changed still advertise this name.
    f"{LEGACY_STEM}-qwen35-4b": _PORTABLE_PROFILE,
    f"{LEGACY_STEM}-qwen35-9b": {
        "profile": "quality",
        "repository": "Qwen/Qwen3.5-9B",
        "revision": "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
    },
}
APPROVED_EMBEDDING_REVISION = "47ea694b257b703fee9253d75c2b1f2985180498"
APPROVED_RERANKER_REVISION = "f7481e6055501a30fb19d090657df9ec1f79ab2c"
DISCOVERY_GOLD_FINGERPRINT = (
    "6c098ca86a6b268b1b8894f5db42df2b22f1cdc083766eca3b283fd367ed4796"
)


def _available(service: GroundedGenerationService) -> bool:
    try:
        return bool(service.available)
    except Exception:
        return False


def _loopback_endpoint(value: object) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def model_portfolio_projection(
    generator: GroundedGenerationService,
    *,
    learned_retrieval: bool,
    answer_workers: int,
    research_workers: int,
    review_workers: int,
    review_source_concurrency: int,
    media_ready: bool,
) -> Mapping[str, object]:
    """Describe active services without implying unverified quality claims."""

    client = getattr(generator, "client", None)
    model_id = str(getattr(client, "model", "") or "")
    endpoint = getattr(client, "endpoint", "")
    direct_mode = bool(getattr(client, "disable_thinking", False))
    schema_mode = str(getattr(client, "schema_mode", "unprobed") or "unprobed")
    schema_enforcement = {
        "verified": "verified",
        "validate_and_retry": "not enforced / validated locally",
        "refused": "refused",
        "unreachable": "not checked",
    }.get(schema_mode, "not checked")
    try:
        from .model_qualification import qualification_label

        model_qualification = str(getattr(client, "model_notice", "") or "") or qualification_label(model_id)
    except (OSError, ValueError):
        model_qualification = "unvalidated model" if model_id else ""
    generator_ready = _available(generator)
    generator_profile = APPROVED_GENERATOR_PROFILES.get(model_id)
    approved_generator = bool(
        generator_ready
        and generator_profile is not None
        and direct_mode
        and _loopback_endpoint(endpoint)
    )
    retrieval_ready = bool(learned_retrieval)
    ready = bool(approved_generator and retrieval_ready)
    generation_concurrency = min(
        max(int(getattr(generator, "maximum_active", 1)), 1), 4
    )

    lanes = (
        {
            "name": "Find & order",
            "engine": "Evidence search",
            "detail": "Finds matching passages and orders the strongest support before an answer is drafted.",
            "capacity": "Bounded source packet",
            "tone": "ready" if retrieval_ready else "attention",
        },
        {
            "name": "Focused answer",
            "engine": "Focused cited response",
            "detail": "Drafts from the selected passages and checks every displayed claim against its citations.",
            "capacity": "Durable answer queue",
            "tone": "ready" if approved_generator else "attention",
        },
        {
            "name": "Broader investigation",
            "engine": "Broader saved investigation",
            "detail": "Runs several focused searches, saves progress, and synthesizes only retained support.",
            "capacity": "Durable investigation queue",
            "tone": "ready" if approved_generator and retrieval_ready else "attention",
        },
        {
            "name": "Every-source check",
            "engine": "Source-by-source check",
            "detail": "Checks every eligible source independently; one source problem does not stop the saved run.",
            "capacity": "Saved progress and per-source decisions",
            "tone": "ready" if approved_generator else "attention",
        },
    )

    return {
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "portfolio_id": PORTFOLIO_ID,
        "ready": ready,
        "headline": (
            "Configured local AI services ready"
            if ready
            else "Local AI portfolio needs attention"
        ),
        "generator": {
            "ready": approved_generator,
            "configured_model": model_id or "Unavailable",
            "profile": (
                str(generator_profile["profile"])
                if generator_profile is not None
                else "unreviewed"
            ),
            "repository": (
                str(generator_profile["repository"])
                if generator_profile is not None
                else "Unreviewed"
            ),
            "revision": (
                str(generator_profile["revision"])
                if generator_profile is not None
                else "Unreviewed"
            ),
            "runtime": "vLLM 0.22.0",
            "mode": "Direct structured generation",
            "schema_enforcement": schema_enforcement,
            "schema_detail": str(getattr(client, "schema_detail", "") or ""),
            "model_qualification": model_qualification,
            "context_tokens": 16_384,
            "maximum_active": generation_concurrency,
        },
        "retrieval": {
            "ready": retrieval_ready,
            "embedding": DEFAULT_EMBEDDING_MODEL,
            "embedding_revision": APPROVED_EMBEDDING_REVISION,
            "reranker": DEFAULT_RERANKER_MODEL,
            "reranker_revision": APPROVED_RERANKER_REVISION,
        },
        "media_ready": bool(media_ready),
        "evidence": {
            "maximum_items": MAX_EVIDENCE_ITEMS,
            "maximum_characters": MAX_EVIDENCE_CHARS,
        },
        "lanes": lanes,
        "evaluation": {
            "suite_fingerprint": DISCOVERY_GOLD_FINGERPRINT,
            "status": "required",
            "quality_metrics_published": False,
            "detail": (
                "The bundled suite is synthetic scaffolding, not evidence of "
                "retrieval, answer, classification, or throughput quality on "
                "this deployment."
            ),
        },
        "candidate_note": (
            "Local review components and capacity are deployment-specific. "
            "Publish quality or performance claims only from a versioned "
            "evaluation artifact produced on the supported configuration."
        ),
    }
