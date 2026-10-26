"""Recognition receipts must describe the settings actually executed."""
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import corpus_recognition


MANIFEST = Path(__file__).resolve().parents[1] / 'benchmarks/corpus/v1/manifest.json'


@pytest.mark.parametrize(('field', 'value'), [
    ('runtime', 'faster-whisper==0.0.0'),
    ('runtime', 'other-runtime==1.2.1'),
    ('compute_type', 'float32'),
    ('compute_type', None),
])
def test_recognition_rejects_manifest_execution_mismatch(monkeypatch, tmp_path, field, value):
    manifest = json.loads(MANIFEST.read_text())
    manifest['models']['asr'][field] = value
    monkeypatch.setitem(sys.modules, 'faster_whisper',
                        SimpleNamespace(WhisperModel=None))
    monkeypatch.setattr(corpus_recognition.importlib.metadata, 'version',
                        lambda name: {'faster-whisper': '1.2.1', 'ctranslate2': '4.8.2', 'av': '18.1.0'}[name])
    with pytest.raises(ValueError, match='manifest.*' + field):
        corpus_recognition.recognize(tmp_path, manifest)


@pytest.fixture
def recognition_runtime(monkeypatch, tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest['models']['ocr']['sha256'] = {}
    pin = manifest['models']['asr']
    weights = b'synthetic-weights-not-a-model'
    pin['sha256'] = {'model.bin': hashlib.sha256(weights).hexdigest()}
    model_path = tmp_path / pin['revision']
    # fixture-write: Create a temporary synthetic snapshot directory.
    model_path.mkdir()
    # fixture-write: Seed synthetic weights, never a real model.
    (model_path / 'model.bin').write_bytes(weights)
    versions, snapshots, model_calls, transcription_calls = [], [], [], []

    def version(name):
        versions.append(name)
        return {'faster-whisper': '1.2.1', 'ctranslate2': '4.8.2', 'av': '18.1.0'}[name]

    def snapshot(settings, *, offline):
        snapshots.append((settings, offline))
        return str(tmp_path / settings['revision'])

    class Model:
        def __init__(self, path, **kwargs):
            model_calls.append((path, kwargs))

        def transcribe(self, path, **kwargs):
            transcription_calls.append((path, kwargs))
            return [SimpleNamespace(start=0, end=1, text='Synthetic recording.')], SimpleNamespace(language='en')

    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(corpus_recognition.importlib.metadata, 'version', version)
    monkeypatch.setattr(corpus_recognition, 'snapshot', snapshot)
    return manifest, versions, snapshots, model_calls, transcription_calls


@pytest.mark.parametrize('defect', ['weights', 'missing-pin', 'revision'])
def test_recognition_rejects_unpinned_weights(recognition_runtime, tmp_path, defect):
    manifest, _, snapshots, model_calls, _ = recognition_runtime
    pin = manifest['models']['asr']
    message = 'ASR model.bin does not match the pinned corpus digest'
    if defect == 'weights':
        pin['sha256'] = {'model.bin': '69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1'}
    elif defect == 'missing-pin':
        pin.pop('sha256')
    else:
        # fixture-write: Give the synthetic snapshot a mutable revision name.
        (tmp_path / pin['revision']).rename(tmp_path / 'main')
        pin['revision'] = 'main'
        message = 'ASR revision must be an immutable commit'
    with pytest.raises(ValueError, match=message):
        corpus_recognition.recognize(tmp_path, manifest)
    assert not model_calls
    if defect == 'revision':
        assert not snapshots


def test_recognition_rejects_missing_weights_file(recognition_runtime, tmp_path):
    manifest, _, _, model_calls, _ = recognition_runtime
    # fixture-write: Remove only the temporary synthetic weights.
    (tmp_path / manifest['models']['asr']['revision'] / 'model.bin').unlink()
    with pytest.raises(ValueError, match='ASR model.bin is missing from the pinned snapshot'):
        corpus_recognition.recognize(tmp_path, manifest)
    assert not model_calls


def test_recognition_executes_manifest_runtime_and_compute_type(recognition_runtime, tmp_path):
    manifest, versions, snapshots, model_calls, transcription_calls = recognition_runtime
    pin = copy.deepcopy(manifest['models']['asr'])
    observations, transcripts = corpus_recognition.recognize(tmp_path, manifest)
    assert versions == ['faster-whisper', 'ctranslate2', 'av']
    assert snapshots == [(pin, True)]
    assert model_calls == [(str(tmp_path / pin['revision']), {
        'device': 'cpu', 'compute_type': pin['compute_type'], 'cpu_threads': 2,
        'num_workers': 1, 'local_files_only': True,
    })]
    assert set(observations) == set(transcripts) == {case['id'] for case in manifest['audio']}
    assert transcription_calls == [(str(tmp_path / case['path']), {
        'language': case['language'], 'beam_size': 5, 'vad_filter': False,
        'condition_on_previous_text': False,
    }) for case in manifest['audio']]
    coverage = corpus_recognition.coverage_contract(full=True)
    assert coverage['recognition'] == {
        'adapter': 'faster_whisper.WhisperModel', 'mode': 'independent-offline-cpu',
    }
    assert all(row['recognition_adapter'] == coverage['recognition']['adapter']
               for row in observations.values())


@pytest.mark.parametrize('full', [False, True])
def test_coverage_contract_excludes_production_service_acceptance(full):
    coverage = corpus_recognition.coverage_contract(full=full)
    assert coverage['schema_version'] == 1
    assert ('image_pdf_ocr' in coverage['product_paths']) is full
    assert {'native_pdf_ingestion', 'media_transcript_import_and_projection',
            'search', 'question_dispatch_admission', 'extractive_answer_service',
            'saved_citation_validation'} <= set(coverage['product_paths'])
    assert coverage['recognition'] == {
        'adapter': 'faster_whisper.WhisperModel' if full else None,
        'mode': 'independent-offline-cpu' if full else 'prepared-transcript-fixture',
    }
    assert {'production_transcription_service', 'production_transcription_worker',
            'production_audio_loading', 'production_decoding_options', 'production_segment_conversion',
            'whisperx_pipeline', 'whisperx_vad', 'forced_alignment', 'diarization',
            'translation', 'gpu_execution', 'learned_retrieval_quality',
            'generative_model_quality'} <= set(coverage['not_exercised'])
    summary = corpus_recognition.coverage_summary(full=full)
    assert 'production transcription service: not exercised' in summary
    assert ('independent faster-whisper' in summary) is full
    assert ('product OCR' in summary) is full
    assert ('prepared transcripts' in summary) is (not full)


def test_fast_receipt_and_cli_disclose_unexercised_service(tmp_path):
    from tests.test_corpus_receipts import checkout, run_receipt

    root = checkout(tmp_path)
    report = tmp_path / 'coverage-receipt.json'
    result = run_receipt(root, report)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(report.read_text())
    assert receipt['passed'] is True
    assert len(receipt['cases']) == 14
    assert receipt['coverage']['recognition'] == {
        'adapter': None, 'mode': 'prepared-transcript-fixture',
    }
    assert {'production_transcription_service', 'production_transcription_worker',
            'production_audio_loading', 'production_decoding_options',
            'production_segment_conversion'} <= set(receipt['coverage']['not_exercised'])
    assert 'prepared transcripts' in result.stdout
    assert 'production transcription service: not exercised' in result.stdout
