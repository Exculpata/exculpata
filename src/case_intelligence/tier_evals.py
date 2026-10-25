"""Local, offline per-tier measurements over the defense-workflow suite."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import defense_eval as evaluation
from . import generation

JSON_CARD = "tier-scorecard.json"
MARKDOWN_CARD = "tier-scorecard.md"
RETRIEVAL_CARD = "retrieval.json"
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
                    tier=tier, profile_ids=ids,
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
    cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")), "CPU")
    result = dict(cpu=cpu, logical_cpus=os.cpu_count(), gpus=[])
    try:
        import torch
        result["gpus"] = [dict(name=torch.cuda.get_device_properties(i).name,
                               vram_mib=torch.cuda.get_device_properties(i).total_memory // 1024**2)
                          for i in range(torch.cuda.device_count())]
        result["gpu_probe"] = "torch visible CUDA devices"
    except (ImportError, RuntimeError):
        result["gpu_probe"] = "CUDA inventory unavailable; no GPU measurement permitted"
    return result


class _Embedding:
    available = True

    def __init__(self, model):
        self.model = model

    def embed(self, texts):
        return tuple(tuple(float(v) for v in row) for row in
                     self.model.encode(list(texts), normalize_embeddings=True))


class _Reranker:
    available = True

    def __init__(self, model):
        self.model = model

    def rerank(self, query, candidates):
        scores = self.model.predict([(query, item.text) for item in candidates])
        ranked = [replace(item, score=float(score)) for item, score in zip(candidates, scores)]
        return tuple(sorted(ranked, key=lambda item: (-item.score, item.page_number, item.chunk_id)))


def _worker_models(row):
    from . import retrieval_worker as worker
    from sentence_transformers import SentenceTransformer, CrossEncoder
    pins = row["models"]
    if (worker.EMBEDDING_MODEL, worker.EMBEDDING_REVISION, worker.RERANKER_MODEL, worker.RERANKER_REVISION) != (
        pins["embedding"]["model_id"], pins["embedding"]["revision"],
        pins["reranker"]["model_id"], pins["reranker"]["revision"],
    ):
        raise ValueError("worker model overrides do not match the tier pins")
    device = "cpu" if row["tier"] == "potato" else "cuda:0"
    previous = os.environ.get("CASE_REVIEW_MODEL_DEVICE")
    try:
        worker._embedding = worker._reranker = None
        os.environ["CASE_REVIEW_MODEL_DEVICE"] = device
        embedder = worker._embedding_model()
        os.environ["CASE_REVIEW_MODEL_DEVICE"] = "cuda:1" if row["tier"] == "ultra" else device
        reranker = worker._reranker_model()
    finally:
        if previous is None:
            os.environ.pop("CASE_REVIEW_MODEL_DEVICE", None)
        else:
            os.environ["CASE_REVIEW_MODEL_DEVICE"] = previous
    if type(embedder) is not SentenceTransformer or type(reranker) is not CrossEncoder:
        raise ValueError("stand-in worker models rejected")
    devices = list(dict.fromkeys([str(embedder.device), str(reranker.model.device)]))
    return _Embedding(embedder), _Reranker(reranker), devices


class _Generator:
    available = True

    def __init__(self, row):
        from vllm import LLM, SamplingParams
        pin = row["models"]["generator"]
        parallel = 2 if row["tier"] == "ultra" else 1
        self.engine = LLM(model=pin["model_id"], revision=pin["revision"],
                          tokenizer_revision=pin["revision"], trust_remote_code=False,
                          tensor_parallel_size=parallel, dtype="bfloat16", max_model_len=8192)
        config = self.engine.llm_engine.vllm_config
        device = str(config.device_config.device)
        expected = "cpu" if row["tier"] == "potato" else "cuda"
        if type(self.engine) is not LLM or device.split(":")[0] != expected or config.parallel_config.tensor_parallel_size != parallel:
            raise ValueError("generator device does not match tier")
        self.params = SamplingParams(temperature=0, max_tokens=4096)

    def generate(self, *, question, evidence, history=(), working_context="", grounding_repair=False):
        system, user = generation._prompt(question, evidence, history, working_context,
                                          grounding_repair=grounding_repair)
        system += "\nJSON schema: " + json.dumps(generation.ANSWER_SCHEMA)
        output = self.engine.chat([{"role": "system", "content": system},
                                   {"role": "user", "content": user}], self.params, use_tqdm=False)
        return json.loads(output[0].outputs[0].text)


def _metric(totals, correct, total):
    return dict(correct=totals[correct], total=totals[total], rate=round(totals[correct] / totals[total], 4))


def evaluate_tier(row, hardware, directory, with_generator):
    if not hardware_matches(row["tier"], hardware):
        row["reason"] = "no matching GPU hardware available in this run"
        return
    started = time.perf_counter()
    phase = "retrieval worker offline startup"
    try:
        embedder, reranker, devices = _worker_models(row)
        row["execution"] = dict(embedding=type(embedder.model).__name__, reranker=type(reranker.model).__name__,
                                devices=devices, generator=None)
        phase = "PostgreSQL retrieval (requires CASE_REVIEW_POSTGRES_TEST_DSN)"
        suite = evaluation.load_suite()
        adapters = (embedder, reranker, dict(minimum_dense_score=evaluation._LEARNED_DENSE_FLOOR,
                                            minimum_rerank_score=evaluation._LEARNED_RERANK_FLOOR))
        with evaluation.PostgresHybridRetriever(suite, adapters=adapters) as retriever:
            card = evaluation.run_eval(suite, evaluation.ReferenceStub(), retriever=retriever,
                                       retriever_record=retriever.record, model={"mode": "retrieval-only-reference"})
            totals = card["aggregates"]
            row.update(status="partial", retrieval=_metric(totals, "retrieval_hit", "retrieval_applicable"),
                       reason="generator not requested; answer accuracy unmeasured")
            # Retain only retrieval accounting from the reference answerer.
            folder = directory / row["tier"]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / RETRIEVAL_CARD).write_text(json.dumps([
                {key: q[key] for key in ("question_id", "retrieved_passage_ids", "retrieval_hit")}
                for q in card["questions"]], indent=2) + "\n")
            if with_generator:
                phase = "offline pinned generator startup or answering"
                client = _Generator(row)
                answerer = evaluation.ConfiguredAnswerer(generation.GroundedGenerationService(client))
                card = evaluation.run_eval(suite, answerer, retriever=retriever,
                                           retriever_record=retriever.record, model=row["models"]["generator"])
                evaluation.write_scorecard(folder, card)
                if card["aggregates"]["errors"]:
                    raise ValueError("answer execution errors; inspect retained harness card")
                row.update(status="measured", answer_accuracy=_metric(card["aggregates"], "answer_correct", "questions"), reason="")
                row["execution"].update(generator="vllm", dtype="bfloat16", tensor_parallel_size=2 if row["tier"] == "ultra" else 1)
    except Exception as exc:
        row["reason"] = f"{phase} failed: {type(exc).__name__}; answer accuracy unmeasured"
    finally:
        row["wall_seconds"] = round(time.perf_counter() - started, 6)


def write_scorecard(directory, card):
    validate_scorecard(card)
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["# Per-tier synthetic evaluation", "", f"Commit: `{card['commit']}`",
             f"Source SHA-256: `{card['source_sha256']}`", f"Command: `{card['command']}`",
             f"Started: {card['started_at']}", f"Suite: {card['suite_id']} v{card['suite_version']}; fingerprint `{card['fingerprint']}`; k={card['top_k']}",
             f"Hardware inventory: `{json.dumps(card['hardware'], sort_keys=True)}`", "",
             "| Tier | Status | Retrieval recall | Answer accuracy | Wall seconds | Reason |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in card["tiers"]:
        cells = ["unmeasured" if row[key] is None else f"{row[key]['correct']}/{row[key]['total']} ({row[key]['rate']})"
                 for key in ("retrieval", "answer_accuracy")]
        lines.append(f"| {row['tier']} | {row['status']} | {' | '.join(cells)} | {row['wall_seconds']} | {row['reason']} |")
    for row in card["tiers"]:
        lines.extend(["", f"## {row['tier']}", "", f"Proposed settings: `{json.dumps(row['settings'], sort_keys=True)}`",
                      f"Pinned models (proposed until executed): `{json.dumps(row['models'], sort_keys=True)}`",
                      f"Executed models and devices: `{json.dumps(row['execution'], sort_keys=True)}`"])
    (directory / JSON_CARD).write_text(json.dumps(card, indent=2, allow_nan=False) + "\n")
    (directory / MARKDOWN_CARD).write_text("\n".join(lines) + "\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--with-generator", action="store_true")
    args = parser.parse_args(argv)
    if any((args.output_dir / name).exists() for name in (JSON_CARD, MARKDOWN_CARD)):
        parser.error("scorecard exists; choose a new output directory")
    os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=evaluation.ROOT, text=True).strip()
    card = new_scorecard(commit, shlex.join([".venv/bin/python", "-m", "case_intelligence.tier_evals", *argv]), hardware_inventory())
    for row in card["tiers"]:
        evaluate_tier(row, card["hardware"], args.output_dir, args.with_generator)
        print(f"{row['tier']}: {row['status']}; {row['reason']}; {row['wall_seconds']}s")
    write_scorecard(args.output_dir, card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
