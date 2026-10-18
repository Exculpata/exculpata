"""Full-lane offline recognition with immutable model identities; no fixture fallback."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path


ASR_FILES = ['config.json', 'model.bin', 'preprocessor_config.json', 'tokenizer.json', 'vocabulary.json']


def snapshot(pin, *, offline):
    from huggingface_hub import snapshot_download
    return snapshot_download(pin['model_id'], revision=pin['revision'],
                             allow_patterns=ASR_FILES, local_files_only=offline)


def recognize(root, manifest):
    models = manifest['models']
    pin = models['asr']
    # Unsupported declarations must fail before any inference can attest to them.
    if pin.get('runtime') != 'faster-whisper==1.2.1':
        raise ValueError('Unsupported corpus manifest ASR runtime')
    if pin.get('compute_type') != 'int8':
        raise ValueError('Unsupported corpus manifest ASR compute_type')
    package, version = pin['runtime'].split('==')
    if importlib.metadata.version(package) != version:
        raise ValueError('Full corpus requires ' + pin['runtime'])
    if importlib.metadata.version('ctranslate2') != '4.8.2':
        raise ValueError('Full corpus requires ctranslate2==4.8.2')
    if importlib.metadata.version('av') != '18.1.0':
        raise ValueError('Full corpus requires av==18.1.0')
    # Product OCR intentionally uses its locked-down OS executable environment.
    data_root = Path('/usr/share/tesseract-ocr/5/tessdata')
    for language, digest in models['ocr']['sha256'].items():
        if hashlib.sha256((data_root / (language+'.traineddata')).read_bytes()).hexdigest() != digest:
            raise ValueError('OCR data does not match the pinned corpus revision')
    model_path = snapshot(pin, offline=True)
    if Path(model_path).name != pin['revision']:
        raise ValueError('ASR snapshot is not the pinned revision')
    from faster_whisper import WhisperModel
    model = WhisperModel(model_path, device='cpu', compute_type=pin['compute_type'], cpu_threads=2, num_workers=1, local_files_only=True)
    observations, transcripts = {}, {}
    for case in manifest['audio']:
        if case['diarization'] is not False:
            raise ValueError('This single-speaker corpus does not evaluate diarization')
        segments, info = model.transcribe(str(root / case['path']), language=case['language'], beam_size=5, vad_filter=False, condition_on_previous_text=False)
        rows = [{'start': s.start, 'end': s.end, 'text': s.text, 'speaker':'SPEAKER_00'} for s in segments]
        if not rows:
            raise ValueError('ASR returned no segments')
        transcripts[case['id']] = rows
        observations[case['id']] = {'text':' '.join(s['text'] for s in rows), 'language':info.language, 'segments':rows}
    return observations, transcripts


def stage(manifest):
    """Explicit CI/setup operation. Inference itself remains local-only."""
    snapshot(manifest['models']['asr'], offline=False)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-models', action='store_true', required=True)
    args = parser.parse_args()
    stage(json.loads((Path(__file__).resolve().parents[1]/'benchmarks/corpus/v1/manifest.json').read_text()))
