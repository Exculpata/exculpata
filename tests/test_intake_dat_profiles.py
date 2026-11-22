"""A confirmed producer mapping is immutable, matter scoped and recoverable."""
import sqlite3

import pytest

from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.matter_archive_schema import validate_schema
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_intake_receipts import OWNER, append, create, descriptor, seed


def test_saved_profile_survives_retry_and_restore(tmp_path):
    workspace, matter, receipts = seed(tmp_path)
    try:
        item = descriptor("Volume/DATA/metadata.DAT")
        profile = {"producer": "Synthetic producer", "sha256": "d" * 64,
                   "fields": {"Start marker": "begin_bates", "File link": "native_path"}}
        mapped = dict(item, dat_profile=profile)
        receipt = create(receipts, matter, 1, (0,))
        append(receipts, matter, receipt, [mapped])
        append(receipts, matter, receipt, [mapped])
        with pytest.raises(WorkspaceProblem, match="changed"):
            append(receipts, matter, receipt, [item])
        receipts.seal(matter.matter_id, OWNER, receipt["receipt_id"])
        assert receipts.items(matter.matter_id, OWNER, receipt["receipt_id"])[0]["dat_profile"] == profile
        other = workspace.create_matter("Another selection", "", OWNER)
        assert receipts.producer_profile(other.matter_id, OWNER, profile["producer"]) is None
        saved_path = tmp_path / "clean-restore.sqlite"
        with sqlite3.connect(saved_path) as backup, workspace._lock:
            workspace.connection.backup(backup)
    finally:
        workspace.close()
    restored = WorkspaceStore(saved_path)
    try:
        validate_schema(restored.connection)
        result = IntakeReceipts(restored).producer_profile(matter.matter_id, OWNER, profile["producer"])
        assert result == {"producer": profile["producer"], "fields": profile["fields"]}
    finally:
        restored.close()


@pytest.mark.parametrize("filename,fields", [
    ("index.opt", {"Start": "begin_bates"}),
    ("index.dat", {"Start": "begin_bates", "Duplicate": "begin_bates"}),
    ("index.dat", {"Start": "assigned_id"}), ("index.dat", {}),
])
def test_invalid_profile_does_not_partially_append(tmp_path, filename, fields):
    workspace, matter, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, matter, 2, (0, 1))
        item = dict(descriptor(filename), dat_profile={
            "producer": "Synthetic", "sha256": "d" * 64, "fields": fields})
        with pytest.raises(WorkspaceProblem):
            append(receipts, matter, receipt, [descriptor("notes.txt"), item])
        assert receipts.items(matter.matter_id, OWNER, receipt["receipt_id"]) == []
    finally:
        workspace.close()
