#!/usr/bin/env python3
"""Evaluate a terminal synthetic full-text ledger through the real adapter.

This never stages a model or contacts anything except the explicit loopback
endpoint. Classification is a controlled fixture; synthesis uses the pinned
local model and application verifier. The optional thinking switch is an
evaluation wire setting, not an application default or portfolio qualification.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from case_intelligence.generation import (
    ANSWER_SCHEMA, GroundedGenerationService, OllamaGenerator,
    VerifiedReviewDecision, GenerationRejected, _bounded_json_get,
    _bounded_json_request, _parse_model_content, _prompt,
)
from case_intelligence.pilot_uploads import PilotUnit

ACTOR = 'development-taylor-morgan'
SUITE = 'full-text-synthesis-24-units-v1'
MODEL = 'qwen3.5:4b'
DIGEST = '2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd'
UNSUPPORTED = 'A submarine transported 9999 satellites to Jupiter.'


class SourceEcho:
    """Deterministic synthesis client for plumbing and restoration only."""
    available = True

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {'answerable': True, 'claims': [
            {'text': item.excerpt, 'evidence_ids': [item.evidence_id]}
            for item in kwargs['evidence'][:8]], 'limitation': None,
            'missing_information': ''}


def synthetic_statements(count=24):
    return [f'Synthetic dispatch record {number} states the crate was '
            + ('not delivered.' if number == count else 'delivered.')
            for number in range(1, count + 1)]


def seed_terminal_review(bench, matter, *, count=24, extra_outcomes=False,
                         long_unit=False, selected_set=False):
    """Persist generated originals, extraction units and a real terminal ledger.

    Only extraction/classification providers are controlled. Review admission,
    range coverage, call charging, decision saving and completion are real.
    """
    bench.full_review.close()
    store = bench.source_store(matter)
    statements = synthetic_statements(count)
    units = list(statements)
    if extra_outcomes:
        units.extend(['Synthetic routine note contains no delivery finding.',
                      'Synthetic classification failure.',
                      'Synthetic unsupported saved finding.', ''])
    if long_unit:
        units.append('Synthetic neutral padding. ' * 260
                     + 'Synthetic late account states the crate was delivered. '
                     + 'Synthetic late competing account states the crate was not delivered.')
    document, _ = store.store_stream('synthetic-dispatch.txt', 'text/plain',
                                    io.BytesIO('\n'.join(units).encode()))
    document.units = [asdict(PilotUnit(number, text,
        excerpt_digest=hashlib.sha256(text.encode()).hexdigest()))
        for number, text in enumerate(units, 1)]
    store._save([document.document_id])
    documents = [document]
    if extra_outcomes:
        failed, _ = store.store_stream('synthetic-extraction-failed.txt', 'text/plain',
            io.BytesIO(b'Synthetic fixture with unavailable extraction.'))
        failed.state = 'failed'
        failed.message = 'Synthetic extraction failure.'
        store._save([failed.document_id])
        documents.append(failed)
        empty, _ = store.store_stream('synthetic-zero-units.txt', 'text/plain',
            io.BytesIO(b'Synthetic extraction fixture without text units.'))
        empty.units = []
        (store.derived / empty.units_file).write_text('{"version":1,"units":[]}')
        store._save([empty.document_id])
        documents.append(empty)
    bench._sync_source_catalog(matter, documents)
    bench.workspace.reconcile_source_organizations(matter.matter_id,
        [(row.document_id, 'upload', row.display_name) for row in documents])
    _, version = bench.workspace.create_review_criterion(matter.matter_id, ACTOR,
        title='Synthetic delivery accounts',
        instructions='Summarize what the dispatch records say about delivery. Retain conflicting accounts and each record number.')
    selection = (bench.workspace.create_source_set(matter.matter_id,
        'Synthetic synthesis population', [row.document_id for row in documents], ACTOR)
        if selected_set else None)
    queued = bench.workspace.queue_review_run(matter.matter_id, ACTOR,
        version.criterion_version_id, run_kind='full', review_mode='full_text',
        source_set_id=selection.source_set_id if selection else None)
    run = bench.workspace.claim_review_run('synthetic-ledger-worker')
    assert run.run_id == queued.run_id
    def classify(**kwargs):
        text = kwargs['evidence'][0].excerpt
        if text == 'Synthetic classification failure.':
            raise GenerationRejected('Synthetic classification failure.')
        if text == 'Synthetic unsupported saved finding.':
            # Deliberately malformed historical machine output tests the
            # adapter's independent source verification, never model quality.
            return VerifiedReviewDecision('include', UNSUPPORTED, ('S1',), True, 1)
        if 'states the crate was' in text:
            sentences = [sentence + '.' for sentence in text.split('.')
                         if 'states the crate was' in sentence]
            return VerifiedReviewDecision('include', ' '.join(sentences).strip(), ('S1',), True, 1)
        return VerifiedReviewDecision('not_identified', 'No delivery finding in this range.', (), True, 1)
    original = bench.generator.classify_source
    bench.generator.classify_source = classify
    try:
        while (decision := bench.workspace.next_review_decision(run.run_id)) is not None:
            result = bench._process_review_decision(run, decision, lambda: False)
            bench._record_review_decision(run, decision, result)
        completed = bench._finish_review_run(run)
    finally:
        bench.generator.classify_source = original
    assert completed.state == 'succeeded'
    return completed, documents, statements


class EvaluationGenerator(OllamaGenerator):
    def generate(self, *, question, evidence, history=(), working_context='', grounding_repair=False):
        system, user = _prompt(question, evidence, history, working_context,
                               grounding_repair=grounding_repair)
        response = _bounded_json_request(self.endpoint + '/api/chat', {
            'model': self.model, 'stream': False, 'think': False,
            'format': ANSWER_SCHEMA,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'options': {'temperature': 0.1, 'num_ctx': 8192, 'num_predict': 1200},
            'keep_alive': '5m'}, timeout=self.timeout)
        return _parse_model_content(response.get('message', {}).get('content'))


class DefaultWireProbeComplete(BaseException):
    """Stop after exactly one real request, without verifier repair retries."""


class DefaultWireProbe(OllamaGenerator):
    def generate(self, **kwargs):
        started = time.monotonic()
        try:
            result = super().generate(**kwargs)
            self.observed = {'structured_response': result, 'parsed': True}
        except Exception as exc:
            self.observed = {'parsed': False, 'failure_type': type(exc).__name__, 'failure': str(exc)}
        self.observed['elapsed_seconds'] = round(time.monotonic() - started, 2)
        raise DefaultWireProbeComplete()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:11439')
    parser.add_argument('--model', default=MODEL)
    parser.add_argument('--expected-digest', default=DIGEST)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--disable-thinking', action='store_true')
    mode.add_argument('--default-wire-probe', action='store_true',
        help='Stop after one actual default-adapter request; this is readiness evidence, not a synthesis-quality pass')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    parsed = urlparse(args.endpoint)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        parser.error('Use an explicit HTTP 127.0.0.1 model endpoint.')
    if args.output.exists():
        parser.error('Choose a new output path; an earlier receipt is never overwritten.')
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    from fastapi.testclient import TestClient
    from case_intelligence.managed_storage import StoragePolicy
    from case_intelligence.workbench import create_workbench_app
    model = next(item for item in _bounded_json_get(args.endpoint + '/api/tags', timeout=5)['models']
                 if item['name'] == args.model)
    if model['digest'].removeprefix('sha256:') != args.expected_digest.removeprefix('sha256:'):
        raise ValueError('Served artifact does not match the expected immutable digest.')
    generator = (DefaultWireProbe if args.default_wire_probe else EvaluationGenerator if args.disable_thinking else OllamaGenerator)(
        args.endpoint, args.model, timeout=90)
    receipt = dict(suite=SUITE, synthetic=True, artifact_digest=model['digest'], model=args.model,
        license='Apache-2.0' if args.model == MODEL and args.expected_digest.removeprefix('sha256:') == DIGEST else 'Verify external catalog',
        runtime=_bounded_json_get(args.endpoint + '/api/version', timeout=5),
        disable_thinking=args.disable_thinking, context_tokens=8192, output_tokens_per_call=1200,
        default_wire_probe=args.default_wire_probe,
        fixture_classification='controlled; synthesis uses actual local model',
        scope='One synthetic terminal full-text run through the production adapter and hierarchy; no production quality or portfolio qualification.')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-full-text-model-") as temporary:
        app = create_workbench_app(Path(temporary).resolve() / 'runtime', generator=SourceEcho(),
            auth_mode='test', storage_policy=StoragePolicy(reserve_bytes=0))
        with TestClient(app):
            bench = app.state.workbench
            bench.research.close()
            matter = bench.create_matter('Synthetic full-text model evaluation', '', ACTOR)
            run, documents, statements = seed_terminal_review(bench, matter, extra_outcomes=True, long_unit=True)
            corpus = [{'source_name': document.display_name, 'extraction_state': document.state,
                       'units': [{'number': unit.number, 'text': unit.text}
                                 for unit in document.iter_parsed_units()]} for document in documents]
            receipt['corpus_digest'] = hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest()
            bench.generator = GroundedGenerationService(generator)
            job, _ = bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, 'research-request-' + 'e' * 32)
            claimed = bench.workspace.claim_research_job('synthetic-model-worker')
            try:
                result = bench._process_research_job(claimed, lambda: False)
                completed = bench._finish_research_job(claimed, result)
                claims = result['answer'].get('claims', [])
                texts = [item['text'] for item in claims]
                originals = {item['support_token']: item['excerpt'] for item in result['evidence']}
                exact = [claim['text'] for claim in claims if claim['text'] in statements and
                         any(originals.get(citation.get('support_token')) == claim['text']
                             for citation in claim['citations'])]
                unreviewed = [text for text in texts if text not in exact]
                recalled = sum(text in exact for text in statements)
                support, competing = statements[0] in exact, statements[-1] in exact
                injected = any('Jupiter' in text or '9999' in text for text in texts)
                input_receipt = result['full_text_synthesis_input']
                accounting_verified = (input_receipt['counts'] == {
                    'candidate_findings': 26, 'admitted': 24, 'unsupported_finding': 1, 'oversized_original': 1}
                    and input_receipt['coverage']['ranges'] == {'no_finding': 2, 'failed': 1}
                    and input_receipt['coverage']['sources'].get('unavailable') == 1
                    and input_receipt['partial'])
                receipt.update(result=result, decisive_accounts={'support': support, 'competing': competing},
                    original_statement_recall={'recalled': recalled, 'total': len(statements)},
                    injected_claim_displayed=injected, false_claims=0 if not unreviewed else None,
                    claims_requiring_manual_inspection=unreviewed,
                    omission_and_failure_accounting_verified=accounting_verified,
                    passed=support and competing and recalled == len(statements) and not injected and not unreviewed and accounting_verified,
                    exports={name: hashlib.sha256(bench.export_research_work_product(matter, completed, name).body).hexdigest()
                             for name in ('json', 'markdown', 'docx')})
            except DefaultWireProbeComplete:
                saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
                receipt.update(passed=False, probe_completed=True, default_wire=generator.observed,
                    last_checkpoint=saved.result,
                    quality_evaluation='Not performed: stopped after the first charged default-wire request.')
            except Exception as exc:
                saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
                receipt.update(passed=False, failure_type=type(exc).__name__, failure=str(exc), last_checkpoint=saved.result)
    receipt['elapsed_seconds'] = round(time.monotonic() - started, 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({key: receipt[key] for key in ('suite', 'passed', 'elapsed_seconds')}))
    return 0 if receipt['passed'] or receipt.get('probe_completed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
