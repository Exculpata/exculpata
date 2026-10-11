"""Optional Home presentation of the existing, read-only discovery briefing."""
from __future__ import annotations

import os

from .briefing import build_briefing
from .intake_receipts import IntakeReceipts
from .workspace_store import WorkspaceProblem


def _processing(readiness):
    return bool(readiness.get("state") == "preparing" or readiness.get("processing_count")
        or readiness.get("overview_processing_count") or readiness.get("discovery", {}).get("working"))


def install_briefing_home(templates, *, bench, auth_context, readiness_for):
    """Install template helpers only; no new route, persistence or generation."""
    enabled = os.getenv("CASE_INTELLIGENCE_BRIEFING", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }

    def home_briefing(request, matter):
        if not enabled or matter is None or request.url.path != f"/matters/{matter.slug}/home":
            return None
        actor = auth_context(request).principal_id
        try:
            bench.workspace.membership(matter.matter_id, actor)
        except KeyError:
            # The existing read-only administrator Home does not grant D1's
            # membership boundary. Never substitute an owner's identity.
            return None
        result = dict(state="processing", briefing=None, message="", can_query=False,
            sources_url=f"/matters/{matter.slug}/setup?view=list",
            refresh_url=f"/matters/{matter.slug}/home")
        try:
            service = bench.entity_service(matter)
            # Both locks are reentrant. Keep the fresh completion check and D1
            # snapshot within the same mutation boundary, without an outer SQL
            # transaction around collaborators that own their transactions.
            with service.source_guard(), bench.workspace._lock:
                bench.workspace.membership(matter.matter_id, actor)
                if _processing(readiness_for(matter)):
                    return result
                briefing = build_briefing(matter, actor, workspace=bench.workspace,
                    entity_service=service, intake_receipts=IntakeReceipts(bench.workspace),
                    source_metadata=bench.source_store(matter).get)
                readiness = readiness_for(matter)
                if _processing(readiness):
                    return result
            result.update(state="ready", briefing=briefing, can_query=bool(readiness.get("can_query")))
        except WorkspaceProblem:
            result.update(state="unavailable", message=(
                "The briefing is unavailable because its complete read limit was reached or records changed. "
                "Review the sources, or refresh Home to try again."))
        except KeyError:
            # Membership or the matter can disappear after Home authorized its
            # initial read. Do not return an already assembled partial result.
            return None
        return result

    templates.env.globals.update(briefing_enabled=enabled, home_briefing=home_briefing)
