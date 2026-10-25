"""Suggest a model tier from detected hardware. Nothing is downloaded."""
from __future__ import annotations

import json
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


@dataclass(frozen=True)
class HardwareReading:
    cpu_cores: int | None
    ram_bytes: int | None
    gpu_vendor: str
    gpu_vram_bytes: int | None
    apple_silicon: bool
    notes: tuple[str, ...]


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
        fallback = ("hardware detection :: using the CPU suggestion",) if self.fallback else ()
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


def suggest_tier(reading: HardwareReading, catalog: Mapping[str, object] | None = None) -> TierSuggestion:
    """Pick one profile. A failed reading uses the CPU profile and says so."""

    document = dict(catalog or load_profiles())
    profiles = [item for item in document.get("profiles", []) if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in profiles}
    fallback_id = str(document.get("fallback_profile") or "cpu")
    chosen, fallback = _match(reading, profiles, fallback_id)
    profile = by_id.get(chosen) or by_id.get(fallback_id) or {}
    if not profile:
        profile = {
            "id": "cpu",
            "label": "CPU only",
            "generator": "Use a small model on a local server.",
            "embed_rerank": "Keep embeddings and reranking inside Exculpata on CPU.",
            "asr": "Skip transcription or use a small int8 model.",
            "tradeoff": "Hardware detection did not match a profile.",
        }
        fallback = True
    return TierSuggestion(
        profile_id=str(profile.get("id") or "cpu"),
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
    vendor, vram = _gpu(run, apple, notes)
    if vendor == "none" and not apple:
        notes.append("No GPU tool reported VRAM.")
    return HardwareReading(cores, ram, vendor, vram, apple, tuple(notes))


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


def _gpu(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    apple: bool,
    notes: list[str],
) -> tuple[str, int | None]:
    nvidia = _nvidia_vram(run, notes)
    if nvidia is not None:
        return "nvidia", nvidia
    amd = _amd_vram(run, notes)
    if amd is not None:
        return "amd", amd
    if apple:
        return "apple", None
    return "none", None


def _nvidia_vram(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    notes: list[str],
) -> int | None:
    if run is None and shutil.which("nvidia-smi") is None:
        return None
    try:
        completed = _run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            run,
        )
    except (OSError, subprocess.TimeoutExpired):
        notes.append("GPU memory was not available.")
        return None
    if completed.returncode != 0:
        notes.append("GPU memory was not available.")
        return None
    totals: list[int] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            totals.append(int(float(stripped.split()[0])))
        except ValueError:
            notes.append("GPU memory figure was unreadable.")
            return None
    if not totals:
        notes.append("GPU memory was not available.")
        return None
    return max(totals) * _MIB


def _amd_vram(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    notes: list[str],
) -> int | None:
    if run is None and shutil.which("rocm-smi") is None:
        return None
    try:
        completed = _run(["rocm-smi", "--showmeminfo", "vram"], run)
    except (OSError, subprocess.TimeoutExpired):
        notes.append("GPU memory was not available.")
        return None
    if completed.returncode != 0:
        notes.append("GPU memory was not available.")
        return None
    value = _parse_amd_vram(completed.stdout)
    if value is None:
        notes.append("GPU memory report was not usable.")
    return value


def _parse_amd_vram(text: str) -> int | None:
    totals: list[int] = []
    for line in text.splitlines():
        folded = line.casefold()
        if "total" not in folded:
            continue
        numbers = [int(item) for item in re.findall(r"\d+", line)]
        if not numbers:
            continue
        value = max(numbers)
        if "mib" in folded or "(mb" in folded:
            totals.append(value * _MIB)
        elif value >= 1_000_000_000:
            totals.append(value)
    return max(totals) if totals else None


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
        apple = _band(profiles, "apple", _mib(reading.ram_bytes), "min_ram_mib", "max_ram_mib")
        if apple is not None:
            return apple, False
    if reading.gpu_vram_bytes is not None and reading.gpu_vendor in {"nvidia", "amd"}:
        gpu = _band(profiles, "gpu", _mib(reading.gpu_vram_bytes), "min_vram_mib", "max_vram_mib")
        if gpu is not None:
            return gpu, False
    cpu = _band(profiles, "cpu", _mib(reading.ram_bytes), "min_ram_mib", "max_ram_mib")
    if cpu is not None:
        return cpu, False
    return fallback_id, True


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
        return f"hardware :: {cores}; Apple Silicon; {memory} unified memory"
    ram = _gib(reading.ram_bytes) if reading.ram_bytes is not None else "RAM unavailable"
    if reading.gpu_vram_bytes is not None:
        vendor = "NVIDIA" if reading.gpu_vendor == "nvidia" else "AMD"
        gpu = f"{vendor} {_gib(reading.gpu_vram_bytes)} VRAM"
    else:
        gpu = "no GPU"
    return f"hardware :: {cores}; {ram} RAM; {gpu}"


def _gib(value: int) -> str:
    return f"{value / _GIB:.1f} GiB"
