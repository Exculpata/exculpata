#!/usr/bin/env python3
"""Prove a specified previous checkout can read stopped synthetic preflight state."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from fastapi.testclient import TestClient
from tests.test_media_preflight import ObservedProcessor, app_for, silence, upload, wait_job
from tests.test_matter_media_workflow import _matter

OLD_READER = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
class NoSubmission:
    available = True
    def ready(self, owner):
        raise AssertionError("Previous reader attempted transcription")
app = create_workbench_app(Path(sys.argv[2]), generator=UnavailableGenerator(),
                          auth_mode="test", media_processor=NoSubmission())
with TestClient(app) as client:
    for row in json.loads(sys.argv[3]):
        prefix = f"/matters/{row['slug']}/sources/{row['token']}"
        page = client.get(prefix)
        assert page.status_code == 200
        assert row["message"] in page.text
        content = client.get(prefix + "/content", headers={"Range": "bytes=0-15"})
        assert content.status_code == 206 and len(content.content) == 16
        status = client.get(prefix + "/media-status").json()
        assert status["state"] == "cancelled" and status["ready"] is False
print(json.dumps({"previous_reader": "PASS", "held_sources": 2,
                  "playback": True, "searchable": False, "submitted": False}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-source", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-preflight-rollback-") as temporary:
        root = Path(temporary)
        quiet = root / "generated-silence.wav"
        silence(quiet, 8)
        video = root / "generated-no-audio.mp4"
        subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=160x120:d=1", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(video)], check=True)
        rows = []
        processor = ObservedProcessor()
        runtime = root / "runtime"
        with TestClient(app_for(runtime, processor)) as client:
            for source, media_type in ((quiet, "audio/wav"), (video, "video/mp4")):
                slug = _matter(client)
                bench, matter, document, token = upload(client, slug, source, media_type)
                job = wait_job(bench, matter, document)
                rows.append({"slug": slug, "token": token, "message": job.message})
            assert processor.submissions == 0
        completed = subprocess.run(
            [sys.executable, "-c", OLD_READER, str(args.previous_source.resolve() / "src"),
             str(runtime), json.dumps(rows)], cwd=root, check=True, capture_output=True, text=True,
            timeout=30,
        )
        print(completed.stdout.strip())


if __name__ == "__main__":
    main()
