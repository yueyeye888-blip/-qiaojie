#!/usr/bin/env python3
"""User-invoked restart of the existing USB source, with idle verification."""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent
INSTALLED=Path.home()/'Library/Application Support/BluetoothBridgeController'


def main():
    if ROOT!=INSTALLED:
        subprocess.run([sys.executable,str(INSTALLED/'restart_source.py')],check=True,cwd=INSTALLED)
        return
    if os.geteuid()==0:
        raise SystemExit('请以 Mac 当前用户启动，仅 USB 服务重启步骤需要管理员密码。')
    cfg=json.loads((ROOT/'config.json').read_text())
    token=(ROOT/cfg['runtime_dir']/'admin_token').read_text().strip()
    request=urllib.request.Request(f"http://{cfg['ui_bind']}:{cfg['ui_port']}/api/status",headers={'X-Admin-Token':token})
    with urllib.request.urlopen(request,timeout=5) as response:
        status=json.load(response)
    if status['lease']['state']!='idle' or status['lease']['target_id']:
        raise SystemExit('共享尚未确认释放，未重启源服务。')
    print('将重启现有 VirtualHere USB 源服务，完成后恢复本地管理后台。',flush=True)
    try:
        subprocess.run(['/usr/bin/sudo',sys.executable,'-I',str(ROOT/'install_source_isolation.py'),'--restart-service'],check=True,cwd=ROOT)
    finally:
        subprocess.run(['/bin/launchctl','kickstart','-k','gui/'+str(os.getuid())+'/com.lumen.bluetoothbridge.controller'],check=True,capture_output=True)
    print('重启步骤完成。请保持两台 Windows 桌面连接，等待接管复测。')


if __name__=='__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit('重启未完成，请保留上方具体错误。')
