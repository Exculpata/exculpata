"""Inspect the same generated 50-document, 180-page selection in four formats."""
from hashlib import sha256

from fastapi.testclient import TestClient

from tests.fixtures.load_files.generate_image_indexes import dii_bytes, lfp_bytes
from tests.test_discovery_review_dat_opt import dat_bytes, opt_bytes
from tests.test_load_file_admission import CountingScanner, app_at, new_matter
from tests.test_production_inspection_http import inspect
from tests.test_production_inspection_manifest_http import envelope


def test_all_four_indexes_report_the_same_missing_last_page_in_its_own_volume(tmp_path):
    dat_rows, opt_rows, blocks, page_records, paths = [], [], [], [], []
    for doc_number in range(1, 51):
        count = 4 if doc_number <= 30 else 3
        first = len(page_records) + 1
        keys = [f"SYN{number:05d}" for number in range(first, first + count)]
        native = f"NATIVES/{keys[0]}.txt"
        text = f"TEXT/{keys[0]}.txt"
        dat_rows.append((keys[0], keys[-1], native, text))
        paths.extend((native, text))
        filenames = []
        for offset, key in enumerate(keys):
            name = f"page-{key}.png"
            path = f"IMAGES/{name}"
            filenames.append(name)
            paths.append(path)
            opt_rows.append((key, "SYNTH001", path, "Y" if not offset else "", "", "", str(count) if not offset else ""))
            page_records.append({"key": key, "boundary": "" if offset else "D", "volume": "SYNTH001",
                                 "directory": "IMAGES", "filename": name})
        blocks.append({"record_id": f"doc-{doc_number}", "begin_bates": keys[0], "page_count": str(count),
                       "directory": "IMAGES", "images": filenames})

    # The final image is present only in the other production.
    absent = paths.pop()
    indexes = {
        "dat": (dat_bytes(("BEGBATES", "ENDBATES", "NATIVEPATH", "TEXTPATH"), dat_rows, encoding="cp1252"), 50, 100),
        "opt": (opt_bytes(opt_rows), 180, 180),
        "dii": (dii_bytes(blocks, encoding="utf-16-le"), 50, 180),
        "lfp": (lfp_bytes(page_records, encoding="utf-16-be"), 180, 180),
    }
    selection = [[f"SYNTH001/DATA/index.{suffix}", True] for suffix in indexes]
    selection.extend([f"SYNTH001/{path}", True] for path in paths)
    selection.append([f"SYNTH002/{absent}", True])
    scanner = CountingScanner()
    with TestClient(app_at(tmp_path / "runtime", scanner)) as client:
        _, matter = new_matter(client)
        for position, (suffix, (body, records, references)) in enumerate(indexes.items()):
            prefix = envelope(selection, index=position)
            response = inspect(client, matter, suffix, prefix + body, **{"X-Selection-Manifest-Bytes": str(len(prefix))})
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["sha256"] == sha256(body).hexdigest()
            assert report["record_count"] == records and len(report["rows"]) == 20
            qc = report["reference_qc"]
            missing = int(suffix != "dat")
            assert qc["total"] == references
            assert (qc["counts"]["matched"], qc["counts"]["missing"]) == (references - missing, missing)
            assert [example["ordinal"] for example in qc["examples"]] == ([records] if missing else [])
        assert scanner.calls == [body for body, _, _ in indexes.values()]
