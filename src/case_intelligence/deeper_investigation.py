"""Optional explicit widening of a model's saved not-answerable response."""
from __future__ import annotations

import os
import uuid


def install_deeper_investigation(templates, *, bench, auth_context):
    """Add a read-only template helper; submission uses the existing Ask route."""
    enabled = os.getenv("CASE_INTELLIGENCE_DEEPER_INVESTIGATION", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }

    def offer(request, matter, message):
        if (not enabled or message.role != "assistant" or message.payload.get("answerable") is not False
                or message.payload.get("generator_answerable") is not False
                or message.payload.get("model_called") is not True
                or message.payload.get("kind") != "not-supported" or message.payload.get("workflow") == "research"):
            return None
        actor = auth_context(request).principal_id
        try:
            basis = bench.workspace.answer_investigation_basis(
                matter.matter_id, actor, message.conversation_id, message.message_id,
            )
        except KeyError:
            return None
        if basis is None or (message.payload.get("source_scope") and not basis.source_set_id):
            return None
        disabled = not basis.scope_available or not basis.can_query or not bench.generator.available or basis.has_active_work
        reason = ("The original source scope is unavailable." if not basis.scope_available else
                  "Wait for the current review work to finish." if basis.has_active_work else
                  "Source preparation and local answering must be ready." if disabled else "")
        key = uuid.uuid5(uuid.NAMESPACE_URL, f"recordbench:deeper:{matter.matter_id}:{message.message_id}:{actor}").hex
        return dict(question=basis.question, conversation=message.conversation_id, source_set=basis.source_set_id,
            request_key="answer-request-" + key, disabled=disabled, reason=reason,
            has_context=bool(message.payload.get("recorded_context_job") or message.payload.get("notebook_context")))

    templates.env.globals.update(deeper_investigation_enabled=enabled, deeper_investigation_offer=offer)
