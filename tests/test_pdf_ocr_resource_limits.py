"""Synthetic real-process checks for OCR's enforced output-file boundary."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

resource = pytest.importorskip("resource")
from case_intelligence import ocr_process_helper, pilot_uploads

HELPER = Path(ocr_process_helper.__file__)
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "OMP_THREAD_LIMIT": "1"}
RASTER = b"P5\n1 1\n255\n\0"
# Keep the helper's ignored SIGXFSZ disposition: excess writes raise EFBIG.
WRITER = """
import pathlib, sys
pathlib.Path(sys.argv[1]).with_suffix('.txt').write_text('synthetic body')
with open(sys.argv[1], 'wb', buffering=0) as output:
    for _ in range(257):
        output.write(b'x' * 65536)
"""


def test_real_ocr_child_cannot_write_past_file_cap(tmp_path):
    output = tmp_path / "synthetic.tsv"
    inherited = resource.getrlimit(resource.RLIMIT_FSIZE)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(HELPER), str(pilot_uploads.MAX_OCR_TSV_BYTES),
         sys.executable, "-I", "-S", "-c", WRITER, str(output)],
        capture_output=True, timeout=10, env=ENVIRONMENT,
    )
    assert result.returncode > 0
    assert 0 < output.stat().st_size <= pilot_uploads.MAX_OCR_TSV_BYTES
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == inherited


def test_ocr_limit_is_classified_and_temporary_outputs_removed(monkeypatch):
    run = subprocess.run
    observations = []

    def synthetic_tesseract(command, **kwargs):
        index = command.index("/usr/bin/tesseract")
        base = Path(command[index + 2])
        # Keep the real helper, its exact cap and timeout; replace only the
        # unavailable platform executable with a synthetic oversized writer.
        result = run(command[:index] + [sys.executable, "-I", "-S", "-c", WRITER, str(base) + ".tsv"], **kwargs)
        observations.append((base.parent, base.with_suffix(".tsv").stat().st_size))
        return result

    monkeypatch.setattr(pilot_uploads.subprocess, "run", synthetic_tesseract)
    with pytest.raises(pilot_uploads._OcrStop) as raised:
        pilot_uploads._tesseract_reading(RASTER, "eng", "1", 10, ENVIRONMENT)
    assert raised.value.status == "output_limit"
    directory, actual_size = observations[0]
    assert actual_size <= pilot_uploads.MAX_OCR_TSV_BYTES
    assert not directory.exists()


def test_reached_file_cap_is_rejected_even_if_executable_reports_success(monkeypatch):
    run = subprocess.run
    observed_sizes = []
    # Python ignores SIGXFSZ itself; catch EFBIG to simulate an executable that
    # reports success despite its truncated output. The read must still fail.
    script = """
import pathlib, sys
try:
    with open(sys.argv[1] + '.tsv', 'wb', buffering=0) as output:
        for _ in range(257):
            output.write(b'x' * 65536)
except OSError:
    pass
pathlib.Path(sys.argv[1] + '.txt').write_text('synthetic body')
"""

    def synthetic_tesseract(command, **kwargs):
        index = command.index("/usr/bin/tesseract")
        base = Path(command[index + 2])
        result = run(command[:index] + [sys.executable, "-I", "-S", "-c", script, str(base)], **kwargs)
        assert result.returncode == 0
        observed_sizes.append(base.with_suffix(".tsv").stat().st_size)
        return result

    monkeypatch.setattr(pilot_uploads.subprocess, "run", synthetic_tesseract)
    with pytest.raises(pilot_uploads._OcrStop) as raised:
        pilot_uploads._tesseract_reading(RASTER, "eng", "1", 10, ENVIRONMENT)
    assert raised.value.status == "output_limit"
    assert observed_sizes == [pilot_uploads.MAX_OCR_TSV_BYTES]


@pytest.mark.parametrize("include_table", [False, True])
def test_real_bounded_child_returns_small_reading_and_cleans_up(monkeypatch, include_table):
    run = subprocess.run
    directories = []
    script = """
import pathlib, sys
pathlib.Path(sys.argv[1] + '.txt').write_text('synthetic body')
if sys.argv[2] == 'True':
    pathlib.Path(sys.argv[1] + '.tsv').write_text('header\\n5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t1\\t1\\t95\\tbody\\n')
print('ignored synthetic OCR stdout')
"""

    def synthetic_tesseract(command, **kwargs):
        index = command.index("/usr/bin/tesseract")
        base = Path(command[index + 2])
        directories.append(base.parent)
        assert Path(kwargs["cwd"]) == base.parent
        return run(command[:index] + [sys.executable, "-I", "-S", "-c", script, str(base), str(include_table)], **kwargs)

    monkeypatch.setattr(pilot_uploads.subprocess, "run", synthetic_tesseract)
    reading = pilot_uploads._tesseract_reading(RASTER, "eng", "1", 10, ENVIRONMENT)
    assert reading == pilot_uploads._OcrReading("synthetic body", (("body", 95.0),) if include_table else ())
    assert not directories[0].exists()


def test_exec_preserves_parent_timeout_and_cleanup(monkeypatch):
    run = subprocess.run
    directories = []
    script = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('synthetic'); time.sleep(30)"

    def synthetic_tesseract(command, **kwargs):
        index = command.index("/usr/bin/tesseract")
        base = Path(command[index + 2])
        directories.append(base.parent)
        return run(command[:index] + [sys.executable, "-I", "-S", "-c", script, str(base) + ".txt"], **kwargs)

    monkeypatch.setattr(pilot_uploads.subprocess, "run", synthetic_tesseract)
    with pytest.raises(subprocess.TimeoutExpired):
        pilot_uploads._tesseract_reading(RASTER, "eng", "1", 0.2, ENVIRONMENT)
    assert not directories[0].exists()


def test_helper_preserves_a_tighter_inherited_file_limit(tmp_path):
    # Set the inherited limit in a separate launcher, never in pytest's process
    # or a threaded caller's preexec_fn.
    launcher = """
import os, resource, sys
resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
os.execv(sys.executable, [sys.executable, '-I', '-S', *sys.argv[1:]])
"""
    output = tmp_path / "synthetic.tsv"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(HELPER), str(pilot_uploads.MAX_OCR_TSV_BYTES),
         sys.executable, "-I", "-S", "-c", WRITER, str(output)],
        capture_output=True, timeout=10, env=ENVIRONMENT,
    )
    assert result.returncode > 0
    assert output.stat().st_size == 1024


def test_native_overflow_does_not_trigger_a_core_dump(tmp_path):
    dd = shutil.which("dd")
    if dd is None:
        pytest.skip("native dd is required to check exec-inherited signal disposition")
    parent_core_limit = resource.getrlimit(resource.RLIMIT_CORE)
    if parent_core_limit[1] == 0:
        pytest.skip("inherited hard core limit cannot enable dumps for this regression")
    launcher = """
import os, resource, sys
soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
enabled = 1048576 if hard == resource.RLIM_INFINITY else min(1048576, hard)
resource.setrlimit(resource.RLIMIT_CORE, (enabled, hard))
os.execv(sys.executable, [sys.executable, '-I', '-S', *sys.argv[1:]])
"""
    output = tmp_path / "synthetic.tsv"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(HELPER), "1024", dd,
         "if=/dev/zero", f"of={output}", "bs=1024", "count=2"],
        capture_output=True, timeout=10, env=ENVIRONMENT, cwd=tmp_path,
    )
    # A positive exit proves an ordinary write error, rather than a signal that
    # might send a dump to an external collector outside this directory.
    assert result.returncode > 0
    assert output.stat().st_size == 1024
    assert list(tmp_path.iterdir()) == [output]
    assert resource.getrlimit(resource.RLIMIT_CORE) == parent_core_limit


@pytest.mark.parametrize("suffix", [".txt", ".tsv"])
@pytest.mark.parametrize("hard", [1024, resource.RLIM_INFINITY], ids=["tight-hard", "soft-only"])
def test_successful_truncated_output_uses_the_actual_inherited_limit(monkeypatch, suffix, hard):
    run = subprocess.run
    observations = []
    launcher = """
import os, resource, sys
resource.setrlimit(resource.RLIMIT_FSIZE, (1024, int(sys.argv[1])))
os.execv(sys.argv[2], sys.argv[2:])
"""
    script = """
import pathlib, sys
base, suffix = sys.argv[1:]
pathlib.Path(base + '.txt').write_text('synthetic body')
pathlib.Path(base + '.tsv').write_text('header\\n')
try:
    with open(base + suffix, 'wb', buffering=0) as output:
        output.write(b'x' * 1025)
except OSError:
    pass
"""

    def synthetic_tesseract(command, **kwargs):
        index = command.index("/usr/bin/tesseract")
        base = Path(command[index + 2])
        bounded = command[:index] + [sys.executable, "-I", "-S", "-c", script, str(base), suffix]
        result = run([sys.executable, "-I", "-S", "-c", launcher, str(hard), *bounded], **kwargs)
        assert result.returncode == 0
        observations.append((base.parent, base.with_suffix(suffix).stat().st_size))
        return result

    monkeypatch.setattr(pilot_uploads.subprocess, "run", synthetic_tesseract)
    with pytest.raises(pilot_uploads._OcrStop) as raised:
        pilot_uploads._tesseract_reading(RASTER, "eng", "1", 10, ENVIRONMENT)
    assert raised.value.status == "output_limit"
    directory, size = observations[0]
    assert size == 1024
    assert not directory.exists()


def test_zero_inherited_cap_is_reported_as_output_limit(monkeypatch):
    run = subprocess.run
    launcher = """
import os, resource, sys
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
os.execv(sys.argv[1], sys.argv[1:])
"""

    def synthetic_tesseract(command, **kwargs):
        index = command.index("/usr/bin/tesseract")
        bounded = command[:index] + [sys.executable, "-I", "-S", "-c", "pass"]
        result = run([sys.executable, "-I", "-S", "-c", launcher, *bounded], **kwargs)
        assert result.returncode == 0
        assert result.stdout == (f"{LEGACY_STEM}-ocr-limit:0\n").encode()
        return result

    monkeypatch.setattr(pilot_uploads.subprocess, "run", synthetic_tesseract)
    with pytest.raises(pilot_uploads._OcrStop) as raised:
        pilot_uploads._tesseract_reading(RASTER, "eng", "1", 10, ENVIRONMENT)
    assert raised.value.status == "output_limit"


@pytest.mark.parametrize("receipt", [b"", (f"{LEGACY_STEM}-ocr-limit:1024").encode(), (f"{LEGACY_STEM}-ocr-limit:-1\n").encode(),
                                    (f"{LEGACY_STEM}-ocr-limit:16777217\n").encode(), (f"junk\n{LEGACY_STEM}-ocr-limit:1024\n").encode()])
def test_success_without_valid_limit_receipt_fails_closed(monkeypatch, receipt):
    directories = []

    def synthetic_tesseract(command, **kwargs):
        index = command.index("/usr/bin/tesseract")
        base = Path(command[index + 2])
        directories.append(base.parent)
        base.with_suffix(".txt").write_text("synthetic body")
        return SimpleNamespace(returncode=0, stdout=receipt)

    monkeypatch.setattr(pilot_uploads.subprocess, "run", synthetic_tesseract)
    with pytest.raises(pilot_uploads._OcrStop) as raised:
        pilot_uploads._tesseract_reading(RASTER, "eng", "1", 10, ENVIRONMENT)
    assert raised.value.status == "failed"
    assert not directories[0].exists()


@pytest.mark.parametrize("arguments", [[], ["invalid"], ["0", "/bin/echo"], ["-1", "/bin/echo"],
                                       [str(pilot_uploads.MAX_OCR_TSV_BYTES + 1), "/bin/echo"], ["1024", "relative"]])
def test_helper_refuses_invalid_limits_or_commands(arguments):
    result = subprocess.run([sys.executable, "-I", "-S", str(HELPER), *arguments],
                            capture_output=True, timeout=10, env=ENVIRONMENT)
    assert result.returncode == 125


def test_failed_limit_installation_never_executes(monkeypatch):
    def refuse_limit(*args):
        raise OSError("synthetic unsupported limit")

    def unexpected_exec(*args):
        raise AssertionError("unbounded executable must not run")

    monkeypatch.setattr(ocr_process_helper.resource, "setrlimit", refuse_limit)
    monkeypatch.setattr(ocr_process_helper.os, "execv", unexpected_exec)
    assert ocr_process_helper.main(["1024", "/usr/bin/tesseract"]) == 125
