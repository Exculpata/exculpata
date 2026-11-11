"""Measure warmed synthetic builds without tracing neighboring test allocations."""
import gc
import json
import subprocess
import sys
import tracemalloc
from pathlib import Path


def _streaming_peak(tmp_path, count, generator, count_keyword):
    generator(tmp_path / f"warm-{count}", **{count_keyword: 20})
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        generator(tmp_path / f"measured-{count}", **{count_keyword: count})
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def streaming_peaks(
    tmp_path, generator, *, module, counts=(1000, 4000), count_keyword="filler_chunks",
):
    child = '''
import importlib
import json
import sys
from pathlib import Path
from tests.streaming_memory import _streaming_peak

generator = getattr(importlib.import_module(sys.argv[3]), sys.argv[4])
peak = _streaming_peak(Path(sys.argv[1]), int(sys.argv[2]), generator, sys.argv[5])
print(json.dumps(peak))
'''
    peaks = []
    for count in counts:
        result = subprocess.run(
            [sys.executable, "-c", child, str(tmp_path), str(count),
             module, generator.__name__, count_keyword],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, check=True, timeout=60,
        )
        peaks.append(json.loads(result.stdout))
    return peaks


def assert_flat_peaks(peaks):
    # Four times the records may use 25% or 256 KB more for allocator noise.
    assert peaks[1] <= max(peaks[0] * 1.25, peaks[0] + 256_000), peaks
