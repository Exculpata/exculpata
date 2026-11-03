"""Fixed fictional namespace and planted fraud case for evals only."""
from __future__ import annotations

import random

FIRST = ('Mara', 'Ivo', 'Sela', 'Neri', 'Tavi', 'Olin', 'Vela', 'Zori')
LAST = ('Venn', 'Quorl', 'Tessil', 'Brindlew', 'Zindle')
COMPANIES = tuple(f'{word} Works' for word in (
    'Cedar Rill', 'Quorl Loom', 'Zindle Arc', 'Tessil Fern', 'Velkin Reed',
    'Orbel Moss', 'Neral Spool', 'Tavik Glen', 'Sovel Flint', 'Ruvin Bloom',
    'Pellin Vale', 'Dovral Ink', 'Kerril Mist', 'Weskin Clay', 'Fennel Coil',
    'Zorbel Tile', 'Istral Paper', 'Melvor Yarn', 'Belkin Glass', 'Arvel Stone'))
BANKS = ('bank-cinder', 'bank-quorl', 'bank-zindle')
PLACES = ('Cedar Rill yard', 'Quorl Annex', 'Zindle Hall')
PATTERNS = ('layering', 'round_trip', 'structuring', 'fan_in', 'fan_out',
            'rapid_in_out', 'no_business_purpose', 'dormant_then_active')


def build_case(seed: int) -> dict:
    """Return the bounded planted core; noise never mutates its identities."""
    rng = random.Random(seed)
    names = [f'{a} {b}' for a in FIRST for b in LAST]
    rng.shuffle(names)
    entities = [dict(entity_id=f'ent-{i:03}', entity_type='person' if i < 40 else 'organization',
                     canonical_name=name, scheme_role=('participant' if i < 10 else
                     'nonparticipant' if i < 40 else 'shell' if i < 55 else 'operating_business'),
                     mention_ids=[f'mention-{i:03}'])
                for i, name in enumerate(names + list(COMPANIES))]
    accounts = [dict(account_id=f'acct-{i:03}', owner_entity_ids=[f'ent-{i:03}'],
                     bank_id=BANKS[i % len(BANKS)], canonical_number=f'771204{i:04}',
                     display_variants=[f'****{i:04}', f'{i:04}'],
                     opened_at='2024-01-08T00:00:00Z', closed_at=None) for i in range(60)]
    edges = (((0, 1), (1, 2), (2, 3)), ((3, 4), (4, 5), (5, 3)),
             ((0, 6),) * 3, ((0, 7), (1, 7), (2, 7)), ((7, 0), (7, 1), (7, 2)),
             ((6, 8), (8, 0)), ((0, 40),), ((9, 1),))
    key = dict(entities=entities, accounts=accounts, transactions=[], money_paths=[],
               timeline_links=[], anomalies=[], facts=[], questions=[], dashboard_panels=[])
    for pattern, pairs in zip(PATTERNS, edges):
        ids = []
        for a, b in pairs:
            n = len(key['transactions'])
            tid = f'txn-{n:05}'
            ids.append(tid)
            key['transactions'].append(dict(transaction_id=tid, posted_at=f'2024-04-03T14:{n:02}:00Z',
                amount='9000.00', currency='XCU', from_account_id=f'acct-{a:03}',
                to_account_id=f'acct-{b:03}', memo='No business purpose supplied' if b == 40 else 'Synthetic transfer',
                locator=dict(source_id=tid, sheet='ledger', row=2)))
        key['money_paths'].append(dict(path_id=f'path-{pattern}', pattern_type=pattern,
            hop_transaction_ids=ids, origin_entity_id=f'ent-{pairs[0][0]:03}',
            destination_entity_id=f'ent-{pairs[-1][1]:03}'))
        key['anomalies'].append(dict(anomaly_id=f'anom-{pattern}', type=pattern, subject_ids=ids,
            source_ids=ids, explanation=f'Planted synthetic {pattern} reference label.'))
    person = names[0]
    place = rng.choice(PLACES)
    key['documents'] = [
        ('identity', 'txt', f'{person} uses the nickname Rill. Meeting place: {place}.'),
        ('notice', 'eml', 'Send txn-00000 after the notice at 14:00 UTC.'),
        ('claim', 'eml', 'The first transfer occurred at 15:00 UTC.'),
        ('correction', 'pdf', 'The first transfer occurred at 14:00 UTC.'),
        ('attachment', 'txt', 'The attachment records approval before txn-00000.'),
        ('transcript', 'txt', 'Speaker 2: I approved the transfer before the notice.'),
        ('thread', 'mbox', 'Fwd: transfer schedule; the draft time was 15:00 UTC.'),
    ] + [(f'duplicate-{i}', 'eml', f'Re: transfer schedule draft 15:00 UTC, revision {i}.') for i in range(6)]
    for i, (typ, doc, text) in enumerate((
        ('fact_lookup', 'identity', f'{person} uses the nickname Rill.'),
        ('chronology', 'notice', 'Send txn-00000 after the notice at 14:00 UTC.'),
        ('contradiction', 'correction', 'The first transfer occurred at 14:00 UTC.'),
        ('impeachment', 'attachment', 'The attachment records approval before txn-00000.'),
        ('media_transcript', 'transcript', 'Speaker 2: I approved the transfer before the notice.'),
        ('not_in_record', None, 'What did the absent audit conclude?'))):
        fid = f'fact-{i}'
        prompt = (f'Which nickname belongs to {person}?', 'What instruction preceded txn-00000?',
                  'Which time does the corrected first-transfer record give?',
                  'What does the approval attachment record?', 'What did Speaker 2 say about approval?',
                  text)[i]
        key['questions'].append(dict(question_id=f'q-{i}', question_type=typ, text=prompt,
            support=[doc + '-v1-p1'] if doc else [],
            contradict=['claim-v1-p1'] if typ == 'contradiction' else [],
            expected_coverage='answerable' if doc else 'no_result',
            required_fact_ids=[fid] if doc else [], forbidden_fact_ids=[], expect_abstention=not doc,
            decisive_passages=[dict(fact_id=fid, chunk_id=doc + '-v1-p1', source_id=doc,
                                   support_text=text)] if doc else []))
        if doc:
            key['facts'].append(dict(fact_id=fid, fact_type=typ, value=text,
                                     subject_ids=['ent-000'], source_ids=[doc]))
    key['anomalies'].append(dict(anomaly_id='anom-time', type='inconsistent_statement',
        subject_ids=['notice', 'claim'], source_ids=['notice', 'claim', 'correction'],
        explanation='The draft time conflicts with the instruction and corrected record.'))
    key['timeline_links'] = [dict(link_id='link-0', event_a_id='notice', event_b_id='txn-00000',
                                  relation='enables', source_ids=['notice', 'txn-00000'])]
    key['dashboard_panels'] = [dict(panel_id='flow', title='Account flow', fact_ids=['fact-1'],
                                   display_values={'hop_count': 3}, source_ids=['notice', 'txn-00000'])]
    return key
