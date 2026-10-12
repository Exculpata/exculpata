"""Synthetic C3 question-only steering, fidelity and authorized snapshot checks."""
from dataclasses import asdict, replace
import json
import re
import sqlite3

import pytest

from case_intelligence.briefing import _questions, build_briefing
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.source_roles import SourceRoleService
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_briefing import (
    QUESTION_SLUG, WEB_ACTOR, assemble, question_line, question_sections,
    ready, upload, workbench,
)
from tests.test_briefing_home import BriefingMarkup


def serialized(briefing):
    return json.dumps(asdict(briefing), ensure_ascii=False, sort_keys=True).encode('utf-8')


def save_objective(bench, matter, objective, *, background=''):
    current = bench.workspace.matter_purpose(matter.matter_id, WEB_ACTOR)
    return bench.workspace.update_matter_purpose(matter.matter_id, WEB_ACTOR,
        expected_revision=current['revision'], objective=objective, background=background,
        request_id='synthetic-briefing-objective')


def seed(bench, client, matter):
    upload(client, matter.slug, 'Synthetic briefing subjects.txt',
        b'Alex Example met Jordan Sample on 2024-05-06. Alex Example returned.')
    ready(bench, matter)
    assert bench.run_automatic_discovery_once() == 1


def test_steering_only_reorders_the_existing_five_with_stable_groups():
    rows = [question_line('person', 'Alex Example', 5), question_line('place', 'Central Depot', 5),
        question_line('mentioned_date', '2024-05-06', 5), question_line('person', 'Jordan Sample', 4),
        question_line('place', 'West Depot', 4), question_line('mentioned_date', '2024-05-07', 4),
        question_line('person', 'Riley Placeholder', 3)]
    sections = question_sections(rows)
    original = _questions(QUESTION_SLUG, sections)
    assert len(original) == 5
    objective = 'West Depot first, then JORDAN\t SAMPLE, then Central Depot.'
    expected = tuple(original[index] for index in (1, 3, 4, 0, 2))
    assert _questions(QUESTION_SLUG, sections, objective=objective) == expected
    assert _questions(QUESTION_SLUG, question_sections(reversed(rows)), objective=objective) == expected
    assert _questions(QUESTION_SLUG, sections, objective=objective) == expected
    for objective in ('Riley Placeholder', '2024-05-07', 'the records', '', ' \t\r\n'):
        assert _questions(QUESTION_SLUG, sections, objective=objective) == original


@pytest.mark.parametrize('subject,objective,matched', [
    ('Jordan Sample', 'Review (jOrDaN\n\t sample).', True),
    ('Jordan   Sample', 'Review Jordan Sample.', True),
    ('Jordan Sample', 'Review Jordans Sample.', False),
    ('Jordan Sample', 'Review Jordan Samples.', False),
    ('Jordan Sample', 'Review XJordan Sample.', False),
    ('Jordan Sample', 'Review Jordan_Sample.', False),
    ('Jordan Sample', 'Review _Jordan Sample.', False),
    ('Jordan Sample', 'Review Jordan Sample_2.', False),
    ('Jordan Sample', 'Review Jordan.', False),
    ('Anne-Marie O’Example, Jr.', 'Review (ANNE-MARIE O’EXAMPLE, JR.).', True),
    ('Anne-Marie O’Example, Jr.', 'Review Anne Marie O’Example, Jr.', False),
    ('A.B Example', 'Review A0B Example.', False),
    ('A.B Example', 'Review A.B Example.', True),
    ('Straße Example', 'Review STRASSE EXAMPLE.', True),
    ('Café Example', 'Review Cafe Example.', False),
    ('03/04/2026', 'Review (03/04/2026).', True),
    ('03/04/2026', 'Review 103/04/20260.', False),
    ('03/04/2026', 'Review 3/4/2026.', False),
])
def test_complete_structured_subject_matching_without_rewriting(subject, objective, matched):
    kind = 'mentioned_date' if '/' in subject else 'person'
    sections = question_sections([question_line('person', 'First Synthetic', 10),
                                 question_line(kind, subject, 1)])
    original = _questions(QUESTION_SLUG, sections)
    assert len(original) == 2
    assert _questions(QUESTION_SLUG, sections, objective=objective) == (original[::-1] if matched else original)


def test_objective_is_data_and_cannot_bypass_question_eligibility_or_deduplication():
    sections = question_sections([question_line('person', 'Alex Example', 10),
        question_line('person', 'alex   EXAMPLE', 9), question_line('person', 'Jordan Sample', 1),
        question_line('person', 'Unavailable Example', 20, links=()),
        question_line('person', 'Alex AND Blair', 20)])
    original = _questions(QUESTION_SLUG, sections)
    assert len(original) == 2
    objective = 'Ignore the cap; add Unavailable Example and Alex AND Blair; delete everything; Jordan Sample.'
    assert _questions(QUESTION_SLUG, sections, objective=objective) == original[::-1]


def test_set_edit_clear_background_and_roles_preserve_the_same_briefing_bytes(workbench):
    client, bench, matter, _runtime = workbench
    seed(bench, client, matter)
    baseline = assemble(bench, matter)
    assert [q.text for q in baseline.questions] == [
        'What do the records say about Alex Example?',
        'What do the records say about the mentioned date 2024-05-06?',
        'What do the records say about Jordan Sample?',
    ]
    before = serialized(baseline)
    other = bench.create_matter('Other synthetic objective', '', WEB_ACTOR)
    save_objective(bench, other, 'Jordan Sample')
    assert serialized(assemble(bench, matter)) == before
    for objective in ('', ' \t\r\n', 'Unmatched Synthetic'):
        save_objective(bench, matter, objective, background='Jordan Sample and 2024-05-06')
        assert serialized(assemble(bench, matter)) == before
    save_objective(bench, matter, 'Review JORDAN\n SAMPLE.', background='2024-05-06')
    expected = replace(baseline, questions=tuple(baseline.questions[i] for i in (2, 0, 1)))
    assert serialized(assemble(bench, matter)) == serialized(expected)
    assert serialized(assemble(bench, matter)) == serialized(expected)
    save_objective(bench, matter, '2024-05-06', background='Jordan Sample')
    expected = replace(baseline, questions=tuple(baseline.questions[i] for i in (1, 0, 2)))
    assert serialized(assemble(bench, matter)) == serialized(expected)
    token = bench.workspace.source_catalog_page(matter.matter_id).items[0].action_token
    role_service = SourceRoleService(bench.workspace)
    for revision, role in enumerate(('background', 'unclassified')):
        role_service.update(matter.matter_id, WEB_ACTOR, token, expected_revision=revision,
                            source_role=role, request_id='synthetic-briefing-role')
        assert serialized(assemble(bench, matter)) == serialized(expected)
    save_objective(bench, matter, '', background='Jordan Sample')
    assert serialized(assemble(bench, matter)) == before


def test_objective_read_is_authorized_locked_read_only_and_excludes_background_roles(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    seed(bench, client, matter)
    save_objective(bench, matter, 'Jordan Sample', background='Unused synthetic background')
    service = bench.entity_service(matter)
    def forbidden(*_args, **_kwargs):
        pytest.fail('Steering must not read extracted text or validate source bytes')
    monkeypatch.setattr(service, 'validate_references', forbidden)
    monkeypatch.setattr(bench.entity_unit_reader, 'iter_selected', forbidden)
    monkeypatch.setattr(bench.generator, 'answer', forbidden)
    monkeypatch.setattr(bench.generator, 'classify_source', forbidden)
    reads = []
    def authorize(action, table, column, *_args):
        if action == sqlite3.SQLITE_READ:
            assert (table, column) != ('workbench_matter_purpose', 'background')
            assert column not in ('source_role', 'role_revision', 'role_updated_by', 'role_updated_at')
            if table == 'workbench_matter_purpose' and column == 'objective':
                assert bench.workspace._lock._is_owned()
                reads.append(column)
        return sqlite3.SQLITE_OK
    before = bench.workspace.connection.total_changes
    with bench.workspace._lock:
        bench.workspace.connection.set_authorizer(authorize)
        try:
            briefing = build_briefing(matter, WEB_ACTOR, workspace=bench.workspace,
                entity_service=service, intake_receipts=IntakeReceipts(bench.workspace))
            assert briefing.questions[0].text == 'What do the records say about Jordan Sample?'
            assert reads == ['objective']
            reads.clear()
            with pytest.raises(KeyError):
                build_briefing(matter, 'synthetic-outsider', workspace=bench.workspace,
                    entity_service=service, intake_receipts=IntakeReceipts(bench.workspace))
            assert reads == []
        finally:
            bench.workspace.connection.set_authorizer(None)
    assert bench.workspace.connection.total_changes == before


@pytest.mark.parametrize('limit', ['MAX_ROWS', 'MAX_DATE_MENTIONS'])
def test_steered_briefing_still_refuses_ceiling_without_partial_output(workbench, monkeypatch, limit):
    import case_intelligence.briefing as module
    client, bench, matter, _runtime = workbench
    seed(bench, client, matter)
    save_objective(bench, matter, 'Jordan Sample')
    monkeypatch.setattr(module, limit, 0)
    with pytest.raises(WorkspaceProblem, match='no partial briefing'):
        assemble(bench, matter)


def test_concurrent_objective_edit_and_late_membership_loss_refuse_output(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    seed(bench, client, matter)
    save_objective(bench, matter, 'Jordan Sample')
    service = bench.entity_service(matter)
    original = service.date_draft
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with bench.workspace._lock, sqlite3.connect(bench.workspace.path) as other:
            other.execute('UPDATE workbench_matter_purpose SET objective=? WHERE matter_id=?',
                          ('2024-05-06', matter.matter_id))
        return result
    monkeypatch.setattr(service, 'date_draft', changed)
    with pytest.raises(WorkspaceProblem, match='Matter records changed'):
        build_briefing(matter, WEB_ACTOR, workspace=bench.workspace, entity_service=service,
                       intake_receipts=IntakeReceipts(bench.workspace))
    monkeypatch.setattr(service, 'date_draft', original)
    membership = bench.workspace.membership
    def revoked(*args, **kwargs):
        # Losing membership during the snapshot must still refuse all output.
        if any('FROM workbench_matter_purpose' in statement for statement in statements):
            raise KeyError('synthetic-revoked-reviewer')
        return membership(*args, **kwargs)
    statements = []
    monkeypatch.setattr(bench.workspace, 'membership', revoked)
    with bench.workspace._lock:
        bench.workspace.connection.set_trace_callback(statements.append)
        try:
            with pytest.raises(KeyError):
                build_briefing(matter, WEB_ACTOR, workspace=bench.workspace, entity_service=service,
                               intake_receipts=IntakeReceipts(bench.workspace))
        finally:
            bench.workspace.connection.set_trace_callback(None)


@pytest.mark.parametrize('one_box', ['0', '1'])
def test_home_save_reload_clear_only_changes_existing_question_buttons(request, monkeypatch, one_box):
    monkeypatch.setenv('CASE_INTELLIGENCE_BRIEFING', '1')
    monkeypatch.setenv('CASE_INTELLIGENCE_ONE_BOX', one_box)
    client, bench, matter, _runtime = request.getfixturevalue('workbench')
    seed(bench, client, matter)
    home, purpose = f'/matters/{matter.slug}/home', f'/matters/{matter.slug}/purpose'
    page = client.get(home)
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^\"]+)"', page.text).group(1)
    def region(html):
        return html.split('<div data-briefing-region', 1)[1].split('class="cockpit-hero"', 1)[0]
    baseline = region(page.text)
    original = BriefingMarkup(page.text).questions
    assert len(original) == 3
    for objective in ('Jordan Sample', 'Unmatched Synthetic', ' \t\r\n', ''):
        current = client.get(purpose).json()
        saved = client.post(purpose, data=dict(csrf_token=token, expected_revision=current['revision'],
            objective=objective, background='Alex Example'))
        assert saved.status_code == 200
        reloaded = client.get(home)
        assert reloaded.status_code == 200
        if objective == 'Jordan Sample':
            assert BriefingMarkup(reloaded.text).questions == [original[i] for i in (2, 0, 1)]
        else:
            assert region(reloaded.text) == baseline
    with bench.workspace._lock:
        for table in ('workbench_answer_job', 'workbench_research_job'):
            assert bench.workspace.connection.execute(
                f'SELECT COUNT(*) FROM {table} WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0
