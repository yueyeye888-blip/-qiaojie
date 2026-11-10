#!/usr/bin/env python3
"""Temporary, user-invoked source-restart experiment; installs no service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
INSTALLED = Path.home() / 'Library/Application Support/BluetoothBridgeController'
PILOTS = {1: 'ecd-example-pilot-c1', 2: 'ecd-example-pilot-c2'}


class Acceptance:
    def __init__(self, api, reset, record, sleep=time.sleep, clock=time.monotonic):
        self.api, self.reset, self.record = api, reset, record
        self.sleep, self.clock = sleep, clock
        self.deadline = None

    def wait(self, predicate, timeout, fail_on_error=False):
        until = self.clock() + timeout
        state = None
        if self.deadline is not None:
            until = min(until, self.deadline)
        while self.clock() < until:
            state = self.api('/api/status')
            if fail_on_error:
                if state['lease']['state'] == 'releasing':
                    raise RuntimeError('ControllerProtectionReleasedTarget')
                for agent in state['agents']:
                    if agent['id'] == state['lease']['target_id'] and agent.get('last_report', {}).get('error'):
                        raise RuntimeError('AgentAttachmentFailed: ' + agent['last_report']['error'])
            if predicate(state):
                return state
            self.sleep(3)
        if self.record and state is not None:
            self.record('confirmation_timeout', state=self.summary(state))
        raise RuntimeError('ConfirmationTimeout')

    @staticmethod
    def summary(state):
        return {'lease_state': state['lease']['state'], 'lease_target': state['lease']['target_id'],
                'pilots': [{'id': a['id'], 'online': a['online'],
                            'desktop_matches': a.get('desktop_id') == PILOTS[a['id']],
                            'vh': a.get('last_report', {}).get('vh_status'),
                            'bluetooth': a.get('last_report', {}).get('bluetooth_status'),
                            'has_error': bool(a.get('last_report', {}).get('error'))}
                           for a in state['agents'] if a['id'] in PILOTS]}

    @staticmethod
    def released(state):
        agents = {a['id']: a for a in state['agents']}
        return (state['lease']['state'] == 'idle' and not state['lease']['target_id']
                and all(i in agents and agents[i]['online']
                        and agents[i].get('desktop_id') == PILOTS[i]
                        and not agents[i].get('last_report', {}).get('error')
                        and agents[i].get('last_report', {}).get('vh_status') == 'released'
                        and agents[i].get('last_report', {}).get('bluetooth_status') == 'absent'
                        for i in PILOTS))

    def run(self, sequence=(2, 1, 2)):
        # Never end an unrelated user's live session to start this experiment.
        initial = self.api('/api/status')
        if initial['lease']['state'] != 'idle' or initial['lease']['target_id']:
            raise RuntimeError('ExistingSessionMustBeReleasedFirst')
        agents = {a['id']: a for a in initial['agents']}
        if any(i not in agents or agents[i].get('desktop_id') != desktop for i, desktop in PILOTS.items()):
            raise RuntimeError('PilotIdentityMismatch')
        if not self.released(initial):
            self.record('preflight_wait', state=self.summary(initial))
        claimed = False
        self.deadline = self.clock() + 8 * 60
        try:
            for target in sequence:
                if target not in PILOTS:
                    raise ValueError('OnlySelectedPilotsAllowed')
                self.wait(self.released, 150)
                if self.deadline - self.clock() < 110:
                    raise RuntimeError('TestTimeBudgetExhausted')
                self.record('reset_start', target=target)
                self.reset()
                self.wait(self.released, 150)
                self.record('acquire_start', target=target)
                claimed = True  # An ambiguous response still requires release.
                self.api('/api/lease/acquire', {'agent_id': target})
                def attached(state):
                    if state['lease']['state'] != 'active' or state['lease']['target_id'] != target:
                        return False
                    for agent in state['agents']:
                        if agent['id'] in PILOTS:
                            report = agent.get('last_report', {})
                            expected = 'ready' if agent['id'] == target else 'absent'
                            if not agent['online'] or report.get('error') or report.get('bluetooth_status') != expected:
                                return False
                    return True
                self.wait(attached, 150, fail_on_error=True)
                self.sleep(6)
                if not attached(self.api('/api/status')):
                    raise RuntimeError('AttachmentDidNotRemainReady')
                self.record('attached_ready', target=target)
                self.api('/api/lease/release', {})
                self.wait(self.released, 150)
                claimed = False
                self.record('released_confirmed', target=target)
            self.record('restart_workaround_passed', cycles=len(sequence))
        finally:
            self.deadline = None  # Always reserve a separate release-confirmation window.
            if claimed:
                self.api('/api/lease/release', {})
                self.wait(self.released, 150)
                self.record('failure_release_confirmed')


def local_api(cfg):
    if cfg['runtime_dir'] != 'runtime':
        raise RuntimeError('KnownRuntimeDirectoryRequired')
    token = (ROOT / cfg['runtime_dir'] / 'admin_token').read_text().strip()
    base = f"http://{cfg['ui_bind']}:{cfg['ui_port']}"
    if cfg['ui_bind'] != '127.0.0.1':
        raise RuntimeError('LocalManagementAddressRequired')
    def call(path, body=None, timeout=10):
        request = urllib.request.Request(base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'X-Admin-Token': token, 'Origin': base, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    return call


def source_pid():
    result = subprocess.run(['/bin/launchctl', 'print', 'system/com.virtualhere.vhusbd'],
                            check=True, capture_output=True, text=True, timeout=10)
    match = re.search(r'^\s*pid = (\d+)$', result.stdout, re.M)
    if not match:
        raise RuntimeError('SourceServicePidMissing')
    return int(match[1])


def privileged_test():
    uid = int(os.environ.get('SUDO_UID', '-1'))
    if os.geteuid() != 0 or uid <= 0 or ROOT.stat().st_uid != uid:
        raise RuntimeError('RunFromTheUserInvokedSudoEntry')
    cfg = json.loads((ROOT / 'config.json').read_text())
    api = local_api(cfg)
    if (ROOT / 'runtime').is_symlink() or (ROOT / 'runtime').stat().st_uid != uid:
        raise RuntimeError('UnexpectedRuntimeOwnership')
    def interrupted(signum, frame):
        raise RuntimeError('TestInterrupted')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    # Output contains phase evidence only; no tokens or raw cloud commands.
    report = ROOT / cfg['runtime_dir'] / ('source-cycle-' + str(int(time.time())) + '.jsonl')
    fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchown(fd, uid, -1)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        def record(event, **fields):
            line = json.dumps({'time': int(time.time()), 'event': event, **fields})
            stream.write(line + '\n'); stream.flush()
            print(line, flush=True)
        def reset():
            before = source_pid()
            try:
                subprocess.run([sys.executable, '-I', str(ROOT / 'install_source_isolation.py'),
                                '--restart-service'], check=True, cwd=ROOT, timeout=60)
                if source_pid() == before:
                    raise RuntimeError('SourcePidDidNotChange')
            finally:
                # Restore the user controller even when a reset failed halfway.
                subprocess.run(['/bin/launchctl', 'kickstart', '-k',
                                f'gui/{uid}/com.lumen.bluetoothbridge.controller'],
                               check=True, capture_output=True, timeout=20)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    state = api('/api/status', timeout=2)
                    if state['management']['source_isolation_ready']:
                        record('source_restarted', old_pid=before, new_pid=source_pid())
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            raise RuntimeError('SourceIsolationNotReadyAfterReset')
        try:
            Acceptance(api, reset, record).run()
        except Exception as exc:
            record('test_failed', error=str(exc)[:500])
            raise
    print('Report: ' + str(report), flush=True)


def user_entry():
    if ROOT != INSTALLED:
        return subprocess.run([sys.executable, str(INSTALLED / Path(__file__).name)], cwd=INSTALLED).returncode
    if os.geteuid() == 0:
        raise RuntimeError('StartAsTheMacUser')
    cfg = json.loads((ROOT / 'config.json').read_text())
    api = local_api(cfg)
    initial = api('/api/status')
    if initial['lease']['state'] != 'idle' or initial['lease']['target_id']:
        raise RuntimeError('ReleaseTheCurrentSessionFirst')
    from integrations import CloudClient
    cloud = CloudClient(cfg.get('cloud_region', 'ap-southeast-1'))
    stop = threading.Event()
    # Keep only these already-selected pilots available for this bounded test.
    def wake_once():
        for desktop in cloud.desktops():
            if desktop['DesktopId'] in PILOTS.values():
                action = {'Stopped': 'StartDesktops', 'Hibernated': 'WakeupDesktops'}.get(desktop.get('DesktopStatus'))
                if action:
                    cloud.call(action, DesktopId=[desktop['DesktopId']])
    print('正在唤醒两台试点，等待在线且释放；随后由 macOS 提示管理员密码。', flush=True)
    wake_once()
    def keep_available():
        deadline = time.monotonic() + 12 * 60
        while not stop.wait(15) and time.monotonic() < deadline:
            try:
                wake_once()
            except Exception:
                print('Pilot wake check failed; USB test will still require online confirmation.', flush=True)
    thread = threading.Thread(target=keep_available, daemon=True)
    thread.start()
    try:
        Acceptance(api, None, None).wait(Acceptance.released, 150)
        print('临时测试：仅对张26、Yorke按顺序重启源服务、接管及释放；不安装常驻管理员组件。', flush=True)
        process = subprocess.Popen(['/usr/bin/sudo', sys.executable, '-I', str(ROOT / Path(__file__).name),
                                    '--privileged-test'], cwd=ROOT)
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The foreground test receives Ctrl-C too; allow its finally block
            # to request release and confirm detachment before restoring the UI.
            return process.wait(timeout=180)
    finally:
        stop.set()
        subprocess.run(['/bin/launchctl', 'kickstart', '-k',
                        f'gui/{os.getuid()}/com.lumen.bluetoothbridge.controller'],
                       check=False, capture_output=True, timeout=20)


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--privileged-test']:
            privileged_test()
        elif not sys.argv[1:]:
            raise SystemExit(user_entry())
        else:
            raise RuntimeError('UnexpectedArguments')
    except Exception as exc:
        print('验收未通过：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
