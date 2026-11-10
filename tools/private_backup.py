#!/usr/bin/env python3
"""Create a LOCAL confidential snapshot without service, network or keychain calls.

Archive is permission-restricted, NOT encrypted. Never upload it to GitHub.
No sudo is requested; inaccessible administrator files are recorded as gaps.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deployment', type=Path, default=Path.home() / 'Library/Application Support/BluetoothBridgeController')
    args = parser.parse_args()
    previous_umask = os.umask(0o077)
    try:
        source = args.deployment.expanduser().resolve()
        if not (source / 'config.json').is_file() or not (source / 'runtime/bridge.sqlite3').is_file():
            raise SystemExit('Expected installed configuration/database not found; no snapshot made.')
        output = args.output.expanduser().resolve()
        repo = Path(__file__).resolve().parents[1]
        if output == repo or repo in output.parents or output == source or source in output.parents:
            raise SystemExit('Choose a private output directory outside the repository and deployment.')
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.chmod(0o700)
        ignore = output / '.gitignore'
        if not ignore.exists():
            ignore.write_text('*\n')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        archive = output / ('bridge-private-backup-' + stamp + '.tar.gz')
        if archive.exists():
            raise SystemExit('Snapshot already exists; choose another output directory.')
        report = {'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'contains_secrets': True, 'encrypted': False, 'copied': [], 'gaps': [],
                  'excluded': ['Keychain contents', 'Tailscale node state/private keys', 'remote Windows/cloud state', 'live SQLite WAL/SHM', 'sockets and locks']}
        files = []
        with tempfile.TemporaryDirectory(prefix='.bridge-private-', dir=output) as temporary:
            root = Path(temporary)
            def copy_file(path, destination):
                try:
                    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                        report['gaps'].append({'path': str(path), 'reason': 'not a regular file'})
                        return
                    before = hashlib.sha256(path.read_bytes()).hexdigest()
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(path, destination)
                    destination.chmod(0o600)
                    saved = hashlib.sha256(destination.read_bytes()).hexdigest()
                    after = hashlib.sha256(path.read_bytes()).hexdigest()
                    if before != saved or saved != after:
                        report['gaps'].append({'path': str(path), 'reason': 'changed during copy; review before restore'})
                    report['copied'].append(str(destination.relative_to(root)))
                except (OSError, PermissionError) as exc:
                    report['gaps'].append({'path': str(path), 'reason': type(exc).__name__})

            for current, dirs, names in os.walk(source, followlinks=False):
                dirs[:] = [name for name in dirs if name not in ('__pycache__', '.git')]
                for name in names:
                    path = Path(current) / name
                    if name in ('controller.lock', '.DS_Store') or name.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.pyc')):
                        continue
                    copy_file(path, root / 'deployment' / path.relative_to(source))
            db_path = root / 'deployment/runtime/bridge.sqlite3'
            db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with sqlite3.connect((source / 'runtime/bridge.sqlite3').as_uri() + '?mode=ro', uri=True) as original:
                with sqlite3.connect(db_path) as snapshot:
                    original.backup(snapshot)
                    result = snapshot.execute('PRAGMA integrity_check').fetchone()[0]
                    if result != 'ok':
                        raise RuntimeError('SQLite snapshot integrity check failed')
                    report['database_integrity'] = result
                    report['lease_at_snapshot'] = dict(zip(('state', 'target_id', 'generation'), snapshot.execute('SELECT state,target_id,generation FROM lease WHERE singleton=1').fetchone()))
                    tables = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'jobs' in tables:
                        report['job_states'] = dict(snapshot.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
            db_path.chmod(0o600)
            report['copied'].append('deployment/runtime/bridge.sqlite3 (SQLite online backup)')
            support = Path('/Library/Application Support/BluetoothBridge')
            for path in (Path.home() / 'Library/LaunchAgents/com.lumen.bluetoothbridge.controller.plist',
                         Path('/Library/LaunchDaemons/com.virtualhere.vhusbd.plist'),
                         Path('/Library/LaunchDaemons/com.lumen.bluetoothbridge.source-reset.plist'),
                         support / 'source-isolation.json', support / 'virtualhere.ini',
                         support / 'reset-helper.json', support / 'source_reset_helper.py'):
                copy_file(path, root / 'system-files' / str(path).lstrip('/'))
            # Copy readable signed vendor bundle as an opaque local recovery artifact.
            vendor = support / 'VirtualHereServer.app'
            if vendor.is_dir():
                try:
                    shutil.copytree(vendor, root / 'vendor/VirtualHereServer.app', symlinks=True)
                    report['copied'].append('vendor/VirtualHereServer.app')
                except OSError as exc:
                    report['gaps'].append({'path': str(vendor), 'reason': type(exc).__name__})
            for path in root.rglob('*'):
                if path.is_file() and not path.is_symlink():
                    files.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size,
                                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            report['files'] = files
            (root / 'snapshot-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            with tarfile.open(archive, 'w:gz') as package:
                package.add(root, arcname='snapshot', recursive=True)
        archive.chmod(0o600)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = archive.with_name(archive.name + '.sha256')
        checksum.write_text(digest + '  ' + archive.name + '\n')
        checksum.chmod(0o600)
        # Re-open the archive and verify every regular file against the manifest.
        with tarfile.open(archive, 'r:gz') as package:
            actual_report = json.load(package.extractfile('snapshot/snapshot-report.json'))
            for item in actual_report['files']:
                data = package.extractfile('snapshot/' + item['path']).read()
                if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise RuntimeError('Archive verification failed')
        print(json.dumps({'archive': str(archive), 'sha256': digest, 'verified_files': len(files),
                          'database_integrity': report['database_integrity'],
                          'administrator_gaps': report['gaps'], 'encrypted': False,
                          'warning': 'LOCAL CONFIDENTIAL ARCHIVE: do not upload; save to encrypted offline storage.'}, ensure_ascii=False))
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    main()
