"""Versioned, restartable synthesis of saved findings against original sources.

The DAG contains claim references, never summary-as-evidence citations. The
existing generation verifier is run against originals at both levels and when
reading saved nodes. JSON lives in the research job's transactional checkpoint.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import time
from typing import Callable, Mapping

from .generation import (
    EvidenceItem, GenerationGroundingRejected, VerifiedAnswer, VerifiedClaim, MEDIA_TRANSCRIPT_NOTICE,
    verify_original_claim,
)

VERSION = 2
POLICY = {"version": VERSION, "max_groups": 12, "sources_per_call": 12,
          "characters_per_call": 48_000, "characters_per_source": 6_000,
          "generation_requests": 32, "wall_seconds": 900,
          "output_tokens_per_call": 1_200}
LEGACY_POLICY = {**POLICY, "version": 1}
LEGACY_FINDING_NOTICE = "Historical checkpoint: finding-level retention was not measured."


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def original(token, ledger, identifier=None):
    source = ledger[token]
    return EvidenceItem(identifier or token, source["source_name"], source["location"],
                        source["excerpt"][:POLICY["characters_per_source"]],
                        source["evidence_kind"], source["document_id"])


def valid_claim(value, ledger):
    if not isinstance(value, dict) or set(value) != {"text", "support_tokens", "parents"}:
        return False
    tokens = value["support_tokens"]
    if not isinstance(tokens, list) or not tokens or any(token not in ledger for token in tokens):
        return False
    return verify_original_claim(value["text"], tokens,
                                 {token: original(token, ledger) for token in tokens}) is not None


def findings_from_passes(passes, ledger):
    findings = []
    rejected = []
    for p, step in enumerate(passes):
        answer = step.get("answer") or {}
        claims = list(answer.get("claims", []))
        limitation = answer.get("source_limitation") if answer.get("verification_notice") else answer.get("limitation")
        if limitation:
            claims.append(limitation)
        for c, claim in enumerate(claims):
            identifier = f"P{p + 1}C{c + 1}"
            tokens = [source.get("support_token") for source in claim.get("citations", [])]
            value = {"text": claim.get("text"), "support_tokens": tokens, "parents": [identifier]}
            if valid_claim(value, ledger):
                findings.append({"id": identifier, **value})
            else:
                rejected.append(identifier)
    return findings, rejected


def groups_for(claims):
    groups = []
    current = []
    tokens = set()
    for claim in claims:
        added = set(claim["support_tokens"])
        # Four claims leave room in the existing 8-claim response for competing
        # evidence and a sourced qualification. A claim's originals stay together.
        if current and (len(current) == 4 or len(tokens | added) > min(POLICY["sources_per_call"], POLICY["characters_per_call"] // POLICY["characters_per_source"])):
            groups.append(current)
            current, tokens = [], set()
        current.append(claim)
        tokens.update(added)
    if current:
        groups.append(current)
    return groups


def node_spec(level, ordinal, claims):
    return {"id": f"{level}{ordinal + 1}", "level": level,
            "parents": [claim["id"] for claim in claims],
            "support_tokens": list(dict.fromkeys(token for claim in claims for token in claim["support_tokens"])),
            "input_digest": digest(claims)}


def retained_parents(claim, inputs):
    """Confirm literal finding survival, separately from original grounding.

    A shared source or overlapping words cannot prove a whole finding survived.
    Match complete contiguous sentences, allowing only whitespace/case changes,
    and require all of that finding's original support. Paraphrases remain valid
    grounded output but their input retention is conservatively unconfirmed.
    """
    def sentences(text):
        return re.split(r"(?<=[.!?])\s+", " ".join(text.split()).casefold())
    output = sentences(claim["text"])
    retained = []
    for parent in inputs:
        expected = sentences(parent["text"])
        if (set(parent["support_tokens"]) <= set(claim["support_tokens"])
                and any(output[index:index + len(expected)] == expected
                        for index in range(len(output) - len(expected) + 1))):
            retained.append(parent["id"])
    return retained


def omitted_parents(claims, inputs):
    retained = {parent for claim in claims for parent in retained_parents(claim, inputs)}
    return [parent["id"] for parent in inputs if parent["id"] not in retained]


def validate_node(node, spec, ledger, inputs, *, legacy=False):
    if not isinstance(node, dict) or any(node.get(key) != value for key, value in spec.items()):
        raise ValueError("Synthesis node no longer matches its original findings.")
    if node.get("state") not in {"complete", "gap"} or not isinstance(node.get("claims"), list):
        raise ValueError("Invalid synthesis node checkpoint.")
    if len(node["claims"]) > 9:
        raise ValueError("Oversized synthesis node.")
    for claim in node["claims"]:
        if (not valid_claim(claim, ledger)
                or claim["parents"] != (spec["parents"] if legacy else retained_parents(claim, inputs))
                or not set(claim["support_tokens"]) <= set(spec["support_tokens"])):
            raise ValueError("An intermediate synthesis claim lacks original source support.")
    if (node["state"] == "complete") != bool(node["claims"]):
        raise ValueError("Invalid synthesis node outcome.")
    if type(node.get("omitted_claims")) is not int or node["omitted_claims"] < 0:
        raise ValueError("Invalid synthesis omission count.")
    if not legacy and node.get("omitted_parents") != omitted_parents(node["claims"], inputs):
        raise ValueError("Invalid synthesis parent omission receipt.")


def level_claims(nodes):
    return [{"id": f"{node['id']}C{index + 1}", **claim}
            for node in nodes for index, claim in enumerate(node["claims"])]


def matter_inputs(nodes):
    # Round-robin across issue summaries so each bounded matter packet compares
    # different issue groups rather than merely regenerating its parent packet.
    return [{"id": f"{node['id']}C{index + 1}", **node["claims"][index]}
            for index in range(max((len(node["claims"]) for node in nodes), default=0))
            for node in nodes if index < len(node["claims"])]


def validate_state(state, passes, evidence, *, final=False):
    ledger = {item["support_token"]: item for item in evidence}
    findings, rejected = findings_from_passes(passes, ledger)
    legacy = state.get("version") == 1
    if state.get("version") not in {1, VERSION} or state.get("policy") != (LEGACY_POLICY if legacy else POLICY):
        raise ValueError("Unknown hierarchical synthesis version or policy.")
    if state.get("basis") != digest({"passes": passes, "evidence": evidence}):
        raise ValueError("Synthesis checkpoint no longer matches saved findings and sources.")
    if (type(state.get("requests_spent")) is not int
            or not 0 <= state["requests_spent"] <= POLICY["generation_requests"]
            or type(state.get("started_at")) not in {int, float}
            or not 0 < state["started_at"] < float("inf")):
        raise ValueError("Invalid synthesis resource receipt.")
    if state.get("rejected_findings") != rejected:
        raise ValueError("Invalid rejected-finding receipt.")
    for level, claims in (("issue", findings), ("matter", matter_inputs(state.get("issue", [])))):
        groups = groups_for(claims)[:POLICY["max_groups"]]
        nodes = state.get(level)
        if not isinstance(nodes, list) or len(nodes) > len(groups):
            raise ValueError("Invalid synthesis group checkpoint.")
        for ordinal, node in enumerate(nodes):
            validate_node(node, node_spec(level, ordinal, groups[ordinal]), ledger,
                          groups[ordinal], legacy=legacy)
    if state["requests_spent"] < len(state["issue"]) + len(state["matter"]):
        raise ValueError("Synthesis calls were not charged.")
    if state.get("matter") and len(state["issue"]) != min(len(groups_for(findings)), POLICY["max_groups"]):
        raise ValueError("Matter synthesis preceded its issue checkpoint.")
    if final:
        if state.get("stop_reason") not in {"completed", "group_budget", "generation_budget", "time_budget", "character_budget"}:
            raise ValueError("Invalid synthesis stop reason.")
        expected = completion_receipt(state, findings, ledger)
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError("Invalid synthesis partial-processing receipt.")
        pending = any(len(state[level]) < min(len(groups_for(claims)), POLICY["max_groups"])
                      for level, claims in (("issue", findings), ("matter", matter_inputs(state["issue"]))))
        if state["stop_reason"] in {"completed", "group_budget"} and pending:
            raise ValueError("Synthesis stopped before its admitted groups completed.")
        if state["stop_reason"] == "completed" and expected["omitted_groups"]:
            raise ValueError("Completed synthesis omitted groups.")
        if state["stop_reason"] == "generation_budget" and state["requests_spent"] != POLICY["generation_requests"]:
            raise ValueError("Synthesis generation budget was not exhausted.")
    return ledger, findings


def completion_receipt(state, findings, ledger):
    omitted = []
    issue_groups = groups_for(findings)
    omitted.extend(f"issue{i + 1}" for i in range(len(state["issue"]), len(issue_groups)))
    if len(state["issue"]) < min(len(issue_groups), POLICY["max_groups"]):
        omitted.append("matter_pending_issue_completion")
    else:
        matter_groups = groups_for(matter_inputs(state["issue"]))
        omitted.extend(f"matter{i + 1}" for i in range(len(state["matter"]), len(matter_groups)))
    used = {token for claim in level_claims(state["matter"]) for token in claim["support_tokens"]}
    uncited = [token for token in ledger if token not in used]
    truncated = sum(max(0, len(source["excerpt"]) - POLICY["characters_per_source"]) for source in ledger.values())
    accounting = {}
    if state["version"] == VERSION:
        missing = omitted_parents(level_claims(state["matter"]), findings)
        accounting = {"retained_findings": [item["id"] for item in findings if item["id"] not in missing],
                      "omitted_findings": missing}
    partial = bool(omitted or state["rejected_findings"] or not findings or uncited or truncated
                   or accounting.get("omitted_findings")
                   or any(node.get("omitted_parents") for level in ("issue", "matter") for node in state[level])
                   or any(node["state"] == "gap" or node["omitted_claims"]
                          for level in ("issue", "matter") for node in state[level]))
    return {"omitted_groups": omitted, "partial": partial,
            "uncited_evidence": uncited, "truncated_characters": truncated, **accounting}


def upgrade_checkpoint(state, findings, ledger):
    """Reaccount validated v1 output without regenerating or refunding work."""
    state.update(version=VERSION, policy=dict(POLICY))
    for level in ("issue", "matter"):
        inputs = findings if level == "issue" else matter_inputs(state["issue"])
        for ordinal, (node, group) in enumerate(zip(state[level], groups_for(inputs))):
            node.update(node_spec(level, ordinal, group))
            for claim in node["claims"]:
                claim["parents"] = retained_parents(claim, group)
            node["omitted_parents"] = omitted_parents(node["claims"], group)
    state.update(completion_receipt(state, findings, ledger))


def run_synthesis(question, passes, evidence, generator, checkpoint: Callable,
                  boundary: Callable, saved=None, *, now=time.time):
    if saved and (saved.get("version") not in {1, VERSION}
                  or saved.get("policy") != (LEGACY_POLICY if saved.get("version") == 1 else POLICY)):
        raise ValueError("Unknown hierarchical synthesis version or policy.")
    basis = digest({"passes": passes, "evidence": evidence})
    ledger = {item["support_token"]: item for item in evidence}
    findings, rejected = findings_from_passes(passes, ledger)
    if saved and saved.get("basis") == basis:
        state = deepcopy(saved)
    else:
        # New searches or source invalidation discard derived nodes, not spent
        # generation budget or the first-start deadline of this investigation.
        state = {"version": VERSION, "policy": dict(POLICY), "basis": basis,
                 "requests_spent": (saved or {}).get("requests_spent", 0),
                 "started_at": (saved or {}).get("started_at", now()),
                 "issue": [], "matter": [], "rejected_findings": rejected}
    validate_state(state, passes, evidence)
    if state["version"] == 1:
        upgrade_checkpoint(state, findings, ledger)
        validate_state(state, passes, evidence)
    state.update(stop_reason="running", partial=True, omitted_groups=[])
    checkpoint(state)

    for level in ("issue", "matter"):
        inputs = findings if level == "issue" else matter_inputs(state["issue"])
        groups = groups_for(inputs)
        for ordinal, claims in enumerate(groups[:POLICY["max_groups"]]):
            spec = node_spec(level, ordinal, claims)
            if ordinal < len(state[level]):
                continue
            boundary()
            if state["requests_spent"] >= POLICY["generation_requests"]:
                state["stop_reason"] = "generation_budget"
                break
            if now() - state["started_at"] >= POLICY["wall_seconds"]:
                state["stop_reason"] = "time_budget"
                break
            state["requests_spent"] += 1
            checkpoint(state)  # Charge before dispatch; interrupted calls stay spent.
            remaining = POLICY["characters_per_call"]
            packet = []
            for index, token in enumerate(spec["support_tokens"], 1):
                item = original(token, ledger, f"S{index}")
                if len(item.excerpt) > remaining:
                    break
                packet.append(item)
                remaining -= len(item.excerpt)
            if len(packet) != len(spec["support_tokens"]):
                # Never drop some of a claim's support to squeeze in a packet.
                state["stop_reason"] = "character_budget"
                break
            context = json.dumps({"task": synthesis_instructions(level), "derived_findings_not_evidence": [
                {"text": claim["text"], "original_sources": [
                    f"S{spec['support_tokens'].index(token) + 1}" for token in claim["support_tokens"]]}
                for claim in claims]}, ensure_ascii=False)
            try:
                answer = generator.answer(
                    question, tuple(packet), working_context=context)
            except GenerationGroundingRejected:
                answer = None
            boundary()
            node = {**spec, "state": "gap", "claims": [],
                    "omitted_claims": answer.omitted_claims if answer else len(claims)}
            if answer and answer.answerable:
                accepted = list(answer.claims)
                limitation = answer.source_limitation if answer.verification_notice else answer.limitation
                if limitation and limitation.evidence_ids:
                    accepted.append(limitation)
                for claim in accepted:
                    value = {"text": claim.text,
                             "support_tokens": [spec["support_tokens"][int(identifier[1:]) - 1]
                                                for identifier in claim.evidence_ids],
                             "parents": []}
                    if valid_claim(value, ledger):
                        value["parents"] = retained_parents(value, claims)
                        node["claims"].append(value)
                    else:
                        node["omitted_claims"] += 1
                if node["claims"]:
                    node["state"] = "complete"
            node["omitted_parents"] = omitted_parents(node["claims"], claims)
            validate_node(node, spec, ledger, claims)
            state[level].append(node)
            checkpoint(state)
        state["omitted_groups"].extend(f"{level}{i + 1}" for i in range(len(state[level]), len(groups)))
        if state["stop_reason"] != "running":
            if level == "issue":
                state["omitted_groups"].append("matter_pending_issue_completion")
            break
    if state["stop_reason"] == "running":
        state["stop_reason"] = "group_budget" if state["omitted_groups"] else "completed"
    state.update(completion_receipt(state, findings, ledger))
    validate_state(state, passes, evidence, final=True)
    checkpoint(state)
    return state


def synthesis_instructions(level):
    """Keep stage instructions separate from the complete generator question."""
    instruction = ("Develop an issue-level section from the saved findings." if level == "issue" else
                   "Develop a matter-level section from the issue summaries.")
    return (instruction + " Retain supporting AND competing accounts with separate original citations; "
            "do not reconcile a contradiction without source support. Identify unresolved questions.")


def synthesis_answer(state, evidence):
    ids = {item["support_token"]: f"S{index + 1}" for index, item in enumerate(evidence)}
    claims, seen = [], set()
    for node in state["matter"]:
        for claim in node["claims"]:
            key = (claim["text"], tuple(claim["support_tokens"]))
            if key not in seen:
                claims.append(VerifiedClaim(claim["text"], tuple(ids[token] for token in claim["support_tokens"])))
                seen.add(key)
    notice = synthesis_notice(state)
    return VerifiedAnswer(bool(claims), "Supporting and competing evidence from saved findings:",
                          tuple(claims), VerifiedClaim(notice, ()) if claims else None,
                          notice if not claims else "", tuple(dict.fromkeys(token for claim in claims for token in claim.evidence_ids)),
                          bool(state["requests_spent"]), 0,
                          evidence_notice=MEDIA_TRANSCRIPT_NOTICE if any(
                              evidence[int(identifier[1:]) - 1]["evidence_kind"] == "transcript"
                              for claim in claims for identifier in claim.evidence_ids) else "",
                          verification_notice=notice)


def synthesis_notice(state):
    finding_notice = (f"saved findings retained: {len(state['retained_findings'])}; "
                      f"saved findings omitted or not confirmed retained: {len(state['omitted_findings'])} "
                      f"({', '.join(state['omitted_findings']) or 'none'}); "
                      if state["version"] == VERSION else "")
    return (f"Hierarchical synthesis {'partial' if state['partial'] else 'complete within its saved finding scope'}: "
            f"{len(state['issue'])} issue groups; {len(state['matter'])} matter sections; "
            f"{state['requests_spent']}/{POLICY['generation_requests']} generation requests spent. "
            f"Stop reason: {state['stop_reason']}. Omitted groups: {len(state['omitted_groups'])}; "
            f"rejected saved findings: {len(state['rejected_findings'])} "
            f"({', '.join(state['rejected_findings']) or 'none'}); "
            f"{finding_notice}"
            f"generated statements omitted: {sum(node['omitted_claims'] for level in ('issue', 'matter') for node in state[level])}; "
            f"uncited evidence units: {len(state.get('uncited_evidence', []))}; "
            f"original characters outside synthesis packets: {state.get('truncated_characters', 0)}. "
            "Unresolved questions: review competing accounts and saved search gaps; absence of a finding is not proof of absence. "
            "Intermediate summaries are not evidence sources. This does not measure whole-matter coverage.")


def portable_hierarchy(state, evidence, safe_citation):
    """Project validated accounting and original citations without private tokens."""
    sources = {item["support_token"]: item for item in evidence}
    result = {key: state[key] for key in (
        "version", "policy", "requests_spent", "stop_reason", "partial",
        "omitted_groups", "rejected_findings", "truncated_characters")}
    if state["version"] == VERSION:
        result.update({key: state[key] for key in ("retained_findings", "omitted_findings")})
    else:
        result["finding_retention_notice"] = LEGACY_FINDING_NOTICE
    result["uncited_sources"] = [safe_citation(sources[token]) for token in state["uncited_evidence"]]
    for level in ("issue", "matter"):
        result[level] = [{
            "id": node["id"], "parents": node["parents"], "state": node["state"],
            "omitted_claims": node["omitted_claims"],
            **({"omitted_parents": node["omitted_parents"]} if state["version"] == VERSION else {}),
            "claims": [{"text": claim["text"], "parents": claim["parents"],
                        "sources": [safe_citation(sources[token]) for token in claim["support_tokens"]]}
                       for claim in node["claims"]],
        } for node in state[level]]
    return result
