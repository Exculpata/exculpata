"""Read-only production snapshots and lossless Bates fact presentation."""
import csv
from dataclasses import asdict, replace
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping
import unicodedata
from .bates_gaps import BatesDocument, compute_bates_gaps
from .pilot_uploads import UploadProblem
from .source_load_files import parse_dat
from .timeline_exports import md_inline
from .workspace_store import WorkspaceProblem

NOTICE = ('Missing means no supplied label could cover the number. Possible means a label might. '
          'An empty list does not show the production is complete.')
SUPPORTED_LOAD_SUFFIXES = ('.csv', '.dat')
_DAT_ALIASES = {
    'begin_bates': frozenset({'begdoc', 'begbates', 'beginbates', 'prodbeg'}),
    'end_bates': frozenset({'enddoc', 'endbates', 'endbatesnum', 'prodend'}),
    'native_path': frozenset({'native', 'nativepath', 'nativelink', 'nativefile', 'path', 'filepath'}),
    'text_path': frozenset({'text', 'textpath', 'textlink', 'ocrpath'}),
}


def parse_load_file(raw: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    if reader.fieldnames != ['path', 'begin_bates', 'end_bates']:
        raise ValueError('Select a CSV with columns path,begin_bates,end_bates')
    rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError('Each load file row must have three columns')
    return rows


def _norm_header(header: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', unicodedata.normalize('NFKC', header).casefold())


def _has_control(value: str) -> bool:
    return any(unicodedata.category(ch) in {'Cc', 'Cf'} for ch in value)


def _safe_rel_path(value: str) -> str:
    if _has_control(value):
        return ''
    cleaned = unicodedata.normalize('NFC', value.strip()).replace('\\', '/')
    if not cleaned or cleaned.startswith('/') or re.match(r'^[A-Za-z]:', cleaned):
        return ''
    parts = cleaned.split('/')
    if '..' in parts or any(':' in part for part in parts):
        return ''
    return '/'.join(part for part in parts if part not in {'', '.'}).casefold()


def parse_dat_load_file(path: Path, *, field_map: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """Parse a Concordance DAT file via the shared bounded parser into summary rows."""
    headers: list[str] = []
    problems: list[str] = []
    raw_rows: list[tuple[str, ...]] = []

    def on_event(event):
        if event.kind == 'file_failure' or event.kind == 'rejected' or event.diagnostics:
            problems.extend(event.diagnostics or (event.kind,))

    for row in parse_dat(path, headers_out=headers, on_event=on_event):
        raw_rows.append(row.values)
    if problems or not headers:
        raise ValueError('The DAT load file could not be parsed cleanly')

    role_idx: dict[str, int] = {}
    if field_map:
        for idx, header in enumerate(headers):
            role = field_map.get(header)
            if role in _DAT_ALIASES:
                if role in role_idx:
                    raise ValueError('Ambiguous DAT header mapping')
                role_idx[role] = idx
    else:
        for role, aliases in _DAT_ALIASES.items():
            matches = [idx for idx, header in enumerate(headers) if _norm_header(header) in aliases]
            if len(matches) > 1:
                raise ValueError('Ambiguous DAT header mapping')
            if len(matches) == 1:
                role_idx[role] = matches[0]
    if 'begin_bates' not in role_idx or ('native_path' not in role_idx and 'text_path' not in role_idx):
        raise ValueError('DAT load file requires a Begin Bates column and a native or text path column')

    begin_i = role_idx['begin_bates']
    end_i = role_idx.get('end_bates')
    native_i = role_idx.get('native_path')
    text_i = role_idx.get('text_path')
    rows: list[dict[str, str]] = []
    for values in raw_rows:
        native_val = values[native_i] if native_i is not None else ''
        text_val = values[text_i] if text_i is not None else ''
        chosen = native_val if (native_val.strip() or _has_control(native_val)) else text_val
        rows.append({
            'path': chosen,
            'begin_bates': values[begin_i],
            'end_bates': values[end_i] if end_i is not None else '',
        })
    return rows


def _resolve_dat_rows(documents, rows, load):
    """Bind safe DAT relative paths uniquely within the load file's production root."""
    load_parts = PurePosixPath(load.relative_path.replace('\\', '/')).parts
    root = load_parts[0].casefold() if len(load_parts) > 1 else ''
    parent = '/'.join(part.casefold() for part in load_parts[:-1]) if len(load_parts) > 1 else ''
    by_norm: dict[str, list] = {}
    for doc in documents:
        if doc.document_id == load.document_id:
            continue
        key = _safe_rel_path(doc.relative_path)
        if key and (not root or key.startswith(root + '/')):
            by_norm.setdefault(key, []).append(doc)
    resolved: list[dict[str, str]] = []
    for row in rows:
        norm = _safe_rel_path(row['path'])
        if not norm:
            resolved.append({**row, 'path': ''})
            continue
        candidates = [norm] if (not root or norm.startswith(root + '/')) else []
        if root and not norm.startswith(root + '/'):
            candidates.append(f'{root}/{norm}')
        if parent and parent != root and not norm.startswith(parent + '/'):
            candidates.append(f'{parent}/{norm}')
        matched = {doc.document_id: doc for cand in candidates for doc in by_norm.get(cand, ())}
        if len(matched) > 1:
            raise ValueError('Ambiguous DAT document path reference')
        if len(matched) == 1:
            doc = next(iter(matched.values()))
            resolved.append({**row, 'path': doc.relative_path})
        else:
            resolved.append({**row, 'path': ''})
    return resolved


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


def build_summary(name, slug, store, load_file_token='', *, field_map: Mapping[str, str] | None = None):
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
                suffix = PurePosixPath(load.relative_path.replace('\\', '/')).suffix.casefold()
                if suffix not in SUPPORTED_LOAD_SUFFIXES:
                    raise ValueError('Select a CSV or DAT load file')
                load_path = store.source_path(load.document_id, verify_digest=True)
                raw = load_path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest != load.digest:
                    raise WorkspaceProblem('The load file changed during the build')
                if suffix == '.dat':
                    rows = _resolve_dat_rows(documents, parse_dat_load_file(load_path, field_map=field_map), load)
                else:
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
                             if d.relative_path.lower().endswith(SUPPORTED_LOAD_SUFFIXES)])


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
        if title == 'Bates gaps' and payload['load_file'] is not None:
            lines += [f"Missing: {payload['counts']['missing']}", f"Possible: {payload['counts']['possible']}", '']
        for item in items:
            links = '; '.join('[' + escape(c['name'] + ' (version ' + c['version_id'] + ')') + '](' + c['url'] + ')'
                              for c in item['citations'])
            lines.append('- ' + escape(item['text']) + (' ' + links if links else ''))
    return '\n'.join(lines) + '\n'


def render_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)
