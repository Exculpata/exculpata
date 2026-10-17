"""Synthetic deterministic extraction, source grounding and bounded behavior."""
from dataclasses import replace
import csv
import io
import hashlib
import json
from pathlib import Path
import time
import pytest
from pypdf import PdfReader
from case_intelligence.review_bench_v2 import Candidate
from case_intelligence.timeline_builder import build_timeline, TimelineLimits
from case_intelligence.timeline_exports import export_timeline
from case_intelligence.answer_evidence import validated_window_text

ROOT = Path(__file__).resolve().parents[1]

def unit(text, identifier='synthetic-a', page=1, matter='synthetic-matter'):
    return Candidate(matter,identifier,identifier+str(page),identifier+'.txt',page,text,
                     excerpt_digest=hashlib.sha256(text.encode()).hexdigest())

def harbor_units():
    units=[]
    for path in sorted((ROOT/'demo_data/harbor-street').rglob('*')):
        if not path.is_file():
            continue
        texts = [p.extract_text() for p in PdfReader(path).pages] if path.suffix=='.pdf' else [path.read_text(errors='replace')]
        for page,text in enumerate(texts,1):
            units.append(unit(text,path.name,page))
    return units

def test_harbor_golden():
    units=harbor_units()
    result=build_timeline(units).payload()
    assert result==json.loads((ROOT/'tests/snapshots/timeline_harbor_street.json').read_text())
    assert json.dumps(result)==json.dumps(build_timeline(units).payload())
    assert [e['sort_key'] for e in result['entries']]==sorted(e['sort_key'] for e in result['entries'])
    assert result['source_count']==40
    originals={u.document_id:u.text for u in units}
    for entry in result['entries']+result['undated']:
        assert entry['citations'] and entry['date_label'] in entry['citations'][0]['excerpt']
        for citation in entry['citations']:
            assert validated_window_text(originals[citation['source_id']],citation['window'])==citation['excerpt']

def test_conflicts_keep_both_sources_and_do_not_merge_distinct_events():
    a=unit('Alex Example delivered the parcel at Cedar Depot on April 18, 2034.','a')
    b=unit('Alex Example delivered the parcel at Cedar Depot on April 19, 2034.','b')
    c=unit('Riley Demo collected the parcel at Cedar Depot on April 20, 2034.','c')
    result=build_timeline([a,b,c]).payload()
    assert len(result['conflicts'])==1
    assert {e['citations'][0]['source_id'] for e in result['conflicts'][0]['entries']}=={'a','b'}
    assert next(e for e in result['entries'] if e['citations'][0]['source_id']=='c')['conflict_group'] is None

def test_relative_anchors_are_document_local_and_cited():
    result=build_timeline([unit('Record date: 2034-04-18.\n\nThe next morning Alex delivered the parcel.','a'),
                           unit('The next morning Riley collected the parcel.','b')]).payload()
    relative=next(e for e in result['entries'] if e['date_label'].lower()=='the next morning')
    assert relative['sort_key']=='2034-04-19' and len(relative['citations'])==2
    assert len(result['undated'])==1
    ambiguous=build_timeline([unit('2034-04-18 and 2034-04-19.\n\nThe next morning Alex delivered the parcel.')]).payload()
    assert ambiguous['undated'][0]['date_label']=='The next morning'

def test_caps_and_invalid_citation(monkeypatch):
    units=[unit(f'Alex delivered parcel number {i} on 2034-04-18.',str(i)) for i in range(151)]
    assert build_timeline(units).capped
    assert len(build_timeline(units).entries)==150
    assert build_timeline(units,limits=TimelineLimits(units=2)).unit_count==2
    from case_intelligence.workspace_store import WorkspaceProblem
    def invalid(*args):
        raise WorkspaceProblem('Invalid window')
    monkeypatch.setattr('case_intelligence.timeline_builder.validated_window_text',invalid)
    result=build_timeline(units[:1])
    assert result.rejected_count==1 and not result.entries

def test_scope_and_exports():
    with pytest.raises(ValueError):
        build_timeline([unit('2034-04-18'),unit('2034-04-19','b',matter='another')])
    payload=build_timeline([unit('Alex delivered the parcel on April 18, 2034.')]).payload()
    for format in ['json','csv','markdown']:
        body=export_timeline(payload,format)
        assert 'synthetic-a' in body and 'April 18, 2034' in body
    assert 'for attorney review' in export_timeline(payload,'markdown')
    payload['entries'][0]['description']='=SUM(1,2)'
    assert "'=SUM" in export_timeline(payload,'csv')

def test_2500_unit_budget():
    units=[unit('Alex delivered the parcel on April 18, 2034.\n'*100,str(i)) for i in range(2500)]
    started=time.monotonic()
    assert build_timeline(units).capped
    assert time.monotonic()-started < 10

@pytest.mark.parametrize('total', [0, 2499, 2500, 2501, 2700])
def test_passage_coverage_at_cutoff_in_every_export(total):
    units = [unit('No dated event in this synthetic passage.', 'a', page=i+1) for i in range(total)]
    payload = build_timeline(units).payload()
    included = min(total, 2500)
    assert payload['unit_count'] == included
    assert payload['total_unit_count'] == total
    assert payload['capped'] is (total > 2500)
    notice = f'Included {included:,} of {total:,} passages from ready sources in the date scan.'
    assert payload['coverage_notice'].startswith(notice)
    assert ('not a complete chronology' in payload['coverage_notice']) is payload['capped']
    for format in ('csv', 'markdown', 'json'):
        assert payload['coverage_notice'] in export_timeline(payload, format)
    metadata, = list(csv.DictReader(io.StringIO(export_timeline(payload, 'csv'))))
    assert metadata['row_type'] == 'metadata'
    assert metadata['unit_count'] == str(included)
    assert metadata['total_unit_count'] == str(total)
    assert metadata['capped'] == str(payload['capped'])
    assert metadata['notice'] == payload['notice']

def test_coverage_counts_scanned_passages_when_entry_limit_stops_early():
    units = [unit('Alex delivered the parcel on April 18, 2034.', f'{i:04}') for i in range(200)]
    result = build_timeline(units)
    assert result.unit_count == 150 and result.total_unit_count == 200
    assert result.capped and 'Included 150 of 200 passages' in result.coverage_notice

def test_explicit_population_count_and_invalid_count():
    result = build_timeline([unit('No dates.')], total_unit_count=3000)
    assert result.capped and result.total_unit_count == 3000
    assert 'Included 1 of 3,000 passages' in result.coverage_notice
    with pytest.raises(ValueError, match='Total passage count'):
        build_timeline([unit('No dates.')], total_unit_count=0)

def test_eval_questions_and_no_header_conflicts():
    data=json.loads((ROOT/'tests/fixtures/synthetic/workflow_eval/chronology.json').read_text())
    assert '#267' in data['integration_todo'] and 5 <= len(data['questions']) <= 8
    assert len({q['id'] for q in data['questions']})==len(data['questions'])
    assert all(q['question'] and q['expected_answer'] and q['expected_source_ids'] for q in data['questions'])
    corpus='\n'.join(u.text for u in harbor_units())
    assert all(source in corpus for q in data['questions'] for source in q['expected_source_ids'])
    a=unit('Record date: 2034-04-18\nPerson: Alex Example\nObject: Cedar Depot','a')
    b=unit('Record date: 2034-04-19\nPerson: Alex Example\nObject: Cedar Depot','b')
    assert not build_timeline([a,b]).conflicts

def test_retroactive_time_does_not_inherit_record_date():
    result=build_timeline([unit('Record date: 2034-04-19.\n\nAlex said the earlier 09:26 estimate was based on a plan.')])
    assert any(e['date_label']=='09:26 ' for e in result.undated)

def test_changed_anchor_invalidates_entry_identity():
    relative=unit('The next morning Alex Example delivered the parcel.','a',2)
    old=build_timeline([unit('Record date: 2034-04-18.','a'),relative])
    new=build_timeline([unit('Record date: 2034-04-19.','a'),relative])
    assert next(e['entry_id'] for e in old.entries if e['date_label']=='The next morning') != next(e['entry_id'] for e in new.entries if e['date_label']=='The next morning')

def test_negation_and_number_are_part_of_event_identity():
    texts=['Alex delivered parcel 1 to Cedar Depot on 2034-04-18.',
           'Alex delivered parcel 2 to Cedar Depot on 2034-04-19.',
           'Alex did not deliver parcel 1 to Cedar Depot on 2034-04-19.']
    assert not build_timeline([unit(t,str(i)) for i,t in enumerate(texts)]).conflicts

def test_duplicate_date_and_time_do_not_conflict_across_sources():
    text='Alex Example delivered the parcel on April 18, 2034 at 14:05.'
    assert not build_timeline([unit(text,'a'),unit(text,'b')]).conflicts

def test_ranges_with_same_start_but_different_end_conflict():
    result=build_timeline([unit('Alex Example inspected the parcel between April 16–18, 2034.','a'),
                           unit('Alex Example inspected the parcel between April 16–19, 2034.','b')])
    assert len(result.conflicts)==1

@pytest.mark.parametrize('relative,expected', [('The next morning','2034-04-19'),
    ('The following day','2034-04-19'), ('The previous day','2034-04-17')])
def test_relative_day_composes_with_clock_and_keeps_anchor_citation(relative, expected):
    result = build_timeline([unit('Record date: April 18, 2034.', 'a', 1),
        unit(relative+' at 09:00 Alex Example delivered the parcel.', 'a', 2)])
    clock = next(e for e in result.entries if e['precision']=='minute')
    day = next(e for e in result.entries if e['date_label']==relative)
    assert day['sort_key'] == expected
    assert clock['sort_key'] == expected+'T09:00'
    assert {c['locator']['page'] for c in clock['citations']} == {1, 2}

@pytest.mark.parametrize('text', [
    '2034-04-18 or 2034-04-19. The next morning at 09:00 Alex delivered the parcel.',
    '2034-04-18. The next morning or the previous day at 09:00 Alex delivered the parcel.',
    '2034-04-18. Alex recalled the next morning at 09:00 when describing the parcel.',
    'On April 18, 2034 at 09:00 and the next morning at 10:00 Alex delivered parcels.',
    '9999-12-31. The next morning at 09:00 Alex delivered the parcel.',
    '0001-01-01. The previous day at 09:00 Alex delivered the parcel.',
])
def test_ambiguous_or_out_of_range_relative_clock_stays_unresolved(text):
    result = build_timeline([unit(text)])
    assert any(e['precision']=='minute' for e in result.undated)
    assert not any(e['precision']=='minute' for e in result.entries)
