"""Fixed entry point executed by the separately pinned Nemotron interpreter.

No Exculpata package imports: Python isolated mode keeps the WhisperX
environment and its dependency versions out of this process.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import resource
import sys
from pathlib import Path


def infer(request):
    import torch
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor
    from transformers.audio_utils import load_audio

    device = request["device"]
    if device != "cpu" and not re.fullmatch(r"cuda:[0-9]+", device):
        raise ValueError("Invalid device")
    fraction = request["gpu_memory_fraction"]
    if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("Invalid resource limit")
    if device.startswith("cuda:"):
        torch.cuda.set_per_process_memory_fraction(fraction, device=device)
    model_path, audio_path = Path(request["model_path"]), Path(request["audio_path"])
    if not model_path.is_dir() or not audio_path.is_file():
        raise ValueError("Local inputs are required")
    processor = AutoProcessor.from_pretrained(str(model_path), local_files_only=True,
                                             trust_remote_code=False)
    model = AutoModelForAudioFrameClassification.from_pretrained(
        str(model_path), local_files_only=True, trust_remote_code=False,
        dtype=torch.float32).to(device).eval()
    sampling_rate = processor.feature_extractor.sampling_rate
    audio = load_audio(str(audio_path), sampling_rate=sampling_rate)
    inputs = processor(audio, sampling_rate=sampling_rate).to(device, dtype=torch.float32)
    with torch.inference_mode():
        logits = model(**inputs).logits
    segments = processor.extract_speaker_dict(logits, inputs.attention_mask)[0]
    return [{"start": float(item["Start"]), "end": float(item["End"]),
             "speaker": int(item["Speaker"])} for item in segments]


def main():
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    try:
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Oversized request")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {
                "model_path", "audio_path", "device", "gpu_memory_fraction"}:
            raise ValueError("Invalid request")
        # Libraries occasionally print progress to stdout. Protocol output must
        # contain one JSON object only; the parent discards dependency stderr.
        with contextlib.redirect_stdout(sys.stderr):
            turns = infer(request)
        json.dump({"schema": "recordbench-nemotron-v1", "turns": turns}, sys.stdout,
                  allow_nan=False)
        return 0
    except Exception:
        # No traceback, media paths or dependency exception text in logs.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
