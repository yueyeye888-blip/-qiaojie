#!/usr/bin/env python3
"""User-invoked administrator installation; no administrator password is stored."""
from pathlib import Path
import os
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
INSTALLED=Path.home()/'Library/Application Support/BluetoothBridgeController'


def main():
    if ROOT!=INSTALLED:
        return subprocess.run([sys.executable,str(INSTALLED/Path(__file__).name)],cwd=INSTALLED).returncode
    if os.geteuid()==0: raise RuntimeError('请从 Mac 当前用户启动。')
    print('一次性安装受限 USB 自动复位组件，之后分配蓝牙无需重复输入管理员密码。',flush=True)
    print('组件仅在本机接受当前用户请求，只能重启既有 USB 源服务；不会修改 sudoers。',flush=True)
    try:
        return subprocess.run(['/usr/bin/sudo','/Library/Developer/CommandLineTools/usr/bin/python3','-I',str(ROOT/'install_source_reset_helper.py'),'--apply'],cwd=ROOT).returncode
    finally:
        subprocess.run(['/bin/launchctl','kickstart','-k',f'gui/{os.getuid()}/com.lumen.bluetoothbridge.controller'],check=False,capture_output=True)


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        print('自动复位尚未启用：'+str(exc),file=sys.stderr)
        raise SystemExit(1)
