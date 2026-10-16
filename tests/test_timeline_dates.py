import pytest
from case_intelligence.timeline_dates import extract_dates, resolve_mention

@pytest.mark.parametrize('text,start,end,precision,kind', [
 ('2034-04-18','2034-04-18',None,'day','exact'),
 ('04/18/2034','2034-04-18',None,'day','exact'),
 ('April 18, 2034','2034-04-18',None,'day','exact'),
 ('18 April 2034','2034-04-18',None,'day','exact'),
 ('April 16–18, 2034','2034-04-16','2034-04-18','day','range'),
 ('April 16–18',None,None,'day','range'),
 ('about April 18, 2034','2034-04-18',None,'day','approximate'),
 ('early April 18, 2034','2034-04-18',None,'day','approximate'),
 ('late April 18, 2034','2034-04-18',None,'day','approximate'),
 ('14:05','T14:05',None,'minute','exact'),
 ('2:05 p.m.','T14:05',None,'minute','exact'),
 ('12:05 a.m.','T00:05',None,'minute','exact'),
 ('around 2:05 p.m.','T14:05',None,'minute','approximate'),
 ('between 2 and 3 p.m.','T14:00','T15:00','hour','range'),
 ('04/18/34',None,None,'day','exact'),
 ('the next morning',None,None,'day','unresolved_relative'),
])
def test_formats(text,start,end,precision,kind):
    mention, = extract_dates(text)
    assert (mention.start_date,mention.end_date,mention.precision,mention.kind)==(start,end,precision,kind)
    assert text[mention.start:mention.end] == mention.text

@pytest.mark.parametrize('text',['2034-02-30','February 30, 2034','02/30/2034','25:00','14:99','13 p.m.','April 18–16, 2034'])
def test_invalid(text):
    assert not extract_dates(text)

def test_relative_resolution():
    mention, = extract_dates('the next morning')
    assert resolve_mention(mention,None)==mention
    resolved=resolve_mention(mention,'2034-04-18')
    assert resolved.start_date=='2034-04-19' and resolved.kind=='approximate'

@pytest.mark.parametrize('text,value,precision',[('early April 2034','2034-04-01','month'),('late 2034','2034-01-01','year')])
def test_month_and_year_precision(text,value,precision):
    mention,=extract_dates(text)
    assert mention.start_date==value and mention.precision==precision and mention.kind=='approximate'

def test_public_extractor_resolves_same_document_relative_date():
    mentions=extract_dates('April 18, 2034. The next morning the parcel moved.')
    assert mentions[-1].start_date=='2034-04-19'
    assert extract_dates('April 18, 2034 or April 19, 2034. The next morning it moved.')[-1].kind=='unresolved_relative'

@pytest.mark.parametrize('relative,expected', [('The next morning','2034-04-19'),
    ('The following day','2034-04-19'), ('The previous day','2034-04-17')])
def test_relative_day_clock_composition(relative, expected):
    mentions = extract_dates('April 18, 2034. '+relative+' at 09:00 Alex delivered the parcel.')
    assert mentions[-1].start_date == expected+'T09:00'
    assert mentions[-2].start_date == expected

@pytest.mark.parametrize('text', ['9999-12-31. The next morning at 09:00 it moved.',
    '0001-01-01. The previous day at 09:00 it moved.'])
def test_relative_calendar_bounds_remain_unresolved(text):
    mentions = extract_dates(text)
    assert mentions[-2].start_date is None
    assert mentions[-1].start_date == 'T09:00'
