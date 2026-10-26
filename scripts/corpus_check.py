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
import wave
from pathlib import Path

try:
    from corpus_recognition import ASR_WEIGHTS_FILE
except ModuleNotFoundError:
    from scripts.corpus_recognition import ASR_WEIGHTS_FILE

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'benchmarks/corpus/v1'
TRANSCRIPT_QUALIFIER = 'The machine transcript appears to say that '
FIXED_ADAPTER_CONTRACTS = {
    'answer': {'provider': 'CorpusExtractiveGenerator + ExtractiveAnswerProvider', 'model': None},
    'retrieval': {'provider': 'product lexical', 'model': None},
}
REVIEWED_V1_CASES = {
    'documents': {'bicycle', 'parcel', 'scan-en', 'scan-es'},
    'audio': {'audio-bicycle', 'audio-parcel'},
    'search': {'search-bicycle', 'search-parcel', 'search-gate', 'search-locker',
               'search-spoken', 'search-scan-en', 'search-scan-es'},
    'answers': {'answer-bicycle', 'answer-time', 'answer-parcel', 'answer-locker', 'answer-spoken'},
}
REVIEWED_ASR = {
    'model_id': 'Systran/faster-whisper-large-v3',
    'revision': 'edaa852ec7e145841d8ffdb056a99866b5f0a478',
    'license': 'MIT',
    'runtime': 'faster-whisper==1.2.1',
    'compute_type': 'int8',
    'sha256': {ASR_WEIGHTS_FILE: '69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1'},
}
REVIEWED_OCR = {
    'model_id': 'tesseract-ocr/tessdata_fast',
    'revision': '87416418657359cb625c412a48b6e1d6d41c29bd',
    'license': 'Apache-2.0',
    'languages': ['eng', 'spa'],
    'sha256': {
        'eng': '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2',
        'spa': '6f2e04d02774a18f01bed44b1111f2cd7f3ba7ac9dc4373cd3f898a40ea6b464',
        'osd': '9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff',
    },
}


def normalize(text: str) -> str:
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def contains_tokens(text: str, phrase: str) -> bool:
    """Match a normalized phrase without accepting part of a word or numeral."""
    normalized = normalize(phrase)
    return bool(normalized) and (' ' + normalized + ' ') in (' ' + normalize(text) + ' ')


def error_rate(expected, actual) -> float:
    """Levenshtein errors/reference length; insertions can exceed one."""
    row = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        next_row = [i]
        for j, right in enumerate(actual, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = next_row
    return row[-1] / max(1, len(expected))


def valid_span_expectation(citation, sources):
    return (isinstance(citation, dict) and citation.get('document_id') in sources
            and isinstance(citation.get('span'), str) and bool(normalize(citation['span']))
            and isinstance(citation.get('alternative_spans', []), list)
            and all(isinstance(span, str) and normalize(span)
                    for span in citation.get('alternative_spans', [])))


def valid_locator_expectation(locator):
    return (isinstance(locator, dict)
            and all(type(locator.get(key)) is int for key in ('start_ms', 'end_ms', 'tolerance_ms'))
            and locator['start_ms'] == 0 < locator['end_ms']
            and 0 <= locator['tolerance_ms'] <= 500
            and locator['end_ms'] > 2 * locator['tolerance_ms'])


def matches_locator(observation, locator, tolerance):
    """Accept only bounded endpoint variation in a reviewed synthetic interval."""
    start, end = observation.get('start_ms'), observation.get('end_ms')
    return (type(start) is int and type(end) is int
            and 0 <= start < end
            and abs(start - locator['start_ms']) <= tolerance
            and abs(end - locator['end_ms']) <= tolerance)


def matches_audio_layout(observation, expected):
    if not isinstance(observation, dict):
        return False
    units = observation.get('units')
    if (not isinstance(units, list) or not units or not all(isinstance(unit, dict)
            and type(unit.get('unit')) is int and unit['unit'] > 0
            and type(unit.get('start_ms')) is int and type(unit.get('end_ms')) is int
            and isinstance(unit.get('text'), str) for unit in units)
            or [unit['unit'] for unit in units] != list(range(1, len(units) + 1))
            or any(right['start_ms'] < left['end_ms'] for left, right in zip(units, units[1:]))
            or '\n'.join(unit['text'] for unit in units) != observation.get('text')):
        return False
    for layout in expected['layouts']:
        if len(units) != len(layout):
            continue
        if all(matches_locator(unit, wanted, expected['locator']['tolerance_ms'])
               and error_rate(normalize(wanted['text']).split(),
                              normalize(re.sub(r'^Speaker [0-9]+:\s*', '', unit['text'])).split()) <= expected['wer']
               for unit, wanted in zip(units, layout)):
            return True
    return False


def load_corpus(root: Path, *, source_bytes=None):
    if source_bytes is None:
        source_bytes = {name: (root / name).read_bytes() for name in ('manifest.json', 'expected.json')}
    manifest = json.loads(source_bytes['manifest.json'])
    expected = json.loads(source_bytes['expected.json'])
    if manifest['schema_version'] != 1 or manifest['synthetic'] is not True:
        raise ValueError('Unsupported or non-synthetic corpus')
    ids = []
    for group in REVIEWED_V1_CASES:
        ids.extend(row['id'] for row in manifest[group])
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate case identities')
    if manifest.get('id') != 'synthetic-discovery-v1':
        raise ValueError('Corpus must use the exact reviewed v1 case inventory')
    for group, reviewed in REVIEWED_V1_CASES.items():
        if {row['id'] for row in manifest[group]} != reviewed:
            raise ValueError(f'{group}: requires the exact reviewed v1 case inventory')
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
    if pin != REVIEWED_ASR:
        raise ValueError('ASR identity must match the complete reviewed corpus ASR record')
    if manifest['models'].get('ocr') != REVIEWED_OCR:
        raise ValueError('OCR identity must match the complete reviewed corpus record')
    for adapter, contract in FIXED_ADAPTER_CONTRACTS.items():
        if manifest['models'].get(adapter) != contract:
            raise ValueError(f'{adapter} identity must match the fixed adapter contract')
    if set(manifest['models']) != {'asr', 'ocr', *FIXED_ADAPTER_CONTRACTS}:
        raise ValueError('Models must use the exact reviewed model inventory')
    for group in ('documents', 'audio', 'search', 'answers'):
        if set(expected[group]) != {row['id'] for row in manifest[group]}:
            raise ValueError(f'{group}: incomplete expected case inventory')
    for group, metric in [('documents', 'cer'), ('audio', 'wer')]:
        for case in expected[group].values():
            if not normalize(case['text']) or not isinstance(case[metric], (int, float)) or not math.isfinite(case[metric]) or not 0 <= case[metric] <= 0.15:
                raise ValueError(f'{group}: invalid expected text or tolerance')
    if any(type(case.get('pages')) is not int or case['pages'] <= 0
           for case in expected['documents'].values()):
        raise ValueError('Documents require positive integer page counts')
    for clip in manifest['audio']:
        locator = expected['audio'][clip['id']].get('locator')
        if not valid_locator_expectation(locator):
            raise ValueError('Audio requires a bounded reviewed clip locator')
        with wave.open(str(root / clip['path'])) as recording:
            duration_ms = round(recording.getnframes() * 1000 / recording.getframerate())
        if locator['end_ms'] != duration_ms:
            raise ValueError('Reviewed audio locator must match the pinned WAV duration')
        layouts = expected['audio'][clip['id']].get('layouts')
        if not isinstance(layouts, list) or not layouts:
            raise ValueError('Audio requires reviewed passage layouts')
        for layout in layouts:
            previous_end = 0
            if not isinstance(layout, list) or not layout:
                raise ValueError('Audio requires nonempty passage layouts')
            for unit in layout:
                if (not isinstance(unit, dict) or not isinstance(unit.get('text'), str) or not normalize(unit['text'])
                        or type(unit.get('start_ms')) is not int or type(unit.get('end_ms')) is not int
                        or not previous_end <= unit['start_ms'] < unit['end_ms'] <= duration_ms
                        or unit['start_ms'] - previous_end > locator['tolerance_ms']):
                    raise ValueError('Audio passage layout requires ordered bounded text intervals')
                previous_end = unit['end_ms']
            if duration_ms - previous_end > locator['tolerance_ms']:
                raise ValueError('Audio passage layout must cover the reviewed clip interval')
    sources = {row['id'] for row in manifest['documents'] + manifest['audio']}
    ocr_sources = {row['id'] for row in manifest['documents'] if row['ocr']}
    discovery_sources = set()
    for case in manifest['search']:
        hits = expected['search'][case['id']]['document_ids']
        if (type(case.get('full_only', False)) is not bool
                or ocr_sources.intersection(hits) and not case.get('full_only', False)):
            raise ValueError('OCR search cases require an explicit full-only lane')
        discovery_sources.update(hits)
        if (type(case['top_k']) is not int or not 0 < case['top_k'] <= len(sources)
                or not hits or len(hits) != len(set(hits)) or not set(hits) <= sources
                or len(hits) > case['top_k']):
            raise ValueError('Search requires nonempty known hits within a positive top-k')
        spans = expected['search'][case['id']].get('citations')
        if (not isinstance(spans, list) or not spans
                or not all(valid_span_expectation(span, sources) for span in spans)
                or {span['document_id'] for span in spans} != set(hits)):
            raise ValueError('Search requires reviewed support spans for every expected source')
    for case in expected['answers'].values():
        if (not case['facts'] or not all(isinstance(fact, str) and normalize(fact) for fact in case['facts'])
                or not case['citations'] or any(c['document_id'] not in sources
                or not isinstance(c['span'], str) or not normalize(c['span'])
                or not isinstance(c.get('alternative_spans', []), list)
                or any(not isinstance(span, str) or not normalize(span) for span in c.get('alternative_spans', []))
                for c in case['citations'])):
            raise ValueError('Answers require nonempty facts and known source spans')
        discovery_sources.update(c['document_id'] for c in case['citations'])
    if not ocr_sources <= discovery_sources:
        raise ValueError('Every OCR source requires a discovery expectation')
    return manifest, expected


def matches_span(citation, required):
    spans = [required['span'], *required.get('alternative_spans', [])]
    return (citation.get('document_id') == required['document_id']
            and any(contains_tokens(citation.get('excerpt', ''), span) for span in spans))


def supports_complete_claim(citation, text, kind):
    """The extractive adapter may add only its exact transcript qualifier.

    Each attached passage must contain the entire remaining normalized claim,
    including details beyond the required gold facts. Never borrow support from
    another claim or treat reviewed numeral alternatives as interchangeable text.
    """
    if kind not in {'document', 'transcript'} or citation.get('evidence_kind') != kind:
        return False
    if kind == 'transcript':
        if not text.startswith(TRANSCRIPT_QUALIFIER):
            return False
        text = text[len(TRANSCRIPT_QUALIFIER):]
    elif kind != 'document' or text.startswith(TRANSCRIPT_QUALIFIER):
        return False
    return citation.get('source_valid') is True and contains_tokens(citation.get('excerpt', ''), text)


def citation_identity(citation):
    """A corpus observation resolves one current source version and passage unit."""
    source, unit = citation.get('document_id'), citation.get('unit')
    if not isinstance(source, str) or type(unit) is not int or unit < 1:
        return None
    return source, unit


def transcript_location(start_ms, end_ms):
    from case_intelligence.media_evidence import format_timestamp
    return f'{format_timestamp(start_ms)}–{format_timestamp(end_ms)}'


def compare(manifest, expected, actual, *, full=False):
    """Return per-case diagnostics; no aggregate or caller-supplied pass flag is trusted."""
    source_kinds = {}
    for group, kind in (('documents', 'document'), ('audio', 'transcript')):
        for source in manifest[group]:
            if source['id'] in source_kinds:
                raise ValueError(f"Duplicate or ambiguous corpus source identity: {source['id']}")
            source_kinds[source['id']] = kind

    def valid_citation(citation):
        if not isinstance(citation, dict):
            return False
        source = citation.get('document_id')
        kind = source_kinds.get(source)
        if kind is None or citation.get('source_valid') is not True or citation.get('evidence_kind') != kind:
            return False
        observation = actual.get(source)
        if not isinstance(observation, dict) or observation.get('state') != 'ready':
            return False
        if (not isinstance(citation.get('excerpt'), str) or not citation['excerpt']
                or type(citation.get('start')) is not int or citation['start'] != 0
                or type(citation.get('end')) is not int or citation['end'] != len(citation['excerpt'])):
            return False
        if kind == 'document':
            observation = actual.get(source)
            # The reviewed PDF corpus contains one page per source, so its
            # observed document text is the complete cited page unit.
            return (isinstance(observation, dict) and type(observation.get('pages')) is int
                    and observation['pages'] == expected['documents'][source]['pages'] == 1
                    and type(citation.get('unit')) is int and citation['unit'] == 1
                    and citation.get('location') == 'Page 1'
                    and citation['excerpt'] == observation.get('text')
                    and citation.get('start_ms') is None and citation.get('end_ms') is None)
        observation = actual.get(source, {})
        return (matches_audio_layout(observation, expected['audio'][source])
                and all(type(citation.get(key)) is int for key in ('unit', 'start_ms', 'end_ms'))
                and citation.get('location') == transcript_location(citation['start_ms'], citation['end_ms'])
                and any(isinstance(unit, dict)
                        and citation.get('excerpt') == unit.get('text')
                        and all(citation.get(key) == unit.get(key) for key in ('unit', 'start_ms', 'end_ms'))
                        for unit in observation['units']))

    results = []
    for group in ('documents', 'audio', 'search', 'answers'):
        for case in manifest[group]:
            key = case['id']
            if not full and (group == 'documents' and case['ocr']
                             or group == 'search' and case.get('full_only', False)):
                continue
            want = expected[group][key]
            got = actual.get(key)
            errors = []
            measurement = {}
            if not isinstance(got, dict):
                errors.append('missing observed case')
            elif group in ('documents', 'audio'):
                if got.get('state') != 'ready':
                    errors.append(f"expected ready source; observed {got.get('state')!r}")
                if group == 'documents' and (type(got.get('pages')) is not int or got['pages'] != want['pages']):
                    errors.append(f"expected {want['pages']} PDF pages; observed {got.get('pages')!r}")
                if group == 'audio':
                    if not matches_audio_layout(got, want):
                        errors.append('transcript must preserve a reviewed passage layout, text and locators')
                    if not full:
                        results.append({'id': key, 'passed': not errors, 'diff': errors})
                        continue
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
                hits = got.get('document_ids', [])
                citations = got.get('citations', [])
                if len(hits) > case['top_k'] or len(citations) > case['top_k']:
                    errors.append(f"search returned more than requested top-k={case['top_k']}")
                if not set(want['document_ids']).issubset(hits):
                    errors.append(f"expected top-{case['top_k']} sources {want['document_ids']}; observed {hits}")
                if any(not valid_citation(c) for c in citations):
                    errors.append('invalid search citation')
                for support in want['citations']:
                    if not any(valid_citation(c) and matches_span(c, support) for c in citations):
                        errors.append(f'missing validated search support: {support}')
            else:
                if got.get('kind') != 'generated':
                    errors.append(f"expected generated answer; observed {got.get('kind')!r}")
                if got.get('answerable') is not True:
                    errors.append(f"expected answerable true; observed {got.get('answerable')!r}")
                if got.get('route') != 'question' or got.get('dispatched') is not True:
                    errors.append(f"expected question route; observed {got.get('route')}")
                claims = got.get('claims')
                if not isinstance(claims, list) or not claims:
                    errors.append('missing observed answer claims')
                    claims = []
                valid_claims = []
                seen_claims = set()
                for index, claim in enumerate(claims, 1):
                    if (not isinstance(claim, dict) or not isinstance(claim.get('text'), str)
                            or not normalize(claim['text']) or not isinstance(claim.get('citations'), list)
                            or not claim['citations'] or not all(isinstance(c, dict) for c in claim['citations'])):
                        errors.append(f'invalid observed claim {index}')
                    else:
                        valid_claims.append(claim)
                        identities = [citation_identity(c) for c in claim['citations']]
                        if len(set(identities)) != len(identities):
                            errors.append(f'duplicate citation in observed claim {index}')
                        # Match generation._verify_text whitespace normalization
                        # and the product's (text, evidence-id set) dedup key.
                        identity = (' '.join(claim['text'].split()), frozenset(identities))
                        if identity in seen_claims:
                            errors.append(f'duplicate observed claim {index}')
                        seen_claims.add(identity)
                        for citation in claim['citations']:
                            if not supports_complete_claim(citation, claim['text'],
                                                           source_kinds.get(citation.get('document_id'))):
                                errors.append(f"complete claim {index} is not supported by its attached passage: "
                                              f"{citation.get('document_id')}")
                # Aggregates are compatibility summaries, never a substitute for
                # the actual per-claim support returned by the product.
                citations = [citation for claim in valid_claims for citation in claim['citations']]
                if got.get('text') != ' '.join(claim['text'] for claim in valid_claims):
                    errors.append('answer text differs from observed claims')
                if got.get('citations') != citations:
                    errors.append('answer citations differ from observed claim citations')
                for fact in want['facts']:
                    matching = [claim for claim in valid_claims if contains_tokens(claim['text'], fact)]
                    if not matching:
                        errors.append(f'missing answer fact in observed claims: {fact}')
                    for claim in matching:
                        if not any(citation.get('source_valid') is True
                                   and contains_tokens(citation.get('excerpt', ''), fact)
                                   and any(matches_span(citation, item) for item in want['citations'])
                                   for citation in claim['citations']):
                            errors.append(f'answer fact lacks support attached to its claim: {fact}')
                for citation in citations:
                    if not valid_citation(citation):
                        errors.append(f'invalid source span: {citation}')
                    if not any(matches_span(citation, item) for item in want['citations']):
                        errors.append(f'unexpected citation: {citation}')
                for required in want['citations']:
                    if not any(matches_span(c, required) and c.get('source_valid') is True for c in citations):
                        errors.append(f'missing required citation: {required}')
            results.append({'id': key, 'passed': not errors, 'diff': errors, **measurement})
    return results


def repository_code_basis(outputs=()):
    """Identify repository bytes, including local edits, before writing a receipt."""
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT)

    tracked = set(git('ls-files', '-z').decode().split('\0')) - {''}
    excluded = {path.resolve() for path in outputs if path}
    if any((ROOT / name).resolve() in excluded for name in tracked):
        raise ValueError('Outputs must not overwrite tracked repository files')
    names = set(git('ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')) - {''}
    hashes = {}
    for name in sorted(names):
        path = ROOT / name
        if path.resolve() in excluded:
            continue
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f'Cannot attest nonregular repository file: {name}')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return {
        'base_head': git('rev-parse', 'HEAD').decode().strip(),
        'dirty': bool(git('status', '--porcelain', '-z', '--untracked-files=all')),
        'files_sha256': hashes,
        'implementation_sha256': hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=CORPUS)
    parser.add_argument('--full', action='store_true', help='Product OCR plus independent pinned faster-whisper recognition; excludes production transcription service')
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
        code_basis = repository_code_basis((args.report, args.proposal)) if args.report else None
        source_bytes = {name: (args.corpus / name).read_bytes()
                        for name in ('manifest.json', 'expected.json')}
        manifest, expected = load_corpus(args.corpus, source_bytes=source_bytes)
        input_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
        input_hashes.update({row['path']: row['sha256'] for row in manifest['documents'] + manifest['audio']})
        from corpus_product import observe
        from corpus_recognition import coverage_contract, coverage_summary
        coverage = coverage_contract(full=args.full)
        transcripts = manifest['fast_transcripts']
        audio = {}
        if args.full:
            from corpus_recognition import recognize
            audio, transcripts = recognize(args.corpus, manifest)
            if any(row.get('recognition_adapter') != coverage['recognition']['adapter'] for row in audio.values()):
                raise ValueError('Recognition adapter differs from the declared coverage')
        actual = observe(args.corpus, manifest, full=args.full, observed_transcripts=transcripts)
        for key, observation in audio.items():
            actual[key]['asr_text'] = observation['text']
        results = compare(manifest, expected, actual, full=args.full)
        if args.report and any(hashlib.sha256((args.corpus / name).read_bytes()).hexdigest() != digest
                               for name, digest in input_hashes.items()):
            raise ValueError('Corpus inputs changed during observation')
        if args.report and repository_code_basis((args.report, args.proposal)) != code_basis:
            raise ValueError('Repository code changed during corpus observation')
        receipt = {
            'schema_version': 1, 'corpus': manifest['id'],
            'manifest_sha256': input_hashes['manifest.json'],
            'expected_sha256': input_hashes['expected.json'],
            'corpus_files_sha256': input_hashes,
            'head': code_basis['base_head'] if code_basis and not code_basis['dirty'] else None,
            'code_basis': code_basis, 'coverage': coverage,
            'mode': 'full' if args.full else 'fast-contract',
            'models': manifest['models'], 'cases': results,
            'passed': all(r['passed'] for r in results),
        }
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
        print(coverage_summary(full=args.full))
        if not args.full:
            print('OCR scans and ASR excluded; transcript fixtures exercise application contracts only.')
        return 0 if receipt['passed'] else 1
    except Exception as error:
        print(f'Corpus gate blocked: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
