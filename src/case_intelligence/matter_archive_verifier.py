"""Offline bounded integrity verifier. Reads ZIP members; never extracts files."""
from __future__ import annotations

import argparse
import hashlib
import sys
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

FORMAT = 'exculpata-preservation-v1'
BUFFER_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024**4
_DIGEST = re.compile(r'[0-9a-f]{64}')
# Frozen v1 format rule: these records preserve closure history but do not
# invalidate the content snapshot when another download or audit occurs.
VOLATILE_TABLES = frozenset({'workbench_audit_event', 'workbench_case_exit',
    'workbench_case_policy', 'workbench_matter_lifecycle', 'workbench_matter_retention'})
MAX_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024


def _preflight_zip(path):
    # CPython's standard-library EOCD reader reads a bounded 64 KiB suffix and
    # fixed-size ZIP64 trailer. Bound the central directory BEFORE ZipFile
    # allocates one object per member.
    with open(path, 'rb') as stream:
        end = zipfile._EndRecData(stream)
    if (end is None or end[zipfile._ECD_ENTRIES_TOTAL] > MAX_ENTRIES + 1 or
            end[zipfile._ECD_SIZE] > MAX_CENTRAL_DIRECTORY_BYTES):
        raise ValueError('Preservation ZIP directory exceeds its verifier bound.')


def snapshot_from_entries(entries):
    ignored = {f'records/{name}.jsonl' for name in VOLATILE_TABLES}
    digest = hashlib.sha256()
    for name, entry in sorted(entries.items()):
        if name not in ignored:
            digest.update(json.dumps([name, entry['sha256'], entry['bytes']], ensure_ascii=False,
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n')
    return digest.hexdigest()


def verify_matter_archive(path, *, expected_sha256=None, maximum_bytes=MAX_ARCHIVE_BYTES):
    """Verify structure, complete member set, lengths, SHA-256 and streaming CRC.

    A matching externally recorded digest proves the checked bytes equal those
    recorded bytes. Without it, this checks internal consistency, not authorship.
    """
    total = 0
    _preflight_zip(path)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        if len(infos) > MAX_ENTRIES + 1 or len(set(names)) != len(names):
            raise ValueError('Duplicate or excessive preservation members.')
        for info in infos:
            parts = PurePosixPath(info.filename).parts
            if (not parts or info.filename.startswith('/') or '\\' in info.filename or
                    any(part in ('', '.', '..') for part in parts) or
                    info.filename != '/'.join(parts) or info.is_dir() or
                    stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise ValueError('Unsafe preservation member.')
            total += info.file_size
            if info.file_size < 0 or total > maximum_bytes:
                raise ValueError('Preservation size exceeds the verifier bound.')
        manifest_info = archive.getinfo('manifest.json')
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Preservation manifest exceeds its read bound.')
        manifest = json.loads(archive.read(manifest_info))
        if not isinstance(manifest, dict) or manifest.get('format') != FORMAT:
            raise ValueError('Unsupported preservation archive format.')
        entries = manifest.get('entries')
        if not isinstance(entries, dict) or set(entries) != set(names) - {'manifest.json'}:
            raise ValueError('Preservation member inventory is incomplete.')
        line_counts = {}
        for name, expected in entries.items():
            info = archive.getinfo(name)
            if (not isinstance(expected, dict) or set(expected) != {'sha256', 'bytes'} or
                    type(expected['bytes']) is not int or expected['bytes'] != info.file_size or
                    not isinstance(expected['sha256'], str) or not _DIGEST.fullmatch(expected['sha256'])):
                raise ValueError('Invalid preservation integrity record.')
            digest, read, lines = hashlib.sha256(), 0, 0
            with archive.open(info) as stream:
                while chunk := stream.read(BUFFER_BYTES):
                    read += len(chunk)
                    if read > expected['bytes']:
                        raise ValueError('Preservation member exceeds declared size.')
                    digest.update(chunk)
                    if name.endswith('.jsonl'):
                        lines += chunk.count(b'\n')
            if read != expected['bytes'] or digest.hexdigest() != expected['sha256']:
                raise ValueError('Preservation member failed integrity verification.')
            line_counts[name] = lines
        _verify_inventory(archive, manifest, line_counts)
        if manifest.get('snapshot_digest') != snapshot_from_entries(entries):
            raise ValueError('Preservation content snapshot digest does not match.')
    if expected_sha256 is not None:
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(BUFFER_BYTES), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError('Preservation archive differs from the recorded digest.')
    return manifest



def _jsonl(archive, name):
    with archive.open(name) as stream:
        while line := stream.readline(MAX_RECORD_BYTES + 1):
            if len(line) > MAX_RECORD_BYTES or not line.endswith(b'\n'):
                raise ValueError('Preservation record exceeds the bounded reader format.')
            yield json.loads(line)


def _verify_inventory(archive, manifest, line_counts):
    coverage, counts = manifest.get('coverage'), manifest.get('table_rows')
    if not isinstance(coverage, dict) or not isinstance(counts, dict):
        raise ValueError('Preservation table coverage is missing.')
    included = {table for table, item in coverage.items()
                if isinstance(item, dict) and item.get('disposition') == 'included'}
    if set(counts) != included:
        raise ValueError('Preservation table inventory is incomplete.')
    for table, count in counts.items():
        if type(count) is not int or count < 0 or line_counts.get('records/' + table + '.jsonl') != count:
            raise ValueError('Preservation table row count is inconsistent.')
    expected_members = {'manifest.json', 'README.txt', 'verify_archive.py', 'sources.jsonl',
                        'unavailable-references.jsonl'} | {'records/' + table + '.jsonl' for table in included}
    seen = set()
    for source in _jsonl(archive, 'sources.jsonl'):
        if not isinstance(source, dict) or not re.fullmatch('[0-9a-f]{32}', source.get('document_id', '')):
            raise ValueError('Preservation source identity is invalid.')
        identifier = source['document_id']
        if identifier in seen:
            raise ValueError('Preservation source inventory contains duplicates.')
        seen.add(identifier)
        original, units = 'originals/' + identifier, 'extracted/' + identifier + '.jsonl'
        expected_members.update((original, units))
        metadata = manifest['entries'].get(original)
        if metadata != {'sha256': source.get('digest'), 'bytes': source.get('size')}:
            raise ValueError('Preservation original does not match its source inventory.')
        playback = 'playback/' + identifier + '.mp4'
        if playback in manifest['entries']:
            expected_members.add(playback)
            if manifest['entries'][playback]['bytes'] != source.get('playback_size'):
                raise ValueError('Preservation rendition size is inconsistent.')
    if expected_members != set(archive.namelist()):
        raise ValueError('Preservation source or table members are unaccounted for.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify a preservation ZIP offline without extracting it.')
    parser.add_argument('archive')
    parser.add_argument('--sha256', help='Digest independently recorded at download time')
    args = parser.parse_args(argv)
    try:
        manifest = verify_matter_archive(args.archive, expected_sha256=args.sha256)
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile):
        print('Archive verification failed. No files were extracted.', file=sys.stderr)
        return 1
    print(f"Verified {len(manifest['entries'])} members; format {manifest['format']}.")
    print('Internal integrity only; no claim of legal retention, authorship or automatic restore.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
