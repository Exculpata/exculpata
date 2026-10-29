"""Schema and validation for synthetic tier scorecards, without model execution."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import defense_eval as evaluation

TIERS = {"potato": ["potato", "cpu"], "consumer": ["gpu-16", "gpu-24"], "ultra": ["ultra"]}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Metric(Record):
    correct: int = Field(ge=0)
    total: int = Field(gt=0)
    rate: float = Field(ge=0, le=1)


class Pin(Record):
    model_id: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    license: str = Field(min_length=1)


class Tier(Record):
    tier: Literal["potato", "consumer", "ultra"]
    profile_ids: list[str]
    settings: dict
    models: dict[str, Pin]
    status: Literal["unmeasured", "partial", "measured"]
    retrieval: Metric | None
    answer_accuracy: Metric | None
    execution: dict | None
    reason: str
    wall_seconds: float = Field(ge=0)


class Scorecard(Record):
    schema_version: Literal[1]
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    command: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hardware: dict
    started_at: str = Field(min_length=1)
    suite_id: str
    suite_version: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    top_k: Literal[8]
    tiers: list[Tier]


def hardware_matches(tier, hardware):
    gpus = hardware.get("gpus", [])
    if tier == "potato":
        return True
    if tier == "consumer":
        return (len(gpus) == 1 and "GeForce" in gpus[0]["name"]
                and 15 * 1024 <= gpus[0]["vram_mib"] <= 25 * 1024)
    return tier == "ultra" and len(gpus) == 2 and all("L40S" in gpu["name"] for gpu in gpus)


def _models(tier):
    manifest = json.loads((evaluation.ROOT / "config/models.json").read_text())
    profile = "portable" if tier == "potato" else "quality"
    return {role: {key: item[key] for key in ("model_id", "revision", "license")}
            for role in ("embedding", "reranker", "generator")
            for item in manifest["models"] if item["role"] == role
            and (role != "generator" or item["profile"] == profile)}


def new_scorecard(commit, command, hardware):
    suite = evaluation.load_suite()
    profiles = json.loads((evaluation.ROOT / "config/model_profiles.json").read_text())["profiles"]
    settings = {p["id"]: {key: p[key] for key in ("generator", "embed_rerank")} for p in profiles}
    source = b"".join((evaluation.ROOT / path).read_bytes() for path in (
        "src/case_intelligence/tier_evals.py", "src/case_intelligence/defense_eval.py",
        "src/case_intelligence/retrieval_worker.py", "src/case_intelligence/generation.py",
        "config/models.json", "config/model_profiles.json"))
    return dict(schema_version=1, commit=commit, command=command, hardware=hardware,
                source_sha256=hashlib.sha256(source).hexdigest(),
                started_at=datetime.now(timezone.utc).isoformat(), suite_id=suite.suite_id,
                suite_version=suite.suite_version, fingerprint=suite.fingerprint,
                top_k=evaluation.DEFAULT_TOP_K, tiers=[dict(
                    tier=tier, profile_ids=list(ids),
                    settings={id_: settings.get(id_, {"profile": "unavailable in this catalog"}) for id_ in ids},
                    models=_models(tier), status="unmeasured", retrieval=None, answer_accuracy=None,
                    execution=None, reason="not executed in this run", wall_seconds=0.0,
                ) for tier, ids in TIERS.items()])


def validate_scorecard(card):
    parsed = Scorecard.model_validate(card)
    if not parsed.hardware.get("cpu") or not isinstance(parsed.hardware.get("gpus"), list):
        raise ValueError("hardware inventory is required")
    datetime.fromisoformat(parsed.started_at)
    if [row.tier for row in parsed.tiers] != list(TIERS):
        raise ValueError("one row per tier is required")
    suite = evaluation.load_suite()
    if (parsed.suite_id, parsed.suite_version, parsed.fingerprint) != (suite.suite_id, suite.suite_version, suite.fingerprint):
        raise ValueError("scorecard must identify the executed versioned suite")
    for row in parsed.tiers:
        if row.profile_ids != TIERS[row.tier] or not row.settings or row.models != {
            role: Pin(**pin) for role, pin in _models(row.tier).items()
        }:
            raise ValueError("tier settings and pinned model provenance are required")
        for metric in (row.retrieval, row.answer_accuracy):
            if metric and (metric.correct > metric.total or
                           metric.rate != round(metric.correct / metric.total, 4)):
                raise ValueError("metric counts and rate disagree")
        if row.status == "unmeasured":
            if row.retrieval is not None or row.answer_accuracy is not None or not row.reason:
                raise ValueError("unmeasured tiers require a reason and reject scores")
            continue
        if row.retrieval is None or not hardware_matches(row.tier, parsed.hardware):
            raise ValueError("measurements require matching hardware and completed retrieval")
        if row.retrieval.total != sum(bool(q.required_facts) for q in suite.questions) or (
            row.answer_accuracy is not None and row.answer_accuracy.total != len(suite.questions)
        ):
            raise ValueError("measurements must cover the complete question set")
        execution = row.execution or {}
        devices = ["cpu"] if row.tier == "potato" else (["cuda:0", "cuda:1"] if row.tier == "ultra" else ["cuda:0"])
        if (execution.get("embedding") != "SentenceTransformer" or
                execution.get("reranker") != "CrossEncoder" or execution.get("devices") != devices):
            raise ValueError("stand-in models or mismatched execution devices cannot be measurements")
        if row.status == "partial":
            if row.answer_accuracy is not None or execution.get("generator") is not None or not row.reason:
                raise ValueError("partial means retrieval only, with a reason")
        elif row.answer_accuracy is None or execution.get("generator") != "vllm":
            raise ValueError("measured requires real generation and answer scores")


def hardware_inventory():
    return {}


def _worker_models(row):
    return None


def evaluate_tier(row, hardware, directory, with_generator):
    try:
        _worker_models(row)
    except Exception:
        return


def write_scorecard(directory, card):
    directory.mkdir(parents=True, exist_ok=True)


def main(argv=None):
    return 0
