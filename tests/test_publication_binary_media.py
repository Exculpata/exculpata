"""Synthetic binary collisions never exempt metadata or candidate policy changes."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.test_publication_media_adjudication import SCAN
from tests.test_publication_pre_push import command, commit, repository

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/publication/compressed-collision.gif"
EMAIL = b"a" + b"@" + b"b.cd"


def inspect(data, name="synthetic.gif", deny=()):
    return SCAN._scan_content(data, name=name, location="synthetic", deny=deny)


def review(tmp_path, monkeypatch, data, **overrides):
    entry = dict(sha256=hashlib.sha256(data).hexdigest(), suffix=".gif",
                 reason="Synthetic fixture review", approver="synthetic-reviewer", expires="2099-12-31")
    entry.update(overrides)
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(dict(version=1, reviews=[entry])))
    monkeypatch.setattr(SCAN, "MEDIA_REVIEW_FILE", path)
    return path


def commented(data, text):
    # GIF comments are a chain of sub-blocks; the address spans the boundary.
    blocks = b"".join(bytes([len(text[i:i+3])]) + text[i:i+3] for i in range(0, len(text), 3))
    return data[:-1] + b"\x21\xfe" + blocks + b"\x00;"


def test_real_compressed_gif_collision_is_reviewed_and_decodable():
    data = FIXTURE.read_bytes()
    assert EMAIL in data
    assert SCAN._scan_bytes(data, location="synthetic", deny=())
    assert inspect(data) == []
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(FIXTURE), "-f", "null", "-"],
                            capture_output=True)
    assert result.returncode == 0 and not result.stderr


def test_gif_comment_blocks_even_when_digest_is_reviewed(tmp_path, monkeypatch):
    data = commented(FIXTURE.read_bytes(), EMAIL)
    review(tmp_path, monkeypatch, data)
    findings = inspect(data)
    assert SCAN.Finding("synthetic#media-metadata", "non-example-email-address") in findings
    assert EMAIL.decode() not in repr(findings)


def test_metadata_deny_and_secret_checks_are_never_adjudicated(tmp_path, monkeypatch):
    text = b"synthetic-denied-marker -----BEGIN " + b"PRIVATE KEY-----"
    data = commented(FIXTURE.read_bytes(), text)
    review(tmp_path, monkeypatch, data)
    rules = {f.rule for f in inspect(data, deny=(b"synthetic-denied-marker",))}
    assert {"operator-deny-term", "private-key-material"} <= rules


def test_unreviewed_changed_expired_and_nonmedia_bytes_keep_raw_checks(tmp_path, monkeypatch):
    data = FIXTURE.read_bytes()
    assert inspect(data + b"\x00")
    assert inspect(data, name="synthetic.txt")
    review(tmp_path, monkeypatch, data, expires="2000-01-01")
    assert SCAN.Finding("synthetic", "non-example-email-address") in inspect(data)
    review(tmp_path, monkeypatch, data, suffix=".png")
    assert inspect(data)


@pytest.mark.parametrize("change", ["missing", "malformed", "duplicate", "symlink", "bad-expiry", "unknown-field"])
def test_invalid_review_policy_fails_closed(tmp_path, monkeypatch, change):
    data = FIXTURE.read_bytes()
    path = review(tmp_path, monkeypatch, data)
    policy = json.loads(path.read_text())
    if change == "missing":
        path.unlink()
    elif change == "malformed":
        path.write_text("[]")
    elif change == "symlink":
        target = tmp_path / "target.json"
        path.rename(target)
        path.symlink_to(target)
    else:
        if change == "duplicate":
            policy["reviews"] *= 2
        elif change == "bad-expiry":
            policy["reviews"][0]["expires"] = "2027-99-99"
        else:
            policy["reviews"][0]["extra"] = "value"
        path.write_text(json.dumps(policy))
    assert SCAN.Finding("synthetic", "media-review-policy-invalid") in inspect(data)


def test_missing_metadata_tool_and_invalid_image_block(tmp_path, monkeypatch):
    data = FIXTURE.read_bytes()
    monkeypatch.setattr(SCAN.shutil, "which", lambda name: None)
    assert SCAN.Finding("synthetic", "media-inspection-unavailable") in inspect(data)
    monkeypatch.undo()
    data = b"synthetic text masquerading as an image"
    review(tmp_path, monkeypatch, data)
    assert SCAN.Finding("synthetic", "media-inspection-failed") in inspect(data)


@pytest.mark.parametrize("output", [b"[]", b"{}", b"invalid", b'[{"File:FileType":"PNG"}]',
    b'[{"File:FileType":"GIF","ExifTool:Warning":"synthetic warning"}]'])
def test_unusable_metadata_output_fails_closed(monkeypatch, output):
    monkeypatch.setattr(SCAN.shutil, "which", lambda name: "exiftool")
    monkeypatch.setattr(SCAN.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, output, b""))
    assert SCAN.Finding("synthetic", "media-inspection-failed") in inspect(FIXTURE.read_bytes())


def test_decoded_metadata_values_are_scanned(monkeypatch):
    import base64
    payload = json.dumps([{"File:FileType": "GIF", "GIF:Comment": "base64:" + base64.b64encode(EMAIL).decode()}]).encode()
    monkeypatch.setattr(SCAN.shutil, "which", lambda name: "exiftool")
    monkeypatch.setattr(SCAN.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, payload, b""))
    assert SCAN.Finding("synthetic#media-metadata", "non-example-email-address") in inspect(FIXTURE.read_bytes())


def test_installed_hook_pins_policy_and_checks_removed_metadata(tmp_path):
    root = repository(tmp_path)
    destination = tmp_path / "trusted-hook"
    assert command(root, sys.executable, str(ROOT / "scripts/install-publication-hook.py"), str(destination)).returncode == 0
    policy = destination / "publication-media-reviews.json"
    assert policy.read_bytes() == (ROOT / "scripts/publication-media-reviews.json").read_bytes()
    shutil.copyfile(FIXTURE, root / "synthetic.gif")
    head = commit(root)
    def hook():
        return command(root, str(destination / "pre-push"),
                       input=f"HEAD {head} refs/heads/main {'0' * 40}\n")
    assert hook().returncode == 0
    changed = commented(FIXTURE.read_bytes(), b"Synthetic changed media")
    (root / "synthetic.gif").write_bytes(changed)
    # Clean metadata leaves only the raw collision: reading the candidate's
    # forged policy would wrongly accept these newly authorized bytes.
    candidate = json.loads(policy.read_text())
    candidate["reviews"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
    (root / "scripts/publication-media-reviews.json").write_text(json.dumps(candidate))
    head = commit(root)
    assert hook().returncode != 0
    (root / "synthetic.gif").write_bytes(commented(FIXTURE.read_bytes(), EMAIL))
    head = commit(root)
    assert hook().returncode != 0
    (root / "synthetic.gif").unlink()
    head = commit(root)
    result = hook()
    assert result.returncode != 0
    assert EMAIL.decode() not in result.stdout + result.stderr


def test_compressed_png_metadata_still_blocks_reviewed_bytes(tmp_path, monkeypatch):
    import struct
    import zlib
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(EMAIL))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))
    review(tmp_path, monkeypatch, data, suffix=".png")
    assert EMAIL not in data
    assert SCAN.Finding("synthetic#media-metadata", "non-example-email-address") in inspect(data, name="synthetic.png")


def test_reviewed_mp4_metadata_still_blocks(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.mp4"
    result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:rate=1",
                             "-t", "1", "-c:v", "mpeg4", "-metadata", "comment=" + EMAIL.decode(), str(path)],
                            capture_output=True)
    assert result.returncode == 0
    data = path.read_bytes()
    review(tmp_path, monkeypatch, data, suffix=".mp4")
    assert SCAN.Finding("synthetic#media-metadata", "non-example-email-address") in inspect(data, name="synthetic.mp4")


def test_reviewed_image_type_mismatch_fails(tmp_path, monkeypatch):
    data = FIXTURE.read_bytes()
    review(tmp_path, monkeypatch, data, suffix=".png")
    assert SCAN.Finding("synthetic", "media-inspection-failed") in inspect(data, name="synthetic.png")


def test_duplicate_json_keys_cannot_override_review_fields(tmp_path, monkeypatch):
    data = FIXTURE.read_bytes()
    path = review(tmp_path, monkeypatch, data)
    path.write_text(path.read_text().replace('"version": 1', '"version": 0, "version": 1'))
    assert SCAN.Finding("synthetic", "media-review-policy-invalid") in inspect(data)


@pytest.mark.parametrize("failure", ["timeout", "exit", "stderr", "oversize"])
def test_image_inspection_failures_block_reviewed_media(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("exiftool", 30)
        return subprocess.CompletedProcess(args, 1 if failure == "exit" else 0,
            b"x" * (SCAN.MAX_DERIVED_SCAN_BYTES + 1) if failure == "oversize" else b"[]",
            b"synthetic diagnostic" if failure == "stderr" else b"")
    monkeypatch.setattr(SCAN.shutil, "which", lambda name: "exiftool")
    monkeypatch.setattr(SCAN.subprocess, "run", run)
    assert SCAN.Finding("synthetic", "media-inspection-failed") in inspect(FIXTURE.read_bytes())


def test_metadata_decoding_covers_nested_escaped_values(monkeypatch):
    output = b'{"chapters":[{"tags":{"comment":"a\\u0040b.cd"}}]}'
    monkeypatch.setattr(SCAN.shutil, "which", lambda name: "ffprobe")
    monkeypatch.setattr(SCAN.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, output, b""))
    assert SCAN.Finding("synthetic#media-metadata", "non-example-email-address") in SCAN._scan_media(
        b"synthetic", name="synthetic.mp4", location="synthetic", deny=())
    value = "C:" + "\\Users\\" + "synthetic-person\\file"
    content = SCAN._metadata_content({"tags": {"comment": [value]}})
    assert SCAN.Finding("synthetic", "personal-home-path") in SCAN._scan_bytes(content, location="synthetic", deny=())


def test_image_inspection_never_executes_candidate_exiftool_config(tmp_path, monkeypatch):
    # ExifTool's default configuration is Perl code, not asset metadata.
    (tmp_path / ".ExifTool_config").write_text(
        "BEGIN { open(my $f, '>', 'executed'); print $f 'synthetic'; close($f); } 1;\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXIFTOOL_HOME", str(tmp_path))
    assert inspect(FIXTURE.read_bytes()) == []
    assert not (tmp_path / "executed").exists()
