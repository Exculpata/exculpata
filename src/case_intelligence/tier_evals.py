"""Schema and validation for synthetic tier scorecards, without model execution."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import defense_eval as evaluation

TIERS = {"potato": ["potato", "cpu"], "consumer": ["gpu-16", "gpu-24"], "ultra": ["ultra"]}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Metric(Record):
    correct: int = Field(ge=0)
    total: int = Field(gt=0)
    rate: float = Field(ge=0, le=1)


class Pin(Record):
    model_id: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    license: str = Field(min_length=1)


class Tier(Record):
    tier: Literal["potato", "consumer", "ultra"]
    profile_ids: list[str]
    settings: dict
    models: dict[str, Pin]
    status: Literal["unmeasured", "partial", "measured"]
    retrieval: Metric | None
    answer_accuracy: Metric | None
    execution: dict | None
    reason: str
    wall_seconds: float = Field(ge=0)


class Scorecard(Record):
    schema_version: Literal[1]
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    command: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hardware: dict
    started_at: str = Field(min_length=1)
    suite_id: str
    suite_version: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    top_k: Literal[8]
    tiers: list[Tier]


def hardware_matches(tier, hardware):
    raise NotImplementedError("tier schema implementation pending")


def _models(tier):
    raise NotImplementedError("tier schema implementation pending")


def new_scorecard(commit, command, hardware):
    raise NotImplementedError("tier schema implementation pending")


def validate_scorecard(card):
    raise NotImplementedError("tier schema implementation pending")
