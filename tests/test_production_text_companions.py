"""DAT originals and produced text open through the authorized source viewer."""
import re

from tests.test_discovery_review_dat_opt import dat_bytes, opened_review, upload_production


def open_production_list(review):
    for _ in range(50):
        listing = review.client.get(review.base)
        assert listing.status_code == 200
        if 'data-production-format="dat"' in listing.text and "Showing all items in source order" not in listing.text:
            return listing
    raise AssertionError("Generated production did not finish indexing through the list route")


def dat_files(review, path, ordinal=1):
    selected = review.document(path)
    params = {"view": "fields"}
    if ordinal != 1:
        attempt = review.repository.current_attempt(review.matter.matter_id, selected.document_id)
        params["record"] = f"dat:{attempt['attempt_id']}:{ordinal}"
    response = review.client.get(review.base + "/items/" + review.token(path), params=params)
    assert response.status_code == 200
    article = re.search(r'<article[^>]*data-record-ordinal="' + str(ordinal) + r'"[^>]*>(.*?)</article>',
                        response.text, re.S)
    assert article, response.text
    return article.group(1)


def file_link(article, role):
    found = re.search(r'<a data-dat-file="' + role + r'" href="([^"]+)"', article)
    return found.group(1) if found else None


def test_dat_declared_files_open_the_original_version_in_the_selected_production(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with opened_review(tmp_path / "runtime") as review:
        for folder in ("First", "Second"):
            upload_production(review, {
                folder + "/LOAD/index.dat": dat_bytes(("BEGBATES", "NATIVEPATH", "TEXTPATH"), [
                    ("ABC0001", r"NATIVES\ABC0001.txt", r"TEXT\ABC0001.txt"),
                    ("UNMATCHED", "", "TEXT/other.txt"),
                    ("MISSING", "", "TEXT/missing.txt")]),
                folder + "/NATIVES/ABC0001.txt": f"Generated {folder} native.\n".encode(),
                folder + "/TEXT/ABC0001.txt": f"Generated {folder} produced text.\n".encode(),
                folder + "/TEXT/other.txt": f"Generated {folder} text-only row.\n".encode(),
            })
        upload_production(review, {"Unrelated/TEXT/missing.txt": b"Generated other collection.\n"})
        open_production_list(review)
        for folder in ("First", "Second"):
            index = folder + "/LOAD/index.dat"
            native = folder + "/NATIVES/ABC0001.txt"
            text = folder + "/TEXT/ABC0001.txt"
            for selected in (index, native):
                article = dat_files(review, selected)
                assert review.document(index).version_id in article and r"TEXT\ABC0001.txt" in article
                for role, path, word in (("matched", native, "native"), ("text", text, "produced text")):
                    url = file_link(article, role)
                    assert url == f"/matters/{review.matter.slug}/sources/{review.token(path)}"
                    opened = review.client.get(url)
                    assert opened.status_code == 200 and f"Generated {folder} {word}." in opened.text
                    assert review.client.get(url + "/content").content == f"Generated {folder} {word}.\n".encode()
            text_only = dat_files(review, index, 2)
            assert file_link(text_only, "matched") is None
            assert file_link(text_only, "text") == f"/matters/{review.matter.slug}/sources/{review.token(folder + '/TEXT/other.txt')}"
            missing = dat_files(review, index, 3)
            assert file_link(missing, "text") is None
            assert 'data-dat-text-status="unavailable"' in missing and "TEXT/missing.txt" in missing

        second = review.document("Second/TEXT/ABC0001.txt")
        review.store.source_path(second.document_id).unlink()
        missing_bytes = dat_files(review, "Second/LOAD/index.dat")
        assert file_link(missing_bytes, "text") is None
        assert 'data-dat-text-status="unavailable"' in missing_bytes

        old_url = file_link(dat_files(review, "First/LOAD/index.dat"), "text")
        assert review.client.post(old_url + "/remove", follow_redirects=False).status_code == 303
        upload_production(review, {"First/TEXT/ABC0001.txt": b"Generated later selection.\n"})
        open_production_list(review)
        assert review.client.get(old_url).status_code == 404
        assert review.client.get(old_url + "/content").status_code == 404
        after_removal = dat_files(review, "First/LOAD/index.dat")
        assert file_link(after_removal, "text") is None
        assert 'data-dat-text-status="unavailable"' in after_removal


def test_dat_conflicting_or_unproven_references_offer_no_text_link(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with opened_review(tmp_path / "runtime") as review:
        index = "Volume/data/index.dat"
        ambiguous = "Volume/data/other.dat"
        upload_production(review, {
            index: dat_bytes(("BEGBATES", "NATIVEPATH", "TEXTPATH"), [
                ("A001", "native/report.txt", "text/notes.txt"),
                ("B001", "native/report.txt", "text/notes.txt"),
                ("C001", "", "../text/notes.txt"),
                ("D001", "", "notes.txt"),
                ("E001", "", "text/table.csv")]),
            ambiguous: dat_bytes(("BEGBATES", "TEXT", "TEXT PATH"), [
                ("F001", "text/F001.txt", "text/other.txt")]),
            "Volume/native/report.txt": b"Generated report.",
            "Volume/text/notes.txt": b"Generated notes.",
            "Volume/text/other.txt": b"Generated other text.",
            "Volume/text/F001.txt": b"Generated Bates-named text.",
            "Volume/text/table.csv": b"Column,Value\nOne,Two\n",
        })
        listing = open_production_list(review)
        assert "No matches are reported for these load files" in listing.text
        assert "No load file was recognized" not in listing.text
        for load, ordinal, status in [(index, 1, "conflict"), (index, 2, "conflict"),
                (index, 3, "unsafe_path"), (index, 4, "unavailable"), (index, 5, "unavailable"),
                (ambiguous, 1, "ambiguous")]:
            article = dat_files(review, load, ordinal)
            assert f'data-dat-text-status="{status}"' in article
            assert file_link(article, "text") is None and file_link(article, "matched") is None
