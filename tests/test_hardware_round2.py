"""Synthetic regressions for tier boundaries and per-device GPU parsing."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import ValidationError

from case_intelligence import hardware_profile as hardware


MIB = 1024 ** 2
ROOT = Path(hardware.__file__).resolve().parents[2]
TOOLS = ("nvidia-smi", "rocm-smi", "amd-smi")
BAD_MEMORY = [
    pytest.param("inf", id="inf"),
    pytest.param("-inf", id="negative-inf"),
    pytest.param("nan", id="nan"),
    pytest.param("1e309", id="exponent-overflow"),
    pytest.param("9" * 400, id="huge"),
    pytest.param("1" + "0" * 308, id="scaled-overflow"),
    pytest.param("[N/A]", id="unreadable"),
    pytest.param("0", id="zero"),
    pytest.param("-1", id="negative"),
]


@pytest.fixture
def installer(monkeypatch):
    # Resolve beside the imported source so archived-source runs load archived scripts.
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("round2_installer", ROOT / "scripts" / "exculpata_install.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.PROJECT == ROOT
    return module


@pytest.mark.parametrize(("ram_mib", "expected"), [(14335, "potato"), (14336, "cpu"), (15.4 * 1024, "cpu")])
def test_reported_cpu_ram_boundary(ram_mib, expected):
    detected = hardware.detect_hardware(
        run=lambda command: subprocess.CompletedProcess(command, 1, "", ""),
        system="Linux", machine="x86_64", cpu_count=8,
        meminfo=f"MemTotal: {int(ram_mib * 1024)} kB",
    )
    suggestion = hardware.suggest_tier(detected)
    assert suggestion.profile_id == expected
    assert suggestion.fallback is False
    profiles = hardware.load_profiles()["profiles"]
    amount = detected.ram_bytes // MIB
    matches = [p["id"] for p in profiles if p["accelerator"] == "cpu"
               and p.get("min_ram_mib", 0) <= amount <= p.get("max_ram_mib", amount)]
    assert matches == [expected]


@pytest.mark.parametrize(("profile_id", "field", "wrong"), [
    ("cpu", "min_ram_mib", 14335), ("cpu", "min_ram_mib", 14337),
    ("potato", "max_ram_mib", 14334), ("potato", "max_ram_mib", 14336),
])
def test_schema_rejects_changed_cpu_boundary(profile_id, field, wrong):
    catalog = hardware.load_profiles()
    next(p for p in catalog["profiles"] if p["id"] == profile_id)[field] = wrong
    with pytest.raises(ValidationError):
        hardware.validate_profiles(catalog)


def tool_output(tool, values):
    if tool == "nvidia-smi":
        return "\n".join(values)
    if tool == "rocm-smi":
        return "\n".join(f"GPU[{index}] : VRAM Total Memory (MiB): {value}" for index, value in enumerate(values))
    return "\n".join(f"GPU: {index}\n  TOTAL_VRAM: {value} MB" for index, value in enumerate(values))


def synthetic_detection(tool, values):
    def run(command):
        if command[0] != tool:
            raise FileNotFoundError(command[0])
        return subprocess.CompletedProcess(command, 0, tool_output(tool, values), "")

    return hardware.detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=8, meminfo="MemTotal: 33554432 kB",
    )


def assert_devices(suggestion, mixed, tool):
    detected = suggestion.reading
    expected = (8192 * MIB, 24576 * MIB) if mixed else ()
    assert detected.gpu_vram_per_device == expected
    assert detected.gpu_count == len(expected)
    assert detected.gpu_vram_total_bytes == (sum(expected) or None)
    assert detected.gpu_vram_bytes == max(expected, default=None)
    vendor = "nvidia" if tool == "nvidia-smi" else "amd"
    assert detected.gpu_vendor == (vendor if mixed else "none")
    assert suggestion.profile_id == ("gpu-24" if mixed else "cpu")


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("bad", BAD_MEMORY)
@pytest.mark.parametrize("mixed", [False, True], ids=["invalid-only", "valid-neighbors"])
def test_probe_rejects_unusable_device_memory(tool, bad, mixed):
    values = ["8192", bad, "24576"] if mixed else [bad]
    assert_devices(hardware.suggest_tier(synthetic_detection(tool, values)), mixed, tool)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("bad", BAD_MEMORY)
@pytest.mark.parametrize("mixed", [False, True], ids=["invalid-only", "valid-neighbors"])
def test_installer_rejects_unusable_device_memory(installer, monkeypatch, tool, bad, mixed):
    values = ["8192", bad, "24576"] if mixed else [bad]
    monkeypatch.setattr(installer, "detect_hardware", lambda **kwargs: synthetic_detection(tool, values))
    assert_devices(installer._hardware_suggestion(), mixed, tool)


@pytest.mark.parametrize("bad", BAD_MEMORY)
@pytest.mark.parametrize("mixed", [False, True], ids=["invalid-only", "valid-neighbors"])
def test_installer_inventory_rejects_unusable_device_memory(installer, bad, mixed):
    invalid = f"1, Synthetic Unusable, {bad}, 0, 8.0\n"
    value = f"0, Synthetic Small, 8192, 8000, 8.0\n{invalid}2, Synthetic Large, 24576, 24000, 8.0\n" if mixed else invalid
    with pytest.raises(RuntimeError, match="nvidia-smi returned an unreadable GPU inventory row"):
        installer._parse_gpu_inventory(value)


@pytest.mark.parametrize("bad_row", ["[N/A]\n", "1, Synthetic Unreadable, 24576, [N/A], 8.0\n"])
def test_installer_inventory_rejects_unreadable_row(installer, bad_row):
    with pytest.raises(RuntimeError, match="nvidia-smi returned an unreadable GPU inventory row"):
        installer._parse_gpu_inventory(bad_row + "2, Synthetic Valid, 24576, 0, 8.0\n")
    devices = installer._parse_gpu_inventory("2, Synthetic Valid, 24576, 0, 8.0\n")
    assert tuple(device.index for device in devices) == ("2",)
    assert devices[0].free_mib == 0


@pytest.mark.parametrize("bad_count", [0, 1, 2])
def test_nvidia_probe_exposes_content_free_skip_count(caplog, bad_count):
    detected = synthetic_detection("nvidia-smi", ["8192"] + ["synthetic-private-row"] * bad_count + ["24576"])
    assert detected.gpu_count == 2
    message = f"nvidia-smi skipped {bad_count} malformed row(s)."
    if bad_count:
        assert message in detected.notes
        assert [record.getMessage() for record in caplog.records] == [message]
        assert f"hardware note :: {message}" in hardware.suggest_tier(detected).lines()
    else:
        assert caplog.records == []
        assert not any("skipped" in note for note in detected.notes)
    assert "synthetic-private-row" not in str(detected.notes) + caplog.text


@pytest.mark.parametrize("bad_count", [0, 1, 2])
@pytest.mark.parametrize("bad_row", [
    "synthetic-private-row", "1, Synthetic, inf, 0, 8.0",
    "1, Synthetic, 8192, [N/A], 8.0", ", Synthetic, 8192, 0, 8.0",
])
def test_installer_inventory_fails_with_content_free_diagnostic(installer, caplog, bad_count, bad_row):
    value = "\n".join(
        ["0, Synthetic Valid, 8192, 0, 8.0", ""] + [bad_row] * bad_count
    )
    if bad_count:
        with pytest.raises(RuntimeError, match="^nvidia-smi returned an unreadable GPU inventory row$"):
            installer._parse_gpu_inventory(value)
    else:
        devices = installer._parse_gpu_inventory(value)
        assert tuple(device.index for device in devices) == ("0",)
    assert caplog.records == []
