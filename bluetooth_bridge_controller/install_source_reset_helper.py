#!/usr/bin/env python3
"""Administrator installer for the fixed-purpose, local USB source helper."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SUPPORT = Path('/Library/Application Support/BluetoothBridge')
SOURCE_PLIST = Path('/Library/LaunchDaemons/com.virtualhere.vhusbd.plist')
HELPER_PLIST = Path('/Library/LaunchDaemons/com.lumen.bluetoothbridge.source-reset.plist')
SOURCE_LABEL = 'system/com.virtualhere.vhusbd'
HELPER_LABEL = 'system/com.lumen.bluetoothbridge.source-reset'
ORIGINAL_BINARY = Path('/Applications/VirtualHereServerUniversal.app/Contents/MacOS/vhusbdosx')
BUNDLE = SUPPORT / 'VirtualHereServer.app'
BINARY = BUNDLE / 'Contents/MacOS/vhusbdosx'
HELPER = SUPPORT / 'source_reset_helper.py'
MANIFEST = SUPPORT / 'reset-helper.json'
INTERPRETER = Path('/Library/Developer/CommandLineTools/usr/bin/python3')


def atomic(path, data, mode=0o600, owner=None):
    fd, name = tempfile.mkstemp(prefix=path.name + '.install-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if owner is not None:
            os.fchown(fd, owner, -1)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def protected(path):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('ProtectedPathPermissions: ' + str(path))


def verify_interpreter():
    # Apple's CLT entry is a root-owned symlink into its versioned framework.
    # Validate both the fixed link's parents and the complete resolved target.
    if INTERPRETER.lstat().st_uid!=0:
        raise RuntimeError('UntrustedAdministratorInterpreter')
    resolved=INTERPRETER.resolve()
    for path in (resolved,*list(resolved.parents)[:-1],*list(INTERPRETER.parents)[:-1]):
        protected(path)


def verify_vendor(bundle):
    subprocess.run(['/usr/bin/codesign','--verify','--strict',str(bundle)],check=True,capture_output=True)
    detail=subprocess.run(['/usr/bin/codesign','-d','--verbose=4',str(bundle)],check=True,capture_output=True,text=True).stderr
    if 'TeamIdentifier=N8C8ZTN347' not in detail or 'Identifier=com.virtualhere.vhusbd' not in detail:
        raise RuntimeError('UnexpectedVendorSignature')


def launch(*args, check=True):
    return subprocess.run(['/bin/launchctl', *args], check=check, capture_output=True, text=True, timeout=35)


def stop(label):
    if launch('print', label, check=False).returncode: return
    launch('bootout', label, check=False)
    for _ in range(60):
        if launch('print', label, check=False).returncode: return
        time.sleep(0.5)
    raise RuntimeError('ServiceDidNotStop: ' + label)


def start(plist):
    for _ in range(10):
        if launch('bootstrap', 'system', str(plist), check=False).returncode == 0: return
        time.sleep(1)
    raise RuntimeError('ServiceDidNotStart: ' + plist.name)


def listening():
    for _ in range(20):
        try:
            with socket.create_connection(('127.0.0.1', 7576), timeout=1): return
        except OSError: time.sleep(1)
    raise RuntimeError('SourceNotListening')


def ping():
    for _ in range(20):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2); connection.connect(str(SUPPORT / 'reset.sock'))
                connection.sendall(b'{"op":"ping"}\n')
                if json.loads(connection.recv(1024)).get('ok'): return
        except (OSError, ValueError): pass
        time.sleep(0.5)
    raise RuntimeError('ResetHelperNotReady')


def api(cfg, path, body=None):
    token = (ROOT / 'runtime/admin_token').read_text().strip()
    base = 'http://127.0.0.1:8766'
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
        headers={'X-Admin-Token': token, 'Origin': base, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=8) as response: return json.load(response)


def restore(backup, uid):
    previous=backup/'previous-vendor.app'
    definition=plistlib.loads((backup/'source.plist').read_bytes())
    candidate=previous if previous.exists() else Path(definition['ProgramArguments'][0]).parents[2]
    verify_vendor(candidate)
    stop(HELPER_LABEL); stop(SOURCE_LABEL)
    if previous.exists():
        if BUNDLE.exists(): os.replace(BUNDLE,backup/('failed-vendor-'+str(time.time_ns())+'.app'))
        os.replace(previous,BUNDLE)
    atomic(SOURCE_PLIST, (backup / 'source.plist').read_bytes(), 0o644)
    atomic(ROOT / 'config.json', (backup / 'controller.json').read_bytes(), owner=uid)
    for key, path in [('helper.plist', HELPER_PLIST), ('helper.py', HELPER), ('manifest.json', MANIFEST)]:
        saved = backup / key
        if saved.exists(): atomic(path, saved.read_bytes(), 0o644 if key.endswith('.plist') else 0o600)
        elif path.exists(): path.unlink()
    start(SOURCE_PLIST); listening()
    if (backup / 'helper.plist').exists(): start(HELPER_PLIST); ping()
    elif (SUPPORT/'reset.sock').exists() and stat.S_ISSOCK((SUPPORT/'reset.sock').lstat().st_mode):
        (SUPPORT/'reset.sock').unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if not args.apply and not args.rollback:
        print('安装一个仅当前 Mac 用户可请求、仅能重启既有 USB 源服务的本机组件。')
        print('服务程序与组件复制到管理员保护目录；不开放 TCP 端口、不修改 sudoers。')
        return
    uid = int(os.environ.get('SUDO_UID', '-1'))
    if os.geteuid() != 0 or uid <= 0 or ROOT.stat().st_uid != uid:
        raise RuntimeError('RunFromTheMacUserAdministratorEntry')
    expected = Path(pwd.getpwuid(uid).pw_dir) / 'Library/Application Support/BluetoothBridgeController'
    if ROOT != expected.resolve(): raise RuntimeError('KnownInstalledDirectoryRequired')
    cfg = json.loads((ROOT / 'config.json').read_text())
    if (cfg.get('runtime_dir') != 'runtime' or cfg.get('ui_bind') != '127.0.0.1'
            or cfg.get('ui_port') != 8766 or cfg.get('usb_backend_port',7576)!=7576
            or not cfg.get('usb_proxy_enabled')):
        raise RuntimeError('ExistingIsolatedControllerRequired')
    state = api(cfg, '/api/status')
    if state['lease']['state'] != 'idle' or state['lease']['target_id']:
        raise RuntimeError('ConfirmedIdleLeaseRequired')
    for path in (SUPPORT,*list(SUPPORT.parents)[:-1],SOURCE_PLIST,SOURCE_PLIST.parent,SUPPORT/'virtualhere.ini'):
        protected(path)
    verify_interpreter()
    definition = plistlib.loads(SOURCE_PLIST.read_bytes())
    argv = definition.get('ProgramArguments', [])
    if (len(argv) != 6 or argv[0] not in (str(ORIGINAL_BINARY), str(BINARY))
            or argv[1:] != ['-o', '-r', '/var/log/vhusbd.log', '-c', str(SUPPORT / 'virtualhere.ini')]):
        raise RuntimeError('UnexpectedSourceDefinition')
    verify_vendor(Path(argv[0]).parents[2])
    api(cfg, '/api/source/maintenance', {})
    if args.rollback:
        backup = args.rollback.resolve()
        if backup.parent != SUPPORT / 'backups' or not backup.name.startswith('reset-helper-'):
            raise RuntimeError('KnownAdministratorBackupRequired')
        protected(backup); protected(backup / 'source.plist'); protected(backup / 'controller.json')
        restore(backup, uid)
        print('已恢复自动复位安装前的 USB 服务及控制器配置。')
        return
    backups = SUPPORT / 'backups'
    protected(backups)
    backup = backups / ('reset-helper-' + str(int(time.time())) + '-' + str(os.getpid()))
    backup.mkdir(mode=0o700)
    for source, name in [(SOURCE_PLIST, 'source.plist'), (ROOT / 'config.json', 'controller.json'),
                         (HELPER_PLIST, 'helper.plist'), (HELPER, 'helper.py'), (MANIFEST, 'manifest.json')]:
        if source.exists(): atomic(backup / name, source.read_bytes())
    print('管理员备份：' + str(backup), flush=True)
    try:
        stop(HELPER_LABEL); stop(SOURCE_LABEL)
        # Pin the signed vendor executable in a root-owned directory so future
        # automatic root restarts never execute a user-writable app binary.
        if Path(argv[0]) != BINARY:
            staging = Path(tempfile.mkdtemp(prefix='.vendor-',dir=SUPPORT))
            try:
                copied = staging / 'VirtualHereServer.app'
                shutil.copytree(ORIGINAL_BINARY.parents[2],copied,symlinks=True)
                for path in [copied,*copied.rglob('*')]:
                    if path.is_symlink():
                        if not path.resolve().is_relative_to(copied.resolve()):
                            raise RuntimeError('VendorBundleExternalSymlink')
                    else:
                        os.chmod(path,0o755 if path.is_dir() or path.stat().st_mode&0o111 else 0o644)
                verify_vendor(copied)
                if BUNDLE.exists():
                    protected(BUNDLE)
                    os.replace(BUNDLE,backup/'previous-vendor.app')
                os.replace(copied,BUNDLE)
            finally:
                shutil.rmtree(staging)
        payload = BINARY.read_bytes()
        verify_vendor(BUNDLE)
        atomic(HELPER, (ROOT / 'source_reset_helper.py').read_bytes())
        manifest = {'version': 1, 'uid': uid, 'admin_token_path': str(ROOT / 'runtime/admin_token'),
                    'binary_sha256': hashlib.sha256(payload).hexdigest()}
        atomic(MANIFEST, json.dumps(manifest).encode())
        definition['ProgramArguments'][0] = str(BINARY)
        atomic(SOURCE_PLIST, plistlib.dumps(definition), 0o644)
        helper_definition = {'Label': 'com.lumen.bluetoothbridge.source-reset',
            'ProgramArguments': [str(INTERPRETER), '-I', str(HELPER)],
            'WorkingDirectory': str(SUPPORT), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 10, 'StandardErrorPath': '/var/log/bluetooth-bridge-reset.log'}
        atomic(HELPER_PLIST, plistlib.dumps(helper_definition), 0o644)
        start(SOURCE_PLIST); listening(); start(HELPER_PLIST); ping()
        cfg['source_reset_enabled'] = True
        cfg['bulk_validation_hold'] = True
        atomic(ROOT / 'config.json', (json.dumps(cfg, indent=2) + '\n').encode(), owner=uid)
        print('自动复位组件已安装并验证；批量接入继续保持暂停。', flush=True)
    except Exception:
        restore(backup, uid)
        print('安装未完成，原服务和配置已恢复。', flush=True)
        raise


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print('自动复位配置失败：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
