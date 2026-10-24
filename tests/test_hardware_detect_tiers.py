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
    ("devices", "tier"),
    [((8,), "gpu-8"), ((16,), "gpu-16"), ((24,), "gpu-24"), ((24, 24), "gpu-24"), ((48, 48), "ultra")],
)
def test_nvidia_per_device_total_and_tiers(devices, tier):
    output = "\n".join(str(value * 1024) for value in devices)

    def run(command):
        if command[0] == "nvidia-smi":
            return completed(command, output)
        raise AssertionError(command)

    detected = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=16, meminfo="MemTotal: 67108864 kB",
    )
    assert detected.gpu_count == len(devices)
    assert detected.gpu_vram_per_device == tuple(value * GIB for value in devices)
    assert detected.gpu_vram_total_bytes == sum(devices) * GIB
    assert detected.gpu_vram_bytes == max(devices) * GIB
    assert suggest_tier(detected).profile_id == tier


@pytest.mark.parametrize(
    ("sample", "tier"),
    [
        (reading(ram_gib=15.999), "potato"),
        (reading(ram_gib=16), "cpu"),
        (reading(vendor="nvidia", devices=(7.999,)), "cpu"),
        (reading(vendor="nvidia", devices=(8,)), "gpu-8"),
        (reading(vendor="nvidia", devices=(12,)), "gpu-12"),
        (reading(vendor="nvidia", devices=(16,)), "gpu-16"),
        (reading(vendor="nvidia", devices=(24,)), "gpu-24"),
        (reading(vendor="nvidia", devices=(47.999, 48)), "gpu-24"),
        (reading(vendor="nvidia", devices=(48, 48)), "ultra"),
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
