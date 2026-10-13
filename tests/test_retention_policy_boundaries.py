"""Controlled-clock checks of ongoing matters and non-destructive check-ins."""
from datetime import timedelta

import pytest

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import CaseIntelligenceWorkbench
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_matter_retention_and_themes import FIXED, _principals


@pytest.mark.parametrize(
    ('age', 'status', 'warning'),
    [(15, 'ongoing', False), (30, 'ongoing', False), (37, 'ongoing', False),
     (76, 'check_in_soon', True), (83, 'check_in_next_week', True),
     (89, 'check_in_next_week', True), (90, 'check_in_due', True),
     (120, 'ownership_review', True), (365, 'ownership_review', True)],
)
def test_check_in_boundaries_never_become_deletion_authority(tmp_path, monkeypatch, age, status, warning):
    monkeypatch.setenv('CASE_INTELLIGENCE_MAINTENANCE_ENABLED', '0')
    bench = CaseIntelligenceWorkbench(tmp_path / 'runtime', generator=UnavailableGenerator(),
                                      learned_retrieval=False)
    try:
        bench.workspace._clock = lambda: FIXED
        _principals(bench.workspace)
        matter = bench.create_matter('Synthetic ongoing review', '', 'principal-owner')
        bench.workspace._clock = lambda: FIXED + timedelta(days=age)
        projection = bench.retention_projection(matter)
        assert projection['status'] == status
        assert projection['show_warning'] is warning
        assert bench.workspace.due_matter_retentions() == ()
        assert bench.workspace.begin_due_matter_purge(matter.matter_id, source_count=0) is None
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    finally:
        bench.close()


def test_check_in_horizon_rolls_forward_beyond_original_creation(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_MAINTENANCE_ENABLED', '0')
    bench = CaseIntelligenceWorkbench(tmp_path / 'runtime', generator=UnavailableGenerator(),
                                      learned_retrieval=False)
    try:
        now = [FIXED]
        bench.workspace._clock = lambda: now[0]
        _principals(bench.workspace)
        matter = bench.create_matter('Synthetic continuing review', '', 'principal-owner')
        for elapsed_days in (0, 300, 600):
            now[0] = FIXED + timedelta(days=elapsed_days)
            policy = bench.workspace.case_lifecycle.check_in(matter.matter_id, 'principal-owner')
            assert bench._parse_utc(policy.next_check_in_at) == now[0] + timedelta(days=90)
            assert bench.retention_projection(matter)['show_warning'] is False
            assert bench.workspace.matter_retention(matter.matter_id) is None
            assert bench.workspace.due_matter_retentions() == ()
            with pytest.raises(WorkspaceProblem, match='check-in'):
                bench.retention_for_date(matter, 'principal-owner', '2029-01-01')
        assert bench._parse_utc(policy.next_check_in_at) == FIXED + timedelta(days=690)
    finally:
        bench.close()
