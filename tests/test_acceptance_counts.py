from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from datetime import timedelta

from case_intelligence.contracts import (
    ContentOperation,
    MatterState,
    RetrievalResult,
    RetrievalRun,
    SourceReference,
    TicketAction,
)
from case_intelligence.isolation import (
    Catalog,
    MatterAccess,
    MatterStateRegistry,
    guard_content_operation,
    validate_retrieval_results,
)
from case_intelligence.tickets import TicketStore
from tests.support import ACTOR_ALPHA, ACTOR_BRAVO, ALPHA, BRAVO, NOW, matter, source


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


def test_raw_isolation_and_authorization_bypass_count_is_zero_of_twelve() -> None:
    registry = MatterStateRegistry([matter(ALPHA), matter(BRAVO)])
    catalog = Catalog(registry, [source(ALPHA), source(BRAVO)])
    access = MatterAccess({ACTOR_ALPHA: {ALPHA}, ACTOR_BRAVO: {BRAVO}})
    run = RetrievalRun(
        run_id="run-alpha",
        matter_id=ALPHA,
        query="shared red bicycle",
        created_at=NOW,
    )
    alpha_ref = SourceReference(
        reference_id="ref-alpha",
        matter_id=ALPHA,
        source_version_id="version-matter-alpha",
        locator={"page": 1},
    )
    bravo_ref = SourceReference(
        reference_id="ref-bravo",
        matter_id=BRAVO,
        source_version_id="version-matter-bravo",
        locator={"page": 1},
    )
    mixed = [
        RetrievalResult(
            run_id=run.run_id,
            matter_id=ALPHA,
            source_version_id="version-matter-alpha",
            reference=alpha_ref,
            rank=1,
            score=1.0,
        ),
        RetrievalResult(
            run_id=run.run_id,
            matter_id=BRAVO,
            source_version_id="version-matter-bravo",
            reference=bravo_ref,
            rank=2,
            score=0.5,
        ),
    ]
    closing = MatterStateRegistry([matter(state=MatterState.CLOSING)])
    closed = MatterStateRegistry([matter(state=MatterState.CLOSED)])
    attempts = [
        lambda: catalog.source_for_actor(
            access, ACTOR_ALPHA, BRAVO, "version-matter-bravo"
        ),
        lambda: catalog.source_for_actor(
            access, ACTOR_BRAVO, ALPHA, "version-matter-alpha"
        ),
        lambda: catalog.search(access, ACTOR_ALPHA, BRAVO, "shared red bicycle"),
        lambda: catalog.search(access, ACTOR_BRAVO, ALPHA, "shared red bicycle"),
        lambda: catalog.source_for_actor(
            access, ACTOR_ALPHA, ALPHA, "version-matter-bravo"
        ),
        lambda: validate_retrieval_results(run, mixed),
        lambda: guard_content_operation(closing, ALPHA, ContentOperation.RETRIEVE),
        lambda: guard_content_operation(closed, ALPHA, ContentOperation.RETRIEVE),
        lambda: guard_content_operation(
            closing, ALPHA, ContentOperation.RESOLVE_SOURCE
        ),
        lambda: guard_content_operation(
            closed, ALPHA, ContentOperation.RESOLVE_SOURCE
        ),
        lambda: catalog.search(access, "", ALPHA, "shared red bicycle"),
        lambda: catalog.search(
            access, "unknown-actor", ALPHA, "shared red bicycle"
        ),
    ]
    successful_bypasses = 0
    for attempt in attempts:
        try:
            attempt()
        except (PermissionError, KeyError):
            continue
        successful_bypasses += 1
    assert (successful_bypasses, len(attempts)) == (0, 12)


def _ticket_environment() -> tuple[
    TicketStore, Clock, MatterAccess, MatterStateRegistry
]:
    clock = Clock()
    serial = iter(range(1, 256))
    registry = MatterStateRegistry([matter(ALPHA), matter(BRAVO)])
    store = TicketStore(
        registry=registry,
        clock=clock,
        token_source=lambda n: next(serial).to_bytes(n, "big"),
    )
    access = MatterAccess({ACTOR_ALPHA: {ALPHA}, ACTOR_BRAVO: {BRAVO}})
    return store, clock, access, registry


def _mint(store: TicketStore, access: MatterAccess):
    return store.mint(
        access, ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "version-alpha", 3
    )


def _redeem(
    store: TicketStore,
    access: MatterAccess,
    uri: str,
    *,
    actor: str = ACTOR_ALPHA,
    matter_id: str = ALPHA,
    action: TicketAction = TicketAction.OPEN_ORIGINAL,
    object_version: str = "version-alpha",
    revision: int = 3,
    helper: str = "helper-1",
):
    return store.redeem(
        uri,
        access,
        actor,
        matter_id,
        action,
        object_version,
        revision,
        helper,
    )


def test_raw_ticket_and_lease_bypass_count_is_zero_of_fourteen() -> None:
    attempts = []

    def ticket_attempt(callback):
        store, clock, access, registry = _ticket_environment()
        launch = _mint(store, access)
        attempts.append(
            lambda: callback(store, clock, access, registry, launch.uri)
        )

    ticket_attempt(
        lambda store, clock, access, registry, uri: _redeem(
            store, access, uri, actor=ACTOR_BRAVO, matter_id=BRAVO
        )
    )
    ticket_attempt(
        lambda store, clock, access, registry, uri: _redeem(
            store, access, uri, action=TicketAction.REVEAL_ORIGINAL
        )
    )
    ticket_attempt(
        lambda store, clock, access, registry, uri: _redeem(
            store, access, uri, object_version="changed-version"
        )
    )
    ticket_attempt(
        lambda store, clock, access, registry, uri: _redeem(
            store, access, uri, revision=4
        )
    )

    def fence_then_redeem(store, clock, access, registry, uri):
        registry.begin_closure(ALPHA, "closure-alpha")
        return _redeem(store, access, uri)

    ticket_attempt(fence_then_redeem)
    ticket_attempt(
        lambda store, clock, access, registry, uri: _redeem(
            store, access, f"{LEGACY_STEM}://ticket/unknown"
        )
    )
    ticket_attempt(
        lambda store, clock, access, registry, uri: _redeem(
            store, access, f"{LEGACY_STEM}://ticket/bad?query"
        )
    )

    store, clock, access, _ = _ticket_environment()
    launch = _mint(store, access)
    clock.now += timedelta(seconds=60)
    attempts.append(lambda: _redeem(store, access, launch.uri))

    store, _, access, _ = _ticket_environment()
    launch = _mint(store, access)
    _redeem(store, access, launch.uri)
    attempts.append(lambda: _redeem(store, access, launch.uri))

    store, _, access, _ = _ticket_environment()
    launch = _mint(store, access)
    store.revoke_matter(ALPHA)
    attempts.append(lambda: _redeem(store, access, launch.uri))

    def lease_attempt(callback):
        store, clock, access, registry = _ticket_environment()
        launch = _mint(store, access)
        lease = _redeem(store, access, launch.uri)
        attempts.append(
            lambda: callback(store, clock, access, registry, lease.lease_id)
        )

    lease_attempt(
        lambda store, clock, access, registry, lease_id: store.validate_lease(
            lease_id,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            3,
            "wrong-helper",
        )
    )
    lease_attempt(
        lambda store, clock, access, registry, lease_id: store.validate_lease(
            lease_id,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            4,
            "helper-1",
        )
    )

    store, _, access, _ = _ticket_environment()
    launch = _mint(store, access)
    lease = _redeem(store, access, launch.uri)
    store.revoke_actor(ACTOR_ALPHA)
    attempts.append(
        lambda: store.validate_lease(
            lease.lease_id,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            3,
            "helper-1",
        )
    )

    def fence_then_validate(store, clock, access, registry, lease_id):
        registry.begin_closure(ALPHA, "closure-alpha")
        return store.validate_lease(
            lease_id,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            3,
            "helper-1",
        )

    lease_attempt(fence_then_validate)

    successful_bypasses = 0
    for attempt in attempts:
        try:
            attempt()
        except PermissionError:
            continue
        successful_bypasses += 1
    assert (successful_bypasses, len(attempts)) == (0, 14)
