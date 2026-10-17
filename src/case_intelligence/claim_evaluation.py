"""Frozen synthetic R1b probes and separately graded, single-pass model captures.

These are evaluation artifacts, never application state or release acceptance.
Use production verification code; do not use its verdict as semantic ground truth.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import time

from jsonschema import Draft202012Validator

from .generation import (
    ANSWER_SCHEMA, EvidenceItem, GenerationRejected, GenerationUnavailable, GenerationSchemaRejected,
    GroundedGenerationService, OllamaGenerator, OpenAICompatibleGenerator,
    _exact_original_span, _prompt, verify_original_claim,
)
from .review_budget import DEFAULT_REVIEW_BUDGET

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "benchmarks/r1b-claims-v2.json"
# Changing v2 requires a new version, not an updated expected fingerprint.
SUITE_SHA256 = "dd64595bb51c83c395616988ca51f77701dee6d59136da1a2912c23afe42691a"
IDENTITY_BOUNDARIES = "before_and_after_every_request_including_failures_and_before_receipt"
IDENTITY_LIMITATION = (
    "Snapshots cannot rule out change and reversion between checks. API model IDs do not attest "
    "loaded weights; artifact and upstream revision linkage remains operator-declared. "
    "Identity observations do not establish inference execution."
)
IMPLEMENTATION_PATHS = ("src/case_intelligence/generation.py",
                        "src/case_intelligence/claim_evaluation.py", "scripts/evaluate-claims.py")


def adapter_name(client):
    if isinstance(client, OllamaGenerator):
        return "OllamaGenerator"
    if isinstance(client, OpenAICompatibleGenerator):
        return "OpenAICompatibleGenerator"
    raise ValueError("Capture requires a supported production adapter.")


def capture_configuration(adapter, model, repetitions, disable_thinking):
    if adapter not in ("OllamaGenerator", "OpenAICompatibleGenerator"):
        raise ValueError("Capture configuration requires a supported adapter.")
    if not isinstance(model, str) or not model or model != model.strip() or len(model) > 200:
        raise ValueError("Capture configuration requires a valid adapter model name.")
    if type(repetitions) is not int or not 1 <= repetitions <= 20:
        raise ValueError("Capture configuration repetitions invalid.")
    if ((adapter == "OllamaGenerator" and disable_thinking is not None)
        or (adapter == "OpenAICompatibleGenerator" and type(disable_thinking) is not bool)):
        raise ValueError("Capture configuration thinking option does not match the adapter.")
    return {"repetitions": repetitions, "seed": None,
            "seed_status": "production_adapters_do_not_set_seed",
            "history": [], "working_context": "", "grounding_repair": False,
            "adapter": adapter, "model": model, "disable_thinking": disable_thinking,
            "temperature": 0.1, "output_tokens": DEFAULT_REVIEW_BUDGET.output_tokens,
            "context_tokens": 8192 if adapter == "OllamaGenerator" else "server_configured",
            "response_schema": copy.deepcopy(ANSWER_SCHEMA)}


def receipt_protocol(mode):
    if mode not in ("model_capture", "injected_verifier_probes"):
        raise ValueError("Unsupported receipt protocol mode.")
    return {"schema_version": 1, "suite_id": load_suite()["suite_id"], "suite_sha256": SUITE_SHA256,
            "mode": mode, "synthetic": True,
            "release_acceptance": "pending_no_release_threshold_defined",
            "supported_hardware_acceptance": "not_established",
            "retrieval": "fixed_evidence_no_embedding_or_reranker_calls",
            "scope": "single_pass_claim_checks_no_repair_retrieval_storage_or_browser_acceptance"}


def validate_identity_snapshot(snapshot, model, artifact_digest, adapter):
    if not isinstance(snapshot, dict) or set(snapshot) != {"method", "model", "artifact_sha256"}:
        raise ValueError("Identity checker must return a complete matched snapshot, not a no-op.")
    expected_method = {"OllamaGenerator": "ollama_tag_digest_snapshots",
                       "OpenAICompatibleGenerator": "api_model_id_snapshots"}.get(adapter)
    if expected_method is None or snapshot["method"] != expected_method:
        raise ValueError("Runtime identity observation method must match the active adapter.")
    expected_artifact = artifact_digest if adapter == "OllamaGenerator" else None
    if snapshot["model"] != model or snapshot["artifact_sha256"] != expected_artifact:
        raise ValueError("Runtime identity snapshot does not match capture model/artifact.")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def json_equal(left, right):
    """Compare recorded JSON without accepting bool/int/float substitutions."""
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def load_suite():
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    if digest(suite) != SUITE_SHA256:
        raise ValueError("Frozen challenge set or rubric changed; use a new suite version.")
    return suite


def seal(receipt):
    return {**receipt, "receipt_sha256": digest(receipt)}


def validate_capture_row(row):
    common = {"sample_id", "case_id", "repetition", "prompt", "elapsed_ms",
              "state", "parsed_response_received"}
    if (not isinstance(row, dict) or not isinstance(row.get("state"), str)
        or row["state"] not in {"generated", "generation_failure"}):
        raise ValueError("Invalid capture sample state.")
    generated = row["state"] == "generated"
    fields = {"raw", "verification", "schema_valid"} if generated else {"failure_type"}
    if set(row) != common | fields:
        raise ValueError("Capture sample fields must match their recorded state.")
    if (not isinstance(row["sample_id"], str) or not isinstance(row["case_id"], str)
        or type(row["repetition"]) is not int or type(row["elapsed_ms"]) is not int
        or row["elapsed_ms"] < 0 or type(row["parsed_response_received"]) is not bool):
        raise ValueError("Invalid capture sample identity, timing or parsed-response evidence.")
    if generated:
        if not isinstance(row["raw"], dict):
            raise ValueError("Generated samples must retain JSON object output.")
        if (row["parsed_response_received"] is not True
            or type(row["schema_valid"]) is not bool or not isinstance(row["verification"], dict)):
            raise ValueError("Generated samples require complete parsed output evidence.")
        try:
            json.dumps(row["raw"], ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("Generated samples must retain finite UTF-8 JSON output.") from exc
    elif (not isinstance(row["failure_type"], str)
          or row["failure_type"] not in {"GenerationRejected", "GenerationUnavailable"}
          or (row["parsed_response_received"] and row["failure_type"] != "GenerationRejected")):
        raise ValueError("Invalid capture failure or parsed-response evidence.")


def validate_receipt(receipt):
    if not isinstance(receipt, dict):
        raise ValueError("Capture receipt must be a JSON object.")
    try:
        json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Capture receipt must be finite UTF-8 JSON.") from exc
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != digest(body):
        raise ValueError("Capture digest mismatch.")
    if receipt.get("suite_sha256") != SUITE_SHA256:
        raise ValueError("Capture uses a different frozen suite.")
    protocol = receipt_protocol(receipt.get("mode"))
    if not json_equal({key: receipt.get(key) for key in protocol}, protocol):
        raise ValueError("Capture receipt contradicts the frozen evaluation protocol.")
    if receipt.get("mode") == "model_capture":
        runtime = receipt.get("runtime")
        if (not isinstance(runtime, dict)
            or runtime.get("basis") != "operator_declared_not_independently_verified"):
            raise ValueError("Runtime provenance must retain its operator-declared basis.")
        validate_runtime({key: value for key, value in runtime.items() if key != "basis"})
        try:
            generator = receipt["models"]["components"][0]
            pinned_generator = model_records(
                generator["profile"], generator_exercised=False)["components"][0]
        except (KeyError, IndexError, TypeError, StopIteration) as exc:
            raise ValueError("Capture must select a documented pinned model profile.") from exc
        # A checksum is not an attestation: revalidate the declarations against
        # the recorded profile's repository pin, as capture does before calls.
        if (generator.get("role") != "generator"
            or any(runtime[key] != pinned_generator[field]
                   or generator.get(field) != pinned_generator[field]
                   for key, field in (("upstream_model_id", "model_id"),
                                      ("upstream_revision", "revision"), ("license", "license")))):
            raise ValueError("Declared artifact provenance must match the selected pinned model profile.")
        configuration = receipt.get("configuration")
        if not isinstance(configuration, dict):
            raise ValueError("Capture configuration must be a JSON object.")
        try:
            expected_configuration = capture_configuration(
                configuration["adapter"], configuration["model"],
                configuration["repetitions"], configuration["disable_thinking"])
        except KeyError as exc:
            raise ValueError("Capture configuration is incomplete.") from exc
        if not json_equal(configuration, expected_configuration):
            raise ValueError("Capture configuration contradicts the fixed adapter protocol.")
        repetitions = configuration["repetitions"]
        cases = {case["case_id"]: case for case in load_suite()["cases"]}
        expected = {f"{case_id}:{repetition}" for case_id in cases
                    for repetition in range(1, repetitions + 1)}
        rows = receipt.get("results", [])
        if not isinstance(rows, list):
            raise ValueError("Capture results must be a complete list of samples.")
        for row in rows:
            validate_capture_row(row)
        if (len(rows) != len(expected) or {row["sample_id"] for row in rows} != expected
            or receipt.get("generation", {}).get("attempts") != len(expected)):
            raise ValueError("Capture must contain every planned sample exactly once.")
        for row in rows:
            if (row.get("case_id") not in cases or type(row.get("repetition")) is not int
                or row["sample_id"] != f"{row['case_id']}:{row['repetition']}"
                or row.get("state") not in {"generated", "generation_failure"}):
                raise ValueError("Invalid capture sample identity or state.")
            case = cases[row["case_id"]]
            evidence = tuple(EvidenceItem(**item) for item in case["evidence"])
            system, user = _prompt(case["question"], evidence, (), "", grounding_repair=False)
            if not json_equal(row.get("prompt"), {"system": system, "user": user}):
                raise ValueError("Capture sample prompt differs from the frozen case protocol.")
        validate_execution_metadata(receipt)
        for row in rows:
            if row["state"] == "generated":
                evidence = tuple(EvidenceItem(**item) for item in cases[row["case_id"]]["evidence"])
                if not json_equal(row["verification"], verify_output(row["raw"], evidence)):
                    raise ValueError("Stored verification differs from captured code replay.")
                if row["schema_valid"] is not Draft202012Validator(ANSWER_SCHEMA).is_valid(row["raw"]):
                    raise ValueError("Stored schema validity differs from captured output.")
        if not json_equal(receipt["generation"], generation_counts(rows)):
            raise ValueError("Generation counters disagree with the complete capture.")
        identity = receipt.get("runtime_identity")
        if not isinstance(identity, dict) or set(identity) != {
            "method", "model", "artifact_sha256", "observations", "boundaries", "limitation"
        }:
            raise ValueError("Gradable captures require complete runtime identity evidence.")
        validate_identity_snapshot({key: identity[key] for key in ("method", "model", "artifact_sha256")},
            receipt["configuration"]["model"], receipt["runtime"]["model_artifact_sha256"],
            receipt["configuration"]["adapter"])
        if (type(identity["observations"]) is not int or identity["observations"] != 2 * len(rows) + 1
            or identity["boundaries"] != IDENTITY_BOUNDARIES or identity["limitation"] != IDENTITY_LIMITATION):
            raise ValueError("Runtime identity evidence has incomplete boundaries or limitations.")
        expected_models = model_records(generator["profile"], generator_exercised=True)
        expected_models["components"][0]["execution"] = generator_execution(rows)
        if not json_equal(receipt["models"], expected_models):
            raise ValueError("Recorded model catalog disagrees with the pinned manifest or capture scope.")


def model_records(profile, *, generator_exercised):
    manifest = json.loads((ROOT / "config/models.json").read_text(encoding="utf-8"))
    records = []
    for role in ("generator", "embedding", "reranker"):
        entry = next(item for item in manifest["models"] if item["role"] == role
                     and (role != "generator" or item["profile"] == profile))
        records.append({**entry, "execution": "capture_planned_no_execution_evidence" if role == "generator"
                        and generator_exercised else "not_exercised",
                        "revision_basis": "repository_manifest_not_runtime_attestation"})
    return {"manifest_sha256": digest(manifest), "components": records}


def execution_metadata():
    # Deliberately omit hostname, username, endpoint, environment and filesystem paths.
    return {
        "os": platform.system(), "os_release": platform.release(),
        "architecture": platform.machine(), "python": platform.python_version(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_dirty": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT)),
        "implementation_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in IMPLEMENTATION_PATHS},
    }


def validate_execution_metadata(receipt):
    execution = receipt.get("execution")
    required = {"os", "os_release", "architecture", "python", "git_commit",
                "working_tree_dirty", "implementation_sha256"}
    if not isinstance(execution, dict) or set(execution) != required:
        raise ValueError("Capture execution metadata must contain exactly the recorded fields.")
    # Platform strings describe the capture host, not the machine grading it.
    # platform may report an empty string when a platform detail is unknown.
    if (any(not isinstance(execution[key], str) for key in ("os", "os_release", "architecture", "python"))
        or type(execution["working_tree_dirty"]) is not bool):
        raise ValueError("Capture execution environment fields have invalid types.")
    commit = execution["git_commit"]
    if (not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit)
        or not commit.strip("0")):
        raise ValueError("Capture execution requires a nonzero full Git commit ID.")
    timestamp = receipt.get("created_at")
    if (not isinstance(timestamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:\+00:00|Z)", timestamp)):
        raise ValueError("Capture timestamp must be an ISO timestamp in UTC.")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Capture timestamp is invalid.") from exc
    fingerprints = execution["implementation_sha256"]
    if (not isinstance(fingerprints, dict) or set(fingerprints) != set(IMPLEMENTATION_PATHS)
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
               for value in fingerprints.values())):
        raise ValueError("Capture execution has incomplete or invalid implementation fingerprints.")
    # Verification is code-sensitive even for a capture from a dirty checkout.
    for name, recorded in fingerprints.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != recorded:
            raise ValueError("Score with the captured implementation; code fingerprint changed.")
    # Read literal local objects. Denying every transport also protects older
    # Git versions that do not understand the no-lazy-fetch environment flag.
    git_environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
                       "GIT_ALLOW_PROTOCOL": ""}
    try:
        kind = subprocess.check_output(["git", "cat-file", "-t", commit], cwd=ROOT,
                                       text=True, stderr=subprocess.DEVNULL, env=git_environment).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("Captured Git commit is not available locally; use its recorded repository.") from exc
    if kind != "commit":
        raise ValueError("Capture execution Git ID must identify an available commit object.")
    if execution["working_tree_dirty"]:
        return
    # A clean declaration must agree with its committed inputs. A dirty capture
    # records its base commit and current hashes without making that clean claim.
    for name in (*IMPLEMENTATION_PATHS, "config/models.json", "benchmarks/r1b-claims-v2.json"):
        try:
            content = subprocess.check_output(["git", "cat-file", "blob", f"{commit}:{name}"],
                                              cwd=ROOT, stderr=subprocess.DEVNULL, env=git_environment)
            if name in fingerprints:
                matches = hashlib.sha256(content).hexdigest() == fingerprints[name]
            else:
                expected = (receipt["models"].get("manifest_sha256")
                            if name == "config/models.json" else receipt["suite_sha256"])
                matches = digest(json.loads(content)) == expected
        except (OSError, subprocess.CalledProcessError, ValueError, UnicodeError) as exc:
            raise ValueError("Clean capture requires readable committed evaluation inputs.") from exc
        if not matches:
            if name == "config/models.json":
                raise ValueError("Clean capture model catalog disagrees with its committed evaluation inputs.")
            raise ValueError("Clean capture fingerprints disagree with its committed evaluation inputs.")


def base_receipt(mode, profile):
    return {
        **receipt_protocol(mode),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution": execution_metadata(),
        "models": model_records(profile, generator_exercised=mode == "model_capture"),
    }


def claim_slots(raw):
    """Keep original positions and malformed slots; never count only survivors."""
    if not isinstance(raw, dict):
        return []
    rows = [(f"claim-{index}", value) for index, value in enumerate(raw["claims"])] if isinstance(raw.get("claims"), list) else []
    if raw.get("limitation") is not None:
        rows.append(("limitation", raw["limitation"]))
    return rows


def verify_output(raw, evidence):
    evidence_map = {item.evidence_id: item for item in evidence}
    rows = []
    accepted_keys = {"ordinary": set(), "exact_span": set()}
    for slot, value in claim_slots(raw):
        claim = (verify_original_claim(value.get("text"), value.get("evidence_ids"), evidence_map)
                 if isinstance(value, dict) and set(value) == {"text", "evidence_ids"} else None)
        row = {"slot": slot}
        for path, verified in (("ordinary", claim), ("exact_span", _exact_original_span(claim, evidence_map))):
            disposition = "rejected"
            if verified is not None:
                key = (verified.text, frozenset(verified.evidence_ids))
                # Production deduplicates material claims, but not the separate
                # limitation. Retain every raw occurrence in the denominator.
                if slot != "limitation" and key in accepted_keys[path]:
                    disposition = "duplicate_omitted"
                else:
                    disposition = "retained"
                    if slot != "limitation":
                        accepted_keys[path].add(key)
            row[path + "_accepted"] = disposition == "retained"
            row[path + "_disposition"] = disposition
        rows.append(row)
    outcomes = {}
    for path, strict in (("ordinary", False), ("exact_span", True)):
        try:
            answer = GroundedGenerationService._verify(raw, evidence, 0, strict_originals=strict)
            outcomes[path] = {"state": "answer" if answer.answerable else "abstained",
                              "retained_claims": len(answer.claims),
                              "duplicate_claims": answer.duplicate_claims,
                              "omitted_claims": answer.omitted_claims}
        except (GenerationRejected, TypeError, AttributeError) as exc:
            outcomes[path] = {"state": "rejected", "failure_type": type(exc).__name__}
        # The application never retains a candidate from a rejected/abstained
        # response, even when that candidate alone passes the text predicate.
        if outcomes[path]["state"] != "answer":
            for row in rows:
                row[path + "_accepted"] = False
                row[path + "_disposition"] = "response_" + outcomes[path]["state"]
    return {"claims": rows, "response": outcomes}


def fraction(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def verifier_metrics(rows):
    invalid = [row for row in rows if not (row["semantic_valid"] and row["citations_valid"])]
    valid = [row for row in rows if row["semantic_valid"] and row["citations_valid"]]
    return {path: {
        "duplicate_omissions": fraction(sum(row.get(path + "_disposition") == "duplicate_omitted"
                                            for row in rows), len(rows)),
        "false_acceptance": fraction(sum(row[path + "_accepted"] for row in invalid), len(invalid)),
        "false_rejection": fraction(sum(not row[path + "_accepted"] for row in valid), len(valid)),
        "semantic_false_acceptance": fraction(sum(row[path + "_accepted"] for row in rows
            if not row["semantic_valid"]), sum(not row["semantic_valid"] for row in rows)),
        "citation_false_acceptance": fraction(sum(row[path + "_accepted"] for row in rows
            if not row["citations_valid"]), sum(not row["citations_valid"] for row in rows)),
    } for path in ("ordinary", "exact_span")}


def run_probes(profile="portable"):
    receipt = base_receipt("injected_verifier_probes", profile)
    rows = []
    for case in load_suite()["cases"]:
        evidence = tuple(EvidenceItem(**item) for item in case["evidence"])
        for candidate in case["candidates"]:
            result = verify_output(candidate["output"], evidence)
            if len(result["claims"]) != 1:
                raise ValueError("Frozen probes require exactly one claim per candidate.")
            rows.append({"case_id": case["case_id"], "category": case["category"],
                         **candidate, **result["claims"][0], "response": result["response"]})
    receipt.update(results=rows, verifier=verifier_metrics(rows),
                   generation={"status": "not_measured_injected_outputs", "model_calls": 0},
                   offline_readiness={"status": "not_exercised_no_models_loaded"})
    return seal(receipt)


def validate_runtime(runtime):
    required = {"runtime_name", "runtime_version", "accelerator", "accelerator_memory_gib",
                "driver", "model_artifact_sha256", "offline_readiness", "offline_evidence_sha256",
                "upstream_model_id", "upstream_revision", "license"}
    if not isinstance(runtime, dict) or set(runtime) != required:
        raise ValueError("Runtime profile must contain exactly the documented fields.")
    try:
        json.dumps(runtime, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Runtime profile must be finite, UTF-8 JSON.") from exc
    for field in ("runtime_name", "runtime_version", "accelerator", "driver"):
        if not isinstance(runtime[field], str) or not 1 <= len(runtime[field]) <= 160:
            raise ValueError("Runtime description is missing or too long.")
    if any(not isinstance(runtime[field], str)
           for field in ("upstream_model_id", "upstream_revision", "license")):
        raise ValueError("Runtime upstream provenance fields must be strings.")
    memory = runtime["accelerator_memory_gib"]
    if type(memory) not in (int, float) or not 0 <= memory <= 100_000:
        raise ValueError("Record actual accelerator memory (0 for CPU).")
    if (not isinstance(runtime["model_artifact_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", runtime["model_artifact_sha256"])):
        raise ValueError("Record the immutable loaded artifact SHA-256.")
    if (not isinstance(runtime["offline_readiness"], str)
        or runtime["offline_readiness"] not in {"not_exercised", "operator_attested"}):
        raise ValueError("Offline readiness must be not_exercised or operator_attested.")
    evidence = runtime["offline_evidence_sha256"]
    if ((runtime["offline_readiness"] == "not_exercised" and evidence is not None)
        or (runtime["offline_readiness"] == "operator_attested"
            and (not isinstance(evidence, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence)))):
        raise ValueError("Offline attestation requires a separate evidence digest.")


def capture(client, *, profile, runtime, repetitions, identity_check):
    """Call a real production adapter once per packet; don't hide repair drafts.

    identity_check returns a matched method/model/artifact snapshot; missing or
    inconsistent observations are fatal. Runtime artifact/offline declarations
    are operator evidence, not a claim that an API model name attests weights.
    """
    validate_runtime(runtime)
    adapter = adapter_name(client)
    generator = model_records(profile, generator_exercised=False)["components"][0]
    if any(runtime[key] != generator[field] for key, field in (
        ("upstream_model_id", "model_id"), ("upstream_revision", "revision"), ("license", "license"))):
        raise ValueError("Declared artifact provenance must match the selected pinned model profile.")
    if type(repetitions) is not int or not 1 <= repetitions <= 20:
        raise ValueError("Choose 1 to 20 repetitions before running the model.")
    receipt = base_receipt("model_capture", profile)
    identity_observations = 0
    first_identity = None

    def checked_identity():
        nonlocal identity_observations, first_identity
        snapshot = identity_check()
        validate_identity_snapshot(snapshot, client.model, runtime["model_artifact_sha256"], adapter)
        if first_identity is not None and snapshot != first_identity:
            raise ValueError("Runtime identity method or snapshot changed during capture.")
        first_identity = dict(snapshot)
        identity_observations += 1
    receipt.update(runtime={**runtime, "basis": "operator_declared_not_independently_verified"},
                   configuration=capture_configuration(
                       adapter, client.model, repetitions, getattr(client, "disable_thinking", None)), results=[])
    # Metadata is part of the final evidence too. Refuse an unencodable runtime
    # or model alias before any identity request or expensive generation call.
    try:
        json.dumps(receipt, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Capture metadata must be finite, UTF-8 JSON.") from exc
    for case in load_suite()["cases"]:
        evidence = tuple(EvidenceItem(**item) for item in case["evidence"])
        system, user = _prompt(case["question"], evidence, (), "", grounding_repair=False)
        for repetition in range(1, repetitions + 1):
            row = {"sample_id": f"{case['case_id']}:{repetition}", "case_id": case["case_id"],
                   "repetition": repetition, "prompt": {"system": system, "user": user},
                   "parsed_response_received": False}
            checked_identity()
            start = time.monotonic()
            try:
                try:
                    from .generator_completion import single_schema_attempt
                    with single_schema_attempt():
                        try:
                            raw = client.generate(question=case["question"], evidence=evidence)
                        except GenerationSchemaRejected as exc:
                            # Evaluation retains invalid objects for grading; production
                            # callers still receive an unavailable response, never this data.
                            raw = exc.payload
                except ValueError as exc:
                    # An adapter parse failure is one failed attempt, not a
                    # reason to discard earlier samples. Identity/transport
                    # boundary exceptions remain outside this data-error catch.
                    raise GenerationRejected("Model response could not be parsed.") from exc
                row["parsed_response_received"] = True
                if not isinstance(raw, dict):
                    raise GenerationRejected("Model output is not a JSON object.")
                try:
                    json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise GenerationRejected("Model output is not finite, UTF-8 JSON.") from exc
                row.update(state="generated", raw=raw, verification=verify_output(raw, evidence),
                           schema_valid=Draft202012Validator(ANSWER_SCHEMA).is_valid(raw))
            except (GenerationRejected, GenerationUnavailable) as exc:
                # No remote error text: it may contain endpoint or runtime details.
                row.update(state="generation_failure", failure_type=(
                    "GenerationUnavailable" if isinstance(exc, GenerationUnavailable) else "GenerationRejected"))
            finally:
                checked_identity()
            row["elapsed_ms"] = round((time.monotonic() - start) * 1000)
            receipt["results"].append(row)
    checked_identity()
    receipt["generation"] = generation_counts(receipt["results"])
    receipt["models"]["components"][0]["execution"] = generator_execution(receipt["results"])
    receipt["runtime_identity"] = {**first_identity, "observations": identity_observations,
                                   "boundaries": IDENTITY_BOUNDARIES, "limitation": IDENTITY_LIMITATION}
    return seal(receipt)


def generator_execution(rows):
    return ("parsed_responses_observed_not_inference_attestation"
            if any(row["parsed_response_received"] for row in rows)
            else "attempted_no_parsed_response")


def generation_counts(rows):
    return {"status": "requires_independent_human_grading",
        "attempts": len(rows),
        "generated": sum(row["state"] == "generated" for row in rows),
        "schema_invalid": sum(not row["schema_valid"] for row in rows if row["state"] == "generated"),
        "abstained": sum(row["verification"]["response"]["ordinary"]["state"] == "abstained"
                         for row in rows if row["state"] == "generated"),
    }


def grade_template(receipt):
    validate_receipt(receipt)
    if receipt["mode"] != "model_capture":
        raise ValueError("Only real-model captures receive human generation grades.")
    return {"capture_sha256": receipt["receipt_sha256"], "rubric_sha256": SUITE_SHA256,
            "grader_id": "", "samples": [{
                "sample_id": row["sample_id"], "answer_correct": None, "rationale": "",
                "claims": [{"slot": slot, "semantic_valid": None, "citations_valid": None,
                            "rationale": ""} for slot, _ in claim_slots(row["raw"])],
            } for row in receipt["results"] if row["state"] == "generated"]}


def score_capture(receipt, grades):
    template = grade_template(receipt)
    if (grades.get("capture_sha256") != template["capture_sha256"]
        or grades.get("rubric_sha256") != SUITE_SHA256):
        raise ValueError("Grades are not bound to this capture and rubric.")
    if not isinstance(grades.get("grader_id"), str) or not grades["grader_id"].strip():
        raise ValueError("Use a pseudonymous grader ID, not a personal identity.")
    expected = {row["sample_id"]: row for row in template["samples"]}
    samples = grades.get("samples", [])
    if len(samples) != len(expected) or {row["sample_id"] for row in samples} != set(expected):
        raise ValueError("Grade every generated sample exactly once.")
    by_id = {row["sample_id"]: row for row in receipt["results"]}
    rows = []
    for sample in samples:
        if type(sample.get("answer_correct")) is not bool or not sample.get("rationale", "").strip():
            raise ValueError("Complete answer correctness and rationale for every sample.")
        slots = [row["slot"] for row in expected[sample["sample_id"]]["claims"]]
        claims = sample.get("claims", [])
        if len(claims) != len(slots) or {row["slot"] for row in claims} != set(slots):
            raise ValueError("Grade every raw claim and limitation exactly once.")
        verdicts = {row["slot"]: row for row in by_id[sample["sample_id"]]["verification"]["claims"]}
        for claim in claims:
            if (any(type(claim.get(field)) is not bool for field in ("semantic_valid", "citations_valid"))
                or not claim.get("rationale", "").strip()):
                raise ValueError("Complete independent semantic and citation grades with rationales.")
            rows.append({**claim, **verdicts[claim["slot"]], "sample_id": sample["sample_id"]})
        raw_row = by_id[sample["sample_id"]]
        if sample["answer_correct"] and (not raw_row["schema_valid"]
            or not isinstance(raw_row["raw"], dict) or raw_row["raw"].get("answerable") is not True
            or not raw_row["raw"].get("claims")
            or any(not (claim["semantic_valid"] and claim["citations_valid"]) for claim in claims)):
            raise ValueError("Correct answers must be well-formed, answerable and have only valid claims.")
    attempted = len(receipt["results"])
    failures = attempted - len(samples)
    errors = sum(not row["answer_correct"] for row in samples)
    return seal({"schema_version": 1, "suite_sha256": SUITE_SHA256,
        "capture_sha256": receipt["receipt_sha256"], "grades_sha256": digest(grades),
        "generation": {
            "failed_attempts": fraction(failures, attempted),
            "answer_error_among_generated": fraction(errors, len(samples)),
            "unsuccessful_attempts": fraction(errors + failures, attempted),
            "semantic_claim_errors": fraction(sum(not row["semantic_valid"] for row in rows), len(rows)),
            "citation_claim_errors": fraction(sum(not row["citations_valid"] for row in rows), len(rows)),
        }, "verifier": verifier_metrics(rows), "grades": grades, "claim_results": rows,
        "release_acceptance": "pending_no_release_threshold_defined",
        "limitation": "Human-graded fixed synthetic packets; not production error rates or installed-stack acceptance."})


def write_new(path, payload):
    # Complete serialization first: invalid values must not reserve a path with
    # a truncated artifact that cannot be retried under the no-overwrite policy.
    serialized = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    temporary = None
    try:
        # Keep unfinished bytes private and off the final path. A same-directory
        # hard link publishes the closed file atomically without replacing even
        # a concurrently created destination or an existing symlink.
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                prefix=".claim-receipt-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cleanup cannot replace the original write/install failure.
                pass
        raise
    else:
        temporary.unlink(missing_ok=True)
