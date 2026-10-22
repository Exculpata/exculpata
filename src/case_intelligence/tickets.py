from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import base64
import hashlib
import re
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import PurePath

from .contracts import (
    CollisionOutcome,
    ContentOperation,
    OperationLease,
    TicketAction,
    TicketLaunchProjection,
    TicketRecord,
)
from .isolation import MatterAccess, MatterStateRegistry


class TicketError(PermissionError):
    pass


_TICKET_URI = re.compile(rf"^{LEGACY_STEM}://ticket/([A-Za-z0-9_-]+)$")


class TicketStore:
    def __init__(
        self,
        *,
        registry: MatterStateRegistry,
        clock: Callable[[], datetime],
        token_source: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._token_source = token_source
        self._records: list[TicketRecord] = []
        self._by_digest: dict[str, int] = {}
        self._leases: dict[str, OperationLease] = {}
        self._lock = threading.Lock()
        self._serial = 0
        # Closure fencing and ticket/lease revocation are one state transition,
        # not a follow-up the closure caller can forget.
        registry.register_fence_callback(self.revoke_matter)

    @property
    def records(self) -> tuple[TicketRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def mint(
        self,
        access: MatterAccess,
        actor_id: str,
        matter_id: str,
        action: TicketAction,
        object_version_id: str,
        mapping_revision: int,
    ) -> TicketLaunchProjection:
        access.require(actor_id, matter_id)

        def create() -> TicketLaunchProjection:
            raw = self._token_source(16)
            if len(raw) < 16:
                raise ValueError("token source must provide at least 128 bits")
            token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            digest = hashlib.sha256(token.encode()).hexdigest()
            now = self._clock()
            with self._lock:
                if digest in self._by_digest:
                    raise ValueError("token source produced a duplicate bearer token")
                self._serial += 1
                record = TicketRecord(
                    ticket_id=f"ticket-{self._serial}",
                    token_sha256=digest,
                    actor_id=actor_id,
                    matter_id=matter_id,
                    action=action,
                    object_version_id=object_version_id,
                    mapping_revision=mapping_revision,
                    issued_at=now,
                    expires_at=now + timedelta(seconds=60),
                )
                self._records.append(record)
                self._by_digest[digest] = len(self._records) - 1
            return TicketLaunchProjection(
                uri=f"{LEGACY_STEM}://ticket/{token}", expires_at=record.expires_at
            )

        return self._registry.perform_active(
            matter_id, ContentOperation.MINT_TICKET, create
        )

    def redeem(
        self,
        uri: str,
        access: MatterAccess,
        actor_id: str,
        matter_id: str,
        action: TicketAction,
        object_version_id: str,
        mapping_revision: int,
        helper_instance_id: str,
    ) -> OperationLease:
        match = _TICKET_URI.fullmatch(uri)
        if match is None:
            raise TicketError("invalid ticket URI")
        access.require(actor_id, matter_id)
        digest = hashlib.sha256(match.group(1).encode()).hexdigest()

        def consume() -> OperationLease:
            now = self._clock()
            with self._lock:
                index = self._by_digest.get(digest)
                if index is None:
                    raise TicketError("unknown ticket")
                record = self._records[index]
                expected = (
                    record.actor_id,
                    record.matter_id,
                    record.action,
                    record.object_version_id,
                    record.mapping_revision,
                )
                actual = (
                    actor_id,
                    matter_id,
                    action,
                    object_version_id,
                    mapping_revision,
                )
                if (
                    expected != actual
                    or record.redeemed_at is not None
                    or record.revoked_at is not None
                    or now >= record.expires_at
                ):
                    raise TicketError("ticket binding, state, or lifetime rejected")
                self._records[index] = TicketRecord.model_validate(
                    record.model_dump() | {"redeemed_at": now}
                )
                lease_id = f"lease-{record.ticket_id}-{helper_instance_id}"
                lease = OperationLease(
                    lease_id=lease_id,
                    actor_id=actor_id,
                    matter_id=matter_id,
                    action=action,
                    object_version_id=object_version_id,
                    mapping_revision=mapping_revision,
                    helper_instance_id=helper_instance_id,
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
                self._leases[lease_id] = lease
                return lease

        return self._registry.perform_active(
            matter_id, ContentOperation.REDEEM_TICKET, consume
        )

    def validate_lease(
        self,
        lease_id: str,
        access: MatterAccess,
        actor_id: str,
        matter_id: str,
        action: TicketAction,
        object_version_id: str,
        mapping_revision: int,
        helper_instance_id: str,
    ) -> OperationLease:
        access.require(actor_id, matter_id)

        def validate() -> OperationLease:
            now = self._clock()
            with self._lock:
                lease = self._leases.get(lease_id)
                if lease is None:
                    raise TicketError("unknown lease")
                expected = (
                    lease.actor_id,
                    lease.matter_id,
                    lease.action,
                    lease.object_version_id,
                    lease.mapping_revision,
                    lease.helper_instance_id,
                )
                actual = (
                    actor_id,
                    matter_id,
                    action,
                    object_version_id,
                    mapping_revision,
                    helper_instance_id,
                )
                if (
                    expected != actual
                    or lease.revoked_at is not None
                    or now >= lease.expires_at
                ):
                    raise TicketError("lease rejected")
                return lease

        return self._registry.perform_active(
            matter_id, ContentOperation.REDEEM_TICKET, validate
        )

    def revoke_matter(self, matter_id: str) -> None:
        now = self._clock()
        with self._lock:
            self._records = [
                TicketRecord.model_validate(
                    record.model_dump() | {"revoked_at": now}
                )
                if record.matter_id == matter_id and record.revoked_at is None
                else record
                for record in self._records
            ]
            self._leases = {
                key: OperationLease.model_validate(
                    lease.model_dump() | {"revoked_at": now}
                )
                if lease.matter_id == matter_id and lease.revoked_at is None
                else lease
                for key, lease in self._leases.items()
            }

    def revoke_actor(self, actor_id: str) -> None:
        now = self._clock()
        with self._lock:
            self._records = [
                TicketRecord.model_validate(
                    record.model_dump() | {"revoked_at": now}
                )
                if record.actor_id == actor_id and record.revoked_at is None
                else record
                for record in self._records
            ]
            self._leases = {
                key: OperationLease.model_validate(
                    lease.model_dump() | {"revoked_at": now}
                )
                if lease.actor_id == actor_id and lease.revoked_at is None
                else lease
                for key, lease in self._leases.items()
            }


def choose_collision_safe_name(
    filename: str,
    existing: set[str],
    *,
    max_attempts: int = 100,
    cancelled: bool = False,
) -> tuple[str | None, str]:
    if cancelled:
        return None, CollisionOutcome.CANCELLED.value
    if filename not in existing:
        return filename, CollisionOutcome.VERSIONED_NAME.value
    path = PurePath(filename)
    for number in range(2, max_attempts + 2):
        candidate = f"{path.stem} ({number}){path.suffix}"
        if candidate not in existing:
            return candidate, CollisionOutcome.VERSIONED_NAME.value
    return None, CollisionOutcome.EXHAUSTED.value
