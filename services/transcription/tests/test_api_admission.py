from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from transcription_v2.auth import owner_key
from transcription_v2.settings import Settings


try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    TestClient = None  # type: ignore[assignment,misc]


if TestClient is not None:
    # api.py retains a module-level ASGI app for deployment. Keep that import-
    # time development runtime isolated from both live and repository data.
    _IMPORT_RUNTIME = tempfile.TemporaryDirectory()
    with patch.dict(
        os.environ,
        {"TRANSCRIPTION_V2_DATA_ROOT": _IMPORT_RUNTIME.name},
        clear=True,
    ):
        import transcription_v2.api as api_module
        from transcription_v2.api import (
            MAX_MULTIPART_OVERHEAD_BYTES,
            create_app,
        )


@unittest.skipUnless(TestClient is not None, "FastAPI is not installed")
class ApiAdmissionTests(unittest.TestCase):
    def _client(
        self,
        *,
        max_upload_bytes: int = 8,
        max_batch_upload_bytes: int = 10,
        max_files_per_request: int = 2,
        max_retained_jobs_per_owner: int = 4,
        max_retained_bytes_per_owner: int = 50,
        max_retained_jobs_global: int = 8,
        max_retained_bytes_global: int = 100,
        minimum_free_disk_bytes: int = 0,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        settings = Settings(
            data_root=root,
            database_path=root / "jobs.sqlite3",
            max_upload_bytes=max_upload_bytes,
            max_batch_upload_bytes=max_batch_upload_bytes,
            max_files_per_request=max_files_per_request,
            max_retained_jobs_per_owner=max_retained_jobs_per_owner,
            max_retained_bytes_per_owner=max_retained_bytes_per_owner,
            max_retained_jobs_global=max_retained_jobs_global,
            max_retained_bytes_global=max_retained_bytes_global,
            minimum_free_disk_bytes=minimum_free_disk_bytes,
        )
        settings.validate()
        app = create_app(settings)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client, app

    def test_history_service_only_reads_cancels_and_deletes_existing_jobs(self):
        active, app = self._client()
        owner = {"X-User-ID": "synthetic-reviewer"}
        created = active.post("/v1/jobs", headers=owner, files=self._files(5))
        self.assertEqual(created.status_code, 201, created.text)
        job_id = created.json()["jobs"][0]["job_id"]
        with patch.dict(os.environ, {"TRANSCRIPTION_V2_HISTORY_ONLY": "1"}):
            history = TestClient(create_app(app.state.runtime.settings))
        self.addCleanup(history.close)
        self.assertEqual(history.post("/v1/jobs", headers=owner, files=self._files(1)).status_code, 409)
        listed = history.get("/v1/jobs", headers=owner)
        self.assertEqual([job["job_id"] for job in listed.json()["jobs"]], [job_id])
        route = f"/v1/jobs/{job_id}"
        self.assertEqual(history.post(route + "/retry", headers=owner).status_code, 409)
        self.assertEqual(history.post(route + "/cancel", headers=owner).status_code, 200)
        self.assertEqual(history.delete(route, headers=owner).status_code, 200)
        self.assertEqual(history.get("/v1/jobs", headers=owner).json()["jobs"], [])

    def test_ready_endpoint_reports_queue_scope_without_worker_gpu_probe(self):
        client, _app = self._client()
        with patch("transcription_v2.resources.gpu_states", side_effect=AssertionError("No GPU in API role")):
            response = client.get("/ready", headers={"X-User-ID": "synthetic-reviewer"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scope"], "queue")
        self.assertEqual(response.json()["status"], "ready")

    @staticmethod
    def _files(*sizes: int):
        return [
            (
                "files",
                (f"recording-{index}.wav", b"x" * size, "audio/wav"),
            )
            for index, size in enumerate(sizes)
        ]

    def test_exact_batch_boundary_is_accepted_with_high_accuracy_default(self) -> None:
        client, _app = self._client()

        response = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(5, 5),
        )

        self.assertEqual(201, response.status_code)
        jobs = response.json()["jobs"]
        self.assertEqual(2, len(jobs))
        self.assertEqual({"high_accuracy"}, {job["profile"] for job in jobs})

    def test_aggregate_limit_rejects_request_before_creating_jobs(self) -> None:
        client, app = self._client()

        response = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(6, 5),
        )

        self.assertEqual(413, response.status_code)
        self.assertEqual((0, 0), app.state.runtime.store.retained_usage())

    def test_unsupported_child_remains_a_partial_noncapacity_rejection(self) -> None:
        client, app = self._client()

        response = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=[
                ("files", ("supported.wav", b"x", "audio/wav")),
                ("files", ("unsupported.exe", b"x", "application/octet-stream")),
            ],
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual(1, len(response.json()["jobs"]))
        self.assertEqual(
            [{"index": 1, "code": "unsupported_media"}],
            response.json()["rejected"],
        )
        self.assertEqual((1, 1), app.state.runtime.store.retained_usage())

    def test_ingest_failure_remains_a_partial_noncapacity_rejection(self) -> None:
        client, app = self._client()
        runtime = app.state.runtime
        original_ingest = runtime.storage.ingest_stream

        def fail_second_ingest(job_id, source, original_name, **kwargs):
            if original_name == "recording-1.wav":
                raise OSError("synthetic local ingest failure")
            return original_ingest(job_id, source, original_name, **kwargs)

        with patch.object(
            runtime.storage,
            "ingest_stream",
            side_effect=fail_second_ingest,
        ):
            response = client.post(
                "/v1/jobs",
                headers={"X-User-ID": "staff-a"},
                data={"options": "{}"},
                files=self._files(1, 1),
            )

        self.assertEqual(201, response.status_code)
        self.assertEqual(1, len(response.json()["jobs"]))
        self.assertEqual(
            [{"index": 1, "code": "ingest_failed"}],
            response.json()["rejected"],
        )
        self.assertEqual((1, 1), runtime.store.retained_usage())

    def test_thirty_small_recordings_are_accepted_as_one_batch(self) -> None:
        client, app = self._client(
            max_upload_bytes=1,
            max_batch_upload_bytes=30,
            max_files_per_request=100,
            max_retained_jobs_per_owner=100,
            max_retained_jobs_global=400,
        )

        response = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(*([1] * 30)),
        )

        self.assertEqual(201, response.status_code)
        payload = response.json()
        self.assertEqual(30, len(payload["jobs"]))
        self.assertEqual([], payload["rejected"])
        self.assertIsNotNone(payload["batch_id"])
        self.assertEqual((30, 30), app.state.runtime.store.retained_usage())

    def test_technical_part_ceiling_rejects_before_creating_jobs(self) -> None:
        client, app = self._client(
            max_upload_bytes=1,
            max_batch_upload_bytes=3,
            max_files_per_request=2,
            max_retained_jobs_per_owner=2,
            max_retained_jobs_global=8,
        )

        rejected = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(1, 1, 1),
        )

        self.assertEqual(400, rejected.status_code)
        self.assertEqual({"detail": "Too many files"}, rejected.json())
        self.assertEqual((0, 0), app.state.runtime.store.retained_usage())

    def test_owner_job_count_preflight_rejects_whole_batch(self) -> None:
        client, app = self._client(
            max_files_per_request=2,
            max_retained_jobs_per_owner=2,
            max_retained_jobs_global=8,
        )
        first = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(1),
        )

        rejected = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(1, 1),
        )

        self.assertEqual(201, first.status_code)
        self.assertEqual(429, rejected.status_code)
        self.assertEqual(
            {"detail": "Owner job capacity is unavailable"},
            rejected.json(),
        )
        self.assertEqual((1, 1), app.state.runtime.store.retained_usage())

    def test_global_job_count_preflight_rejects_whole_batch(self) -> None:
        client, app = self._client(
            max_files_per_request=2,
            max_retained_jobs_per_owner=2,
            max_retained_jobs_global=2,
        )
        first = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(1),
        )

        rejected = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-b"},
            data={"options": "{}"},
            files=self._files(1, 1),
        )

        self.assertEqual(201, first.status_code)
        self.assertEqual(507, rejected.status_code)
        self.assertEqual(
            {"detail": "Service job capacity is unavailable"},
            rejected.json(),
        )
        self.assertEqual((1, 1), app.state.runtime.store.retained_usage())

    def test_concurrent_capacity_contention_never_partially_retains_a_batch(self) -> None:
        client_a, app = self._client(
            max_upload_bytes=2,
            max_batch_upload_bytes=2,
            max_files_per_request=2,
            max_retained_jobs_per_owner=2,
            max_retained_jobs_global=3,
        )
        client_b = TestClient(app)
        self.addCleanup(client_b.close)
        runtime = app.state.runtime

        first_ingest_started = threading.Event()
        allow_first_ingest = threading.Event()
        second_request_is_at_admission = threading.Event()
        second_first_file_ingested = threading.Event()
        first_second_file_ingested = threading.Event()
        original_ingest = runtime.storage.ingest_stream
        original_normalized_options = api_module._normalized_options
        original_validate_media_name = api_module.validate_media_name

        def observed_normalized_options(raw_options, settings):
            normalized = original_normalized_options(raw_options, settings)
            if normalized[1].source_language == "es":
                # This executes immediately before the request-level admission
                # lock, proving the second request is actively contending.
                second_request_is_at_admission.set()
            return normalized

        def controlled_validate_media_name(filename):
            result = original_validate_media_name(filename)
            if filename == "first-1.wav":
                # With the fixed request-scoped lock, the second request cannot
                # ingest while the first is between children, so this bounded
                # wait expires and the first finishes atomically. Under the old
                # per-child lock, the second enters here and is forced into the
                # reproducible one-accepted/one-rejected quota race.
                second_first_file_ingested.wait(timeout=1.0)
            elif filename == "second-1.wav":
                if not first_second_file_ingested.wait(timeout=5.0):
                    raise AssertionError("first batch did not finish its second ingest")
            return result

        def controlled_ingest(job_id, source, original_name, **kwargs):
            if original_name == "first-0.wav":
                first_ingest_started.set()
                if not allow_first_ingest.wait(timeout=5.0):
                    raise AssertionError("concurrent test did not release first ingest")
            ingested = original_ingest(
                job_id,
                source,
                original_name,
                **kwargs,
            )
            if original_name == "second-0.wav":
                second_first_file_ingested.set()
            elif original_name == "first-1.wav":
                first_second_file_ingested.set()
            return ingested

        first_files = [
            ("files", (f"first-{index}.wav", b"x", "audio/wav"))
            for index in range(2)
        ]
        second_files = [
            ("files", (f"second-{index}.wav", b"x", "audio/wav"))
            for index in range(2)
        ]

        with (
            patch.object(
                api_module,
                "_normalized_options",
                side_effect=observed_normalized_options,
            ),
            patch.object(
                api_module,
                "validate_media_name",
                side_effect=controlled_validate_media_name,
            ),
            patch.object(
                runtime.storage,
                "ingest_stream",
                side_effect=controlled_ingest,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first_future = pool.submit(
                client_a.post,
                "/v1/jobs",
                headers={"X-User-ID": "staff-a"},
                data={"options": "{}"},
                files=first_files,
            )
            self.assertTrue(first_ingest_started.wait(timeout=5.0))
            second_future = pool.submit(
                client_b.post,
                "/v1/jobs",
                headers={"X-User-ID": "staff-b"},
                data={"options": '{"source_language":"es"}'},
                files=second_files,
            )
            try:
                self.assertTrue(second_request_is_at_admission.wait(timeout=5.0))
            finally:
                allow_first_ingest.set()
            first_response = first_future.result(timeout=10.0)
            second_response = second_future.result(timeout=10.0)

        self.assertEqual(201, first_response.status_code)
        self.assertEqual(2, len(first_response.json()["jobs"]))
        self.assertEqual([], first_response.json()["rejected"])
        self.assertEqual(507, second_response.status_code)
        self.assertEqual(
            {"detail": "Service job capacity is unavailable"},
            second_response.json(),
        )
        self.assertEqual((2, 2), runtime.store.retained_usage())
        self.assertEqual((0, 0), runtime.store.retained_usage(owner_key("staff-b")))

    def test_owner_byte_preflight_rejects_whole_batch(self) -> None:
        client, app = self._client(
            max_retained_bytes_per_owner=10,
        )
        first = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(4),
        )

        rejected = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(4, 4),
        )

        self.assertEqual(201, first.status_code)
        self.assertEqual(429, rejected.status_code)
        self.assertEqual(
            {"detail": "Owner byte capacity is unavailable"},
            rejected.json(),
        )
        self.assertEqual((1, 4), app.state.runtime.store.retained_usage())

    def test_global_byte_preflight_rejects_whole_batch(self) -> None:
        client, app = self._client(
            max_retained_bytes_per_owner=10,
            max_retained_bytes_global=10,
        )
        first = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(4),
        )

        rejected = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-b"},
            data={"options": "{}"},
            files=self._files(4, 4),
        )

        self.assertEqual(201, first.status_code)
        self.assertEqual(507, rejected.status_code)
        self.assertEqual(
            {"detail": "Service byte capacity is unavailable"},
            rejected.json(),
        )
        self.assertEqual((1, 4), app.state.runtime.store.retained_usage())

    def test_free_disk_preflight_rejects_whole_batch(self) -> None:
        client, app = self._client(minimum_free_disk_bytes=5)

        with patch("transcription_v2.api.shutil.disk_usage") as disk_usage:
            disk_usage.return_value.free = 10
            rejected = client.post(
                "/v1/jobs",
                headers={"X-User-ID": "staff-a"},
                data={"options": "{}"},
                files=self._files(3, 3),
            )

        self.assertEqual(507, rejected.status_code)
        self.assertEqual(
            {"detail": "Storage capacity is unavailable"},
            rejected.json(),
        )
        self.assertEqual((0, 0), app.state.runtime.store.retained_usage())

    def test_oversized_content_length_is_rejected_before_form_parsing(self) -> None:
        client, app = self._client()
        declared_length = 10 + MAX_MULTIPART_OVERHEAD_BYTES + 1

        response = client.post(
            "/v1/jobs",
            headers={
                "Content-Length": str(declared_length),
                "X-User-ID": "staff-a",
            },
            content=b"",
        )

        self.assertEqual(413, response.status_code)
        self.assertEqual("batch_too_large", response.json()["code"])
        self.assertEqual((0, 0), app.state.runtime.store.retained_usage())

    def test_global_job_limit_applies_across_owners(self) -> None:
        client, app = self._client(
            max_upload_bytes=8,
            max_batch_upload_bytes=8,
            max_files_per_request=1,
            max_retained_jobs_per_owner=1,
            max_retained_jobs_global=1,
        )
        first = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-a"},
            data={"options": "{}"},
            files=self._files(1),
        )

        second = client.post(
            "/v1/jobs",
            headers={"X-User-ID": "staff-b"},
            data={"options": "{}"},
            files=self._files(1),
        )

        self.assertEqual(201, first.status_code)
        self.assertEqual(507, second.status_code)
        self.assertEqual((1, 1), app.state.runtime.store.retained_usage())


if __name__ == "__main__":
    unittest.main()
