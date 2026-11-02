from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from transcription_v2.auth import owner_key
from transcription_v2.domain import (
    JobStatus,
    PipelineStage,
    SegmentDraft,
    StageStatus,
    TranscriptionOptions,
)
from transcription_v2.exporters import ExportBundle, source_export_base_name
from transcription_v2.settings import Settings


try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    TestClient = None  # type: ignore[assignment,misc]


if TestClient is not None:
    _IMPORT_RUNTIME = tempfile.TemporaryDirectory()
    with patch.dict(
        os.environ,
        {"TRANSCRIPTION_V2_DATA_ROOT": _IMPORT_RUNTIME.name},
        clear=True,
    ):
        from transcription_v2.api import create_app
        from transcription_v2.batch_exports import (
            BATCH_ARCHIVE_ARTIFACT,
            build_or_reuse_batch_archive as _real_build_or_reuse_batch_archive,
        )


@unittest.skipUnless(TestClient is not None, "FastAPI is not installed")
class BatchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        settings = Settings(
            data_root=root,
            database_path=root / "jobs.sqlite3",
            max_upload_bytes=128,
            max_batch_upload_bytes=256,
            max_files_per_request=4,
            max_retained_jobs_per_owner=8,
            max_retained_bytes_per_owner=1024,
            max_retained_jobs_global=16,
            max_retained_bytes_global=2048,
            minimum_free_disk_bytes=0,
        )
        settings.validate()
        self.app = create_app(settings)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self._test_bundles: dict[str, ExportBundle] = {}

    @staticmethod
    def _headers(user: str = "staff-a") -> dict[str, str]:
        return {"X-User-ID": user}

    @staticmethod
    def _files(*names: str):
        return [
            ("files", (name, f"synthetic-{index}".encode("ascii"), "audio/wav"))
            for index, name in enumerate(names)
        ]

    def _submit(self, *names: str, user: str = "staff-a") -> dict:
        response = self.client.post(
            "/v1/jobs",
            headers=self._headers(user),
            data={"options": "{}"},
            files=self._files(*names),
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def _finish_queued(self, outcomes: dict[str, str]) -> None:
        sequence = 0
        while True:
            worker = f"worker-{sequence}"
            job = self.app.state.runtime.store.claim_next(worker)
            if job is None:
                return
            outcome = outcomes[job.id]
            if outcome == "succeeded":
                self.app.state.runtime.store.set_stage(
                    job.id,
                    worker,
                    PipelineStage.EXPORT,
                    StageStatus.SUCCEEDED,
                )
                self.app.state.runtime.store.complete_job(job.id, worker)
            elif outcome == "failed":
                self.app.state.runtime.store.fail_job(
                    job.id,
                    worker,
                    "pipeline_or_export_error",
                )
            else:  # pragma: no cover - test helper guard
                raise AssertionError(f"unsupported outcome: {outcome}")
            sequence += 1

    def _refresh_artifacts(self, _runtime, job_id: str, _owner: str) -> ExportBundle:
        return self._test_bundles[job_id]

    def _write_artifacts(
        self,
        job_id: str,
        *,
        marker: str = "x",
        translation: bool = False,
    ) -> Path:
        output = self.app.state.runtime.storage.job_paths(job_id, create=False).output
        job_files = self.app.state.runtime.store.list_files(job_id, owner_key("staff-a"))
        self.assertEqual(1, len(job_files))
        stem = source_export_base_name(job_files[0].safe_name)
        artifacts = {
            f"{stem}.docx": b"docx-" + marker.encode("ascii"),
            f"{stem}.txt": b"text-" + marker.encode("ascii"),
            f"{stem}.srt": b"srt-" + marker.encode("ascii"),
            f"{stem}.json": b"{}",
            f"{stem}.csv": b"speaker,text\n",
            f"{stem}.vtt": b"WEBVTT\n",
        }
        if translation:
            artifacts.update(
                {
                    f"{stem}.translation.en.docx": b"translated-docx-"
                    + marker.encode("ascii"),
                    f"{stem}.translation.en.txt": b"translated-text-"
                    + marker.encode("ascii"),
                    f"{stem}.translation.en.srt": b"translated-srt-"
                    + marker.encode("ascii"),
                    f"{stem}.translation.en.json": b"{}",
                    f"{stem}.translation.en.vtt": b"WEBVTT\n",
                }
            )
        for name, payload in artifacts.items():
            (output / name).write_bytes(payload)

        artifact_metadata = {
            name: {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in artifacts.items()
        }
        manifest_name = f"{stem}.manifest.json"
        manifest_path = output / manifest_name
        manifest_path.write_text(
            json.dumps(
                {
                    "source": {"file_name": job_files[0].safe_name},
                    "delivery": {"archive_file": f"{stem}.delivery.zip"},
                    "artifacts": artifact_metadata,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        bundle_files = {
            name: output / name for name in (*artifacts, manifest_name)
        }
        package_path = output / f"{stem}.delivery.zip"
        package_path.write_bytes(b"nested-zip-must-not-ship")
        (output / "another.ZIP").write_bytes(b"nested-zip-must-not-ship")
        (output / "ui-summary.json").write_bytes(b"{}")
        (output / ".private").write_bytes(b"not-a-delivery-artifact")
        (output / "unrelated.keep").write_bytes(b"not-a-delivery-artifact")
        self._test_bundles[job_id] = ExportBundle(
            output_dir=output,
            files=bundle_files,
            manifest_path=manifest_path,
            zip_path=package_path,
            zip_sha256=None,
        )
        return output

    def test_batch_identifier_is_generated_persisted_and_legacy_rows_load(self) -> None:
        payload = self._submit("one.wav", "two.wav")

        batch_id = payload["batch_id"]
        self.assertRegex(batch_id, r"^batch_[a-f0-9]{32}$")
        jobs = [self.app.state.runtime.store.get_job(item["job_id"]) for item in payload["jobs"]]
        self.assertEqual({batch_id}, {job.options.batch_id for job in jobs})
        self.assertEqual(
            batch_id,
            TranscriptionOptions.from_json(jobs[0].options.to_json()).batch_id,
        )

        single = self._submit("single.wav")
        self.assertIsNone(single["batch_id"])
        self.assertIsNone(
            TranscriptionOptions.from_mapping({"source_language": "en"}).batch_id
        )
        self.assertNotIn("batch_id", TranscriptionOptions().to_dict())

        forged = self.client.post(
            "/v1/jobs",
            headers=self._headers(),
            data={"options": '{"batch_id":"batch_00000000000000000000000000000000"}'},
            files=self._files("forged.wav"),
        )
        self.assertEqual(422, forged.status_code)

    def test_batch_status_is_owner_scoped_and_projects_only_safe_fields(self) -> None:
        payload = self._submit("one.wav", "two.wav")
        batch_id = payload["batch_id"]

        hidden = self.client.get(
            f"/v1/batches/{batch_id}", headers=self._headers("staff-b")
        )
        self.assertEqual(404, hidden.status_code)
        hidden_package = self.client.post(
            f"/v1/batches/{batch_id}/delivery-package",
            headers=self._headers("staff-b"),
            json={"purge_after_download": False},
        )
        self.assertEqual(404, hidden_package.status_code)

        visible = self.client.get(
            f"/v1/batches/{batch_id}", headers=self._headers()
        )
        self.assertEqual(200, visible.status_code)
        value = visible.json()
        self.assertEqual("processing", value["status"])
        self.assertFalse(value["download_ready"])
        self.assertEqual(2, value["counts"]["active"])
        for job in value["jobs"]:
            self.assertEqual(
                {"job_id", "primary_filename", "status", "stage"}, set(job)
            )
            self.assertEqual(
                {"code", "label", "status", "progress"}, set(job["stage"])
            )

        # Even a deliberately colliding internal row remains isolated to its
        # own owner; it is never merged with staff-a's jobs.
        other = self.app.state.runtime.store.create_job(
            owner_key=owner_key("staff-b"),
            options=TranscriptionOptions(batch_id=batch_id),
        )
        other_view = self.client.get(
            f"/v1/batches/{batch_id}", headers=self._headers("staff-b")
        ).json()
        self.assertEqual([other.id], [item["job_id"] for item in other_view["jobs"]])

    def test_batch_cardinality_guard_rejects_corrupt_grouping(self) -> None:
        payload = self._submit("one.wav", "two.wav", "three.wav", "four.wav")
        batch_id = payload["batch_id"]
        self.app.state.runtime.store.create_job(
            owner_key=owner_key("staff-a"),
            options=TranscriptionOptions(batch_id=batch_id),
        )

        response = self.client.get(
            f"/v1/batches/{batch_id}", headers=self._headers()
        )

        self.assertEqual(409, response.status_code)
        self.assertEqual("invalid_job_state", response.json()["code"])

    def test_partial_failure_waits_then_delivers_only_successful_recordings(self) -> None:
        payload = self._submit("included.wav", "failed.wav")
        batch_id = payload["batch_id"]
        included_id, failed_id = [item["job_id"] for item in payload["jobs"]]

        first = self.app.state.runtime.store.claim_next("worker-first")
        self.assertEqual(included_id, first.id)
        self.app.state.runtime.store.set_stage(
            first.id,
            "worker-first",
            PipelineStage.EXPORT,
            StageStatus.SUCCEEDED,
        )
        self.app.state.runtime.store.complete_job(first.id, "worker-first")
        self._write_artifacts(first.id, marker="included")

        not_ready = self.client.post(
            f"/v1/batches/{batch_id}/delivery-package",
            headers=self._headers(),
            json={"purge_after_download": False},
        )
        self.assertEqual(409, not_ready.status_code)

        failed = self.app.state.runtime.store.claim_next("worker-failed")
        self.assertEqual(failed_id, failed.id)
        self.app.state.runtime.store.fail_job(
            failed.id,
            "worker-failed",
            "pipeline_or_export_error",
        )
        status = self.client.get(
            f"/v1/batches/{batch_id}", headers=self._headers()
        ).json()
        self.assertEqual("ready", status["status"])
        self.assertEqual(
            {"total": 2, "active": 0, "succeeded": 1, "failed": 1, "canceled": 0},
            status["counts"],
        )

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            package = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            )
            delivered = self.client.get(package.json()["download_url"])
        self.assertEqual(200, package.status_code, package.text)
        self.assertEqual(1, package.json()["included_jobs"])
        self.assertEqual(1, package.json()["omitted_jobs"])

        self.assertEqual(200, delivered.status_code)
        with zipfile.ZipFile(io.BytesIO(delivered.content)) as archive:
            names = archive.namelist()
        self.assertTrue(names)
        self.assertIn("included.docx", names)
        self.assertIn("included.srt", names)
        self.assertTrue(all("/" not in name and "\\" not in name for name in names))
        self.assertFalse(any("failed" in name for name in names))

    def test_zip_is_flat_collision_safe_exactly_allowlisted_and_reused(self) -> None:
        payload = self._submit("hearing.wav", "hearing.wav", "HEARING.mp3")
        batch_id = payload["batch_id"]
        jobs = [item["job_id"] for item in payload["jobs"]]
        self._finish_queued({job_id: "succeeded" for job_id in jobs})
        first_output = self._write_artifacts(jobs[0], marker="first")
        self._write_artifacts(jobs[1], marker="second")
        self._write_artifacts(jobs[2], marker="third")

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            first = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            )
            self.assertEqual(200, first.status_code, first.text)
            archive_path = first_output / BATCH_ARCHIVE_ARTIFACT
            first_stat = archive_path.stat()
            second = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            )
            self.assertEqual(200, second.status_code, second.text)
            second_stat = archive_path.stat()
            os.chmod(archive_path, 0o600)
            with zipfile.ZipFile(archive_path, "a") as archive:
                archive.writestr("unexpected.txt", b"must-not-survive-cache-check")
            third = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            )
            self.assertEqual(200, third.status_code, third.text)
            third_stat = archive_path.stat()
            delivered = self.client.get(third.json()["download_url"])

        self.assertEqual(first_stat.st_ino, second_stat.st_ino)
        self.assertEqual(first_stat.st_mtime_ns, second_stat.st_mtime_ns)
        self.assertNotEqual(second_stat.st_ino, third_stat.st_ino)
        self.assertEqual("application/zip", delivered.headers["content-type"])
        self.assertIn("transcriptions.zip", delivered.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(delivered.content)) as archive:
            names = archive.namelist()
            manifests = {
                stem: json.loads(archive.read(f"{stem}.manifest.json"))
                for stem in ("hearing", "hearing-2", "HEARING-3")
            }
            archived_hashes = {
                name: hashlib.sha256(archive.read(name)).hexdigest() for name in names
            }

        expected_tails = {
            ".docx",
            ".txt",
            ".srt",
            ".json",
            ".csv",
            ".vtt",
            ".manifest.json",
        }
        self.assertEqual(
            {
                f"{stem}{tail}"
                for stem in ("hearing", "hearing-2", "HEARING-3")
                for tail in expected_tails
            },
            set(names),
        )
        self.assertEqual(len(names), len({name.casefold() for name in names}))
        self.assertTrue(all("/" not in name and "\\" not in name for name in names))
        self.assertFalse(any(name.casefold().endswith(".zip") for name in names))
        self.assertFalse(
            any(
                "ui-summary" in name
                or ".private" in name
                or "unrelated" in name
                or "unexpected" in name
                for name in names
            )
        )
        for stem, manifest in manifests.items():
            self.assertEqual("transcriptions.zip", manifest["delivery"]["archive_file"])
            self.assertEqual("flat", manifest["delivery"]["archive_layout"])
            self.assertEqual(
                {f"{stem}{tail}" for tail in expected_tails - {".manifest.json"}},
                set(manifest["artifacts"]),
            )
            for artifact_name, metadata in manifest["artifacts"].items():
                self.assertEqual(metadata["sha256"], archived_hashes[artifact_name])

    def test_flat_family_allocator_prevents_primary_translation_collision(self) -> None:
        payload = self._submit("call.wav", "call.translation.en.wav")
        batch_id = payload["batch_id"]
        jobs = [item["job_id"] for item in payload["jobs"]]
        self._finish_queued({job_id: "succeeded" for job_id in jobs})
        self._write_artifacts(jobs[0], marker="translated", translation=True)
        self._write_artifacts(jobs[1], marker="primary")

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            package = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            )
            delivered = self.client.get(package.json()["download_url"])

        self.assertEqual(200, package.status_code, package.text)
        self.assertEqual(200, delivered.status_code)
        with zipfile.ZipFile(io.BytesIO(delivered.content)) as archive:
            names = archive.namelist()
            first_manifest = json.loads(archive.read("call.manifest.json"))
            second_manifest = json.loads(
                archive.read("call.translation.en-2.manifest.json")
            )

        self.assertIn("call.translation.en.docx", names)
        self.assertIn("call.translation.en-2.docx", names)
        self.assertIn("call.translation.en-2.srt", names)
        self.assertEqual(len(names), len({name.casefold() for name in names}))
        self.assertTrue(all("/" not in name and "\\" not in name for name in names))
        self.assertIn("call.translation.en.docx", first_manifest["artifacts"])
        self.assertTrue(
            all(
                name.startswith("call.translation.en-2.")
                for name in second_manifest["artifacts"]
            )
        )

    def test_bundle_member_with_unsafe_name_is_rejected(self) -> None:
        payload = self._submit("safe.wav", "other.wav")
        batch_id = payload["batch_id"]
        jobs = [item["job_id"] for item in payload["jobs"]]
        self._finish_queued({job_id: "succeeded" for job_id in jobs})
        first_output = self._write_artifacts(jobs[0])
        self._write_artifacts(jobs[1])
        bundle = self._test_bundles[jobs[0]]
        unsafe_source = first_output / "unsafe-extra.txt"
        unsafe_source.write_bytes(b"unsafe")
        self._test_bundles[jobs[0]] = ExportBundle(
            output_dir=bundle.output_dir,
            files={**bundle.files, "../unsafe-extra.txt": unsafe_source},
            manifest_path=bundle.manifest_path,
            zip_path=bundle.zip_path,
            zip_sha256=bundle.zip_sha256,
        )

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            package = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            )

        self.assertEqual(409, package.status_code)
        self.assertFalse((first_output / BATCH_ARCHIVE_ARTIFACT).exists())

    def test_all_failed_batch_never_claims_a_download_is_ready(self) -> None:
        payload = self._submit("one.wav", "two.wav")
        batch_id = payload["batch_id"]
        jobs = [item["job_id"] for item in payload["jobs"]]
        self._finish_queued({job_id: "failed" for job_id in jobs})

        status = self.client.get(
            f"/v1/batches/{batch_id}", headers=self._headers()
        ).json()
        package = self.client.post(
            f"/v1/batches/{batch_id}/delivery-package",
            headers=self._headers(),
            json={"purge_after_download": False},
        )

        self.assertEqual("failed", status["status"])
        self.assertTrue(status["all_terminal"])
        self.assertFalse(status["download_ready"])
        self.assertEqual(409, package.status_code)

    def test_delivery_rebuilds_after_edit_commits_during_token_revalidation(self) -> None:
        payload = self._submit("hearing-one.wav", "hearing-two.wav")
        batch_id = payload["batch_id"]
        jobs = [item["job_id"] for item in payload["jobs"]]
        owner = owner_key("staff-a")
        edited_segment = None
        for index, job_id in enumerate(jobs):
            worker = f"edit-worker-{index}"
            claimed = self.app.state.runtime.store.claim_next(worker)
            self.assertEqual(job_id, claimed.id)
            if index == 0:
                job_file = self.app.state.runtime.store.list_files(job_id, owner)[0]
                edited_segment = self.app.state.runtime.store.add_segments(
                    job_id,
                    job_file.id,
                    worker,
                    [SegmentDraft(0, 0, 1000, "machine draft")],
                )[0]
            self.app.state.runtime.store.set_stage(
                job_id, worker, PipelineStage.EXPORT, StageStatus.SUCCEEDED
            )
            self.app.state.runtime.store.complete_job(job_id, worker)
            self._write_artifacts(job_id, marker=f"before-{index}")
        self.assertIsNotNone(edited_segment)

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            package = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            ).json()

        edited_path = (
            self.app.state.runtime.storage.job_paths(jobs[0], create=False).output
            / "hearing-one.txt"
        )
        build_calls = 0

        def edit_during_first_build(*args, **kwargs):
            nonlocal build_calls
            build_calls += 1
            archive = _real_build_or_reuse_batch_archive(*args, **kwargs)
            if build_calls == 1:
                self.app.state.runtime.store.update_segment(
                    edited_segment.id,
                    owner,
                    expected_revision=edited_segment.revision,
                    edited_text="reviewed wording",
                )
                edited_path.write_bytes(b"reviewed artifact after token issuance")
            return archive

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ), patch(
            "transcription_v2.api.build_or_reuse_batch_archive",
            side_effect=edit_during_first_build,
        ):
            delivered = self.client.get(package["download_url"])

        self.assertEqual(200, delivered.status_code)
        self.assertGreaterEqual(build_calls, 2)
        with zipfile.ZipFile(io.BytesIO(delivered.content)) as archive:
            self.assertEqual(
                b"reviewed artifact after token issuance",
                archive.read("hearing-one.txt"),
            )

    def test_delivery_token_is_not_served_while_failed_child_is_being_retried(self) -> None:
        payload = self._submit("ready.wav", "retry.wav")
        batch_id = payload["batch_id"]
        ready_id, failed_id = [item["job_id"] for item in payload["jobs"]]
        self._finish_queued({ready_id: "succeeded", failed_id: "failed"})
        self._write_artifacts(ready_id, marker="ready")
        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            package = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": False},
            ).json()

        retried = self.client.post(
            f"/v1/jobs/{failed_id}/retry", headers=self._headers(), json={}
        )
        self.assertEqual(200, retried.status_code)
        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            stale = self.client.get(package["download_url"])

        self.assertEqual(404, stale.status_code)
        self.assertEqual(JobStatus.QUEUED, self.app.state.runtime.store.get_job(failed_id).status)
        self.assertEqual(2, self.app.state.runtime.store.retained_usage()[0])

    def test_post_stream_retry_race_prevents_batch_purge(self) -> None:
        payload = self._submit("ready.wav", "retry.wav")
        batch_id = payload["batch_id"]
        ready_id, failed_id = [item["job_id"] for item in payload["jobs"]]
        self._finish_queued({ready_id: "succeeded", failed_id: "failed"})
        self._write_artifacts(ready_id, marker="ready")
        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            package = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": True},
            ).json()

        store = self.app.state.runtime.store
        original_conditional_delete = store.delete_batch_if_unchanged

        def retry_before_background_delete(
            anchor_job_id: str,
            owner: str,
            expected_snapshot: str,
            *,
            delete_files=None,
        ) -> bool:
            store.retry_job(failed_id, owner)
            return original_conditional_delete(
                anchor_job_id,
                owner,
                expected_snapshot,
                delete_files=delete_files,
            )

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ), patch.object(
            store,
            "delete_batch_if_unchanged",
            side_effect=retry_before_background_delete,
        ):
            delivered = self.client.get(package["download_url"])

        self.assertEqual(200, delivered.status_code)
        self.assertEqual(2, store.retained_usage()[0])
        self.assertEqual(JobStatus.QUEUED, store.get_job(failed_id).status)
        self.assertEqual(JobStatus.SUCCEEDED, store.get_job(ready_id).status)

    def test_aggregate_download_and_delete_purges_every_batch_job(self) -> None:
        payload = self._submit("one.wav", "two.wav")
        batch_id = payload["batch_id"]
        jobs = [item["job_id"] for item in payload["jobs"]]
        self._finish_queued({job_id: "succeeded" for job_id in jobs})
        for index, job_id in enumerate(jobs):
            self._write_artifacts(job_id, marker=str(index))

        with patch(
            "transcription_v2.batch_exports.refresh_review_exports",
            side_effect=self._refresh_artifacts,
        ):
            package = self.client.post(
                f"/v1/batches/{batch_id}/delivery-package",
                headers=self._headers(),
                json={"purge_after_download": True},
            ).json()
            token_path = urlsplit(package["download_url"]).path
            delivered = self.client.get(token_path)

        self.assertEqual(200, delivered.status_code)
        self.assertEqual((0, 0), self.app.state.runtime.store.retained_usage())
        self.assertEqual(404, self.client.get(token_path).status_code)


if __name__ == "__main__":
    unittest.main()
