"""A small synthetic production with independently specified expected Markdown."""
from pathlib import Path

from case_intelligence.production_summary import build_summary
from tests.test_production_summary import source


def test_synthetic_production_markdown():
    entity = dict(entity_id='e1', entity_type='person', display_name='Alex Example', status='suggested')
    mention = dict(document_id='a', source_version_id='v-a', support_token='synthetic-passage',
                   available=True, review_status='suggested', location='Page 1', excerpt='Alex Example visited Example Cooperative.')
    summary = build_summary('Synthetic production', 'm-synthetic', [
        source('a', 'Bates: SYN001\n2034-04-16\nReference: missing.pdf', pages=2, labels=['SYN001']),
        source('b', 'Bates: SYN004\n2034-04-20', message='OCR failed: 1', labels=['SYN004']),
        source('c', state='failed', pages=0, message='Unreadable file'),
    ], entities=[(entity, [mention]),
                 (dict(entity, entity_id='e2', entity_type='organization', display_name='Example Cooperative'), [mention])])
    expected = Path(__file__).parent / 'fixtures/synthetic/production_summary/summary.md'
    assert summary.to_markdown() == expected.read_text()
