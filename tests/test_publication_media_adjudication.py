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


def test_short_email_collision_requires_exact_reviewed_digest(monkeypatch):
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    short = b"\x00" + b"a" + b"@" + b"b.co" + b"\xff"
    longer = b"\x00" + b"someone" + b"@" + b"public.test" + b"\xff"
    media = _box(b"ftyp", b"isom") + _box(b"moov", b"clean") + _box(b"mdat", short)
    digest = hashlib.sha256(media).hexdigest()
    email = SCAN.Finding("synthetic", "non-example-email-address")
    assert email in inspect(media)
    assert email not in inspect(media, digests=(digest,))
    assert email in inspect(media + b"\x01", digests=(digest,))
    assert email in inspect(media, name="notes.txt", digests=(digest,))
    sample = _box(b"ftyp", b"isom") + _box(b"mdat", longer)
    assert email in inspect(sample, digests=(hashlib.sha256(sample).hexdigest(),))


def test_launch_video_review_is_exact_blob_only(monkeypatch):
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    root = Path(__file__).resolve().parents[1]
    expected = {
        "docs/assets/exculpata-launch-16x9.mp4":
            "764e069b47c7fb122f7907376868e79498594710745247f0e46416e9f59bd7ca",
        "site/assets/exculpata-launch-16x9.mp4":
            "764e069b47c7fb122f7907376868e79498594710745247f0e46416e9f59bd7ca",
        "docs/assets/exculpata-launch-9x16.mp4":
            "b82dceab0752282f7442eba2c8630f7f01e4bcd0ec46948c091aba4f8098a62d",
        "site/assets/exculpata-launch-9x16.mp4":
            "b82dceab0752282f7442eba2c8630f7f01e4bcd0ec46948c091aba4f8098a62d",
    }
    assert set(SCAN.REVIEWED_MEDIA_DIGESTS) == set(expected.values())
    email = SCAN.Finding("synthetic", "non-example-email-address")
    for rel, digest in expected.items():
        data = (root / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        assert email in inspect(data, name=rel)
        assert email not in inspect(data, name=rel, digests=(digest,))


def test_decoded_metadata_and_other_secret_checks_still_block(monkeypatch):
    def metadata(data, *, name, location, deny):
        return SCAN._scan_bytes(b"xyz", location=location, deny=deny)
    monkeypatch.setattr(SCAN, "_scan_media", metadata)
    data = b"synthetic-compressed-xyz-marker"
    assert inspect(data, digests=(hashlib.sha256(data).hexdigest(),))
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    data += b" -----BEGIN " + b"PRIVATE KEY-----"
    assert inspect(data, digests=(hashlib.sha256(data).hexdigest(),))
