"""Generated truth precedes rendering; no engine parser supplies expected pages.

Digit-ending prefixes always have a digit count unlike the base series width.
"""
from itertools import cycle
import random
import string

import pytest

from case_intelligence.bates_gaps import BatesDocument, compute_bates_gaps

def marked(label, prefix, rng):
    marks = "".join(rng.choices(string.punctuation + " ‥－．＿", k=rng.randint(1, 3)))
    style = rng.randrange(10)
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
            if len(run) > 2 and rng.random() < .35:
                end, count = prefix + str(run[1]).zfill(width), len(run)
            else:
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
def test_oracle_generated_truth(seed):
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
