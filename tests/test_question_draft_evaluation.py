"""Synthetic regression: the fixed question-draft set and its scoring."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from case_intelligence.generation import GroundedGenerationService, UnavailableGenerator
from case_intelligence import question_draft_evaluation
from case_intelligence.question_draft_evaluation import (
    DEFAULT_CASES, MIN_QUESTIONS_PER_CASE, QUESTION_SET_FINGERPRINT, evaluate, load_cases, receipt,
)

ROOT = Path(__file__).resolve().parents[1]


class Scripted:
    """Drafts from the passages it is given, citing them as instructed."""
    def __init__(self, choose, count=3, quote=None):
        self.choose, self.count, self.quote = choose, count, quote

    @property
    def available(self):
        return True

    def draft_questions(self, *, purpose, topic, evidence):
        questions = []
        for index in range(self.count):
            item = self.choose(evidence, index)
            words = " ".join(item.excerpt.split()[:6]).strip(".,:;\"")
            text = f"{index + 1}. {('Who', 'What', 'Where')[index]} can explain the record that says {words}?"
            if self.quote:
                text = f'{("Why", "How", "When")[index]} does it say "{self.quote}" about {words}?'
            questions.append({"text": text, "evidence_ids": [item.evidence_id]})
        return {"questions": questions}


class MixedCitations(Scripted):
    """Cites the distractor (S3) together with a relevant passage."""
    def __init__(self):
        super().__init__(lambda evidence, index: evidence[2])

    def draft_questions(self, *, purpose, topic, evidence):
        reply = super().draft_questions(purpose=purpose, topic=topic, evidence=evidence)
        for question in reply["questions"]:
            question["evidence_ids"] = [evidence[0].evidence_id, evidence[2].evidence_id]
        return reply


class Malformed(Scripted):
    """Ignores the schema: evidence_ids is a scalar."""
    def __init__(self):
        super().__init__(lambda evidence, index: evidence[0])

    def draft_questions(self, *, purpose, topic, evidence):
        reply = super().draft_questions(purpose=purpose, topic=topic, evidence=evidence)
        for question in reply["questions"]:
            question["evidence_ids"] = 1
        return reply


class Echo(Scripted):
    """Repeats one valid question three times with different terminal punctuation."""
    def __init__(self):
        super().__init__(lambda evidence, index: evidence[0])

    def draft_questions(self, *, purpose, topic, evidence):
        reply = super().draft_questions(purpose=purpose, topic=topic, evidence=evidence)
        stem = reply["questions"][0]["text"].rstrip("?")
        for question, ending in zip(reply["questions"], ("?", "??", "?!")):
            question["text"] = stem + ending
        return reply


def run(client):
    data, _ = load_cases()
    return evaluate(GroundedGenerationService(client), data)


def test_the_set_is_synthetic_fixed_and_mixes_relevant_and_unrelated_passages():
    data, fingerprint = load_cases()
    assert len(data["cases"]) == 10 and fingerprint == QUESTION_SET_FINGERPRINT
    assert {case["purpose"] for case in data["cases"]} == {"witness", "discovery"}
    for case in data["cases"]:
        ids = {passage["id"] for passage in case["passages"]}
        assert set(case["relevant"]) < ids, case["id"]  # every case has a distractor


def test_relevant_grounded_drafts_pass_the_bar():
    result = run(Scripted(lambda evidence, index: evidence[index % 2]))
    assert result["passed"] is True
    assert result["shown_questions"] == 10 * MIN_QUESTIONS_PER_CASE and result["relevant_rate"] == 1.0
    assert result["shown_unsupported_quotes"] == result["raw_unsupported_quotes"] == 0


def test_drafts_citing_unrelated_passages_quoting_absent_text_or_too_few_fail():
    unrelated = run(Scripted(lambda evidence, index: evidence[2]))
    assert unrelated["passed"] is False and unrelated["relevant_rate"] == 0.0
    # Invented quotations never reach the reviewer, so every case falls below the minimum.
    quoting = run(Scripted(lambda evidence, index: evidence[0], quote="it was already open"))
    assert quoting["passed"] is False and quoting["shown_unsupported_quotes"] == 0
    assert quoting["raw_unsupported_quotes"] == 30 and len(quoting["cases_below_minimum"]) == 10
    # A one-word invented quotation is counted and kept from the reviewer too.
    short = run(Scripted(lambda evidence, index: evidence[0], quote="red"))
    assert short["passed"] is False and short["raw_unsupported_quotes"] == 30 and short["shown_unsupported_quotes"] == 0
    # Citing a distractor alongside a relevant passage does not make a question relevant.
    mixed = run(MixedCitations())
    assert mixed["passed"] is False and mixed["relevant_rate"] == 0.0 and mixed["shown_questions"] == 30
    # A malformed citation list is scored, not allowed to stop the run.
    malformed = run(Malformed())
    assert malformed["passed"] is False and malformed["shown_questions"] == 0
    assert len(malformed["cases_below_minimum"]) == 10
    # Repeating one question with different punctuation does not meet the minimum.
    echo = run(Echo())
    assert echo["passed"] is False and len(echo["cases_below_minimum"]) == 10
    thin = run(Scripted(lambda evidence, index: evidence[0], count=2))
    assert thin["passed"] is False and thin["relevant_rate"] == 1.0 and len(thin["cases_below_minimum"]) == 10


def test_without_a_runtime_the_model_gate_stays_outstanding():
    result = receipt(GroundedGenerationService(UnavailableGenerator()))
    assert result["model_gate"] == "outstanding" and "No model evaluation was executed" in result["limitation"]
    completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "evaluate-question-drafts.py")],
                               capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, check=True)
    assert json.loads(completed.stdout)["model_gate"] == "outstanding"
    assert DEFAULT_CASES.is_file()


ARTIFACT = 'a' * 64
CLEAN = dict(git_commit='1' * 40, working_tree_dirty=False, implementation_sha256={})


def runtime_profile(**changes):
    """A synthetic runtime declaration for the portable profile's repository pin."""
    pinned = next(item for item in json.loads((ROOT / 'config' / 'models.json').read_text())['models']
                  if item['role'] == 'generator' and item['profile'] == 'portable')
    return {**dict(runtime_name='synthetic', runtime_version='0', accelerator='CPU', accelerator_memory_gib=0,
                   driver='none', model_artifact_sha256=ARTIFACT, upstream_model_id=pinned['model_id'],
                   upstream_revision=pinned['revision'], license=pinned['license'],
                   offline_readiness='not_exercised', offline_evidence_sha256=None), **changes}


def identity_of(*artifacts):
    snapshots = iter(artifacts)

    def observe(client):
        return dict(method='test', model='synthetic', artifact_sha256=next(snapshots))
    return observe


def test_the_gate_passes_only_for_a_committed_revision_and_the_pinned_model_artifact(monkeypatch):
    good = Scripted(lambda evidence, index: evidence[index % 2])
    pinned = dict(profile='portable', runtime=runtime_profile())
    monkeypatch.setattr(question_draft_evaluation, 'execution_metadata', lambda: dict(CLEAN))
    bound = receipt(GroundedGenerationService(good), identity=identity_of(ARTIFACT, ARTIFACT), **pinned)
    assert bound['model_gate'] == 'passed' and bound['binding_problems'] == []
    assert bound['model_identity']['before']['artifact_sha256'] == ARTIFACT and bound['execution'] == CLEAN
    pin = bound['model_pin']
    assert pin['artifact_sha256'] == ARTIFACT and pin['revision'] == pinned['runtime']['upstream_revision']
    assert pin['basis'] == 'operator_declared_not_independently_verified' and len(pin['manifest_sha256']) == 64
    for identity, extra, problem in [
        (identity_of(None, None), pinned, 'no immutable model artifact'),
        (identity_of(ARTIFACT, 'b' * 64), pinned, 'changed or could not be observed'),
        (identity_of('c' * 64, 'c' * 64), pinned, 'not the one the runtime profile declares'),
        (identity_of(ARTIFACT, ARTIFACT), {}, 'No runtime profile linked'),
    ]:
        result = receipt(GroundedGenerationService(good), identity=identity, **extra)
        assert result['passed'] is True and result['model_gate'] == 'unbound', problem
        assert any(problem in text for text in result['binding_problems']), problem
    for execution, problem in [(dict(CLEAN, working_tree_dirty=True), 'uncommitted changes'),
                               (dict(CLEAN, git_commit=None, working_tree_dirty=None), 'No Git commit')]:
        monkeypatch.setattr(question_draft_evaluation, 'execution_metadata', lambda execution=execution: execution)
        result = receipt(GroundedGenerationService(good), identity=identity_of(ARTIFACT, ARTIFACT), **pinned)
        assert result['model_gate'] == 'unbound' and any(problem in text for text in result['binding_problems'])
    # A failing score fails, however well bound.
    monkeypatch.setattr(question_draft_evaluation, 'execution_metadata', lambda: dict(CLEAN))
    failing = receipt(GroundedGenerationService(Scripted(lambda evidence, index: evidence[2])),
                      identity=identity_of(ARTIFACT, ARTIFACT), **pinned)
    assert failing['model_gate'] == 'failed'


def test_a_runtime_profile_that_contradicts_the_pin_is_refused_before_any_model_call():
    class Untouchable(Scripted):
        def draft_questions(self, **_):
            raise AssertionError('the model must not be called')
    service = GroundedGenerationService(Untouchable(lambda evidence, index: evidence[0]))
    for profile, runtime in [('portable', runtime_profile(upstream_revision='0' * 40)),
                             ('quality', runtime_profile()),
                             ('portable', runtime_profile(model_artifact_sha256='not-a-digest')),
                             ('portable', None), (None, runtime_profile())]:
        with pytest.raises(ValueError):
            receipt(service, profile=profile, runtime=runtime)


def test_the_receipt_records_the_code_it_ran():
    execution = question_draft_evaluation.execution_metadata()
    assert set(execution) == {'git_commit', 'working_tree_dirty', 'implementation_sha256'}
    assert set(execution['implementation_sha256']) == set(question_draft_evaluation.IMPLEMENTATION_PATHS)
    assert all(len(value) == 64 for value in execution['implementation_sha256'].values())
    # Runtimes without a content digest record no artifact.
    assert question_draft_evaluation.model_identity(Scripted(lambda evidence, index: evidence[0])) == dict(
        method='none', model=None, artifact_sha256=None)


class TagsOpener:
    """Answers the Ollama model list with a fixed synthetic payload."""
    def __init__(self, models):
        self.body = json.dumps({'models': models}).encode()

    def open(self, request, timeout):
        assert request.full_url.endswith('/api/tags')
        return io.BytesIO(self.body)


def test_an_ollama_runtime_reports_its_model_digest():
    from case_intelligence.generation import OllamaGenerator
    observe = question_draft_evaluation.model_identity
    one = TagsOpener([{'name': 'synthetic:4b', 'digest': 'sha256:' + ARTIFACT}, {'name': 'other', 'digest': 'b' * 64}])
    client = OllamaGenerator('http://127.0.0.1:11434', 'synthetic:4b', opener=one)
    assert observe(client) == dict(method='ollama_tag_digest', model='synthetic:4b', artifact_sha256=ARTIFACT)
    for models in ([], [{'name': 'synthetic:4b', 'digest': 'not-a-digest'}],
                   [{'name': 'synthetic:4b', 'digest': ARTIFACT}, {'name': 'synthetic:4b', 'digest': ARTIFACT}]):
        missing = OllamaGenerator('http://127.0.0.1:11434', 'synthetic:4b', opener=TagsOpener(models))
        assert observe(missing)['artifact_sha256'] is None, models


def test_a_receipt_with_non_utf8_model_text_is_still_written(tmp_path, monkeypatch):
    """A lone surrogate in raw model output is dropped from what is shown and escaped in the receipt."""
    class Surrogate(Scripted):
        def draft_questions(self, **kwargs):
            reply = super().draft_questions(**kwargs)
            reply["questions"][0]["text"] = reply["questions"][0]["text"][:-1] + " \ud800?"
            return reply
    result = run(Surrogate(lambda evidence, index: evidence[index % 2]))
    assert result["shown_questions"] == 20 and result["passed"] is False
    text = json.dumps(result, ensure_ascii=True)
    assert "\\ud800" in text and text.encode("utf-8")


def test_a_receipt_with_non_finite_model_values_is_strict_json():
    """NaN or Infinity in raw model output is dropped from what is shown and kept as text."""
    class NonFinite(Scripted):
        def draft_questions(self, **kwargs):
            reply = super().draft_questions(**kwargs)
            reply["questions"][0]["text"] = float("nan")
            reply["questions"][1]["evidence_ids"] = [float("inf")]
            return reply
    result = run(NonFinite(lambda evidence, index: evidence[index % 2]))
    assert result["shown_questions"] == 10 and result["passed"] is False
    text = json.dumps(question_draft_evaluation.json_safe(result), allow_nan=False)
    assert '"nan"' in text and '"inf"' in text
    assert json.loads(text, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
