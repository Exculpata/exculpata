"""Synthetic Bates regressions and precision guards G1-G5 for 378a."""
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
def test_U16_fullwidth_digit_relates(end_label):
    facts = middle("ABC0002", "ABC000２") if end_label else middle("ABC000２")
    protected(facts, "b")
    assert not any(f.series == "ALL" for f in facts)
    assert cited(facts, "b", "computed" if end_label else "possible")

@pytest.mark.parametrize("label", ["ABC000٢", "ABC–0002", "ABC‑0002", "ABC\u09030002",
                                    "ABC0002\u200b", "ABC0002\u0903", "ABC0002\u0301", "ABC0002\t"])
@pytest.mark.parametrize("end_label", [False, True])
def test_U16_any_unreadable_character(label, end_label):
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

@pytest.mark.parametrize("label,upper", [
    (label, 2) for label in ["ABC--0002", "ABC-_0002", "ABC - 0002", "ABC. 0002",
                           "ABC (0002)", "ABC‥0002", "-ABC0002", "[ABC0002]",
                           "AB-C0002", "ABC-X0002"]
] + [(label, 8) for label in ["ABC0,005", "ABC0.005", "ABC00-02"]])
def test_U17_marked_payloads(label, upper):
    facts = compute_bates_gaps([doc("a", "ABC0001"), doc("b", label), doc("c", f"ABC{upper + 1:04d}")])
    protected(facts, "b", 2, upper)
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
