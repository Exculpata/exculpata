"""Frozen bindings to the existing application services and route helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, Mapping

from anyio import CapacityLimiter
from fastapi import HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .derived_text import presentation_text
from .identity import AuthContext, IdentityService

if TYPE_CHECKING:
    from .work_product_exports import ExportArtifact
    from .workbench import CaseIntelligenceWorkbench
    from .workspace_store import MatterRecord


@dataclass(frozen=True)
class AppContext:
    """One application's shared bindings; referenced services remain mutable."""

    bench: CaseIntelligenceWorkbench
    identity: IdentityService
    templates: Jinja2Templates
    auth_context: Callable[[Request], AuthContext]
    authorized_matter: Callable[[Request, str], MatterRecord]
    require_csrf: Callable[..., Awaitable[None]]
    require_csrf_header: Callable[..., Awaitable[None]]
    audit: Callable[..., None]
    requested_path: Callable[[Request], str]
    base_context: Callable[..., dict[str, object]]
    present_value: Callable[[Any], Any]
    download_response: Callable[[Request, ExportArtifact], Response]
    require_matter_response_lease: Callable[[Request, str], Iterator[None]]
    require_matter_bundle_response_lease: Callable[[Request, str], Iterator[None]]
    response_lease_matter: Callable[[Request, str], MatterRecord]
    transfer_matter_response_lease: Callable[..., Response]
    recording_decision_limit: int
    recording_decision_capacity: CapacityLimiter
    recording_decisions_pending: set[str]
    report_labels: Mapping[str, str]
    report_citation_hrefs: Callable[..., dict[str, str]]
    report_edit_recovery: Callable[..., Response]
    report_work_choices: Callable[..., tuple[list[dict[str, object]], list[str]]]
    render_report_compilation: Callable[..., Response]
    add_report_material: Callable[..., Response]


def present_value(value):
    if not isinstance(value, str):
        return value
    projected = presentation_text(value)
    # Preserve Jinja's escaping boundary for already-escaped markup.
    return Markup(projected) if isinstance(value, Markup) else projected


def auth_context(request: Request) -> AuthContext:
    context = getattr(request.state, "auth", None)
    if not isinstance(context, AuthContext):
        raise HTTPException(401, "Sign in is required.")
    return context


def requested_path(request: Request) -> str:
    value = request.url.path
    if request.url.query:
        value += "?" + request.url.query
    return IdentityService.safe_next(value)
