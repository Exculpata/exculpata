from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import random

import pytest

from case_intelligence import exact_search


SEED = 45760
PLANTED_HIT_DIGEST = "59c580860813142814aaf45522e7d410ba5b1b294df4d5652878924667275f21"


@dataclass(frozen=True)
class SeededHit:
    family: str
    document_id: str
    query: str
    text: str


@dataclass(frozen=True)
class FamilyMeasurement:
    planted: int
    found: int

    @property
    def recall(self) -> float:
        return self.found / self.planted

    @property
    def estimated_missed(self) -> int:
        return self.planted - self.found


FAMILY_VARIANTS = {
    "letter case": (
        "Signal Harbor",
        ("signal harbor", "SIGNAL HARBOR", "Signal Harbor", "sIgNaL hArBoR"),
    ),
    "apostrophes": (
        "O'Riley marker",
        ("O’Riley marker", "O‘Riley marker", "o'riley MARKER", "O'Riley marker"),
    ),
    "hyphenated line breaks": (
        "microfilm ledger",
        ("micro-\nfilm ledger", "micro-\r\nfilm ledger", "micro-\nfilm LEDGER", "MICRO-\nFILM ledger"),
    ),
    "Unicode spaces": (
        '"amber vessel"',
        ("amber\u00a0vessel", "amber\u2002vessel", "amber\u2003vessel", "amber\u2009vessel"),
    ),
    "diacritics": (
        "café marker",
        ("cafe\u0301 marker", "CAFÉ MARKER", "café marker", "CAFE\u0301 marker"),
    ),
    "ligatures": (
        "final flare",
        ("ﬁnal ﬂare", "FINAL ﬂARE", "ﬁnal FLARE", "final flare"),
    ),
    "adjacent punctuation": (
        '"cobalt parcel"',
        ("(cobalt parcel)", "cobalt, parcel.", "cobalt—parcel", "[COBALT] {PARCEL}"),
    ),
    "phrase line breaks": (
        '"quiet lantern"',
        ("quiet\nlantern", "quiet\r\nlantern", "QUIET\nLANTERN", "quiet\n\nlantern"),
    ),
}


EXPECTED_GAPS = {
    "hyphenated line breaks": (4, "An ASCII hyphen is retained inside a token, so a line-break hyphen does not rejoin the word."),
}

FAMILY_FLOORS = {
    family: 1.0 if family not in EXPECTED_GAPS else 0.0
    for family in FAMILY_VARIANTS
}


def build_seeded_hits(seed: int = SEED) -> tuple[SeededHit, ...]:
    """Build invented planted hits; the seed controls neutral context and order."""
    rng = random.Random(seed)
    contexts = ("alpha", "bravo", "delta", "echo", "kilo", "tango")
    hits = []
    for family_index, (family, (query, variants)) in enumerate(FAMILY_VARIANTS.items()):
        for variant_index, variant in enumerate(variants):
            before, after = rng.sample(contexts, 2)
            hits.append(SeededHit(
                family=family,
                document_id=f"seed-{family_index:02d}-{variant_index:02d}-{rng.randrange(10_000):04d}",
                query=query,
                text=f"{before} {variant} {after}",
            ))
    rng.shuffle(hits)
    return tuple(hits)


def planted_hit_digest(hits: tuple[SeededHit, ...]) -> str:
    planted = [
        (hit.family, hit.document_id, hit.query, hit.text)
        for hit in sorted(hits, key=lambda item: item.document_id)
    ]
    return hashlib.sha256(json.dumps(planted, ensure_ascii=False).encode()).hexdigest()


def measure(hits: tuple[SeededHit, ...]) -> dict[str, FamilyMeasurement]:
    counts = {family: [0, 0] for family in FAMILY_VARIANTS}
    for hit in hits:
        counts[hit.family][0] += 1
        if exact_search.parse_query(hit.query).matches_units([hit.text]):
            counts[hit.family][1] += 1
    return {
        family: FamilyMeasurement(planted=planted, found=found)
        for family, (planted, found) in counts.items()
    }


def assert_family_floors(measurements: dict[str, FamilyMeasurement]) -> None:
    for family, result in measurements.items():
        assert result.recall >= FAMILY_FLOORS[family], family
        expected_missed = EXPECTED_GAPS.get(family, (0, ""))[0]
        assert result.estimated_missed == expected_missed, family


def markdown_table(measurements: dict[str, FamilyMeasurement]) -> str:
    rows = [
        "| Family | Planted | Found | Measured recall | Estimated missed |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for family, result in measurements.items():
        rows.append(
            f"| {family} | {result.planted} | {result.found} | "
            f"{result.recall:.2%} | {result.estimated_missed} |"
        )
    return "\n".join(rows)


def test_seeded_set_has_a_stable_planted_hit_digest():
    first = build_seeded_hits()
    second = build_seeded_hits()
    assert first == second
    assert planted_hit_digest(first) == planted_hit_digest(second) == PLANTED_HIT_DIGEST


def test_per_family_floor_and_expected_gap_counts():
    measurements = measure(build_seeded_hits())
    print(markdown_table(measurements))
    assert_family_floors(measurements)
    assert "Measured recall" in markdown_table(measurements)


def test_identity_normalization_mutation_is_detected(monkeypatch):
    # Mutation check: normalization is required for the benchmark's handled families.
    monkeypatch.setattr(exact_search, "_normalize_text", lambda text: text)
    with pytest.raises(AssertionError, match="letter case"):
        assert_family_floors(measure(build_seeded_hits()))
