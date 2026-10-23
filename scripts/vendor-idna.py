#!/usr/bin/env python3
"""Reproduce the offline IDNA source archive from the hash-locked wheel."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

WHEEL_SHA256 = 'ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c'
FILES = ('idna/core.py', 'idna/idnadata.py', 'idna/intranges.py',
         'idna/package_data.py', 'idna/uts46data.py')
LICENSE = 'idna-3.20.dist-info/licenses/LICENSE.md'


def build(wheel, destination):
    data = wheel.read_bytes()
    if hashlib.sha256(data).hexdigest() != WHEEL_SHA256:
        raise ValueError('IDNA wheel does not match the reviewed upstream hash')
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / 'idna-3.20.pyz'
    manifest = {'version': '3.20', 'upstream_wheel_sha256': WHEEL_SHA256, 'files': {}}
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(archive, 'w') as output:
        for name in (*FILES, LICENSE):
            content = source.read(name)
            target = 'LICENSE.md' if name == LICENSE else name
            info = zipfile.ZipInfo(target, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o644 << 16
            output.writestr(info, content, compress_type=zipfile.ZIP_STORED)
            manifest['files'][target] = hashlib.sha256(content).hexdigest()
        (destination / 'LICENSE.md').write_bytes(source.read(LICENSE))
    manifest['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(manifest['archive_sha256'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    build(args.wheel, args.destination)
