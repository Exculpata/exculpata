"""Fixed internal feature-module registry."""

from __future__ import annotations

from case_intelligence.modules import Module
from case_intelligence.modules.discovery_review.routes import route_hook
from case_intelligence.modules.discovery_review.purge import purge_hook


REGISTERED: tuple[Module, ...] = (
    Module(name="discovery_review", version="0.1.0", layer="modules.discovery_review",
           route_hook=route_hook, purge_hook=purge_hook),
)


def lookup(name: str) -> Module | None:
    """Return the registered module with ``name``, when present."""
    return next((module for module in REGISTERED if module.name == name), None)


__all__ = ["REGISTERED", "lookup"]
