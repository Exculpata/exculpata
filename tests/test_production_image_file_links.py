"""Open the original image actually selected with an independently uploaded index."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.fixtures.load_files.generate_image_indexes import dii_bytes, lfp_bytes
from tests.test_discovery_review_dat_opt import complete_listing, opened_review
from tests.test_discovery_review_image_load_files import image_bytes, upload_image_production


class ImageLinks(HTMLParser):
    def __init__(self, response):
        super().__init__()
        assert response.status_code == 200, response.text
        self.navigation, self.entries, self.links = {}, {}, []
        self.entry = None
        self.feed(response.text)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "li" and "data-image-entry" in attributes:
            self.entry = {"status": attributes["data-image-status"], "links": [], "text": ""}
            ordinal = int(attributes["data-image-entry"])
            assert ordinal not in self.entries
            self.entries[ordinal] = self.entry
        if tag == "a" and "data-dii-files" in attributes:
            self.navigation[attributes["data-dii-files"]] = attributes["href"]
        if tag == "a" and "data-image-file" in attributes:
            self.links.append(attributes["href"])
            if self.entry is not None:
                self.entry["links"].append(attributes["href"])

    def handle_data(self, data):
        if self.entry is not None:
            self.entry["text"] += data

    def handle_endtag(self, tag):
        if tag == "li":
            self.entry = None


def test_dii_pages_through_named_files_and_isolates_originals(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with opened_review(tmp_path / "runtime") as review:
        images = tuple(f"piece-{i:02}.png" for i in range(1, 13))
        expected = {}
        for root, color in (("First", "red"), ("Second", "blue")):
            folder = root + "/IMAGES/Set/"
            directory = (root + "\\" if root == "First" else "") + "IMAGES\\Set"
            # A byte-for-byte different image with the same path and Bates in each receipt.
            data = image_bytes("PNG", color)
            index = root + "/DATA/documents.dii"
            files = {index: dii_bytes([{"record_id": "item", "begin_bates": "SYN0035",
                         "directory": directory, "images": images}]),
                     folder + images[0]: data, folder + images[10]: data}
            if root == "First":
                files[folder + images[2]] = image_bytes("PNG", "green")
                files[root + "/" + folder + images[2]] = image_bytes("PNG", "black")
            else:
                # The first index's missing reference cannot resolve from this receipt.
                files[folder + images[1]] = image_bytes("PNG", "white")
            upload_image_production(review, files)
            expected[index] = (root, data)
        for index, (root, data) in expected.items():
            initial = complete_listing(review, doc=review.token(index), view="fields")
            assert "unmatched" in initial.text
            opening = ImageLinks(initial)
            assert opening.entries == {}
            first_href = opening.navigation["open"]
            first = ImageLinks(review.client.get(first_href))
            assert list(first.entries) == list(range(1, 11))
            next_href = first.navigation["next"]
            assert parse_qs(urlsplit(next_href).query)["image_start"] == ["11"]
            last = ImageLinks(review.client.get(next_href))
            assert list(last.entries) == [11, 12] and "next" not in last.navigation
            assert ImageLinks(review.client.get(last.navigation["previous"])).entries == first.entries
            for ordinal, entry in (first.entries | last.entries).items():
                assert images[ordinal - 1] in entry["text"]
                if ordinal in {1, 11} or (root == "Second" and ordinal == 2):
                    assert entry["status"] == "matched"
                    href, = entry["links"]
                    assert href == f"/matters/{review.matter.slug}/sources/{review.token(root + '/IMAGES/Set/' + images[ordinal - 1])}"
                    assert review.client.get(href).status_code == 200
                    assert review.client.get(href + "/content").content == (image_bytes("PNG", "white")
                        if ordinal == 2 else data)
                else:
                    assert entry["status"] == ("ambiguous" if root == "First" and ordinal == 3 else "unavailable")
                    assert entry["links"] == []
            assert "Supplied page Bates" not in initial.text and 'data-page-image-unavailable' not in initial.text


@pytest.mark.parametrize("kind,image_format,suffix", [("opt", "TIFF", "tif"), ("lfp", "PNG", "png")])
def test_standalone_page_index_opens_original_via_current_source_page(tmp_path, monkeypatch, kind, image_format, suffix):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with opened_review(tmp_path / "runtime") as review:
        image_name = "original." + suffix
        image_path = "Volume/IMAGES/Part/" + image_name
        index = "Volume/DATA/pages." + kind
        content = (("SYN0035,VOL,IMAGES\\Part\\" + image_name + ",Y,,,1\n").encode() if kind == "opt" else
                   lfp_bytes([{"key": "SYN0035", "volume": "VOL", "directory": "IMAGES\\Part",
                               "filename": image_name, "image_type": "4"}]))
        upload_image_production(review, {index: content, image_path: image_bytes(image_format, "red")})
        listing = complete_listing(review, doc=review.token(index), view="document")
        source, = ImageLinks(listing).links
        assert source == f"/matters/{review.matter.slug}/sources/{review.token(image_path)}"
        assert review.client.get(source).status_code == 200
        if kind == "opt":
            assert 'data-page-image-unavailable' in listing.text
        removed = review.client.post(source + "/remove", follow_redirects=False)
        assert removed.status_code == 303
        assert review.client.get(source, follow_redirects=False).status_code in {404, 410}
        updated = complete_listing(review, doc=review.token(index), view="document")
        assert not ImageLinks(updated).links
