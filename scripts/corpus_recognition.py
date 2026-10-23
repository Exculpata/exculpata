"""Independent pinned faster-whisper recognition; production service is not exercised."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from pathlib import Path


ASR_WEIGHTS_FILE = 'model.bin'
ASR_FILES = ['config.json', ASR_WEIGHTS_FILE, 'preprocessor_config.json', 'tokenizer.json', 'vocabulary.json']
INDEPENDENT_ASR_ADAPTER = 'faster_whisper.WhisperModel'


def coverage_contract(*, full):
    """Describe the executed corpus paths, independently of a passing score."""
    product_paths = [
        'native_pdf_ingestion', 'media_transcript_import_and_projection', 'search',
        'question_dispatch_admission', 'extractive_answer_service', 'saved_citation_validation',
    ]
    if full:
        product_paths.extend(['image_pdf_ocr', 'media_ingestion_preflight'])
    return {
        'schema_version': 1,
        'product_paths': product_paths,
        'recognition': {
            'adapter': INDEPENDENT_ASR_ADAPTER if full else None,
            'mode': 'independent-offline-cpu' if full else 'prepared-transcript-fixture',
        },
        'not_exercised': [
            'production_transcription_service', 'production_transcription_worker',
            'production_audio_loading', 'production_decoding_options', 'production_segment_conversion',
            'whisperx_pipeline', 'whisperx_vad', 'forced_alignment', 'diarization',
            'translation', 'gpu_execution', 'learned_retrieval_quality', 'generative_model_quality',
        ],
    }


def coverage_summary(*, full):
    recognition = 'product OCR and independent faster-whisper recognition' if full else 'prepared transcripts; OCR and ASR excluded'
    return ('Coverage: product ingestion/search/projection and extractive answers; '
            + recognition + '; production transcription service: not exercised '
            '(worker, audio loading, decoding options, segment conversion, WhisperX, alignment, diarization).')


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
    if not re.fullmatch(r'[0-9a-f]{40}', pin['revision']):
        raise ValueError('ASR revision must be an immutable commit')
    model_path = snapshot(pin, offline=True)
    if Path(model_path).name != pin['revision']:
        raise ValueError('ASR snapshot is not the pinned revision')
    expected_digest = pin.get('sha256', {}).get(ASR_WEIGHTS_FILE)
    digest = hashlib.sha256()
    if expected_digest:
        with (Path(model_path) / ASR_WEIGHTS_FILE).open('rb') as weights:
            for chunk in iter(lambda: weights.read(1024 * 1024), b''):
                digest.update(chunk)
    if not expected_digest or digest.hexdigest() != expected_digest:
        raise ValueError('ASR model.bin does not match the pinned corpus digest')
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
        observations[case['id']] = {'text':' '.join(s['text'] for s in rows), 'language':info.language,
                                   'segments':rows, 'recognition_adapter':INDEPENDENT_ASR_ADAPTER}
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
