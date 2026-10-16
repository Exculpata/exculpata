
from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from case_intelligence.contracts import SourceLocator, SourceReference, TicketAction
from case_intelligence.projections import project_reference, project_retrieval, project_source, project_ticket
from case_intelligence.tickets import TicketStore
from case_intelligence.isolation import MatterAccess, MatterStateRegistry
from tests.support import ACTOR_ALPHA, ALPHA, NOW, matter, source


def all_keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(all_keys(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(all_keys(v) for v in value), set())
    return set()


def test_staff_projections_are_explicit_and_hide_internal_fields() -> None:
    item = source()
    ref = SourceReference(reference_id="ref", matter_id=ALPHA, source_version_id=item.source_version_id, locator=SourceLocator(page=2, printed_page="A-2", bates_start="SYN001"))
    projections = [project_source(item).model_dump(), project_reference(item, ref).model_dump(), project_retrieval(item, ref).model_dump()]
    forbidden = {"matter_id", "source_version_id", "source_location_id", "relative_path", "sha256", "score", "run_id", "processor_version", "linux_root", "unc_root", "mapping_revision"}
    assert all(not (all_keys(value) & forbidden) for value in projections)
    assert projections[1]["source_name"] == "Synthetic Report.txt"
    assert projections[1]["page"] == 2


def test_ticket_projection_contains_only_opaque_uri_and_expiry() -> None:
    tickets = TicketStore(registry=MatterStateRegistry([matter()]), clock=lambda: NOW, token_source=lambda n: b"z" * n)
    launch = tickets.mint(MatterAccess({ACTOR_ALPHA: {ALPHA}}), ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "version-alpha", 1)
    projected = project_ticket(launch).model_dump()
    assert set(projected) == {"uri", "expires_at"}
    assert projected["uri"].startswith(f"{LEGACY_STEM}://ticket/")
