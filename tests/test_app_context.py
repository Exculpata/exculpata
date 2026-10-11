"""Application bindings stay shared when reports consume the frozen context."""
from dataclasses import FrozenInstanceError
from inspect import getclosurevars

import pytest

from case_intelligence.app_context import AppContext
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app


@pytest.fixture
def app(tmp_path):
    application = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test"
    )
    try:
        yield application
    finally:
        application.state.workbench.close()


def test_reports_share_one_frozen_context_and_existing_dependencies(app):
    context = app.state.app_context
    assert isinstance(context, AppContext)
    assert context.bench is app.state.workbench
    assert context.identity is app.state.identity
    assert context.templates.env.finalize is context.present_value
    with pytest.raises(FrozenInstanceError):
        context.bench = None

    reports = [route for route in app.routes if route.path.startswith("/matters/{slug}/reports")]
    assert len(reports) == 18
    for route in reports:
        assert getclosurevars(route.endpoint).nonlocals == {"app_context": context}
        dependencies = [dependency.call for dependency in route.dependant.dependencies]
        if "POST" in route.methods:
            assert context.require_csrf in dependencies
        if route.name == "export_matter_report":
            assert context.require_matter_response_lease in dependencies

    renderer_bindings = getclosurevars(context.render_report_compilation).nonlocals
    assert renderer_bindings["templates"] is context.templates
    assert renderer_bindings["auth_context"] is context.auth_context
    assert renderer_bindings["base_context"] is context.base_context
    assert renderer_bindings["report_labels"] is context.report_labels
    assert renderer_bindings["report_work_choices"] is context.report_work_choices


def test_recording_admission_keeps_original_shared_capacity_and_pending_set(app, tmp_path):
    context = app.state.app_context
    recording_bindings = [
        getclosurevars(route.endpoint).nonlocals
        for route in app.routes if hasattr(route, "endpoint")
        and "recording_decision_capacity" in getattr(
            getattr(route.endpoint, "__code__", None), "co_freevars", ()
        )
    ]
    assert len(recording_bindings) == 1
    bindings = recording_bindings[0]
    assert bindings["recording_decision_capacity"] is context.recording_decision_capacity
    assert bindings["recording_decisions_pending"] is context.recording_decisions_pending
    assert bindings["recording_decision_limit"] == context.recording_decision_limit
    assert context.recording_decision_capacity.total_tokens == context.recording_decision_limit

    other = create_workbench_app(
        tmp_path / "other", generator=UnavailableGenerator(), auth_mode="test"
    )
    try:
        assert other.state.app_context is not context
        assert other.state.app_context.recording_decision_capacity is not context.recording_decision_capacity
        assert other.state.app_context.recording_decisions_pending is not context.recording_decisions_pending
    finally:
        other.state.workbench.close()
