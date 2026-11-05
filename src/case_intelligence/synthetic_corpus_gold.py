"""Seeded, streaming synthetic artifacts for local-first evals only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
from contextlib import ExitStack
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from .fraud_case_gold import build_case
from .defense_eval import suite_fingerprint

SCALES = dict(ci=0, small=2000, medium=50000, large=250000, xl=1000000)
MEDIA_TYPES = dict(csv='text/csv', pdf='application/pdf', eml='message/rfc822',
                   mbox='application/mbox', txt='text/plain')
COUNTS = {'10k': 10000, '100k': 100000, '1m': 1000000}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def pdf_bytes(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})})})
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream = DecodedStreamObject()
    stream.set_data(f'BT /F1 10 Tf 40 700 Td ({escaped}) Tj ET'.encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def mail_bytes(text, sid, attachment=False):
    message = EmailMessage(policy=SMTP)
    message['From'], message['To'] = 'sender@example.invalid', 'reader@example.invalid'
    message['Date'], message['Message-ID'] = 'Wed, 03 Apr 2024 14:00:00 +0000', f'<{sid}@example.invalid>'
    message['Subject'] = 'Synthetic transfer schedule'
    message.set_content(text)
    if attachment:
        child = EmailMessage(policy=SMTP)
        child.set_content('Synthetic enclosed notice.')
        child.add_attachment(b'The attachment records approval before txn-00000.',
                             maintype='text', subtype='plain', filename=str(Path('attachment.txt')))
        child.set_boundary('synthetic-child-boundary')
        message.add_attachment(child)
        message.set_boundary('synthetic-parent-boundary')
    return message.as_bytes()


def generate(out: Path, *, seed=7, scale='ci', filler_chunks=None):
    """Create a fresh corpus; never replace an existing output directory."""
    key = build_case(seed)
    planted = len(key['transactions'])
    count = (COUNTS[scale] - planted if scale in COUNTS else SCALES[scale]) if filler_chunks is None else filler_chunks
    if type(count) is not int or count < 0:
        raise ValueError('filler-chunks must be a nonnegative integer')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'artifacts').mkdir()
    (out / 'passages').mkdir()
    header = dict(answer_key_version='1.0.0', corpus_id=f'cedar-rill-seed-{seed}', seed=seed, scale=scale)
    suite = dict(schema_version='1', suite_id='defense-workflow', suite_version='2', synthetic_only=True,
                 description='Synthetic fraud measured recall fixture.', passages=[], questions=[])
    files, byte_count = 5, 0
    with ExitStack() as stack:
        handles = {name: stack.enter_context((out / f'{name}.jsonl').open('w', encoding='utf-8', newline='\n'))
                   for name in ('sources', 'chunks', 'gold')}
        def emit(name, value):
            handles[name].write(canonical(value) + '\n')
        def gold(kind, value):
            emit('gold', dict(header, kind=kind, value=value))
        def artifact(sid, ext, text, *, include=False):
            nonlocal files, byte_count
            data = (pdf_bytes(text) if ext == 'pdf' else mail_bytes(text, sid, sid == 'notice')
                    if ext in {'eml', 'mbox'} else text.encode('utf-8'))
            if ext == 'mbox':
                data = b'From synthetic Wed Apr  3 14:00:00 2024\n' + data + b'\n'
            path = f'artifacts/{sid}.{ext}'
            (out / path).write_bytes(data)
            files += 1
            byte_count += len(data)
            source = dict(source_id=sid, version=1, version_hash=hashlib.sha256(data).hexdigest(),
                          path=path, type=ext, media_type=MEDIA_TYPES[ext], sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                          restricted=False, processed=True, family='mail-1' if sid in {'notice', 'attachment'} else None,
                          parent_source_id='notice' if sid == 'attachment' else None)
            emit('sources', source)
            start = 0 if ext == 'pdf' else data.index(text.encode())
            chunk = dict(chunk_id=sid + '-v1-p1', source_id=sid, version=1, text=text,
                         restricted=False, locator=({'sheet': 'ledger', 'row': 2} if ext == 'csv' else
                         {'page': 1, 'region_box': [0, 0, 1, 1]}), start=start, end=start + len(text.encode()))
            if sid == 'identity':
                chunk['mentions'] = [dict(mention_id=f'mention-{i:03}', source_id=sid,
                    chunk_id=chunk['chunk_id'], surface=e['canonical_name'], start=text.index(e['canonical_name']),
                    end=text.index(e['canonical_name']) + len(e['canonical_name'])) for i, e in enumerate(key['entities'])]
            emit('chunks', chunk)
            if include:
                relative = f'passages/{sid}.txt'
                encoded = text.encode()
                (out / relative).write_bytes(encoded)
                files += 1
                byte_count += len(encoded)
                suite['passages'].append(dict(passage_id=chunk['chunk_id'], matter_id='fraud', path=relative,
                    sha256=hashlib.sha256(encoded).hexdigest(), evidence_kind='document', locator=canonical(chunk)))
        def transaction(row):
            stream = io.StringIO(newline='')
            fields = [field for field in row if field != 'locator']
            writer = csv.DictWriter(stream, fields, lineterminator='\n')
            writer.writeheader()
            writer.writerow({field: row[field] for field in fields})
            artifact(row['transaction_id'], 'csv', stream.getvalue())
            gold('transactions', row)
        for kind, values in key.items():
            if kind not in {'documents', 'transactions'}:
                for value in values:
                    gold(kind, value)
        for row in key['transactions']:
            transaction(row)
        for sid, ext, text in key['documents']:
            if sid == 'identity':
                text += '\n' + '\n'.join(e['canonical_name'] for e in key['entities'])
            artifact(sid, ext, text, include=True)
        for question in key['questions']:
            supports = question['decisive_passages']
            suite['questions'].append(dict(question_id=question['question_id'], matter_id='fraud',
                type=question['question_type'], question=question['text'], expect_abstention=question['expect_abstention'],
                required_facts=[dict(fact_id=p['fact_id'], answer_text=p['support_text'],
                    supports=[dict(passage_id=p['chunk_id'], support_text=p['support_text'])]) for p in supports],
                forbidden_facts=['The first transfer occurred at 15:00 UTC.'] if question['question_type'] == 'contradiction' else [],
                near_misses=['claim-v1-p1'] if question['question_type'] != 'contradiction' else ['thread-v1-p1']))
        rng = random.Random(seed)
        for i in range(count):
            tid = f'noise-{i:07}'
            transaction(dict(transaction_id=tid, posted_at='2024-02-01T12:00:00Z',
                amount=f'{rng.randint(1, 500)}.00', currency='XCU', from_account_id='acct-055',
                to_account_id='acct-056', memo=f'Ordinary supplies {rng.randrange(1000000)}',
                locator=dict(source_id=tid, sheet='ledger', row=2)))
    suite['fingerprint'] = suite_fingerprint(suite)
    (out / 'suite.json').write_text(canonical(suite) + '\n', encoding='utf-8')
    manifest = dict(header, generator_version='1', synthetic=True, requested_noise=count,
                    transactions=planted + count, files=files, artifact_bytes=byte_count)
    (out / 'manifest.json').write_text(canonical(manifest) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--scale', choices=(*SCALES, *COUNTS), default='ci')
    parser.add_argument('--filler-chunks', type=int)
    args = parser.parse_args()
    print(canonical(generate(args.out, seed=args.seed, scale=args.scale, filler_chunks=args.filler_chunks)))


if __name__ == '__main__':
    main()
