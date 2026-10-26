"""Generated truth precedes rendering; no engine parser supplies expected pages.

Digit-ending prefixes always have a digit count unlike the base series width.
"""
from itertools import cycle
import random
import re
import string
import unicodedata

import pytest

from case_intelligence.bates_gaps import BatesDocument, compute_bates_gaps
from tests.bates_gaps_378a import compute_bates_gaps as compute_378a

def marked(label, prefix, rng):
    marks = "".join(rng.choices(string.punctuation + " ‥－．＿", k=rng.randint(1, 3)))
    style = rng.randrange(13)
    if style == 12:
        return prefix + "-" + label[len(prefix):].zfill(rng.randint(25, 30))
    if style == 10:
        return rng.choice(["X-", "SET "]) + label
    if style == 11:
        position = rng.randrange(len(prefix) + 1, len(label))
        return label[:position] + marks + label[position:]
    if style == 0:
        return label[len(prefix):]
    if style == 1:
        return prefix + marks + "X" + label[len(prefix):]
    if style == 2:
        left, right = rng.choice([("[", "]"), ("(", ")")])
        return left + label + right
    if style == 3:
        return prefix + marks[0] * 2 + label[len(prefix):]
    position = rng.choice([0, rng.randrange(1, len(prefix)), len(prefix),
                           rng.randrange(len(prefix) + 1, len(label)), len(label)])
    return label[:position] + marks + label[position:]

def generate(seed):
    rng = random.Random(seed)
    mode, size, width = seed % 24, rng.randint(7, 16), rng.randint(3, 5)
    prefix = "NB" if mode == 15 else rng.choice(["ABC", "QQ"])
    withheld = {n for n in range(2, size) if rng.random() < .4} or {3}
    held = set(range(1, size + 1)) - withheld
    truth = {prefix: (held, withheld)}
    aliases = {prefix: prefix}
    coordinates = {prefix: (prefix, 0)}
    family = None
    if mode in (13, 14):
        n = rng.randint(2, size - 1)
        own = bool(rng.getrandbits(1))
        if own:
            held.discard(n)
            withheld.add(n)
            family = prefix + str(n).zfill(width) + "."
            truth[family] = ({1, 3}, {2})
            coordinates[family] = (family, 0)
        else:
            held.add(n)
            withheld.discard(n)
        family = (n, own, family)
    if mode == 15:
        truth[prefix + "2"] = ({1, 3}, {2})
        coordinates[prefix + "2"] = (prefix, 2 * 10 ** (width + 1))
    docs, owners = [], {}
    shapes = cycle(rng.sample(list(range(1, 13)) + [16], 2) if mode >= 22 else [mode])

    def add(labels, series, count=1, complete=True):
        identity = str(len(docs))
        docs.append(BatesDocument(identity, identity + "v", tuple(labels), count, complete))
        owners[(identity, identity + "v")] = series

    pages = sorted(held - ({family[0]} if family else set()))
    while pages:
        n = pages.pop(0)
        run = [n]
        shape = next(shapes)
        shape = 0 if n in (1, size) and shape >= 16 else shape
        if shape in (8, 9, 10) or shape >= 16:
            while (pages and pages[0] == run[-1] + 1 and (shape < 16 or pages[0] < size)
                   and rng.random() < .7):
                run.append(pages.pop(0))
        label = prefix + str(n).zfill(width)
        end = prefix + str(run[-1]).zfill(width) if len(run) > 1 else None
        count, complete = 1, True
        shape = 0 if n in (1, size) and shape in range(1, 8) else shape
        if shape == 1:
            label += rng.choice([" ", "/", ":", ",", ".", "-", "_"]) + "001"
        elif shape == 2:
            label += "(1)"
        elif shape == 3:
            rendered_prefix = "".join(rng.choice([c.upper(), c.lower()]) for c in prefix)
            aliases[rendered_prefix] = prefix
            label = rendered_prefix + str(n).zfill(width)
        elif shape == 4:
            label = "".join(chr(ord(c) + 0xFEE0) for c in label)
        elif shape == 5:
            label = rng.choice(["ᴀʙᴄ", "ıBC", "АВС", "ΑΒΣ", "ɑBC"]) + str(n).zfill(width)
        elif shape == 6:
            label = prefix + rng.choice(["\u200b", "\u202e", "\u00a0", "\u034f", "\ufe00", "\u2028", "\u1680", "\u2800"]) + str(n).zfill(width)
        elif shape == 7:
            label = prefix + str(n).zfill(rng.randint(1, width + 1))
        elif shape == 8 and end:
            label, end = end, label
        elif shape == 10:
            end, count = None, rng.choice([len(run), 0, None])
        elif shape == 11:
            complete = False
        elif shape == 12:
            label = rng.choice([" ", "\t", "\n", "\u2003", "\u202f"]) + label + " "
        elif shape >= 16:
            if len(run) > 1 and rng.random() < .5:
                end = rng.choice([None, prefix + str(run[1]).zfill(width)])
                count = len(run)
            side = rng.randrange(3)
            label = marked(label, prefix, rng) if end is None or side != 1 else label
            end = marked(end, prefix, rng) if end and side != 0 else end
        if mode == 14:
            label += "." + str(1).zfill(width + 1)
        add([] if shape == 11 and rng.random() < .5 else [(label, end)], prefix, count, complete)
        if shape == 9 and rng.random() < .5:
            add([(label, end)], prefix)
    if family:
        n, own, name = family
        for sub in (1, 3):
            add([(prefix + str(n).zfill(width) + "." + str(sub).zfill(width + 1), None)], name if own else prefix)
    if mode == 15:
        for n in (1, 3):
            add([(prefix + "2" + str(n).zfill(width + 1), None)], prefix + "2")
    rng.shuffle(docs)
    return docs, truth, owners, aliases, coordinates, mode in (0, 9)

@pytest.mark.parametrize("seed", range(3000))
def test_oracle_A_generated_truth(seed):
    docs, truth, owners, aliases, coordinates, clean = generate(seed)
    facts = compute_bates_gaps(docs)
    context = (seed, [(d.labels, d.page_count, d.labels_complete) for d in docs])
    missing = [f for f in facts if f.kind == "missing"]
    for f in missing:
        series = aliases.get(f.series, f.series)
        absent = {n + offset for hidden, (rendered, offset) in coordinates.items()
                  if rendered == series for n in truth[hidden][1]}
        assert f.upper - f.lower + 1 <= len(absent), context
        assert set(range(f.lower, f.upper + 1)) <= absent, context  # A
    for series, (present, absent) in truth.items():
        for page in absent:
            if not min(present) < page < max(present):
                continue
            rendered, offset = coordinates[series]
            covered = any(aliases.get(f.series, f.series) == rendered and f.kind in {"missing", "possible"} and
                          f.lower <= page + offset <= f.upper for f in facts)
            notice = any(f.kind == "not_computed" and (aliases.get(f.series, f.series) in {series, "ALL"} or
                         any(owners[c] == series for c in f.cited_ids)) for f in facts)
            assert covered or notice, context  # B
    if clean:
        actual = {n for f in missing for n in range(f.lower, f.upper + 1)}
        assert actual == next(iter(truth.values()))[1], context  # C
        assert all(f.kind in {"missing", "computed"} for f in facts), context

def namer_reach_in_gap(docs, gap):
    """Bounded spec oracle for B only; A continues to use pre-rendered truth."""
    pages = set(range(gap.lower, gap.upper + 1))
    plain_pattern = re.compile(r"([A-Za-z]+)([0-9]{1,18})")
    widths = [len(match[2]) for d in docs for pair in d.labels for label in pair
              if label and (match := plain_pattern.fullmatch(label)) and match[1] == gap.series]
    width = max(widths)
    reached = set()
    for document in docs:
        for start, end in document.labels:
            labels = [start] if end is None else [start, end]
            folded = [unicodedata.normalize("NFKC", label) for label in labels]
            plain = [plain_pattern.fullmatch(label) for label in labels]
            letters = ["".join(c for c in label.casefold() if "a" <= c <= "z") for label in folded]
            digits = ["".join(c for c in label if "0" <= c <= "9") for label in folded]
            if not any(not (p and p[1] == gap.series) and
                       (gap.series.casefold() in text or (not text and number))
                       for p, text, number in zip(plain, letters, digits)):
                continue
            if any(not number or len(number) > 24 for number in digits):
                return pages, True
            values = []
            for number, p in zip(digits, plain):
                values.append({int(p[2])} if end is not None and p else
                              {int(number[offset:offset + length])
                               for length in range(1, min(18, len(number)) + 1)
                               for offset in range(len(number) - length + 1)})
            narrow = re.fullmatch(re.escape(gap.series) + r" ?([0-9]+)-([0-9]+)", folded[0])
            if end is None and narrow and len(narrow[1]) == len(narrow[2]) == width and int(narrow[1]) <= int(narrow[2]):
                intervals = [(int(narrow[1]), int(narrow[2]))]
            elif end is not None:
                numbers = set.union(*values)
                intervals = [(min(numbers), max(numbers))]
            else:
                intervals = [(n, n) for n in values[0]]
            count = document.page_count
            for lower, upper in intervals:
                upper = upper + count - 1 if count is not None and count > 0 else gap.upper
                reached.update(n for n in pages if lower <= n <= upper)
    return reached, False

@pytest.mark.parametrize("seed", range(3000))
def test_oracle_B_possible_pages_never_increase(seed):
    docs, *_ = generate(seed)
    baseline = [f for f in compute_378a(docs) if f.kind == "possible"]
    facts = compute_bates_gaps(docs)
    for fact in facts:
        if fact.kind == "possible":
            assert any(old.series == fact.series and old.lower <= fact.lower and
                       fact.upper <= old.upper for old in baseline), (seed, fact, baseline)
    for gap in baseline:
        if gap.reason != "label may name series":
            continue
        allowed, whole_series = namer_reach_in_gap(docs, gap)
        possible = {n for fact in facts if fact.kind == "possible" and fact.series == gap.series
                    for n in range(max(gap.lower, fact.lower), min(gap.upper, fact.upper) + 1)}
        assert possible <= allowed, (seed, gap, possible - allowed)
        if whole_series:
            assert possible == allowed, (seed, gap, allowed - possible)

def test_oracle_B_at_least_one_namer_seed_improves_precision():
    for seed in range(3000):
        docs, *_ = generate(seed)
        baseline = [f for f in compute_378a(docs)
                    if f.kind == "possible" and "label may name series" in f.reason]
        for fact in compute_bates_gaps(docs):
            if fact.kind == "missing" and any(
                old.series == fact.series and max(old.lower, fact.lower) <= min(old.upper, fact.upper)
                for old in baseline
            ):
                return
    pytest.fail("No seed with a namer reports a Missing page that 378a made Possible")

def test_oracle_B_at_least_one_gap_has_missing_and_possible_runs():
    for seed in range(3000):
        docs, *_ = generate(seed)
        facts = compute_bates_gaps(docs)
        for gap in compute_378a(docs):
            if gap.kind != "possible" or gap.reason != "label may name series":
                continue
            kinds = {fact.kind for fact in facts if fact.series == gap.series and
                     fact.kind in {"missing", "possible"} and
                     max(gap.lower, fact.lower) <= min(gap.upper, fact.upper)}
            if kinds == {"missing", "possible"}:
                return
    pytest.fail("No seed has both Missing and Possible runs in one namer-only gap")
