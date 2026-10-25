"""Synthetic hardware detection and model-tier boundaries."""
from __future__ import annotations

import subprocess

import pytest

from case_intelligence.hardware_profile import HardwareReading, detect_hardware, suggest_tier, validate_profiles


GIB = 1024 ** 3
MIB = 1024 ** 2


def completed(command, stdout="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def reading(*, ram_gib=16, vendor="none", devices=(), apple=False):
    per_device = tuple(int(value * GIB) for value in devices)
    return HardwareReading(
        8,
        int(ram_gib * GIB),
        vendor,
        max(per_device, default=None),
        apple,
        (),
        gpu_vram_per_device=per_device,
        gpu_vram_total_bytes=sum(per_device) or None,
        gpu_count=len(per_device),
    )


@pytest.mark.parametrize(
    ("installed_gib", "tier"),
    [(16, "apple-16"), (32, "apple-32"), (64, "apple-32")],
)
def test_apple_silicon_detection_records_chip_and_usable_memory(installed_gib, tier):
    def run(command):
        key = command[-1]
        if key == "hw.memsize":
            return completed(command, str(installed_gib * GIB))
        if key == "machdep.cpu.brand_string":
            return completed(command, "Apple M4 Pro")
        raise FileNotFoundError(command[0])

    detected = detect_hardware(run=run, system="Darwin", machine="arm64", cpu_count=12)
    assert detected.apple_chip == "Apple M4 Pro"
    assert detected.model_usable_memory_bytes == int(installed_gib * GIB * 0.75)
    assert suggest_tier(detected).profile_id == tier


@pytest.mark.parametrize(
    ("output", "devices"),
    [
        ("GPU[0] : VRAM Total Memory (B): 17179869184\n", (16,)),
        ("card0 VRAM Total Memory (MiB): 8192\ncard1 VRAM Total Memory (MiB): 24576\n", (8, 24)),
        ("gpu 0 | total vram: 12 GiB\ngpu 1 | total vram: 16 GiB\n", (12, 16)),
    ],
)
def test_rocm_detection_handles_format_differences_and_multiple_devices(output, devices):
    def run(command):
        if command[0] == "nvidia-smi":
            raise FileNotFoundError(command[0])
        if command[0] == "rocm-smi":
            return completed(command, output)
        raise AssertionError(command)

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=8, meminfo="MemTotal: 33554432 kB",
    )
    assert detected.gpu_vendor == "amd"
    assert detected.gpu_vram_per_device == tuple(value * GIB for value in devices)
    assert detected.gpu_vram_total_bytes == sum(devices) * GIB
    assert detected.gpu_vram_bytes == max(devices) * GIB


def test_amd_smi_is_used_when_rocm_smi_is_not_present():
    def run(command):
        if command[0] in {"nvidia-smi", "rocm-smi"}:
            raise FileNotFoundError(command[0])
        return completed(command, "GPU: 0\nVRAM Total Memory: 24576 MiB\n")

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=8, meminfo="MemTotal: 33554432 kB",
    )
    assert detected.gpu_vendor == "amd"
    assert detected.gpu_vram_per_device == (24 * GIB,)


@pytest.mark.parametrize(
    ("devices_mib", "tier"),
    [
        ((8192,), "gpu-8"), ((16384,), "gpu-16"), ((24576,), "gpu-24"),
        ((24576, 24576), "gpu-24"), ((46068,), "gpu-24"), ((46067, 49140), "gpu-24"),
    ],
)
def test_nvidia_per_device_total_and_tiers(devices_mib, tier):
    output = "\n".join(str(value) for value in devices_mib)

    def run(command):
        if command[0] == "nvidia-smi":
            return completed(command, output)
        raise AssertionError(command)

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=16, meminfo="MemTotal: 67108864 kB",
    )
    assert detected.gpu_count == len(devices_mib)
    assert detected.gpu_vram_per_device == tuple(value * MIB for value in devices_mib)
    assert detected.gpu_vram_total_bytes == sum(devices_mib) * MIB
    assert detected.gpu_vram_bytes == max(devices_mib) * MIB
    assert suggest_tier(detected).profile_id == tier


@pytest.mark.parametrize(
    ("sample", "tier"),
    [
        (reading(ram_gib=13.999), "potato"),
        (reading(ram_gib=16), "cpu"),
        (reading(vendor="nvidia", devices=(7.999,)), "cpu"),
        (reading(vendor="nvidia", devices=(8,)), "gpu-8"),
        (reading(vendor="nvidia", devices=(12,)), "gpu-12"),
        (reading(vendor="nvidia", devices=(16,)), "gpu-16"),
        (reading(vendor="nvidia", devices=(24,)), "gpu-24"),
    ],
)
def test_tier_boundaries(sample, tier):
    assert suggest_tier(sample).profile_id == tier


@pytest.mark.parametrize("failure", ["missing", "nonzero", "timeout", "unparseable"])
def test_gpu_failures_return_cpu_suggestion_with_a_note(failure):
    def run(command):
        if failure == "missing":
            raise FileNotFoundError(command[0])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 3)
        if failure == "nonzero":
            return completed(command, returncode=1)
        return completed(command, "not a memory report")

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=8, meminfo="MemTotal: 16777216 kB",
    )
    assert detected.gpu_vendor == "none"
    assert any("CPU suggestion" in note for note in detected.notes)
    assert suggest_tier(detected).profile_id == "cpu"


def test_profile_schema_and_local_tradeoff_contract():
    validate_profiles()
    from case_intelligence.hardware_profile import load_profiles

    catalog = load_profiles()
    profiles = catalog["profiles"]
    assert catalog["profile_aliases"]["cpu-16"] == "cpu"
    assert {item["id"] for item in profiles} >= {
        "potato", "cpu", "gpu-8", "gpu-12", "gpu-16", "gpu-24", "apple-16", "apple-32", "ultra",
    }
    for profile in profiles:
        assert profile["tradeoff"].endswith("This is a suggestion, not a measured quality result.")
        assert "http://" not in profile["generator"] and "https://" not in profile["generator"]


@pytest.mark.parametrize("reported_mib", [46068, 49140], ids=["L40S", "A6000-RTX6000Ada"])
def test_ultra_accepts_reported_48gb_cards(reported_mib):
    def run(command):
        assert command[0] == "nvidia-smi"
        return completed(command, f"{reported_mib}\n{reported_mib}\n")

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=16, meminfo="MemTotal: 67108864 kB",
    )
    assert detected.gpu_vram_per_device == (reported_mib * MIB,) * 2
    assert suggest_tier(detected).profile_id == "ultra"


@pytest.mark.parametrize("failure", ["missing", "error", "empty", "unparseable"])
@pytest.mark.parametrize("cores", [None, 8], ids=["no-cores", "cores-only"])
def test_no_usable_signal_falls_back_to_potato(failure, cores):
    def run(command):
        if failure == "missing":
            raise FileNotFoundError(command[0])
        if failure == "error":
            return completed(command, returncode=1)
        return completed(command, "" if failure == "empty" else "not a memory report")

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=cores, meminfo=None,
    )
    suggestion = suggest_tier(detected)
    assert suggestion.profile_id == "potato"
    assert suggestion.fallback is True
    assert "hardware detection :: using the potato CPU suggestion" in suggestion.lines()


@pytest.fixture
def rocm_two_48gb_devices():
    return """GPU[0] : VRAM Total Memory (B): 51539607552
GPU[0] : VRAM Total Used Memory (B): 1073741824
GPU[1] : VRAM Total Memory (B): 51539607552
GPU[1] : VRAM Total Used Memory (B): 2147483648
VRAM Total Memory (B): 103079215104
"""


def test_rocm_counts_only_device_total_rows(rocm_two_48gb_devices):
    def run(command):
        if command[0] == "rocm-smi":
            return completed(command, rocm_two_48gb_devices)
        raise FileNotFoundError(command[0])

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=16, meminfo="MemTotal: 67108864 kB",
    )
    assert detected.gpu_count == 2
    assert detected.gpu_vram_per_device == (51539607552, 51539607552)
    assert detected.gpu_vram_total_bytes == 103079215104
    assert suggest_tier(detected).profile_id == "ultra"


def test_amd_smi_accepts_total_vram_mb():
    def run(command):
        if command[0] != "amd-smi":
            raise FileNotFoundError(command[0])
        return completed(command, "GPU: 0\n  VRAM_USAGE:\n    TOTAL_VRAM: 49140 MB\n    USED_VRAM: 1024 MB\n")

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=8, meminfo="MemTotal: 33554432 kB",
    )
    assert detected.gpu_vendor == "amd"
    assert detected.gpu_count == 1
    assert detected.gpu_vram_per_device == (49140 * MIB,)
    assert suggest_tier(detected).profile_id == "gpu-24"


def test_profile_schema_requires_potato_fallback():
    from jsonschema import ValidationError
    from case_intelligence.hardware_profile import load_profiles

    catalog = load_profiles()
    catalog["fallback_profile"] = "cpu"
    with pytest.raises(ValidationError):
        validate_profiles(catalog)
