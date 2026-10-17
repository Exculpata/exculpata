"""Authored wording and each export must remain neutral and visibly qualified."""
from dataclasses import asdict
import csv
import io
import json
import pytest

from case_intelligence.review_candidate_exports import export_payload
from case_intelligence.review_candidates import FORMAT, NOTICE, prohibited_phrases, detect
from tests.test_review_candidates import ROOT, PASSAGES, unit


def payload():
    rows=[asdict(c) for c in detect([unit(r['text'],r['id']) for r in PASSAGES])]
    return dict(notice=NOTICE,format=FORMAT,candidates=rows,dropped=0,capped=False,unit_count=len(PASSAGES))


def test_templates_rules_docs_and_exports_have_no_conclusions():
    for path in ['src/case_intelligence/templates/workbench_review_candidates.html',
                 'src/case_intelligence/data/review_candidate_rules.json','docs/REVIEW_CANDIDATES.md']:
        assert not prohibited_phrases((ROOT/path).read_text()), path
    for kind in ('json','markdown','csv'):
        body=export_payload(payload(),kind)
        wording=body
        if kind=='json':
            def strings(value):
                if isinstance(value,str): return [value]
                if isinstance(value,dict): return [s for v in value.values() for s in strings(v)]
                if isinstance(value,list): return [s for v in value for s in strings(v)]
                return []
            wording=' '.join(strings(json.loads(body)))
        assert NOTICE in body and not prohibited_phrases(wording)
        if kind=='json': assert next(iter(json.loads(body)))=='notice'
        elif kind=='csv': assert next(csv.reader(io.StringIO(body)))==[NOTICE]
        else: assert body.startswith(NOTICE)


def test_every_prohibited_term_is_detected_case_insensitively():
    for phrase in ('is Brady','is Giglio','Brady material','Brady violation','must be disclosed',
                   'required to disclose','withheld','suppressed','exculpatory evidence','proves',
                   'establishes','guilty','innocent','lied'):
        assert prohibited_phrases(phrase.upper()),phrase
    assert not prohibited_phrases(NOTICE)


def test_export_injection_protection_and_original_source_attribution():
    data=payload(); data['candidates']=data['candidates'][:1]; row=data['candidates'][0]
    row['citation'].update(source_name='=1+1',excerpt='<script>synthetic</script>\n# Heading',location=' @SUM(A1)')
    markdown=export_payload(data,'markdown')
    assert '<script>' not in markdown and '\\# Heading' in markdown and 'Original passage:' in markdown
    csv_rows=list(csv.reader(io.StringIO(export_payload(data,'csv'))))
    assert csv_rows[3][7]=="'=1+1" and csv_rows[3][8].startswith("'")
    original='The witness said the officer lied.'
    row['citation']['excerpt']=original
    assert json.loads(export_payload(data,'json'))['candidates'][0]['citation']['excerpt']==original


@pytest.mark.parametrize('value',['=1+1','+1','-1','@SUM(A1)',' \t=1+1','\tSynthetic','\rSynthetic','\nSynthetic','  \tSynthetic','  \rSynthetic'])
def test_csv_guards_formula_and_leading_control_cells(value):
    data=payload(); data['candidates']=data['candidates'][:1]
    data['candidates'][0]['citation']['source_name']=value
    rows=list(csv.reader(io.StringIO(export_payload(data,'csv'))))
    assert rows[3][7]=="'"+value
