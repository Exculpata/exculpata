"""Generated synthetic productions exercise the summary engine and its exports."""
import re
import pytest
from case_intelligence import pilot_uploads
from case_intelligence.bates_gaps import compute_bates_gaps
from case_intelligence.timeline_exports import md_inline
from tests.test_bates_gaps_oracle import generate
from tests.test_workflow_quality_handoff import pdf_bytes
from tests.test_production_summary import production_client, production, markdown, payload, expected, fact_payload, link


@pytest.mark.parametrize('seed', range(500))
def test_T3_generated_production(production_client, monkeypatch, seed):
    generated, truth, _owners, aliases, coordinates, _clean = generate(seed)
    counts = {d.document_id + '.pdf': d.page_count or 0 for d in generated}
    original = pilot_uploads.PilotStore._extract
    def extracted(store, document, *args, **kwargs):
        original(store, document, *args, **kwargs)
        if document.relative_path in counts:
            document.page_count = counts[document.relative_path]
    monkeypatch.setattr(pilot_uploads.PilotStore, '_extract', extracted)
    files = [(d.document_id + '.pdf', pdf_bytes(['Synthetic generated document.']), 'application/pdf') for d in generated]
    rows = [(d.document_id + '.pdf', start, end or '') for d in generated if d.labels_complete
            for start, end in d.labels]
    p = production(production_client, files, rows)
    text = markdown(p)
    assert payload(p)['facts'] == fact_payload(p), seed
    facts = compute_bates_gaps(expected(p))
    lines = [line for line in text.splitlines() if line.startswith('- Missing Bates ')]
    assert len(lines) == sum(f.kind == 'missing' for f in facts), seed
    for line in lines:
        match = re.match(r'- Missing Bates ([A-Za-z]+)(\d+)(?: to ([A-Za-z]+)(\d+))? ', line)
        assert match, (seed, line)
        series = aliases.get(match[1], match[1])
        absent = {n + offset for hidden, (rendered, offset) in coordinates.items()
                  if rendered == series for n in truth[hidden][1]}
        assert set(range(int(match[2]), int(match[4] or match[2]) + 1)) <= absent, (seed, line)
    for fact in facts:
        if fact.kind == 'possible':
            lines = [line for line in text.splitlines() if line.startswith('- ' + md_inline(fact.text) + ' ')]
            assert len(lines) == 1, seed
            for identity, version in fact.cited_ids:
                doc = next(d for d in p.docs if d.document_id == identity)
                assert link(p, doc) in lines[0] and version in lines[0], seed
