"""Read-only production snapshots and lossless Bates fact presentation."""
import csv
from dataclasses import asdict, replace
import hashlib
import io
import json
import unicodedata
from .bates_gaps import BatesDocument, compute_bates_gaps
from .pilot_uploads import UploadProblem
from .timeline_exports import md_inline
from .workspace_store import WorkspaceProblem

NOTICE = ('Missing means no supplied label could cover the number. Possible means a label might. '
          'An empty list does not show the production is complete.')


def parse_load_file(raw: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    if reader.fieldnames != ['path', 'begin_bates', 'end_bates']:
        raise ValueError('Select a CSV with columns path,begin_bates,end_bates')
    rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError('Each load file row must have three columns')
    return rows


def _real_label(begin):
    return any('0' <= c <= '9' for c in unicodedata.normalize('NFKC', begin))


def map_documents(documents, rows, load_file_id):
    by_path = {}
    for row in rows:
        by_path.setdefault(row['path'], []).append((row['begin_bates'], row['end_bates'] or None))
    mapped = []
    for document in documents:
        if document.document_id == load_file_id:
            continue
        document_rows = by_path.get(document.relative_path, ())
        labels = tuple(row for row in document_rows if _real_label(row[0]))
        complete = document.state == 'ready' and bool(document_rows) and len(labels) == len(document_rows)
        mapped.append(BatesDocument(document.document_id, document.version_id, labels,
                                    document.page_count if document.media_type == 'application/pdf' else None,
                                    complete))
    return tuple(mapped)


def visible(text):
    return ''.join(f'[U+{ord(c):04X}]' if unicodedata.category(c) in {'Cc', 'Cf', 'Mn', 'Me'} else c for c in text)


def build_summary(name, slug, store, load_file_token=''):
    with store.mutation_guard():
        documents = tuple(replace(d) for d in store.documents.values())
        versions = {d.document_id: (d.version_id, d.digest) for d in documents}
        citations = {(d.document_id, d.version_id): dict(name=d.display_name, version_id=d.version_id,
                     url=f'/matters/{slug}/sources/{store.action_token(d)}') for d in documents}
        def line(text, docs):
            return dict(text=text, cited_ids=[(d.document_id, d.version_id) for d in docs])
        overview = [line(f'Documents: {len(documents)}', documents)]
        for media in sorted({d.media_type for d in documents}):
            group = [d for d in documents if d.media_type == media]
            overview.append(line(f'{media}: {len(group)}', group))
        pdfs = [d for d in documents if d.media_type == 'application/pdf']
        overview.append(line(f'Recorded PDF pages: {sum(d.page_count for d in pdfs)}', pdfs))
        failed = [line(f'{d.display_name}: {d.state}', [d]) for d in documents if d.state != 'ready']
        rows, bates, facts, load_file = [], [], (), None
        try:
            if load_file_token:
                load = replace(store.get_by_action_token(load_file_token))
                if not load.relative_path.lower().endswith('.csv'):
                    raise ValueError('Select a CSV load file')
                raw = store.source_path(load.document_id, verify_digest=True).read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest != load.digest:
                    raise WorkspaceProblem('The load file changed during the build')
                rows = parse_load_file(raw)
                load_file = dict(document_id=load.document_id, version_id=load.version_id, sha256=digest)
                bates.append(line(f'Load file sha256: {digest}', [load]))
                for number, row in enumerate(rows, 2):
                    matches = [d for d in documents if d.relative_path == row['path']]
                    if matches and not _real_label(row['begin_bates']):
                        continue
                    label = row['begin_bates'] + (' to ' + row['end_bates'] if row['end_bates'] else '')
                    bates.append(line('Bates label: ' + label, matches) if matches else
                                 line(f'Load file row {number} names no document in this production', [load]))
                facts = compute_bates_gaps(map_documents(documents, rows, load.document_id))
                if hashlib.sha256(store.source_path(load.document_id, verify_digest=True).read_bytes()).hexdigest() != digest:
                    raise WorkspaceProblem('The load file changed during the build')
            else:
                bates.append(line('Bates gaps not computed: no load file selected', []))
            if versions != {d.document_id: (d.version_id, d.digest) for d in store.documents.values()}:
                raise WorkspaceProblem('The production changed during the build')
            sections = [('Overview', overview), ('Failed and unreadable files', failed), ('Bates gaps', bates)]
            for item in [item for _, items in sections for item in items] + [asdict(f) for f in facts]:
                for identity in item['cited_ids']:
                    citations[tuple(identity)]
        except KeyError as exc:
            raise WorkspaceProblem('A cited source is no longer available') from exc
        except (UploadProblem, OSError) as exc:
            raise WorkspaceProblem('The load file changed or is no longer available') from exc
        return dict(name=name, slug=slug, notice=NOTICE, facts=[asdict(f) for f in facts], load_rows=rows,
                    load_file=load_file, sections=sections, citations=list(citations.items()),
                    counts={kind: sum(f.kind == kind for f in facts) for kind in ('missing', 'possible')},
                    choices=[dict(token=store.action_token(d), name=d.display_name) for d in documents
                             if d.relative_path.lower().endswith('.csv')])


def sections(payload):
    citations = {tuple(identity): citation for identity, citation in payload['citations']}
    for title, items in payload['sections']:
        if title == 'Bates gaps':
            items = [*items, *payload['facts']]
        yield title, [dict(text=item['text'], citations=[citations[tuple(i)] for i in item['cited_ids']]) for item in items]


def render_markdown(payload):
    escape = lambda text: md_inline(visible(text))
    lines = ['# Production summary: ' + escape(payload['name']), '', escape(payload['notice'])]
    for title, items in sections(payload):
        lines += ['', '## ' + title, '']
        if title == 'Bates gaps':
            lines += [f"Missing: {payload['counts']['missing']}", f"Possible: {payload['counts']['possible']}", '']
        for item in items:
            links = '; '.join('[' + escape(c['name'] + ' (version ' + c['version_id'] + ')') + '](' + c['url'] + ')'
                              for c in item['citations'])
            lines.append('- ' + escape(item['text']) + (' ' + links if links else ''))
    return '\n'.join(lines) + '\n'


def render_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)
