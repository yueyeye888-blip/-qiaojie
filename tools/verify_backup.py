#!/usr/bin/env python3
"""Verify packaged files only; never import the application or contact a service."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / 'backup/manifest.json').read_text())
    failures = []
    for item in manifest['files']:
        relative = Path(item['path'])
        path = ROOT / relative
        if relative.is_absolute() or '..' in relative.parts or path.is_symlink():
            failures.append(str(relative) + ': unsafe path')
            continue
        if not path.is_file():
            failures.append(str(relative) + ': missing')
            continue
        data = path.read_bytes()
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            failures.append(str(relative) + ': checksum mismatch')
        if sys.platform != 'win32' and bool(path.stat().st_mode & 0o111) != item['executable']:
            failures.append(str(relative) + ': executable permission mismatch')
    if failures:
        print('\n'.join(failures))
        return 1
    print('Verified %d files against baseline %s' % (len(manifest['files']), manifest['baseline']))
    print('This verifies packaged bytes, not USB readiness, credentials or live deployment.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
