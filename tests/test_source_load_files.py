from pathlib import Path

import pytest

from case_intelligence.source_load_files import LoadFileError, parse_dat, parse_opt, resolve_volume_path


def _dat_bytes(rows, encoding="cp1252"):
    return ("\n".join("\x14".join(f"\xfe{value}\xfe" for value in row) for row in rows) + "\n").encode(encoding)


def test_dat_preserves_bates_and_special_characters(tmp_path: Path):
    path = tmp_path / "synthetic.dat"
    path.write_bytes(_dat_bytes([
        ("BEGDOC", "TITLE"),
        ("PRE-0007-A", "alpha\x14inside\xaecontinued"),
    ]))  # fixture-write: Create a synthetic load file for parser coverage.
    rows = list(parse_dat(path))
    assert rows[0].values == ("PRE-0007-A", "alpha\x14inside\ncontinued")
    assert rows[0].line == 2


def test_opt_preserves_page_keys_exactly(tmp_path: Path):
    path = tmp_path / "synthetic.opt"
    path.write_text("PRE-0007-A,VOL001,IMAGES/P1.TIF,Y,,,1\n", encoding="utf-8")  # fixture-write: Create a synthetic page load file.
    assert list(parse_opt(path))[0].image_key == "PRE-0007-A"


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8", "utf-16"])
def test_supported_dat_encodings_are_identical(tmp_path: Path, encoding: str):
    path = tmp_path / f"synthetic-{encoding}.dat"
    text = "þBEGDOCþ\x14þENDDOCþ\nþSYN0001þ\x14þSYN0002þ\n"
    path.write_bytes(text.encode(encoding))  # fixture-write: Create equivalent synthetic encoding variants.
    assert [row.values for row in parse_dat(path)] == [("SYN0001", "SYN0002")]


@pytest.mark.parametrize("tail", ["þunfinished", "þaþ\x14þbþ"])
def test_truncated_or_wrong_width_dat_is_cited(tmp_path: Path, tail: str):
    path = tmp_path / "broken.dat"
    path.write_bytes(("þAþ\n" + tail).encode("cp1252"))  # fixture-write: Create a hostile synthetic DAT row.
    with pytest.raises(LoadFileError, match=r"broken.dat:2:"):
        list(parse_dat(path))


def test_middle_bom_is_rejected(tmp_path: Path):
    path = tmp_path / "mixed.dat"
    path.write_bytes(b"\xfeA\xfe\n\xef\xbb\xbf\xfeB\xfe\n")  # fixture-write: Create a synthetic mixed-encoding marker.
    with pytest.raises(LoadFileError, match="mixed encoding"):
        list(parse_dat(path))


@pytest.mark.parametrize("hostile", ["../outside.txt", "/outside.txt"])
def test_volume_paths_reject_escape(tmp_path: Path, hostile: str):
    with pytest.raises(LoadFileError, match="contained"):
        resolve_volume_path(tmp_path, hostile, "synthetic.dat", 2)


def test_volume_paths_reject_outward_symlink(tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("synthetic", encoding="utf-8")  # fixture-write: Create a synthetic outside target for containment coverage.
    (tmp_path / "link.txt").symlink_to(outside)  # fixture-write: Create a hostile synthetic volume symlink.
    with pytest.raises(LoadFileError, match="contained"):
        resolve_volume_path(tmp_path, "link.txt", "synthetic.dat", 3)
