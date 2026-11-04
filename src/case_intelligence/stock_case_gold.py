"""Seeded fictional stock-ownership facts for evaluation, without I/O."""
import calendar
import random
from datetime import date
from fractions import Fraction

from .fraud_case_gold import FIRST, LAST

COMPANIES = ('Orbel Spindle Works', 'Velkin Thimble Holdings')
BROKERS = ('Tessil Sprig Brokerage', 'Zindle Shuttle Brokerage')


def build_case(seed=7):
    rng = random.Random(seed)
    names = [f'{a} {b}' for a in FIRST for b in LAST]
    rng.shuffle(names)
    entities = [dict(entity_id=f'person-{i}', canonical_name=names[10 + i],
                     entity_type='person', role=role)
                for i, role in enumerate(('subject', 'spouse', 'associate'))]
    entities += [dict(entity_id=role, canonical_name=name, entity_type='organization', role=role)
                 for name, role in zip(COMPANIES, ('issuer', 'controlled_llc'))]
    entities[-1]['controller_entity_id'] = 'person-0'
    accounts = [dict(account_id=f'stk-{i}', broker_id=BROKERS[i % 2],
                     owner_entity_ids=owners, authorized_trader='person-0' if i < 4 else 'person-2')
                for i, owners in enumerate((['person-0'], ['person-0', 'person-1'],
                                             ['person-1'], ['controlled_llc'], ['person-2']))]
    unit = rng.randint(1, 9)
    history = [dict(effective=effective, filed=filed, shares=n * unit, event=event, split=ratio)
               for effective, filed, n, event, ratio in (
                   ('2024-01-01', '2024-01-01', 1000000, 'initial', '1/1'),
                   ('2024-04-01', '2024-04-01', 1200000, 'issuance', '1/1'),
                   ('2024-09-01', '2024-09-01', 800000, 'buyback', '1/1'),
                   ('2025-01-01', '2025-01-01', 1600000, 'split', '2/1'),
                   ('2025-03-01', '2025-04-15', 2000000, 'late_issuance', '1/1'))]
    months = [date(2024 + m // 12, m % 12 + 1, 1) for m in range(18)]
    trades = []
    def trade(day, account, amount):
        trades.append(dict(trade_id=f'st-{len(trades):03}', date=day,
                           account_id=account, shares=amount * unit))
    for m, month in enumerate(months):
        for i, account in enumerate(accounts):
            amount = (24000, 24000, 22000, 20000, 15000)[i] if m == 0 else (100 if m % 2 else -100)
            trade(month.replace(day=5).isoformat(), account['account_id'], amount)
        if m < 15:
            for day, amount in ((10, 10), (12, -10)):
                trade(month.replace(day=day).isoformat(), 'stk-0', amount)
    for i in range(4):
        trade('2024-10-15', f'stk-{i}', -6000)
    trades.sort(key=lambda t: (t['date'], t['trade_id']))
    statements = []
    for month in months:
        day = month.replace(day=calendar.monthrange(month.year, month.month)[1]).isoformat()
        for account in accounts:
            aid = account['account_id']
            balance = sum((Fraction(t['shares']) * (2 if t['date'] < '2025-01-01' <= day else 1)
                           for t in trades if t['account_id'] == aid and t['date'] <= day), Fraction(0))
            statements.append(dict(date=day, account_id=aid, shares=int(balance),
                                   trade_ids=[t['trade_id'] for t in trades
                                              if t['account_id'] == aid and t['date'][:7] == day[:7]]))
    associate_orders = {t['date'][:7]: t['shares'] for t in trades if t['account_id'] == 'stk-4'}
    communications = [dict(kind=kind, date=months[i % 18].replace(day=4).isoformat(),
                           sender='person-0', recipient='person-2',
                           text=(f"Order {associate_orders[months[i % 18].isoformat()[:7]]:+d} shares in stk-4 tomorrow. Revision {i}." if kind == 'email'
                                 else f'Confirm receipt of schedule revision {i}.'),
                           attachment=f'schedule-{i:03}.csv' if i % 11 == 0 else None)
                      for kind, count in (('email', 110), ('text', 30)) for i in range(count)]
    wires = [dict(wire_id=f'wire-{i:03}', date=months[i].isoformat(),
                  from_account_id=f'stk-{i % 5}', to_account_id=f'stk-{(i + 1) % 5}',
                  amount=f'{10000 * unit}.00', currency='XCU') for i in range(15)]
    interviews = [dict(date=months[i].replace(day=20).isoformat(), speaker='person-2',
                       text='I never received trading instructions.' if i % 3 == 0
                       else 'I received a trading schedule before placing the monthly order.') for i in range(12)]
    filings = [dict(share_count=row, kind=row['event'], date=row['filed']) for row in history]
    filings += [dict(share_count=max((r for r in history if r['effective'] <= day and r['filed'] <= day),
                                    key=lambda r: r['effective']), kind='quarterly_report', date=day)
                for day in ('2024-03-31', '2024-06-30', '2024-09-30', '2024-12-31', '2025-03-31', '2025-06-30')]
    filings += [dict(share_count=history[2], kind='holder_report', date='2024-09-30',
                     account_id=a['account_id']) for a in accounts[:4]]
    return dict(matter_id='stock-ownership', symbol='ORBL-7', seed=seed, entities=entities,
                accounts=accounts, window=('2024-01-01', '2025-06-30'),
                threshold_window=('2024-09-01', '2024-10-14'), buyback_date='2024-09-01',
                combined_account_ids=[a['account_id'] for a in accounts[:4]], share_history=history,
                trades=trades, statements=statements, filings=filings, wires=wires,
                communications=communications, interviews=interviews,
                productions=[dict(first=i * 68 + 1, last=min((i + 1) * 68, 405)) for i in range(6)])
