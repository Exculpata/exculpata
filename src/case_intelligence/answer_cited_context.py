"""Read-only, bounded comparison of a saved generated passage and one citation.

The route owns source/control guards and current request authority. Nothing here
updates a saved answer or treats a current source locator as historical evidence.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import urlencode

from .answer_presentation import answer_limitation
from .answer_evidence import saved_evidence_window
from .workspace_store import WorkspaceProblem


CONTEXT_NOTICE = (
    "Cited context is for comparison, not proof that the generated passage is supported. "
    "Showing the first 6,000 characters at most; open the full source to review the rest."
)
UNAVAILABLE_NOTICE = (
    "Cited context is unavailable: the source changed, is missing, or lacks an exact "
    "saved text version. The historical citation has not been refreshed. Review the "
    "source and ask again in a new conversation."
)


def saved_cited_context(bench, matter, conversation_id, message_id, passage, citation_index):
    """Caller must authorize the matter and hold source then workspace guards."""
    # Read only one bounded payload, not a conversation's full context receipts.
    row = bench.workspace.connection.execute(
        "SELECT m.payload_json FROM workbench_message m "
        "JOIN workbench_conversation c ON c.conversation_id=m.conversation_id "
        "WHERE c.matter_id=? AND c.conversation_id=? AND m.message_id=? "
        "AND m.role='assistant' AND length(m.payload_json)<=100000",
        (matter.matter_id, conversation_id, message_id),
    ).fetchone()
    if row is None:
        raise KeyError(message_id)
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise KeyError(message_id) from exc
    if not isinstance(payload, dict) or payload.get("kind") != "generated":
        raise KeyError(message_id)
    if passage == "limitation":
        selected = payload.get("limitation")
    elif re.fullmatch(r"claim-[0-9]{1,3}", passage):
        claims = payload.get("claims")
        index = int(passage.removeprefix("claim-"))
        if not isinstance(claims, list) or index >= len(claims):
            raise KeyError(passage)
        selected = claims[index]
    else:
        raise KeyError(passage)
    if not isinstance(selected, dict) or not isinstance(selected.get("text"), str):
        raise KeyError(passage)
    citations = selected.get("citations")
    if not isinstance(citations, list) or not 0 <= citation_index < len(citations):
        raise KeyError(citation_index)
    citation = citations[citation_index]
    if not isinstance(citation, Mapping):
        raise KeyError(citation_index)
    result = {
        "state": "unavailable", "excerpt": "", "source_href": "",
        "passage_text": answer_limitation(payload) if passage == "limitation" else selected["text"],
        "notice": UNAVAILABLE_NOTICE,
        **{key: str(citation.get(key) or "")[:maximum] for key, maximum in (
            ("source_name", 300), ("location", 300),
            ("source_version_id", 64), ("excerpt_digest", 64),
        )},
    }
    try:
        # Notebook preview mode verifies the COMPLETE unit before shortening it.
        # Its source scan/record/time budgets remain in force for this one source.
        window = saved_evidence_window(payload, citation)
        reference, = bench._saved_answer_references(matter, [citation],
            notebook_preview=True, source_navigation=True,
            **({"source_windows": {citation["support_token"]: window}} if window is not None else {}))
    except (KeyError, TypeError, ValueError, WorkspaceProblem) as exc:
        if "source-validation limit" in str(exc):
            result["notice"] = (
                "This source exceeds the bounded comparison limit. Review the source "
                "separately; the historical citation has not been refreshed."
            )
        return result
    store = bench.source_store(matter)
    document = store.get(reference["document_id"])
    navigation = reference["source_navigation"]
    source_parameters = {"unit": navigation["ordinal"],
        "entity_return_to": (f"/matters/{matter.slug}?conversation={conversation_id}"
            f"#answer-support-{message_id}")}
    source_fragment = ""
    if reference["kind"] == "transcript":
        source_parameters["start_ms"] = navigation["start_ms"]
        source_fragment = f"#segment-{navigation['ordinal']}"
    source_query = urlencode(source_parameters)
    notice = CONTEXT_NOTICE if window is None else (
        "Cited context is for comparison, not proof that the generated passage is supported. "
        f"Showing the saved source window, characters {window['start'] + 1:,}–{window['end']:,} "
        f"of {window['original_chars']:,}; open the full source to review omitted context.")
    result.update(state="available", excerpt=reference["excerpt"], notice=notice,
        source_href=f"/matters/{matter.slug}/sources/{store.action_token(document)}?{source_query}{source_fragment}")
    return result
