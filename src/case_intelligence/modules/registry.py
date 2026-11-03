"""Fixed internal feature-module registry."""

from __future__ import annotations

from case_intelligence.modules import Module


REGISTERED: tuple[Module, ...] = ()


def lookup(name: str) -> Module | None:
    """Return the registered module with ``name``, when present."""
    return next((module for module in REGISTERED if module.name == name), None)


__all__ = ["REGISTERED", "lookup"]
