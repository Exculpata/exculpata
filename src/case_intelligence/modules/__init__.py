"""Contracts for fixed, internally registered feature modules."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Protocol


ModuleHook = Callable[..., object]
PurgeHook = Callable[[Path, str], None]


@dataclass(frozen=True, slots=True)
class ModuleDatabase:
    """Borrow the application's connection and transaction lock."""

    connection: Connection
    lock: AbstractContextManager


@dataclass(frozen=True, slots=True)
class ModuleContext:
    """Shared capabilities; modules do not acquire application authority.

    ``ensure_capacity`` checks protected disk capacity together with admitted
    upload, playback and archive-staging reservations. It takes the storage
    reservation lock before the database lock, so callers must hold neither
    ``db.lock`` nor a module lock when they call it.
    """

    db: ModuleDatabase
    membership: Callable[[str, str], Any]
    source_store: Callable[[Any], Any]
    matters_root: Path
    scanner: Any
    scan_mode: str
    ensure_capacity: Callable[[int], None]


class RouteHook(Protocol):
    def __call__(
        self, app: Any, *, context: ModuleContext,
        authorized_matter: Callable[[Any, str], Any],
        auth_context: Callable[[Any], Any],
        require_csrf: Callable[..., Any], require_csrf_header: Callable[..., Any],
        templates: Any, audit: Callable[..., None],
        base_context: Callable[..., dict[str, Any]],
        require_response_lease: Callable[..., Iterator[None]],
        transfer_response_lease: Callable[..., Any],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Module:
    """Describe one feature module without granting application authority."""

    name: str
    version: str
    layer: str
    hooks: tuple[ModuleHook, ...] = ()
    route_hook: RouteHook | None = None
    purge_hook: PurgeHook | None = None


__all__ = ["Module", "ModuleContext", "ModuleDatabase", "ModuleHook", "RouteHook", "PurgeHook"]
