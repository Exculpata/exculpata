import hashlib
import importlib.util
from pathlib import Path
import sys

SPEC = importlib.util.spec_from_file_location("media_publication", Path(__file__).resolve().parents[1] / "scripts/publication-check.py")
SCAN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCAN
SPEC.loader.exec_module(SCAN)


def inspect(data, *, name="example.mp4", digests=(), deny=(b"xyz",)):
    return SCAN._scan_content(data, name=name, location="synthetic", deny=deny,
                              reviewed_media_digests=digests)


def test_only_exact_media_bytes_receive_short_match_disposition(monkeypatch):
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    data = b"synthetic-compressed-xyz-marker"
    digest = hashlib.sha256(data).hexdigest()
    assert inspect(data)
    assert not inspect(data, digests=(digest,))
    assert inspect(data + b"changed", digests=(digest,))
    assert inspect(data, name="example.txt", digests=(digest,))
    assert inspect(data, deny=(b"compressed",), digests=(digest,))


def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def test_short_email_shapes_inside_media_samples_are_ignored(monkeypatch):
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    short = b"\x00" + b"a" + b"@" + b"b.co" + b"\xff"
    longer = b"\x00" + b"someone" + b"@" + b"public.test" + b"\xff"
    media = _box(b"ftyp", b"isom") + _box(b"moov", b"clean") + _box(b"mdat", short)
    assert inspect(media) == []
    leaked = _box(b"ftyp", b"isom") + _box(b"moov", short) + _box(b"mdat", b"\x00\x01")
    assert SCAN.Finding("synthetic", "non-example-email-address") in inspect(leaked)
    sample = _box(b"ftyp", b"isom") + _box(b"mdat", longer)
    assert SCAN.Finding("synthetic", "non-example-email-address") in inspect(sample)
    assert SCAN.Finding("synthetic", "non-example-email-address") in inspect(media, name="notes.txt")


def test_launch_video_samples_have_no_email_finding(monkeypatch):
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    root = Path(__file__).resolve().parents[1]
    for name in ("exculpata-launch-16x9.mp4", "exculpata-launch-9x16.mp4"):
        data = (root / "docs/assets" / name).read_bytes()
        findings = inspect(data, name=name)
        assert all(item.rule != "non-example-email-address" for item in findings), name


def test_decoded_metadata_and_other_secret_checks_still_block(monkeypatch):
    def metadata(data, *, name, location, deny):
        return SCAN._scan_bytes(b"xyz", location=location, deny=deny)
    monkeypatch.setattr(SCAN, "_scan_media", metadata)
    data = b"synthetic-compressed-xyz-marker"
    assert inspect(data, digests=(hashlib.sha256(data).hexdigest(),))
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    data += b" -----BEGIN " + b"PRIVATE KEY-----"
    assert inspect(data, digests=(hashlib.sha256(data).hexdigest(),))
