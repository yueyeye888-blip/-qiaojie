#!/usr/bin/env python3
"""User-invoked, one-time setup. Administrator password stays in macOS sudo."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent
LABEL='com.lumen.bluetoothbridge.controller'


def main():
    installed=Path.home()/'Library/Application Support/BluetoothBridgeController'
    if ROOT!=installed and (installed/'config.json').exists():
        subprocess.run([sys.executable,str(installed/'setup_local.py')],check=True,cwd=installed)
        return
    if os.geteuid()==0:
        raise SystemExit('请以 Mac 当前用户启动；仅 USB 服务配置步骤会请求管理员密码。')
    cfg=json.loads((ROOT/'config.json').read_text())
    db_path=ROOT/cfg['runtime_dir']/'bridge.sqlite3'
    if db_path.exists():
        with sqlite3.connect(db_path) as db:
            row=db.execute('SELECT state,target_id FROM lease WHERE singleton=1').fetchone()
            if row and (row[0]!='idle' or row[1]):
                raise SystemExit('共享尚未确认释放。请打开 Yorke 等待控制台显示空闲后，再运行首次配置。')
    domain='gui/'+str(os.getuid())+'/'+LABEL
    loaded=subprocess.run(['/bin/launchctl','print',domain],capture_output=True).returncode==0
    if loaded:
        subprocess.run(['/bin/launchctl','bootout',domain],check=True,capture_output=True)
    else:
        result=subprocess.run(['/usr/sbin/lsof','-t','-nP','-iTCP:'+str(cfg['ui_port']),'-sTCP:LISTEN'],capture_output=True,text=True)
        for pid in result.stdout.split():
            cmd=subprocess.run(['/bin/ps','-ww','-p',pid,'-o','command='],capture_output=True,text=True,env={**os.environ,'LC_ALL':'en_US.UTF-8'}).stdout
            if str(ROOT/'controller.py') not in cmd:
                raise SystemExit('本机端口由其他进程占用，未停止该进程。')
            os.kill(int(pid),signal.SIGTERM)
    time.sleep(1)
    subprocess.run([sys.executable,str(ROOT/'install_login_service.py'),'--apply'],check=True,cwd=ROOT)
    if ROOT!=installed:
        subprocess.run([sys.executable,str(installed/'setup_local.py')],check=True,cwd=installed)
        return
    base=f"http://{cfg['ui_bind']}:{cfg['ui_port']}"
    token=(ROOT/cfg['runtime_dir']/'admin_token').read_text().strip()
    def status():
        request=urllib.request.Request(base+'/api/status',headers={'X-Admin-Token':token})
        with urllib.request.urlopen(request,timeout=3) as response:return json.load(response)
    for _ in range(30):
        try:status();break
        except (OSError,ValueError):time.sleep(1)
    else:raise SystemExit('后台尚未启动，请检查 runtime/controller.log。')
    try:
        if not cfg.get('usb_proxy_enabled'):
            print('将把现有 USB 服务改为本机监听，并启用按租约转发。管理员密码由 macOS 本机读取。',flush=True)
            subprocess.run(['/usr/bin/sudo',sys.executable,'-I',str(ROOT/'install_source_isolation.py'),'--apply'],check=True,cwd=ROOT)
    finally:
        # Also clear maintenance mode after a failed or cancelled administrator step.
        subprocess.run(['/bin/launchctl','kickstart','-k',domain],check=True,capture_output=True)
    for _ in range(30):
        try:
            result=status()
            if result['management']['source_isolation_ready']:
                print('本地后台与源端隔离已启动。接下来配置云 API、Telegram，并在 Yorke 复测接管和释放。')
                break
        except (OSError,ValueError,KeyError):pass
        time.sleep(1)
    else:
        raise SystemExit('源端尚未就绪，请查看控制台诊断；批量接入仍保持关闭。')
    subprocess.run([sys.executable,str(ROOT/'launch.py')],check=True,cwd=ROOT)


if __name__=='__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit('配置未完成，请保留上方的具体错误；未继续后续部署。')
