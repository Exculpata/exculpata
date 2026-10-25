"""Inspect synthetic candidate decisions without giving recorded output model authority."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import hashlib
import json
from collections import deque
from pathlib import Path

from .question_grounding import QuestionDraftReceipt, audit_attempts


def explain_candidates(raw, evidence, relevant):
    """Use the production audit, including internal judge vetoes, for explanations."""
    from .question_drafting import inspect_question
    from .question_language import transcript_drop_reason

    by_id = {item.evidence_id: item for item in evidence}
    envelope = isinstance(raw, QuestionDraftReceipt)
    attempts = raw.attempts if envelope else (raw,)
    audit = audit_attempts(attempts, evidence, raw.judge_drops if envelope else None)
    drops = {drop['candidate_id']: drop for drop in audit.drops if drop['candidate_id']}
    rows = []
    for number, attempt in enumerate(attempts, 1):
        values = attempt.get('questions') if isinstance(attempt, dict) else None
        valid = isinstance(attempt, dict) and set(attempt) == {'questions'} and isinstance(values, list) and len(values) <= 8
        if not valid or not values:
            rows.append(dict(attempt=number, candidate_id=None,
                             category='no_candidate' if valid else 'other',
                             check='empty_batch' if valid else 'invalid_attempt_shape'))
            continue
        for index, value in enumerate(values, 1):
            key = f'a{number}q{index}'
            drop = drops.get(key)
            if drop:
                reason = drop['reason']
                question, diagnostic = (inspect_question(value.get('text'), value.get('evidence_ids'), by_id)
                                        if isinstance(value, dict) and set(value) == {'text', 'evidence_ids'}
                                        else (None, {'check': 'invalid_question_shape'}))
                diagnostic = diagnostic or {'check': reason}
                if question is not None and reason.startswith(('cross_speaker', 'transcript_', 'wrong_transcript')):
                    transcript_drop_reason(question.text, [by_id[key] for key in question.evidence_ids], details=diagnostic)
                category = ('duplicate' if reason == 'near_duplicate' else
                            'quote_not_found' if reason == 'unsupported_quotation' else
                            'span_mismatch' if reason in {'uncited_digit_bearing_detail', 'cross_locator_detail',
                                                         'cross_speaker_detail', 'wrong_transcript_speaker_quotation'}
                            else 'other')
                rows.append(dict(attempt=number, candidate_id=key, category=category,
                                 reason=reason, **diagnostic))
            else:
                rows.append(dict(attempt=number, candidate_id=key,
                                 category='retained' if set(value['evidence_ids']) <= set(relevant) else 'relevance',
                                 check='retained', evidence_ids=value['evidence_ids']))
    return rows


def replay_receipt(path, cases_path, *, explain=False):
    """Replay raw synthetic batches only; never inherit historic scores or model identity.

    This narrow input format deliberately excludes receipt outcomes, judge results,
    calls, model/runtime metadata and model-declared acceptance decisions.
    """
    from .generation import GroundedGenerationService
    from .question_draft_evaluation import evaluate, execution_metadata, load_cases
    from .question_drafting import MAX_TOPIC_CHARS

    encoded = Path(path).read_bytes()
    if len(encoded) > 1_000_000:
        raise ValueError('Recorded output must be at most 1 MB.')
    recorded = json.loads(encoded)
    if (not isinstance(recorded, dict) or set(recorded) != {'format', 'synthetic_only', 'cases'}
            or recorded['format'] != f"{LEGACY_STEM}-question-replay-v1" or recorded['synthetic_only'] is not True
            or not isinstance(recorded['cases'], list) or not 1 <= len(recorded['cases']) <= 100):
        raise ValueError('Not a synthetic question replay.')
    data, fingerprint = load_cases(cases_path)
    by_id = {case['id']: case for case in data['cases']}
    selected, replies, seen = [], {}, set()
    for row in recorded['cases']:
        if (not isinstance(row, dict) or set(row) != {'id', 'attempts'} or not isinstance(row['id'], str)
                or row['id'] not in by_id or row['id'] in seen or not isinstance(row['attempts'], list)
                or not 1 <= len(row['attempts']) <= 3):
            raise ValueError('Replay needs unique known case IDs and one to three raw attempts.')
        case = by_id[row['id']]
        seen.add(row['id'])
        selected.append(case)
        passages = tuple((p['id'], p['source_name'], p['location'], p['excerpt']) for p in case['passages'])
        topic = ' '.join((case['topic'] or '').split())[:MAX_TOPIC_CHARS]
        key = case['purpose'], topic, passages
        replies.setdefault(key, deque()).append(row['attempts'])

    class RecordedClient:
        available = True

        def draft_questions(self, *, purpose, topic, evidence):
            passages = tuple((p.evidence_id, p.source_name, p.location, p.excerpt) for p in evidence)
            return QuestionDraftReceipt(replies[purpose, topic, passages].popleft(), profile={}, calls=[])

    result = evaluate(GroundedGenerationService(RecordedClient()), {'cases': selected}, explain=explain)
    passed = result.pop('passed')
    return dict(result, format=f"{LEGACY_STEM}-question-replay-result-v1", synthetic_only=True, replay_only=True,
                  model_gate='not_run', model_judge_executed=False, execution=execution_metadata(),
                  source_set_fingerprint=fingerprint, recorded_output_sha256=hashlib.sha256(encoded).hexdigest(),
                  limitation='Offline deterministic replay only. No model inference or model judge ran; '
                             'a fresh real-model evaluation is still required.',
                  deterministic_checks_passed=passed)
