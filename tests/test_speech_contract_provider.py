"""Keep the local contract fixture limited to its fixed synthetic inputs."""
from pathlib import Path

import pytest

from tests.speech_contract_provider import ASR_PATH, TRANSLATION_PATH, contract_reply

RECORDING = Path(__file__).resolve().parents[1] / "src/case_intelligence/config/generated-speech.wav"


def test_asr_fixture_recognizes_only_the_packaged_recording():
    fields = {"model": "synthetic-asr", "response_format": "verbose_json",
              "timestamp_granularities[]": "segment", "file": RECORDING.read_bytes()}
    assert contract_reply(ASR_PATH, fields)["language"] == "es"
    for change in ({"model": "other"}, {"file": b"different recording"}):
        with pytest.raises(ValueError):
            contract_reply(ASR_PATH, fields | change)


def test_translation_fixture_keeps_opaque_segment_id_and_rejects_unrelated_text():
    fields = {"model": "synthetic-translation", "source_language": "es", "target_language": "en",
              "segments": [{"id": "keep-this-id", "text": "Grabación de prueba."}]}
    assert contract_reply(TRANSLATION_PATH, fields) == {
        "segments": [{"id": "keep-this-id", "text": "Test recording."}]}
    fields["segments"][0]["text"] = "Different synthetic input."
    with pytest.raises(ValueError):
        contract_reply(TRANSLATION_PATH, fields)
