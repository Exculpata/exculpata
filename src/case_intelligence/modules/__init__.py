"""Contracts for fixed, internally registered feature modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


ModuleHook = Callable[..., object]


@dataclass(frozen=True, slots=True)
class Module:
    """Describe one feature module without granting application authority."""

    name: str
    version: str
    layer: str
    hooks: tuple[ModuleHook, ...] = ()


__all__ = ["Module", "ModuleHook"]
