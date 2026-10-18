from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / '.github' / 'ISSUE_TEMPLATE'
REQUIRED_FIELDS = {
    'problem': 'textarea',
    'restate_goal': 'textarea',
    'level': 'dropdown',
    'regression': 'textarea',
    'acceptance': 'textarea',
    'size': 'dropdown',
}


@pytest.mark.parametrize('path', sorted(TEMPLATES.glob('*.yml')), ids=lambda p: p.name)
def test_issue_template_yaml_loads_as_a_mapping(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert isinstance(document, dict), f'{path.name} must contain a YAML mapping'
    if path.name == 'config.yml':
        assert isinstance(document['blank_issues_enabled'], bool)
    else:
        assert document['name'] and document['description']
        assert isinstance(document['body'], list) and document['body']


def agent_form() -> dict:
    path = TEMPLATES / 'agent_task.yml'
    assert path.is_file(), 'Add .github/ISSUE_TEMPLATE/agent_task.yml'
    return yaml.safe_load(path.read_text(encoding='utf-8'))


@pytest.mark.parametrize('field_id,field_type', REQUIRED_FIELDS.items())
def test_agent_task_requires_scoped_evidence(field_id: str, field_type: str) -> None:
    matches = [field for field in agent_form()['body'] if field.get('id') == field_id]
    assert len(matches) == 1, f'agent_task.yml needs exactly one {field_id} field'
    field = matches[0]
    assert field['type'] == field_type
    assert field['validations']['required'] is True, f'{field_id} must be required'
    assert field['attributes']['label']


def test_agent_task_preserves_fix_hierarchy_and_kickoff_process() -> None:
    form = agent_form()
    assert form['labels'] == ['agent-task']
    fields = {field['id']: field for field in form['body'] if 'id' in field}
    assert fields['level']['attributes']['options'] == [
        'Code (types, invariants, boundaries)',
        'Static check or CI test',
        'AGENTS.md rule',
        'Human review',
    ]
    assert fields['size']['attributes']['options'] == ['S', 'M', 'L']
    assert fields['why_level']['validations']['required'] is False
    kickoff = fields['restate_goal']['attributes']['description']
    for requirement in ('Before coding', 'first kickoff comment', 'matches this issue',
                        'resolves any mismatch'):
        assert requirement in kickoff
    regression = fields['regression']['attributes']['description']
    assert 'synthetic test' in regression and 'commit it must fail on' in regression


@pytest.mark.parametrize('path,target', [
    ('AGENTS.md', 'docs/IDEAL.md'),
    ('.github/ISSUE_TEMPLATE/agent_task.yml',
     'https://github.com/Exculpata/exculpata/blob/main/docs/IDEAL.md'),
])
def test_agent_guidance_links_existing_ideal_contract(path: str, target: str) -> None:
    text = (ROOT / path).read_text(encoding='utf-8')
    if path.endswith('.yml'):
        form = yaml.safe_load(text)
        text = '\n'.join(field['attributes']['value'] for field in form['body']
                         if field['type'] == 'markdown')
    links = re.findall(r'\[[^\]]+\]\(([^)]+)\)', text)
    assert target in links, f'{path} must link directly to docs/IDEAL.md'
    assert (ROOT / 'docs' / 'IDEAL.md').is_file(), 'The linked contract must exist'
    assert 'https://github.com/Exculpata/exculpata/issues/305' not in links
    assert 'dependency gate' not in text


def test_agent_guidance_links_standards_and_preserves_safeguards() -> None:
    guide = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    assert guide.startswith('# Exculpata contributor judgment rules\n')
    assert '## Correct the environment, not the agent' in guide
    assert '| Rule | Enforced by |' in guide
    assert 'Name that commit in the PR description' in guide
    assert 'linked issue and an expiry' in guide
    assert 'code starts only after they match' in guide
    standards = ROOT / 'CODING_STANDARDS.md'
    assert standards.is_file(), 'Move detailed guidance into CODING_STANDARDS.md'
    assert '(CODING_STANDARDS.md)' in guide
    form_text = (TEMPLATES / 'agent_task.yml').read_text(encoding='utf-8')
    for text in (guide, form_text):
        assert 'CODING_STANDARDS.md' in text
    detailed = standards.read_text(encoding='utf-8')
    assert '.venv/bin/exculpata-workbench --host 127.0.0.1' in detailed
    for safeguard in ('synthetic fixtures only', 'pre-push publication check',
                      'QUALITY_GATES.md#review-policy', 'backup', 'restore drill',
                      'Python 3.12', 'scripts/bootstrap-dev.py'):
        assert safeguard in detailed


def test_agent_kickoff_is_verified_separately_from_the_authors_form_entry() -> None:
    fields = {field['id']: field for field in agent_form()['body'] if 'id' in field}
    kickoff = fields['restate_goal']['attributes']
    assert "Issue author's goal" in kickoff['label']
    assert 'This form entry does not replace that comment' in kickoff['description']
    guide = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    pr = (ROOT / '.github/pull_request_template.md').read_text(encoding='utf-8')
    assert 'Kickoff permalink:' in pr
    for text in (guide, pr):
        assert "comment's author" in text
        assert 'first implementation commit' in text
        assert 'blocks readiness' in text
    assert 'human review verifies the actual evidence' in guide


def test_neutral_language_rule_has_a_durable_enforcement_contract() -> None:
    guide = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    assert '## Keep public language neutral' in guide
    for text in (guide, (TEMPLATES / 'agent_task.yml').read_text(encoding='utf-8')):
        for requirement in ('neutral criminal-defense terminology',
                            'private organizational affiliations', 'synthetic examples only'):
            assert requirement in text
    rows = [line for line in guide.splitlines() if line.startswith('| Keep public language neutral')]
    assert len(rows) == 1
    assert 'installed publication guard' in rows[0]
    assert 'public text outside Git' in rows[0]


def test_hash_locked_test_environment_includes_the_yaml_parser() -> None:
    lock = (ROOT / 'requirements/application-test.lock').read_text(encoding='utf-8')
    match = re.search(r'^pyyaml==6\.[^\n]+\n(?:[ \t]+[^\n]+\n)+', lock, re.MULTILINE)
    assert match, 'Regenerate application-test.lock when adding the YAML parser'
    assert '--hash=sha256:' in match.group()
