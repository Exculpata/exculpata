"""Stream the fictional stock case to native-text documents and page passages."""
import argparse
import hashlib
import io
import json
from contextlib import ExitStack
from datetime import datetime
from email.utils import format_datetime
from itertools import islice
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from .stock_case_gold import build_case


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def pdf_bytes(pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/F1'): DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})})})
        lines = [line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') for line in text.splitlines()]
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 9 Tf 12 TL 40 750 Td ' +
                         ' T* '.join(f'({line}) Tj' for line in lines) + ' ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


def trade_text(row):
    return (f"Trade: id={row['trade_id']}; date={row['date']}; "
            f"account={row['account_id']}; shares={row['shares']:+d}")


def documents(case):
    trades = {t['trade_id']: t for t in case['trades']}
    names = {e['entity_id']: e['canonical_name'] for e in case['entities']}
    for row in case['statements']:
        account = next(a for a in case['accounts'] if a['account_id'] == row['account_id'])
        yield 'statement', 'pdf', [f"{account['broker_id']}\nStatement {row['date']}\n"
              f"Account {row['account_id']}; owners: {', '.join(names[o] for o in account['owner_entity_ids'])}",
              f"{names['issuer']} ({case['symbol']})\nPosition: date={row['date']}; "
              f"account={row['account_id']}; shares={row['shares']}",
              'Monthly activity\n' + '\n'.join(trade_text(trades[t]) for t in row['trade_ids'])]
    for row in case['trades'][:120]:
        yield 'confirmation', 'pdf', [f"{names['issuer']} ({case['symbol']})\n" + trade_text(row)]
    for filing in case['filings']:
        row = filing['share_count']
        detail = 'Capital record\n' + '\n'.join(f'{k}: {v}' for k, v in row.items())
        if 'account_id' in filing:
            position = next(s for s in case['statements']
                            if s['account_id'] == filing['account_id'] and s['date'] == filing['date'])
            detail += f"\nHolder: account={position['account_id']}; shares={position['shares']}"
        yield 'filing', 'pdf', [f"{names['issuer']} ({case['symbol']})\n{filing['kind']}; report date={filing['date']}\n"
              f"Share count: effective={row['effective']}; filed={row['filed']}; shares={row['shares']}; "
              f"event={row['event']}; split={row['split']}", detail]
    for account in case['accounts']:
        yield 'account_opening', 'pdf', [f"{account['broker_id']}\nAccount {account['account_id']}\n"
              f"Beneficial owners: {', '.join(names[o] for o in account['owner_entity_ids'])}\n"
              f"Authorized trader: {names[account['authorized_trader']]}."]
    for title in ('Operating agreement', 'Formation record'):
        controller = next(e['controller_entity_id'] for e in case['entities'] if e['entity_id'] == 'controlled_llc')
        yield 'account_opening', 'pdf', [f"{title}: {names['controlled_llc']}\n"
              f"{names[controller]} is the sole member and controls stk-3."]
    for row in case['wires']:
        yield 'wire', 'csv', [','.join(row) + '\n' + ','.join(str(v) for v in row.values())]
    for row in case['communications']:
        text = f"{row['date']}\nFrom: {names[row['sender']]}\nTo: {names[row['recipient']]}\n{row['text']}"
        if row['attachment']:
            text += f"\nAttachment referenced: {row['attachment']}"
        yield row['kind'], 'eml' if row['kind'] == 'email' else 'txt', [text]
    for row in case['interviews']:
        yield 'interview', 'pdf', [f"Interview {row['date']}: {names[row['speaker']]}\n{row['text']}"]
    for i, row in enumerate(case['productions']):
        text = f"Production {i + 1}\nBates range: SPND-{row['first']:06} through SPND-{row['last']:06}"
        yield 'production', 'pdf' if i % 2 == 0 else 'csv', [text if i % 2 == 0 else
              f"production,first_bates,last_bates\n{i + 1},SPND-{row['first']:06},SPND-{row['last']:06}"]


def render(out, *, seed=7, document_limit=None):
    """Create a fresh corpus, retaining at most one document's rendered bytes."""
    if document_limit is not None and (type(document_limit) is not int or not 0 <= document_limit <= 405):
        raise ValueError('document_limit must be an integer from 0 to 405')
    case = build_case(seed)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'artifacts').mkdir()
    with ExitStack() as stack:
        handles = {name: stack.enter_context((out / f'{name}.jsonl').open('w', encoding='utf-8', newline='\n'))
                   for name in ('sources', 'passages')}
        mailboxes = {}
        for i, (kind, ext, body) in enumerate(islice(documents(case), document_limit), 1):
            doc_id, bates = f'stock-{i:06}', f'SPND-{i:06}'
            pages = [f'{bates} | {doc_id} | Page {n}\n{text}' for n, text in enumerate(body, 1)]
            if ext == 'eml':
                sent = format_datetime(datetime.fromisoformat(body[0].splitlines()[0] + 'T12:00:00+00:00'))
                data = ('\r\n'.join(['From: sender@example.invalid', 'To: reader@example.invalid',
                        f'Date: {sent}', f'Message-ID: <{doc_id}@example.invalid>', f'Subject: {bates}',
                        'MIME-Version: 1.0', 'Content-Type: text/plain; charset=us-ascii',
                        'Content-Transfer-Encoding: 7bit', '', pages[0].replace('\n', '\r\n'), ''])).encode('ascii')
                group = (i - 1) // 40
                if group not in mailboxes:
                    mailboxes[group] = stack.enter_context((out / f'artifacts/mail-{group}.mbox').open('wb'))
                mailboxes[group].write(b'From synthetic Thu Jan 04 12:00:00 2024\n' + data + b'\n')
            else:
                data = pdf_bytes(pages) if ext == 'pdf' else ('\n'.join(body) + '\n').encode('utf-8')
            path = f'artifacts/{doc_id}.{ext}'
            (out / path).write_bytes(data)
            handles['sources'].write(canonical(dict(doc_id=doc_id, kind=kind, path=path,
                                                    bates=bates, pages=len(pages))) + '\n')
            for n, text in enumerate(pages if ext in {'pdf', 'eml'} else body, 1):
                handles['passages'].write(canonical(dict(doc_id=doc_id, page=n, text=text)) + '\n')
    sha256 = {}
    for path in (p for directory in (out, out / 'artifacts') for p in directory.iterdir() if p.is_file()):
        with path.open('rb') as handle:
            sha256[path.relative_to(out).as_posix()] = hashlib.file_digest(handle, 'sha256').hexdigest()
    digest = hashlib.sha256()
    for path, value in sorted(sha256.items()):
        digest.update(f'{path}\0{value}\n'.encode())
    manifest = dict(seed=seed, synthetic=True, sha256=sha256, combined_digest=digest.hexdigest())
    (out / 'manifest.json').write_text(canonical(manifest) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=7)
    args = parser.parse_args()
    print(canonical(render(args.out, seed=args.seed)))


if __name__ == '__main__':
    main()
