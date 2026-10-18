"""Recognition receipts must describe the settings actually executed."""
import copy
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


def test_recognition_executes_manifest_runtime_and_compute_type(monkeypatch, tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest['models']['ocr']['sha256'] = {}
    pin = copy.deepcopy(manifest['models']['asr'])
    versions, snapshots, model_calls = [], [], []

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
            return [SimpleNamespace(start=0, end=1, text='Synthetic recording.')], SimpleNamespace(language='en')

    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(corpus_recognition.importlib.metadata, 'version', version)
    monkeypatch.setattr(corpus_recognition, 'snapshot', snapshot)
    observations, transcripts = corpus_recognition.recognize(tmp_path, manifest)
    assert versions == ['faster-whisper', 'ctranslate2', 'av']
    assert snapshots == [(pin, True)]
    assert model_calls == [(str(tmp_path / pin['revision']), {
        'device': 'cpu', 'compute_type': pin['compute_type'], 'cpu_threads': 2,
        'num_workers': 1, 'local_files_only': True,
    })]
    assert set(observations) == set(transcripts) == {case['id'] for case in manifest['audio']}
