"""Verify a real preservation archive after a producer-mapped DAT import."""
import hashlib
import zipfile

from case_intelligence.matter_archive import build_matter_archive, cleanup_archive
from case_intelligence.matter_archive_verifier import verify_matter_archive
from tests.case_lifecycle_helpers import lifecycle_session
from tests.test_discovery_review_dat_opt import ACTOR, dat_bytes, opened_review, source_text
from tests.test_matter_archive import records
from tests.test_production_dat_profiles import upload_mapped_files
from tests.test_production_text_companions import open_production_list


def test_completed_profiled_dat_preserves_receipt_binding_applied_roles_and_originals(tmp_path):
    headers = ("Native link", " Série ", "Text copy")
    cells = ("Native/RX001.txt", "RX001", "Text/RX001.txt")
    roles = ("native_path", "begin_bates", "text_path")
    mapping = dict(zip(headers, roles))
    index = "Volume/Data/index.dat"
    native = "Volume/Native/RX001.txt"
    files = {native: source_text(1), index: dat_bytes(headers, [cells]), "Volume/Text/RX001.txt": source_text(2)}
    with opened_review(tmp_path / "runtime") as review:
        upload_mapped_files(review, files, index, mapping)
        open_production_list(review)
        dat_source = review.document(index)
        attempt = review.repository.current_attempt(review.matter.matter_id, dat_source.document_id)
        assert attempt["status"] == "complete" and attempt["matched_count"] == 1
        session = lifecycle_session(review.bench.workspace, ACTOR)
        review.bench.workspace.case_lifecycle.begin_exit(review.matter.slug, ACTOR, session_id=session.session_id)
        artifact = build_matter_archive(review.bench, review.matter, ACTOR, source_store=review.store)
        try:
            manifest = verify_matter_archive(artifact.path, expected_sha256=artifact.sha256)
            assert manifest["coverage"]["workbench_intake_dat_profile"]["disposition"] == "included"
            receipt, = records(artifact, "workbench_intake_receipt")
            profile, = records(artifact, "workbench_intake_dat_profile")
            assert profile == {"receipt_id": receipt["receipt_id"], "matter_id": review.matter.matter_id,
                               "ordinal": 1, "producer": "Volume", "digest": hashlib.sha256(files[index]).hexdigest(),
                               "fields_json": [list(pair) for pair in sorted(mapping.items())]}
            item, = [row for row in records(artifact, "workbench_intake_item") if row["relative_path"] == index]
            assert (item["receipt_id"], item["ordinal"]) == (profile["receipt_id"], profile["ordinal"])
            transfer, = [row for row in records(artifact, "workbench_intake_transfer")
                         if (row["receipt_id"], row["ordinal"]) == (profile["receipt_id"], profile["ordinal"])]
            archived, = records(artifact, "workbench_discovery_production")
            assert archived["version_id"] == transfer["source_version_id"] == dat_source.version_id
            assert archived["digest"] == profile["digest"]
            assert archived["profile_positions_json"] == [[i, role] for i, role in enumerate(roles)]
            row, = records(artifact, "workbench_discovery_dat_record")
            assert row["attempt_id"] == archived["attempt_id"]
            assert (row["headers_json"], row["cells_json"]) == (list(headers), list(cells))
            assert row["semantic_json"] == [["begin_bates", "RX001"]]
            assert (row["association_status"], row["document_id"]) == ("matched", review.document(native).document_id)
            with zipfile.ZipFile(artifact.path) as archive:
                for path, raw in files.items():
                    assert archive.read("originals/" + review.document(path).document_id) == raw
        finally:
            cleanup_archive(artifact)
