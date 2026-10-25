"""Suggest a model tier from detected hardware. Nothing is downloaded."""
from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping



_CATALOG = Path(__file__).resolve().parents[2] / "config" / "model_profiles.json"
_SCHEMA = Path(__file__).resolve().parents[2] / "config" / "model_profiles.schema.json"
_GIB = 1024 ** 3
_MIB = 1024 ** 2
_APPLE_MODEL_MEMORY_FRACTION = 0.75


@dataclass(frozen=True)
class HardwareReading:
    cpu_cores: int | None
    ram_bytes: int | None
    gpu_vendor: str
    gpu_vram_bytes: int | None
    apple_silicon: bool
    notes: tuple[str, ...]
    gpu_vram_per_device: tuple[int, ...] = ()
    gpu_vram_total_bytes: int | None = None
    gpu_count: int = 0
    apple_chip: str | None = None
    model_usable_memory_bytes: int | None = None


@dataclass(frozen=True)
class TierSuggestion:
    profile_id: str
    label: str
    generator: str
    embed_rerank: str
    asr: str
    tradeoff: str
    fallback: bool
    reading: HardwareReading

    def lines(self) -> tuple[str, ...]:
        hardware = _hardware_line(self.reading)
        notes = tuple(f"hardware note :: {note}" for note in self.reading.notes)
        fallback = (f"hardware detection :: using the {self.profile_id} CPU suggestion",) if self.fallback else ()
        return (
            hardware,
            *notes,
            *fallback,
            f"suggested tier :: {self.profile_id}; {self.label}",
            f"suggested generator :: {self.generator}",
            f"embed and rerank :: {self.embed_rerank}",
            f"speech recognition :: {self.asr}",
            f"tradeoff :: {self.tradeoff}",
            "model download :: not started",
        )


def load_profiles(path: Path | None = None) -> dict[str, object]:
    catalog = path or _CATALOG
    value = json.loads(catalog.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("model profile catalog is invalid")
    return value


def profile_schema(path: Path | None = None) -> dict[str, object]:
    from jsonschema import Draft202012Validator

    schema = json.loads((path or _SCHEMA).read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("model profile schema is invalid")
    Draft202012Validator.check_schema(schema)
    return schema


def validate_profiles(catalog: Mapping[str, object] | None = None, schema: Mapping[str, object] | None = None) -> None:
    from jsonschema import Draft202012Validator

    document = dict(catalog or load_profiles())
    Draft202012Validator(dict(schema or profile_schema())).validate(document)
    profiles = document["profiles"]
    if not isinstance(profiles, list):
        raise ValueError("model profile catalog is invalid")
    identifiers = [item["id"] for item in profiles if isinstance(item, dict)]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("model profile identifiers must be unique")
    if document.get("fallback_profile") not in identifiers:
        raise ValueError("model profile fallback is missing")
    aliases = document.get("profile_aliases", {})
    if not isinstance(aliases, dict) or any(target not in identifiers for target in aliases.values()):
        raise ValueError("model profile alias target is missing")


def suggest_tier(reading: HardwareReading, catalog: Mapping[str, object] | None = None) -> TierSuggestion:
    """Pick one profile. A failed reading uses the smallest CPU profile and says so."""

    document = dict(catalog or load_profiles())
    profiles = [item for item in document.get("profiles", []) if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in profiles}
    fallback_id = str(document.get("fallback_profile") or "potato")
    chosen, fallback = _match(reading, profiles, fallback_id)
    profile = by_id.get(chosen) or by_id.get(fallback_id) or {}
    if not profile:
        profile = {
            "id": "potato",
            "label": "CPU only",
            "generator": "Use a small model on a local server.",
            "embed_rerank": "Keep embeddings and reranking inside Exculpata on CPU.",
            "asr": "Skip transcription or use a small int8 model.",
            "tradeoff": "Hardware detection did not match a profile.",
        }
        fallback = True
    return TierSuggestion(
        profile_id=str(profile.get("id") or "potato"),
        label=str(profile.get("label") or "CPU only"),
        generator=str(profile.get("generator") or ""),
        embed_rerank=str(profile.get("embed_rerank") or ""),
        asr=str(profile.get("asr") or ""),
        tradeoff=str(profile.get("tradeoff") or ""),
        fallback=fallback or chosen == fallback_id and _no_signal(reading),
        reading=reading,
    )


def detect_hardware(
    *,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    system: str | None = None,
    machine: str | None = None,
    cpu_count: int | None | object = ...,
    meminfo: str | None | object = ...,
) -> HardwareReading:
    """Read CPU, RAM, and GPU memory. A missing tool is a note, never an exception."""

    notes: list[str] = []
    cores = _cpu_cores(cpu_count, notes)
    ram = _ram_bytes(meminfo, system, run, notes)
    apple = _apple_silicon(system, machine)
    apple_chip = _apple_chip(run, notes) if apple else None
    vendor, devices = _gpu(run, apple, notes)
    vram = max(devices, default=None)
    if vendor == "none" and not apple:
        notes.append("No GPU tool reported VRAM; using the CPU suggestion.")
    usable = int(ram * _APPLE_MODEL_MEMORY_FRACTION) if apple and ram is not None else None
    return HardwareReading(
        cores, ram, vendor, vram, apple, tuple(notes), tuple(devices), sum(devices) or None,
        len(devices), apple_chip, usable,
    )


def _cpu_cores(cpu_count: int | None | object, notes: list[str]) -> int | None:
    if cpu_count is ...:
        value = os.cpu_count()
    else:
        value = cpu_count  # type: ignore[assignment]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        notes.append("CPU core count was not available.")
        return None
    return value


def _ram_bytes(
    meminfo: str | None | object,
    system: str | None,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    notes: list[str],
) -> int | None:
    host = system if system is not None else platform.system()
    if meminfo is ...:
        if host == "Darwin":
            return _darwin_ram(run, notes)
        return _linux_ram(None, notes)
    if meminfo is None:
        notes.append("System RAM was not available.")
        return None
    if host == "Darwin" and isinstance(meminfo, str) and meminfo.strip().isdigit():
        return int(meminfo.strip())
    return _linux_ram(str(meminfo), notes)


def _linux_ram(text: str | None, notes: list[str]) -> int | None:
    if text is None:
        try:
            text = Path("/proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            notes.append("System RAM was not available.")
            return None
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    if match is None:
        notes.append("System RAM was not available.")
        return None
    return int(match.group(1)) * 1024


def _darwin_ram(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    notes: list[str],
) -> int | None:
    if shutil.which("sysctl") is None and run is None:
        notes.append("System RAM was not available.")
        return None
    try:
        completed = _run(["sysctl", "-n", "hw." + "memsize"], run)
    except (OSError, subprocess.TimeoutExpired):
        notes.append("System RAM was not available.")
        return None
    if completed.returncode != 0 or not completed.stdout.strip().isdigit():
        notes.append("System RAM was not available.")
        return None
    return int(completed.stdout.strip())


def _apple_silicon(system: str | None, machine: str | None) -> bool:
    host = system if system is not None else platform.system()
    arch = machine if machine is not None else platform.machine()
    return host == "Darwin" and arch.lower() in {"arm64", "aarch64"}


def _apple_chip(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    notes: list[str],
) -> str | None:
    """Read the Apple chip name from injected or local sysctl output."""
    try:
        completed = _run(["sysctl", "-n", "machdep.cpu.brand_string"], run)
    except (OSError, subprocess.TimeoutExpired):
        notes.append("Apple chip name was not available.")
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        notes.append("Apple chip name was not available.")
        return None
    return value


def _gpu(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    apple: bool,
    notes: list[str],
) -> tuple[str, tuple[int, ...]]:
    if apple:
        return "apple", ()
    nvidia = _nvidia_vram_devices(run, notes)
    if nvidia:
        return "nvidia", nvidia
    amd = _amd_vram_devices(run, notes)
    if amd:
        return "amd", amd
    return "none", ()


def _nvidia_vram_devices(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    notes: list[str],
) -> tuple[int, ...]:
    if run is None and shutil.which("nvidia-smi") is None:
        return ()
    try:
        completed = _run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            run,
        )
    except (OSError, subprocess.TimeoutExpired):
        notes.append("GPU memory was not available.")
        return ()
    if completed.returncode != 0:
        notes.append("GPU memory was not available.")
        return ()
    totals: list[int] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        value = parse_vram_bytes(stripped.split()[0], _MIB)
        if value is None:
            notes.append("GPU memory figure was unreadable.")
            continue
        totals.append(value)
    if not totals:
        notes.append("GPU memory was not available.")
        return ()
    return tuple(totals)


def parse_vram_bytes(value: str, multiplier: int) -> int | None:
    """Convert a tool's memory figure; unusable devices have no capacity."""
    try:
        amount = float(value) * multiplier
        if not math.isfinite(amount):
            return None
        result = int(amount)
    except (ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _amd_vram_devices(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    notes: list[str],
) -> tuple[int, ...]:
    for tool, command in (
        ("rocm-smi", ["rocm-smi", "--showmeminfo", "vram"]),
        ("amd-smi", ["amd-smi", "metric", "--mem-usage"]),
    ):
        if run is None and shutil.which(tool) is None:
            continue
        try:
            completed = _run(command, run)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        values = _parse_amd_vram(completed.stdout, tool)
        if values:
            return values
    notes.append("AMD GPU memory was not available or was unreadable.")
    return ()


def _parse_amd_vram(text: str, tool: str) -> tuple[int, ...]:
    totals: list[int] = []
    for line in text.splitlines():
        if tool == "rocm-smi":
            device_row = re.fullmatch(
                r"\s*(?:GPU\[\d+\]|card\d+|gpu\s+\d+)\s*(?:[:|]\s*)?(.*)", line, re.IGNORECASE,
            )
            if device_row is None:
                continue
            line = device_row.group(1)
        match = re.fullmatch(
            r"\s*(?:VRAM Total Memory|total vram|TOTAL_VRAM)\s*"
            r"(?:\((B|MiB|MB|GiB|GB)\))?\s*:\s*(\d+(?:\.\d+)?)\s*(B|MiB|MB|GiB|GB)?\s*",
            line, re.IGNORECASE,
        )
        if match is None:
            continue
        unit = (match.group(1) or match.group(3) or "").casefold()
        multiplier = {"b": 1, "mib": _MIB, "mb": _MIB, "gib": _GIB, "gb": _GIB}.get(unit)
        if multiplier is not None:
            value = parse_vram_bytes(match.group(2), multiplier)
            if value is not None:
                totals.append(value)
    return tuple(totals)


def _run(
    command: list[str],
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
) -> subprocess.CompletedProcess[str]:
    if run is not None:
        return run(command)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
        stdin=subprocess.DEVNULL,
    )


def _match(
    reading: HardwareReading,
    profiles: list[dict[str, object]],
    fallback_id: str,
) -> tuple[str, bool]:
    if _no_signal(reading):
        return fallback_id, True
    if reading.apple_silicon:
        apple_memory = reading.model_usable_memory_bytes
        if apple_memory is None and reading.ram_bytes is not None:
            apple_memory = int(reading.ram_bytes * _APPLE_MODEL_MEMORY_FRACTION)
        apple = _band(profiles, "apple", _mib(apple_memory), "min_ram_mib", "max_ram_mib")
        if apple is not None:
            return apple, False
    if reading.gpu_vram_bytes is not None and reading.gpu_vendor in {"nvidia", "amd"}:
        ultra = _ultra(profiles, reading)
        if ultra is not None:
            return ultra, False
        gpu = _band(profiles, "gpu", _mib(reading.gpu_vram_bytes), "min_vram_mib", "max_vram_mib")
        if gpu is not None:
            return gpu, False
    cpu = _band(profiles, "cpu", _mib(reading.ram_bytes), "min_ram_mib", "max_ram_mib")
    if cpu is not None:
        return cpu, False
    return fallback_id, True


def _ultra(profiles: list[dict[str, object]], reading: HardwareReading) -> str | None:
    devices = reading.gpu_vram_per_device
    for profile in profiles:
        minimum_devices = profile.get("min_devices")
        minimum_vram = profile.get("min_device_vram_mib")
        if profile.get("accelerator") != "multi-gpu" or not isinstance(minimum_devices, int):
            continue
        if not isinstance(minimum_vram, int) or len(devices) < minimum_devices:
            continue
        if sum(value >= minimum_vram * _MIB for value in devices) >= minimum_devices:
            return str(profile.get("id"))
    return None


def _band(
    profiles: list[dict[str, object]],
    accelerator: str,
    amount: int | None,
    minimum_key: str,
    maximum_key: str,
) -> str | None:
    if amount is None:
        return None
    matches: list[tuple[int, str]] = []
    for profile in profiles:
        if profile.get("accelerator") != accelerator:
            continue
        minimum = profile.get(minimum_key, 0)
        maximum = profile.get(maximum_key)
        if isinstance(minimum, bool) or not isinstance(minimum, int):
            minimum = 0
        if amount < minimum:
            continue
        if isinstance(maximum, int) and not isinstance(maximum, bool) and amount > maximum:
            continue
        matches.append((minimum, str(profile.get("id"))))
    if not matches:
        return None
    return max(matches)[1]


def _mib(value: int | None) -> int | None:
    if value is None:
        return None
    return value // _MIB


def _no_signal(reading: HardwareReading) -> bool:
    return (
        reading.cpu_cores is None
        and reading.ram_bytes is None
        and reading.gpu_vram_bytes is None
        and not reading.apple_silicon
    )


def _hardware_line(reading: HardwareReading) -> str:
    cores = f"{reading.cpu_cores} CPU cores" if reading.cpu_cores is not None else "CPU cores unavailable"
    if reading.apple_silicon:
        memory = _gib(reading.ram_bytes) if reading.ram_bytes is not None else "memory unavailable"
        chip = reading.apple_chip or "Apple Silicon"
        usable = (
            f"; {_gib(reading.model_usable_memory_bytes)} usable for models"
            if reading.model_usable_memory_bytes is not None else ""
        )
        return f"hardware :: {cores}; {chip}; {memory} unified memory{usable}"
    ram = _gib(reading.ram_bytes) if reading.ram_bytes is not None else "RAM unavailable"
    if reading.gpu_vram_bytes is not None:
        vendor = "NVIDIA" if reading.gpu_vendor == "nvidia" else "AMD"
        gpu = f"{vendor} {reading.gpu_count or 1} device(s); {_gib(reading.gpu_vram_bytes)} largest VRAM"
    else:
        gpu = "no GPU"
    return f"hardware :: {cores}; {ram} RAM; {gpu}"


def _gib(value: int) -> str:
    return f"{value / _GIB:.1f} GiB"
