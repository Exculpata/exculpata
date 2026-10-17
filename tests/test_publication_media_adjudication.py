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


def test_historical_launch_dispositions_are_preserved():
    # These original blobs remain in published history, even after compression.
    assert set(SCAN.REVIEWED_MEDIA_DIGESTS) == {
        "764e069b47c7fb122f7907376868e79498594710745247f0e46416e9f59bd7ca",
        "b82dceab0752282f7442eba2c8630f7f01e4bcd0ec46948c091aba4f8098a62d",
    }


def test_compressed_launch_videos_need_no_disposition():
    root = Path(__file__).resolve().parents[1]
    expected = {
        "16x9": "96649404d4e396ed5281237f4a739c59c34500c897597377e6ba843a3fb1b1f8",
        "9x16": "e5d8ef83157756883bd92784d7afa5f44c2a6fd7585eec08a871363f788f4efc",
    }
    for cut, digest in expected.items():
        rel = f"docs/assets/exculpata-launch-{cut}.mp4"
        data = (root / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        assert 0 < len(data) < 10_000_000
        assert data == (root / f"site/assets/exculpata-launch-{cut}.mp4").read_bytes()
        assert digest not in SCAN.REVIEWED_MEDIA_DIGESTS
        # Inspect real metadata and raw bytes, with no allowlisted digest.
        assert not inspect(data, name=rel, deny=())


def test_decoded_metadata_and_other_secret_checks_still_block(monkeypatch):
    def metadata(data, *, name, location, deny):
        return SCAN._scan_bytes(b"xyz", location=location, deny=deny)
    monkeypatch.setattr(SCAN, "_scan_media", metadata)
    data = b"synthetic-compressed-xyz-marker"
    assert inspect(data, digests=(hashlib.sha256(data).hexdigest(),))
    monkeypatch.setattr(SCAN, "_scan_media", lambda *args, **kwargs: [])
    data += b" -----BEGIN " + b"PRIVATE KEY-----"
    assert inspect(data, digests=(hashlib.sha256(data).hexdigest(),))
