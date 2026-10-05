"""Score drafted questions on the fixed synthetic set (assistant actions brief, increment C).

Pass bar, recorded by the maintainer: at least 90% of the questions shown to a
reviewer cite only relevant passages, and none quotes text that is not in its cited
passages. So that dropping questions cannot pass on its own, every case must
also show at least MIN_QUESTIONS_PER_CASE questions.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .generation import (
    EvidenceItem, GenerationRejected, GenerationUnavailable, OllamaGenerator, _bounded_json_get,
)
from .claim_evaluation import validate_runtime
from .question_drafting import PURPOSES, draft_questions, unsupported_quotes

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "benchmarks" / "question-drafts-v1.json"
MODEL_MANIFEST = ROOT / "config" / "models.json"
# The code a receipt is bound to: the drafting check and prompt, the model adapters,
# and this scorer and its command.
IMPLEMENTATION_PATHS = (
    "src/case_intelligence/question_drafting.py",
    "src/case_intelligence/question_draft_evaluation.py",
    "src/case_intelligence/generation.py",
    "scripts/evaluate-question-drafts.py",
)
# The set is fixed: changing it needs a new file and a new recorded fingerprint.
QUESTION_SET_FINGERPRINT = "7674d1da108db0dffe5392a7ca841c483e09e9ec220838a1afac5b5f99a5ea95"
RELEVANT_RATE_BAR = 0.90
MIN_QUESTIONS_PER_CASE = 3


def json_safe(value):
    """A copy of value that strict JSON can hold: non-finite numbers become strings.

    A runtime that ignores the schema can return NaN or Infinity in raw output, which
    Python parses but strict JSON cannot represent; the receipt keeps them as text.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def receipt_text(result: dict) -> str:
    """The receipt as strict JSON, ASCII-escaped so any raw model text can be written."""
    return json.dumps(json_safe(result), indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def write_new_receipt(path: Path, result: dict) -> str:
    """Publish a receipt at a new path atomically, never replacing an existing file.

    The receipt is written to a private temporary file in the same directory and
    hard-linked into place, so a concurrently created destination is never
    overwritten and an interrupted write never leaves a partial receipt.
    """
    text = receipt_text(result)
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".question-draft-receipt-",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return text


def load_cases(path: Path = DEFAULT_CASES) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if data.get("format") != "recordbench-question-draft-set-v1" or data.get("synthetic_only") is not True:
        raise ValueError("Not a synthetic question-draft set.")
    for case in data["cases"]:
        if case["purpose"] not in PURPOSES or not set(case["relevant"]) <= {p["id"] for p in case["passages"]}:
            raise ValueError(f"Invalid case {case.get('id')!r}.")
    return data, hashlib.sha256(raw).hexdigest()


class _Recording:
    """Wraps the configured client to keep each raw reply for the receipt."""

    def __init__(self, client):
        self.client = client
        self.raw = None

    @property
    def available(self):
        return self.client.available

    def draft_questions(self, **kwargs):
        self.raw = self.client.draft_questions(**kwargs)
        return self.raw


def evaluate(service, data: dict) -> dict:
    """Run every case through the production drafting path and score what a reviewer would see."""
    recording = _Recording(service.client)
    original = service.client
    service.client = recording
    cases, shown_total, relevant_total, shown_bad_quotes, raw_bad_quotes, thin = [], 0, 0, 0, 0, []
    try:
        for case in data["cases"]:
            evidence = tuple(EvidenceItem(p["id"], p["source_name"], p["location"], p["excerpt"],
                                          "transcript" if p["source_name"].endswith(".wav") else "document")
                             for p in case["passages"])
            by_id = {item.evidence_id: item for item in evidence}
            recording.raw = None
            result = dict(id=case["id"], purpose=case["purpose"])
            try:
                draft = draft_questions(service, case["purpose"], case["topic"], evidence)
                shown = draft.questions
                result.update(omitted=draft.omitted, elapsed_ms=draft.elapsed_ms)
            except GenerationRejected as exc:
                shown = ()
                result.update(rejected=str(exc))
            raw_questions = recording.raw.get("questions") if isinstance(recording.raw, dict) else None
            for value in raw_questions if isinstance(raw_questions, list) else ():
                if isinstance(value, dict) and isinstance(value.get("text"), str):
                    ids = value.get("evidence_ids")
                    # A malformed reply (not a list) is scored, never allowed to stop the run.
                    ids = ids if isinstance(ids, (list, tuple)) else ()
                    cited = [by_id[i] for i in ids if isinstance(i, str) and i in by_id]
                    raw_bad_quotes += bool(unsupported_quotes(value["text"], cited or evidence[:0]))
            questions = []
            for question in shown:
                # Relevant only if every cited passage is one the case allows: citing a
                # distractor alongside a relevant passage does not count.
                relevant = set(question.evidence_ids) <= set(case["relevant"])
                bad = unsupported_quotes(question.text, [by_id[i] for i in question.evidence_ids])
                relevant_total += relevant
                shown_bad_quotes += bool(bad)
                questions.append(dict(text=question.text, evidence_ids=list(question.evidence_ids),
                                      relevant=relevant, unsupported_quotes=list(bad)))
            shown_total += len(questions)
            if len(questions) < MIN_QUESTIONS_PER_CASE:
                thin.append(case["id"])
            result.update(questions=questions, raw=recording.raw)
            cases.append(result)
    finally:
        service.client = original
    rate = relevant_total / shown_total if shown_total else 0.0
    return dict(
        shown_questions=shown_total, relevant_questions=relevant_total, relevant_rate=round(rate, 4),
        shown_unsupported_quotes=shown_bad_quotes, raw_unsupported_quotes=raw_bad_quotes,
        cases_below_minimum=thin,
        passed=rate >= RELEVANT_RATE_BAR and shown_bad_quotes == 0 and not thin,
        cases=cases,
    )


def execution_metadata() -> dict:
    """The code that ran: commit, uncommitted changes and implementation hashes.

    Hostname, user, endpoint and paths are deliberately not recorded.
    """
    record = dict(git_commit=None, working_tree_dirty=None,
                  implementation_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                         for name in IMPLEMENTATION_PATHS})
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                         stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT,
                                             stderr=subprocess.DEVNULL))
    except (OSError, subprocess.CalledProcessError):
        return record
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        record.update(git_commit=commit, working_tree_dirty=dirty)
    return record


def model_identity(client) -> dict:
    """Observe the runtime's immutable model artifact, when it exposes one.

    Ollama reports a content digest for each installed model; a model name alone is
    mutable, so other runtimes record no artifact and cannot pass the gate.
    """
    snapshot = dict(method="none", model=getattr(client, "model", None), artifact_sha256=None)
    if isinstance(client, OllamaGenerator):
        snapshot["method"] = "ollama_tag_digest"
        opener = getattr(client, "_opener", None)
        payload = _bounded_json_get(f"{client.endpoint}/api/tags", timeout=5.0,
                                    **({"opener": opener} if opener is not None else {}))
        models = payload.get("models")
        matches = [item for item in models if isinstance(item, dict) and item.get("name") == client.model] \
            if isinstance(models, list) else []
        value = matches[0].get("digest") if len(matches) == 1 else None
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value.removeprefix("sha256:")):
            snapshot["artifact_sha256"] = value.removeprefix("sha256:")
    return snapshot


def model_pin(profile: str, runtime: dict) -> dict:
    """Link a declared runtime artifact to the repository-pinned generator revision.

    The runtime profile is the operator's declaration (as for the claim evaluation):
    the artifact digest it names must be the one observed, and its upstream model,
    revision and license must equal the selected profile in config/models.json.
    """
    validate_runtime(runtime)
    raw = MODEL_MANIFEST.read_bytes()
    entries = [item for item in json.loads(raw)["models"]
               if item.get("role") == "generator" and item.get("profile") == profile]
    if len(entries) != 1:
        raise ValueError("Choose a documented generator profile from config/models.json.")
    pinned = entries[0]
    if any(runtime[key] != pinned[field] for key, field in (
            ("upstream_model_id", "model_id"), ("upstream_revision", "revision"), ("license", "license"))):
        raise ValueError("Declared artifact provenance must match the selected pinned model profile.")
    return dict(profile=profile, model_id=pinned["model_id"], revision=pinned["revision"],
                license=pinned["license"], manifest_sha256=hashlib.sha256(raw).hexdigest(),
                artifact_sha256=runtime["model_artifact_sha256"],
                basis="operator_declared_not_independently_verified")


def binding_problems(execution: dict, identity: dict, pin: dict | None = None) -> list[str]:
    """Why a receipt cannot stand for a committed revision and one pinned model artifact."""
    problems = []
    if not execution.get("git_commit"):
        problems.append("No Git commit was recorded.")
    elif execution.get("working_tree_dirty") is not False:
        problems.append("The checkout had uncommitted changes.")
    before, after = identity.get("before"), identity.get("after")
    if not (isinstance(before, dict) and before.get("artifact_sha256")):
        problems.append("The runtime exposed no immutable model artifact digest.")
    elif before != after:
        problems.append("The model artifact changed or could not be observed after the run.")
    if pin is None:
        problems.append("No runtime profile linked the model artifact to a repository-pinned revision.")
    elif isinstance(before, dict) and before.get("artifact_sha256") \
            and before["artifact_sha256"] != pin["artifact_sha256"]:
        problems.append("The observed model artifact is not the one the runtime profile declares.")
    return problems


def receipt(service, path: Path = DEFAULT_CASES, *, identity=model_identity,
            profile: str | None = None, runtime: dict | None = None) -> dict:
    """Run the set and record a receipt bound to the code and the pinned model.

    The gate passes only when the pass bar is met on the pinned set, from a clean
    checkout at a recorded commit, with the same immutable model artifact observed
    before and after the run, and that artifact declared by a runtime profile whose
    upstream model, revision and license match the selected pinned profile. An
    invalid or conflicting runtime profile is refused before any model call.
    """
    if (profile is None) != (runtime is None):
        raise ValueError("Give both a generator profile and a runtime profile, or neither.")
    pin = model_pin(profile, runtime) if runtime is not None else None
    data, fingerprint = load_cases(path)
    client = service.client
    result = dict(format="recordbench-question-draft-evaluation-v1", synthetic_only=True,
                  set_fingerprint=fingerprint, pinned_set=fingerprint == QUESTION_SET_FINGERPRINT,
                  case_count=len(data["cases"]),
                  pass_bar=dict(relevant_rate=RELEVANT_RATE_BAR, unsupported_quotes=0,
                                minimum_questions_per_case=MIN_QUESTIONS_PER_CASE),
                  adapter=type(client).__name__, model=getattr(client, "model", None),
                  execution=execution_metadata(), model_pin=pin, runtime=runtime,
                  model_gate="outstanding")
    try:
        available = service.available
    except Exception:
        available = False
    if not available:
        result["limitation"] = ("Configured runtime unavailable. No model evaluation was executed; "
                                "deterministic tests are separate.")
        return result
    def observe():
        try:
            return identity(client)
        except Exception:
            return None
    observed = dict(before=observe())
    try:
        result.update(evaluate(service, data))
    except GenerationUnavailable as exc:
        result["limitation"] = f"The configured runtime failed during evaluation: {exc}"
        return result
    observed["after"] = observe()
    result["model_identity"] = observed
    # Only the pinned set can pass the gate recorded in a pull request, and only a
    # run bound to a committed revision and one model artifact.
    problems = binding_problems(result["execution"], observed, pin)
    result["binding_problems"] = problems
    if not (result["passed"] and result["pinned_set"]):
        result["model_gate"] = "failed"
    else:
        result["model_gate"] = "unbound" if problems else "passed"
    return result
