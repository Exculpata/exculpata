#!/usr/bin/env python3
"""Run a fixed synthetic hierarchy through a locally staged Ollama artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from case_intelligence.generation import (
    ANSWER_SCHEMA, GroundedGenerationService, OllamaGenerator,
    _bounded_json_get, _bounded_json_request, _parse_model_content, _prompt,
)
from case_intelligence.hierarchical_synthesis import run_synthesis, synthesis_answer, POLICY


class EvaluationGenerator(OllamaGenerator):
    """Explicit evaluation-only wire setting for the installed native profile."""
    def generate(self, *, question, evidence, history=(), working_context="", grounding_repair=False):
        system, user = _prompt(question, evidence, history, working_context,
                               grounding_repair=grounding_repair)
        response = _bounded_json_request(self.endpoint + "/api/chat", {
            "model": self.model, "stream": False, "think": False,
            "format": ANSWER_SCHEMA,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 1200},
            "keep_alive": "5m",
        }, timeout=self.timeout)
        return _parse_model_content(response.get("message", {}).get("content"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11435")
    parser.add_argument("--model", required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--disable-thinking", action="store_true",
                        help="Record the installed native evaluation profile's explicit think=false setting; does not change application defaults")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = (EvaluationGenerator if args.disable_thinking else OllamaGenerator)(args.endpoint, args.model, timeout=90)
    tags = _bounded_json_get(client.endpoint + "/api/tags", timeout=5)
    model = next(item for item in tags["models"] if item["name"] == args.model)
    if model["digest"] != args.expected_digest.removeprefix("sha256:"):
        raise ValueError("The served artifact does not match the requested immutable digest")
    passes, evidence = [], []
    for index in range(24):
        text = (f"Synthetic dispatch record {index + 1} states the crate was delivered."
                if index != 23 else "Synthetic dispatch record 24 states the crate was not delivered.")
        token = hashlib.sha1(f"synthetic-{index}".encode()).hexdigest()
        evidence.append({"support_token": token, "source_name": f"synthetic-{index + 1}.txt",
                         "location": "page 1", "excerpt": text, "evidence_kind": "document",
                         "document_id": f"synthetic-source-{index + 1}", "source_version_id": "synthetic-v1"})
        passes.append({"answer": {"claims": [{"text": text, "citations": [{"support_token": token}]}]}})
    # A saved unsupported finding must not be promoted into model context.
    passes[0]["answer"]["claims"].append({"text": "A submarine transported 9999 satellites to Jupiter.",
                                        "citations": [{"support_token": evidence[0]["support_token"]}]})
    receipt = {"suite": "hierarchical-synthesis-24-units-v1", "model": args.model,
               "artifact_digest": model["digest"], "disable_thinking": args.disable_thinking,
               "context_tokens": 8192, "output_tokens_per_call": 1200, "policy": POLICY,
               "runtime": _bounded_json_get(client.endpoint + "/api/version", timeout=5)}
    started = time.monotonic()
    checkpoints = []
    try:
        state = run_synthesis("Summarize what the dispatch records say about delivery. Retain conflicting accounts and each record's number.",
            passes, evidence, GroundedGenerationService(client),
            lambda value: checkpoints.append(json.loads(json.dumps(value))), lambda: None)
        answer = synthesis_answer(state, evidence)
        # This fixed corpus has only one proposition and its explicit negation.
        # Require original support at both ends, then inspect every returned claim.
        support = any("S1" in claim.evidence_ids and "delivered" in claim.text.lower()
                      and "not" not in claim.text.lower().split() for claim in answer.claims)
        contradiction = any("S24" in claim.evidence_ids and "not" in claim.text.lower().split()
                            and "delivered" in claim.text.lower() for claim in answer.claims)
        injected = "Jupiter" in answer.text or "9999" in answer.text
        # The endpoint accounts alone cannot certify retention of this corpus.
        # Require every source statement with its own citation, and fail closed
        # on paraphrases that this deterministic evaluator cannot adjudicate.
        exact = {claim.text for claim in answer.claims if any(
            claim.text == source["excerpt"] and f"S{index + 1}" in claim.evidence_ids
            for index, source in enumerate(evidence))}
        recalled = sum(source["excerpt"] in exact for source in evidence)
        unreviewed = [claim.text for claim in answer.claims if claim.text not in exact]
        accounting_verified = (not state["omitted_findings"] and not state["omitted_groups"]
            and not state["uncited_evidence"] and not state["truncated_characters"]
            and state["rejected_findings"] == ["P1C2"]
            and not any(node["omitted_parents"] or node["omitted_claims"]
                        for level in ("issue", "matter") for node in state[level]))
        receipt.update(state=state, claims=[{"text": claim.text, "evidence_ids": claim.evidence_ids} for claim in answer.claims],
                       decisive_evidence_recall={"support": support, "contradiction": contradiction},
                       original_statement_recall={"recalled": recalled, "total": len(evidence)},
                       claims_requiring_manual_inspection=unreviewed,
                       finding_accounting_verified=accounting_verified,
                       unsupported_injected_claim_displayed=injected,
                       passed=support and contradiction and not injected and recalled == len(evidence)
                       and not unreviewed and accounting_verified and state["stop_reason"] == "completed")
    except Exception as exc:
        receipt.update(passed=False, failure_type=type(exc).__name__, failure=str(exc),
                       last_checkpoint=checkpoints[-1] if checkpoints else None)
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: receipt[key] for key in ("suite", "passed", "elapsed_seconds")}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
