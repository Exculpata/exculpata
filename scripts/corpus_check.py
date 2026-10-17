#!/usr/bin/env python3
"""Small synthetic discovery acceptance; expected results are never inputs to inference."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'benchmarks/corpus/v1'


def normalize(text: str) -> str:
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def error_rate(expected, actual) -> float:
    """Levenshtein errors/reference length; insertions can exceed one."""
    row = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        next_row = [i]
        for j, right in enumerate(actual, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = next_row
    return row[-1] / max(1, len(expected))


def load_corpus(root: Path):
    manifest = json.loads((root / 'manifest.json').read_text())
    expected = json.loads((root / 'expected.json').read_text())
    if manifest['schema_version'] != 1 or manifest['synthetic'] is not True:
        raise ValueError('Unsupported or non-synthetic corpus')
    ids = []
    for group, minimum in [('documents', 3), ('audio', 2), ('search', 5), ('answers', 5)]:
        if len(manifest[group]) < minimum:
            raise ValueError(f'{group}: requires at least {minimum} cases')
        ids.extend(row['id'] for row in manifest[group])
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate case identities')
    if manifest['max_file_bytes'] != 10_000_000:
        raise ValueError('Media limit must remain 10 MB')
    for row in manifest['documents'] + manifest['audio']:
        path = root / row['path']
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"{row['id']}: unsafe media path")
        data = path.read_bytes()
        if not 0 < len(data) < manifest['max_file_bytes'] or len(data) != row['bytes']:
            raise ValueError(f"{row['id']}: media size mismatch")
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError(f"{row['id']}: media digest mismatch")
    approved = json.loads((ROOT / 'config/models.json').read_text())['models']
    pin = manifest['models']['asr']
    if not any(m.get('model_id') == pin['model_id'] and m.get('revision') == pin['revision'] and m.get('license') == pin['license'] for m in approved):
        raise ValueError('ASR pin must match the reviewed model catalog')
    if not re.fullmatch('[0-9a-f]{40}', manifest['models']['ocr']['revision']):
        raise ValueError('OCR requires an immutable revision')
    for group in ('documents', 'audio', 'search', 'answers'):
        if set(expected[group]) != {row['id'] for row in manifest[group]}:
            raise ValueError(f'{group}: incomplete expected case inventory')
    for group, metric in [('documents', 'cer'), ('audio', 'wer')]:
        for case in expected[group].values():
            if not normalize(case['text']) or not isinstance(case[metric], (int, float)) or not math.isfinite(case[metric]) or not 0 <= case[metric] <= 0.15:
                raise ValueError(f'{group}: invalid expected text or tolerance')
    sources = {row['id'] for row in manifest['documents'] + manifest['audio']}
    for case in manifest['search']:
        hits = expected['search'][case['id']]['document_ids']
        if (type(case['top_k']) is not int or not 0 < case['top_k'] <= len(sources)
                or not hits or len(hits) != len(set(hits)) or not set(hits) <= sources
                or len(hits) > case['top_k']):
            raise ValueError('Search requires nonempty known hits within a positive top-k')
    for case in expected['answers'].values():
        if (not case['facts'] or not all(isinstance(fact, str) and normalize(fact) for fact in case['facts'])
                or not case['citations'] or any(c['document_id'] not in sources
                or not isinstance(c['span'], str) or not normalize(c['span']) for c in case['citations'])):
            raise ValueError('Answers require nonempty facts and known source spans')
    return manifest, expected


def compare(manifest, expected, actual, *, full=False):
    """Return per-case diagnostics; no aggregate or caller-supplied pass flag is trusted."""
    results = []
    for group in ('documents', 'audio', 'search', 'answers'):
        for case in manifest[group]:
            key = case['id']
            if not full and (group == 'audio' or group == 'documents' and case['ocr']):
                continue
            want = expected[group][key]
            got = actual.get(key)
            errors = []
            measurement = {}
            if not isinstance(got, dict):
                errors.append('missing observed case')
            elif group in ('documents', 'audio'):
                before = normalize(want['text'])
                after = normalize(got.get('asr_text', '') if group == 'audio' else got.get('text', ''))
                if group == 'audio':
                    # The product adds display speaker labels to projected units.
                    projected = re.sub(r'(?m)^Speaker [0-9]+:\s*', '', got.get('text', ''))
                    if normalize(projected) != after:
                        errors.append('projected transcript differs from observed ASR text')
                metric = 'cer' if group == 'documents' else 'wer'
                score = error_rate(before if metric == 'cer' else before.split(), after if metric == 'cer' else after.split())
                measurement = {'metric': metric, 'error_rate': score, 'tolerance': want[metric]}
                if score > want[metric]:
                    errors.append(f'{metric}={score:.4f} exceeds {want[metric]:.4f}')
                    errors.extend(difflib.unified_diff([before+'\n'], [after+'\n'], fromfile='expected', tofile='observed'))
                if group == 'documents' and case['ocr'] and not got.get('ocr_pages'):
                    errors.append('image-only PDF did not exercise OCR')
            elif group == 'search':
                hits = got.get('document_ids', [])[:case['top_k']]
                if not set(want['document_ids']).issubset(hits):
                    errors.append(f"expected top-{case['top_k']} sources {want['document_ids']}; observed {hits}")
                citations = got.get('citations', [])
                if any(c.get('source_valid') is not True for c in citations):
                    errors.append('invalid search citation')
                for source in want['document_ids']:
                    if not any(c.get('document_id') == source and c.get('source_valid') is True
                               and normalize(c.get('excerpt', '')) for c in citations):
                        errors.append(f'missing validated search support: {source}')
            else:
                if got.get('route') != 'question':
                    errors.append(f"expected question route; observed {got.get('route')}")
                for fact in want['facts']:
                    if normalize(fact) not in normalize(got.get('text', '')):
                        errors.append(f'missing answer fact: {fact}')
                citations = got.get('citations', [])
                for citation in citations:
                    if citation.get('source_valid') is not True:
                        errors.append(f'invalid source span: {citation}')
                    if not any(citation.get('document_id') == item['document_id'] and normalize(item['span']) in normalize(citation.get('excerpt', '')) for item in want['citations']):
                        errors.append(f'unexpected citation: {citation}')
                for required in want['citations']:
                    if not any(c.get('document_id') == required['document_id'] and normalize(required['span']) in normalize(c.get('excerpt', '')) and c.get('source_valid') is True for c in citations):
                        errors.append(f'missing required citation: {required}')
            results.append({'id': key, 'passed': not errors, 'diff': errors, **measurement})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=CORPUS)
    parser.add_argument('--full', action='store_true', help='Require real OCR and pinned offline ASR; no substitution')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--update-expected', action='store_true', help='Write a proposal only; never overwrite reviewed gold')
    parser.add_argument('--proposal', type=Path)
    args = parser.parse_args(argv)
    if args.update_expected != bool(args.proposal):
        parser.error('--update-expected requires --proposal and vice versa')
    try:
        for output in (args.report, args.proposal):
            if output and output.resolve().is_relative_to(args.corpus.resolve()):
                raise ValueError('Outputs must be outside the reviewed corpus')
        if args.report and args.proposal and args.report.resolve() == args.proposal.resolve():
            raise ValueError('Report and proposal must use different paths')
        manifest, expected = load_corpus(args.corpus)
        from corpus_product import observe
        transcripts = manifest['fast_transcripts']
        audio = {}
        if args.full:
            from corpus_recognition import recognize
            audio, transcripts = recognize(args.corpus, manifest)
        actual = observe(args.corpus, manifest, full=args.full, observed_transcripts=transcripts)
        for key, observation in audio.items():
            actual[key]['asr_text'] = observation['text']
        results = compare(manifest, expected, actual, full=args.full)
        receipt = {'schema_version': 1, 'corpus': manifest['id'], 'manifest_sha256': hashlib.sha256((args.corpus/'manifest.json').read_bytes()).hexdigest(), 'expected_sha256': hashlib.sha256((args.corpus/'expected.json').read_bytes()).hexdigest(), 'head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(), 'mode': 'full' if args.full else 'fast-contract', 'models': manifest['models'], 'cases': results, 'passed': all(r['passed'] for r in results)}
        if args.report:
            args.report.write_text(json.dumps(receipt, indent=2)+'\n')
        if args.proposal:
            protected = {p.resolve() for p in args.corpus.rglob('*') if p.is_file()}
            if args.proposal.resolve() in protected:
                raise ValueError('Proposal must be outside the reviewed corpus')
            with args.proposal.open('x') as stream:
                json.dump({'observations':actual,'review_required':True}, stream, indent=2)
        for row in results:
            score = f" {row['metric']}={row['error_rate']:.4f} limit={row['tolerance']:.4f}" if 'metric' in row else ''
            print(('PASS' if row['passed'] else 'FAIL')+' '+row['id']+score)
            for line in row['diff']:
                print('  '+line.rstrip())
        print(f"{sum(r['passed'] for r in results)}/{len(results)} cases passed ({receipt['mode']})")
        if not args.full:
            print('OCR scans and ASR excluded; transcript fixtures exercise application contracts only.')
        return 0 if receipt['passed'] else 1
    except Exception as error:
        print(f'Corpus gate blocked: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
