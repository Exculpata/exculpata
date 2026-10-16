
from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from datetime import timedelta

import pytest

from case_intelligence.contracts import TicketAction
from case_intelligence.isolation import MatterAccess, MatterStateRegistry
from case_intelligence.tickets import TicketError, TicketStore, choose_collision_safe_name
from tests.support import ACTOR_ALPHA, ACTOR_BRAVO, ALPHA, BRAVO, NOW, matter


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def store() -> tuple[TicketStore, Clock, MatterAccess, MatterStateRegistry]:
    clock = Clock()
    access = MatterAccess({ACTOR_ALPHA: {ALPHA}, ACTOR_BRAVO: {BRAVO}})
    registry = MatterStateRegistry([matter(ALPHA), matter(BRAVO)])
    serial = iter(range(1, 256))
    tickets = TicketStore(
        registry=registry,
        clock=clock,
        token_source=lambda n: next(serial).to_bytes(n, "big"),
    )
    return tickets, clock, access, registry


def test_ticket_is_opaque_digest_only_and_exact_ttl() -> None:
    tickets, clock, access, _ = store()
    launch = tickets.mint(
        access, ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "version-alpha", 3
    )
    assert launch.uri.startswith(f"{LEGACY_STEM}://ticket/")
    assert (
        "matter" not in launch.uri
        and "open" not in launch.uri
        and "?" not in launch.uri
        and "#" not in launch.uri
    )
    record = tickets.records[0]
    assert not hasattr(record, "token")
    assert len(record.token_sha256) == 64
    assert record.expires_at - record.issued_at == timedelta(seconds=60)
    clock.now = record.expires_at
    with pytest.raises(TicketError):
        tickets.redeem(
            launch.uri,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            3,
            "helper-1",
        )


def test_ticket_is_single_use_and_all_bindings_are_checked() -> None:
    tickets, _, access, _ = store()
    launch = tickets.mint(
        access, ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "version-alpha", 3
    )
    lease = tickets.redeem(
        launch.uri,
        access,
        ACTOR_ALPHA,
        ALPHA,
        TicketAction.OPEN_ORIGINAL,
        "version-alpha",
        3,
        "helper-1",
    )
    assert lease.helper_instance_id == "helper-1"
    with pytest.raises(TicketError):
        tickets.redeem(
            launch.uri,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            3,
            "helper-1",
        )
    for actor, matter_id, action, obj, revision in [
        (ACTOR_BRAVO, BRAVO, TicketAction.OPEN_ORIGINAL, "version-alpha", 3),
        (ACTOR_ALPHA, ALPHA, TicketAction.REVEAL_ORIGINAL, "version-alpha", 3),
        (ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "changed", 3),
        (ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "version-alpha", 4),
    ]:
        other = tickets.mint(
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            3,
        )
        with pytest.raises((TicketError, PermissionError)):
            tickets.redeem(
                other.uri,
                access,
                actor,
                matter_id,
                action,
                obj,
                revision,
                "helper-1",
            )


def test_closure_fence_automatically_invalidates_ticket_and_lease() -> None:
    tickets, _, access, registry = store()
    pending = tickets.mint(
        access, ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "version-alpha", 1
    )
    launch = tickets.mint(
        access, ACTOR_ALPHA, ALPHA, TicketAction.SAVE_ARTIFACT, "artifact-v1", 2
    )
    lease = tickets.redeem(
        launch.uri,
        access,
        ACTOR_ALPHA,
        ALPHA,
        TicketAction.SAVE_ARTIFACT,
        "artifact-v1",
        2,
        "helper-1",
    )
    registry.begin_closure(ALPHA, "closure-alpha")
    with pytest.raises(PermissionError):
        tickets.redeem(
            pending.uri,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            1,
            "helper-1",
        )
    with pytest.raises(PermissionError):
        tickets.validate_lease(
            lease.lease_id,
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.SAVE_ARTIFACT,
            "artifact-v1",
            2,
            "helper-1",
        )
    with pytest.raises(PermissionError):
        tickets.mint(
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            1,
        )


def test_collision_decision_never_overwrites() -> None:
    assert choose_collision_safe_name("memo.pdf", {"memo.pdf"}) == (
        "memo (2).pdf",
        "versioned_name",
    )
    assert (
        choose_collision_safe_name(
            "memo.pdf", {"memo.pdf", "memo (2).pdf"}, max_attempts=1
        )[1]
        == "exhausted"
    )
    assert choose_collision_safe_name("memo.pdf", set(), cancelled=True)[1] == "cancelled"


def test_ticket_uri_parser_rejects_noncanonical_and_empty_tokens() -> None:
    tickets, _, access, _ = store()
    for uri in (
        f"{LEGACY_STEM}://ticket/",
        f"{LEGACY_STEM}://ticket/token%20value",
        f"{LEGACY_STEM}://ticket/token:part",
        f"{LEGACY_STEM}://ticket/token/part",
    ):
        with pytest.raises(TicketError):
            tickets.redeem(
                uri,
                access,
                ACTOR_ALPHA,
                ALPHA,
                TicketAction.OPEN_ORIGINAL,
                "version-alpha",
                1,
                "helper-1",
            )


def test_duplicate_bearer_token_is_never_aliased_to_another_record() -> None:
    clock = Clock()
    access = MatterAccess({ACTOR_ALPHA: {ALPHA}})
    tickets = TicketStore(
        registry=MatterStateRegistry([matter()]),
        clock=clock,
        token_source=lambda n: b"x" * n,
    )
    tickets.mint(
        access, ACTOR_ALPHA, ALPHA, TicketAction.OPEN_ORIGINAL, "version-alpha", 1
    )
    with pytest.raises(ValueError):
        tickets.mint(
            access,
            ACTOR_ALPHA,
            ALPHA,
            TicketAction.OPEN_ORIGINAL,
            "version-alpha",
            1,
        )
