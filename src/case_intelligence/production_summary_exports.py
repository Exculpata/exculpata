"""Portable production summaries with source links and explicit read limits."""
import json
import re
from .work_product_exports import ExportArtifact, ExportProblem, safe_file_stem


def markdown_text(value):
    # Source text stays inert in Markdown; links are constructed by the service.
    return re.sub(r'([\\`*_{}\[\]()<>#!|])', r'\\\1', str(value)).replace('\n', ' ')


def export_summary(summary, format, name='production'):
    payload = summary.to_dict()
    stem = safe_file_stem(name, fallback='exculpata') + '-production-summary'
    if format == 'json':
        return ExportArtifact((json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode(), 'application/json', stem + '.json')
    if format != 'markdown':
        raise ExportProblem('Choose Markdown or JSON.')
    lines = ['# Production summary', '', summary.notice, '', f'Format: {summary.format}',
             f'Generated: {summary.generated_at}', f'Scope: {markdown_text(summary.scope)}', '', '## Totals', '']
    def link(label, href):
        return f'[{markdown_text(label)}]({href})'
    for key in ('documents', 'pages', 'bytes'):
        lines.append('- ' + link(f'{summary.totals[key]} {key}', summary.totals['link']))
    lines += ['', '## Types', '']
    for row in summary.by_type:
        lines.append('- ' + link(f"{row['type']}: {row['documents']} documents, {row['pages']} PDF pages", row['link']))
    lines += ['', '## Dates', '', link(f"Earliest: {summary.date_range['earliest'] or 'Not recorded'}", summary.date_range['earliest_link']),
              link(f"Latest: {summary.date_range['latest'] or 'Not recorded'}", summary.date_range['latest_link']),
              link(f"{summary.date_range['undated_count']} documents with no ordered date", summary.date_range['link'])]
    lines.extend('- ' + link('Date source', href) for href in summary.date_range['links'])
    for title, rows in (('People', summary.people), ('Organizations', summary.organizations)):
        lines += ['', '## ' + title, '']
        for row in rows:
            lines.append('- ' + link(f"{row['name']}: {row['mentions']} recorded mentions", row['link']))
            lines.extend('  - ' + link('Passage', href) for href in row['links'])
    lines += ['', '## Gaps', '']
    for kind, rows in summary.gaps.items():
        lines += ['', '### ' + kind.replace('_', ' ').title(), '']
        for row in rows:
            if kind == 'referenced_absent':
                lines.append('- ' + link(row['reference_text'], row['citation']['link']) + ': referenced, not located in this production')
            elif kind == 'unreadable':
                lines.append('- ' + link(row['reason'], row['link']))
            else:
                lines.append('- ' + markdown_text(f"{row.get('prefix', '')} {row.get('start', row.get('bates_start', ''))}–{row.get('end', '')}"))
                lines.extend('  - ' + link('Source', href) for href in row.get('links', [row.get('link', '')]))
                if row.get('links_omitted'):
                    lines.append(f"  - {row['links_omitted']} support references omitted.")
    lines += ['', '## Caps and limits', '']
    lines.extend('- ' + markdown_text(value) for value in summary.limits)
    lines.extend(f"- {kind}: {cap['shown']} shown, {cap['omitted']} omitted of {cap['total']}." for kind, cap in summary.capped.items())
    lines += ['', '## Underlying sources', '']
    lines.extend('- ' + link(row['name'], row['link']) for row in summary.sources)
    return ExportArtifact(('\n'.join(lines) + '\n').encode(), 'text/markdown; charset=utf-8', stem + '.md')
