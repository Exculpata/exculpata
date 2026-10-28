"""Synthetic Bates regressions and precision guards for 378a and 378b."""
import pytest

from case_intelligence.bates_gaps import BatesDocument, compute_bates_gaps

def doc(identity, start=None, end=None, count=1, complete=True):
    return BatesDocument(identity, identity + "-v1", ((start, end),) if start else (), count, complete)

def missing(facts, series="ABC"):
    return {(f.lower, f.upper) for f in facts if f.kind == "missing" and f.series == series}

def cited(facts, identity, kind=None):
    return [f for f in facts if (identity, identity + "-v1") in f.cited_ids and (kind is None or f.kind == kind)]

def protected(facts, identity, lower=2, upper=2, series="ABC"):
    assert not any(a <= upper and b >= lower for a, b in missing(facts, series))
    assert cited(facts, identity)

def middle(label, end=None):
    return compute_bates_gaps([doc("a", "ABC0001"), doc("b", label, end), doc("c", "ABC0003")])

def test_U1_suffixed_label():
    for suffix in [" 001", "/001", ":001", ",001", ".001", "-001", "_001", "(1)"]:
        facts = middle("ABC0002" + suffix)
        protected(facts, "b")
        assert cited(facts, "b", "possible")

def test_U2_small_caps_and_dotless_i():
    for label in ["abc0002.001", "ᴀʙᴄ0002.001", "ɑBC0002.001", "ａｂｃ0002.001", "ıBC0002.001"]:
        facts = middle(label)
        protected(facts, "b")
        assert cited(facts, "b", "possible" if label.startswith(("abc", "ａｂｃ")) else "not_computed")

def test_U3_unordered_label_does_not_silently_hide():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0002"), doc("c", "abc0004.001")])
    assert any("gaps outside ABC0001 to ABC0002 not computed" in f.text for f in cited(facts, "c"))

def test_U4_family_outside_plain_span():
    for end, start, stop in [("ABC0003", "ABC0005.001", "ABC0005.010"), ("ABC0098", "ABC0100.0001", "ABC0100.0040")]:
        facts = compute_bates_gaps([doc("a", "ABC0001", end), doc("b", start, stop)])
        assert any("gaps outside" in f.text for f in cited(facts, "b", "not_computed"))

def test_U5_wide_numeric_tail():
    protected(middle("ABC0002.0001", "ABC0002.0060"), "b")

def test_U6_letter_suffix():
    protected(middle("ABC0002A"), "b")

def test_U7_unequal_width_reversed():
    facts = compute_bates_gaps([doc("a", "B1"), doc("b", "B10", "B2"), doc("c", "B12")])
    assert not missing(facts, "B")
    assert any(f.series == "B" and "reversed" in f.reason for f in cited(facts, "b", "not_computed"))

def test_U8_unsafe_or_mixed_script():
    labels = ["ABC" + c + "0002" for c in "\u200b\u202e\u00a0\u034f\ufe00\u2028\u1680\u2800"]
    for label in labels + ["АВС0002", "ΑΒΣ0002", "İBC0002"]:
        facts = middle(label)
        protected(facts, "b")
        assert len(facts) == 1 and facts[0].text == "Bates series ALL: not computed (unreadable character in a label)"

def test_U9_case_fold():
    protected(middle("abc0002"), "b")

def test_U10_start_only_on_shared_gap_bracket():
    for count in [5, 0, None]:
        facts = compute_bates_gaps([doc("a", "ABC0001", count=count), doc("c", "ABC0006")])
        protected(facts, "a", 2, 5)
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0002", count=3), doc("c", "ABC0003"), doc("d", "ABC0007")])
    protected(facts, "b", 4, 4)
    assert any("touch" in f.reason for f in cited(facts, "b", "possible"))

def test_U11_incomplete_documents():
    for extra in [doc("d"), doc("d", "QQ0001", complete=False)]:
        facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0003"), extra])
        protected(facts, "d")
        assert any("document" in f.reason for f in cited(facts, "d", "possible"))
        assert cited(compute_bates_gaps([doc("a", "ABC0006"), extra]), "d", "not_computed")

def test_U12_digit_ending_prefix():
    facts = compute_bates_gaps([doc("a", "NB0001"), doc("b", "NB0003"), doc("c", "NB20012")])
    assert missing(facts, "NB") == {(2, 2)}
    assert any(f.lower == 4 and f.upper == 20011 and "width" in f.reason for f in facts if f.kind == "possible")

def test_U13_families_without_plain_series():
    for labels in [["VOL001-0001", "VOL001-0002", "VOL003-0001"], ["EPA-HQ-0001-0100", "EPA-HQ-2019-01"], ["ABC-0001", "ABC-0003"]]:
        facts = compute_bates_gaps([doc(str(i), label) for i, label in enumerate(labels)])
        assert not any(f.kind == "missing" for f in facts)
        assert all(cited(facts, str(i), "not_computed") for i in range(len(labels)))

def test_U14_separator_between_prefix_and_digits():
    for sep in ["-", "_", ".", ":", "/", " ", ",", "\u2013"]:
        facts = middle("ABC" + sep + "0002")
        protected(facts, "b")
        assert cited(facts, "b", "possible" if sep.isascii() else "not_computed")

def test_U15_range_with_one_unread_end():
    for start, end in [("ABC0002", "ABC-0009"), ("ABC-0002", "ABC0009"), ("ABC0002", "ABC 0009")]:
        facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", start, end), doc("c", "ABC0010")])
        protected(facts, "b", 2, 9)
        assert cited(facts, "b", "possible")

def test_U16_missing_text_uses_bracket_width():
    facts = compute_bates_gaps([doc("a", "NB0001"), doc("b", "NB0003"), doc("c", "NB20012")])
    assert "Missing Bates NB0002" in {f.text for f in facts}
    assert next(f.text for f in facts if f.kind == "possible").startswith(
        "Possible missing Bates NB00004 to NB20011 (")

@pytest.mark.parametrize("end_label", [False, True])
def test_U21_fullwidth_digit_relates(end_label):
    facts = middle("ABC0002", "ABC000２") if end_label else middle("ABC000２")
    protected(facts, "b")
    assert not any(f.series == "ALL" for f in facts)
    assert cited(facts, "b", "computed" if end_label else "possible")

@pytest.mark.parametrize("label", ["ABC000٢", "ABC–0002", "ABC‑0002", "ABC\u09030002",
                                    "ABC0002\u200b", "ABC0002\u0903", "ABC0002\u0301", "ABC0002\t"])
@pytest.mark.parametrize("end_label", [False, True])
def test_U22_any_unreadable_character(label, end_label):
    unsafe = doc("b", "ABC0002", label) if end_label else doc("b", label)
    facts = compute_bates_gaps([doc("a", "ABC0001"), unsafe, doc("c", "ABC0003"),
                                doc("d", "QQ0001"), doc("e", "QQ0003")])
    assert len(facts) == 1
    fact = facts[0]
    assert (fact.kind, fact.series, fact.lower, fact.upper) == ("not_computed", "ALL", None, None)
    assert fact.text == "Bates series ALL: not computed (unreadable character in a label)"
    assert fact.cited_ids == (("b", "b-v1"),)

def test_G1_unrelated_labels():
    for extra in [doc("c", "CONFIDENTIAL"), doc("c", "QQ0001", "RR0002")]:
        facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0500"), extra])
        assert missing(facts) == {(2, 499)} and cited(facts, "c", "not_computed")
        if extra.labels[0][1]:
            assert {f.series for f in cited(facts, "c", "not_computed")} == {"QQ", "RR"}

def test_G2_duplicate_brackets():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0001"), doc("c", "ABC0003")])
    assert missing(facts) == {(2, 2)}
    assert next(f for f in facts if f.kind == "missing").cited_ids == (("a", "a-v1"), ("b", "b-v1"), ("c", "c-v1"))

def test_G3_plain_ranges():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0003", "ABC0005"), doc("c", "ABC0009")])
    assert missing(facts) == {(2, 2), (6, 8)}
    assert any(f.text == "Bates series ABC: computed" for f in facts)
    assert {f.text for f in facts if f.kind == "missing"} == {"Missing Bates ABC0002", "Missing Bates ABC0006 to ABC0008"}

def test_G4_gap_facts_count_toward_limit():
    with pytest.raises(OverflowError):
        compute_bates_gaps(doc(str(i), f"ABC{i * 2:06d}") for i in range(100002))

@pytest.mark.parametrize("label,upper,expected_missing", [
    (label, 2, None) for label in ["ABC--0002", "ABC-_0002", "ABC - 0002", "ABC. 0002",
                           "ABC (0002)", "ABC‥0002", "-ABC0002", "[ABC0002]",
                           "AB-C0002", "ABC-X0002"]
] + [("ABC0,005", 8, {(2, 4), (6, 8)}), ("ABC0.005", 8, {(2, 4), (6, 8)}),
     ("ABC00-02", 8, {(3, 8)})])
def test_U17_marked_payloads(label, upper, expected_missing):
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", label), doc("c", f"ABC{upper + 1:04d}")])
    if expected_missing is None:
        protected(facts, "b", 2, upper)
    else:
        assert missing(facts, "ABC") == expected_missing
    assert cited(facts, "b", "possible")

@pytest.mark.parametrize("start,end", [("ABC0002", "ABC--0009")])
def test_U18_marked_range_end(start, end):
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", start, end), doc("c", "ABC0010")])
    protected(facts, "b", 2, 9)
    assert cited(facts, "b", "possible")

@pytest.mark.parametrize("label", ["0002"])
def test_U19_bare_digits(label):
    facts = middle(label)
    protected(facts, "b")
    assert cited(facts, "b", "possible")

@pytest.mark.parametrize("count", [7])
def test_U20_range_page_count(count):
    facts = compute_bates_gaps([doc("a", "ABC0001", "ABC0002", count=count), doc("b", "ABC0010")])
    protected(facts, "a", 3, 7)
    assert cited(facts, "a", "possible")

@pytest.mark.parametrize("labels", [("XYZ-0002",), ("CONFIDENTIAL",), ("XYZ-0002", "CONFIDENTIAL")])
def test_G5_unrelated_marks(labels):
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0003")] +
                               [doc(str(i), label) for i, label in enumerate(labels)])
    assert missing(facts) == {(2, 2)}

def gap_runs(facts):
    return {(f.kind, f.lower, f.upper) for f in facts
            if f.series == "ABC" and f.kind in {"missing", "possible"}}

def possible_run(facts, lower, upper, *identities, reason="label may name series"):
    runs = [f for f in facts if (f.kind, f.series, f.lower, f.upper) ==
            ("possible", "ABC", lower, upper)]
    assert len(runs) == 1
    assert runs[0].reason == reason
    assert runs[0].cited_ids == tuple(sorted((i, i + "-v1") for i in identities))

def test_U23_zero_page_count_touches_like_unknown():
    facts = compute_bates_gaps([doc("a", "ABC0001", count=0), doc("c", "ABC0010")])
    assert gap_runs(facts) == {("possible", 2, 9)}
    possible_run(facts, 2, 9, "a", "c", reason="page count touches gap")

def test_U24_reach_splits_gap_and_cites_outside_notice():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0003"),
                                doc("c", "ABC0010"), doc("d", "ABC 0,007")])
    assert gap_runs(facts) == {("missing", 2, 2), ("missing", 4, 6),
                               ("possible", 7, 7), ("missing", 8, 9)}
    possible_run(facts, 7, 7, "b", "c", "d")
    outside = [f for f in facts if "gaps outside ABC0001 to ABC0010 not computed" in f.text]
    assert len(outside) == 1
    assert (outside[0].kind, outside[0].series, outside[0].lower, outside[0].upper) == (
        "not_computed", "ABC", None, None)
    assert outside[0].cited_ids == (("d", "d-v1"),)

@pytest.mark.parametrize("label", ["X-ABC0002", "SET ABC 0002"])
def test_U25_letters_before_prefix(label):
    facts = middle(label)
    assert gap_runs(facts) == {("possible", 2, 2)}
    possible_run(facts, 2, 2, "a", "b", "c")

def test_U26_reach_extends_by_page_count():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC-5", count=3),
                                doc("c", "ABC0010")])
    assert gap_runs(facts) == {("missing", 2, 4), ("possible", 5, 7), ("missing", 8, 9)}
    possible_run(facts, 5, 7, "a", "b", "c")

def test_U27_unknown_count_extends_reach_to_series_max():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC-5", count=None),
                                doc("c", "ABC0010")])
    assert gap_runs(facts) == {("missing", 2, 4), ("possible", 5, 9)}
    possible_run(facts, 5, 9, "a", "b", "c")

def test_U28_nonplain_range_end_reaches_interval():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC 0004", "ABC0006"),
                                doc("c", "ABC0007"), doc("d", "ABC0009")])
    assert gap_runs(facts) == {("possible", 2, 5), ("missing", 8, 8)}
    possible_run(facts, 2, 5, "a", "b")

def test_U29_long_digits_fall_back_to_whole_series():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC-" + "9" * 25),
                                doc("c", "ABC0010")])
    assert gap_runs(facts) == {("possible", 2, 9)}
    possible_run(facts, 2, 9, "a", "b", "c")

def test_U30_nonplain_label_gets_not_read_notice():
    notices = [f for f in middle("X-ABC0002") if f.text == "Bates labels not read: X-ABC0002"]
    assert len(notices) == 1
    assert (notices[0].kind, notices[0].lower, notices[0].upper) == ("not_computed", None, None)
    assert notices[0].cited_ids == (("b", "b-v1"),)

@pytest.mark.parametrize("label", ["ABC-TWO", "ABC SET", "ABC Ⅱ"])
def test_G6_digitless_label_may_name_series(label):
    facts = middle(label)
    assert gap_runs(facts) == {("possible", 2, 2)}
    possible_run(facts, 2, 2, "a", "b", "c")
    notices = [f for f in facts if f.text == f"Bates labels not read: {label}"]
    assert len(notices) == 1
    assert (notices[0].kind, notices[0].lower, notices[0].upper) == ("not_computed", None, None)
    assert notices[0].cited_ids == (("b", "b-v1"),)

def test_G7_mixed_production_keeps_missing_and_possible_side_by_side():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0004"),
                                doc("c", "ABC0010"), doc("d", "ABC0014"),
                                doc("e", "ABC-5", count=2), doc("f", "X-ABC8")])
    assert gap_runs(facts) == {("missing", 2, 3), ("possible", 5, 6), ("missing", 7, 7),
                               ("possible", 8, 8), ("missing", 9, 9), ("missing", 11, 13)}
    possible_run(facts, 5, 6, "b", "c", "e")
    possible_run(facts, 8, 8, "b", "c", "f")
    assert not any("gaps outside" in f.text for f in facts)

def test_U31_nonplain_range_extends_from_largest_reach_by_count():
    facts = compute_bates_gaps([doc("a", "ABC0010"), doc("b", "ABC-12", "ABC-13", count=5),
                                doc("c", "ABC0020")])
    assert missing(facts) == {(18, 19)}
    possible_run(facts, 11, 17, "a", "b", "c")

def test_U32_narrow_range_shape_reaches_interior():
    facts = compute_bates_gaps([doc("a", "ABC0000"), doc("b", "ABC 0001-0003", count=1),
                                doc("c", "ABC0005")])
    assert missing(facts) == {(4, 4)}
    possible_run(facts, 1, 3, "a", "b", "c")

@pytest.mark.parametrize("label", ["ABC 001-0003", "ABC 0003-0001"])
def test_U33_unequal_width_or_reversed_label_keeps_wide_reach(label):
    facts = compute_bates_gaps([doc("a", "ABC0000"), doc("b", label), doc("c", "ABC0005")])
    assert missing(facts) == {(2, 2), (4, 4)}
    possible_run(facts, 1, 1, "a", "b", "c")
    possible_run(facts, 3, 3, "a", "b", "c")

def test_U34_prefixed_two_run_label_keeps_wide_reach():
    facts = compute_bates_gaps([doc("a", "ABC2000"), doc("b", "X-ABC0002.001"),
                                doc("c", "ABC2002")])
    assert not missing(facts)
    possible_run(facts, 2001, 2001, "a", "b", "c")

def test_U35_narrow_range_requires_series_digit_width():
    facts = compute_bates_gaps([doc("a", "ABC0511"), doc("b", "ABC05-12"),
                                doc("c", "ABC0513")])
    assert not missing(facts)
    possible_run(facts, 512, 512, "a", "b", "c")

def test_U36_24_digits_still_compute_reach():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC-" + "0" * 23 + "5"),
                                doc("c", "ABC0010")])
    assert gap_runs(facts) == {("missing", 2, 4), ("possible", 5, 5), ("missing", 6, 9)}
    possible_run(facts, 5, 5, "a", "b", "c")

def test_U37_narrow_range_uses_maximum_mixed_plain_width():
    facts = compute_bates_gaps([doc("a", "ABC001"), doc("b", "ABC 0003-0005"),
                                doc("c", "ABC0010")])
    assert gap_runs(facts) == {("possible", 2, 2), ("possible", 3, 5), ("possible", 6, 9)}
    for lower, upper, identities, reason in [
        (2, 2, ("a", "c"), "bracketing digit widths differ"),
        (3, 5, ("a", "b", "c"), "label may name series; bracketing digit widths differ"),
        (6, 9, ("a", "c"), "bracketing digit widths differ"),
    ]:
        possible_run(facts, lower, upper, *identities, reason=reason)

@pytest.mark.parametrize("label", [pytest.param("", id="blank"), pytest.param("N/A", id="not_available")])
def test_unavailable_label_makes_gaps_possible(label):
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0003"),
                                doc("c", "ABC0006"),
                                BatesDocument("d", "d-v1", ((label, None),))])
    assert gap_runs(facts) == {("possible", 2, 2), ("possible", 4, 5)}
    possible_run(facts, 2, 2, "a", "b", "d", reason="unlabeled or incomplete document")
    possible_run(facts, 4, 5, "b", "c", "d", reason="unlabeled or incomplete document")
    assert {f.text for f in facts if f.kind == "possible"} == {
        "Possible missing Bates ABC0002 (unlabeled or incomplete document)",
        "Possible missing Bates ABC0004 to ABC0005 (unlabeled or incomplete document)",
    }
    assert any(f.text == f"Bates labels not read: {label}" for f in cited(facts, "d", "not_computed"))

def test_normal_prefixed_series_keeps_missing_gaps():
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", "ABC0003"),
                                doc("c", "ABC0006")])
    assert gap_runs(facts) == {("missing", 2, 2), ("missing", 4, 5)}
    assert {(f.text, f.reason, f.cited_ids) for f in facts if f.kind == "missing"} == {
        ("Missing Bates ABC0002", "", (("a", "a-v1"), ("b", "b-v1"))),
        ("Missing Bates ABC0004 to ABC0005", "", (("b", "b-v1"), ("c", "c-v1"))),
    }
    assert all(f.kind in {"missing", "computed"} for f in facts)
