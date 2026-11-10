#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote

root = Path(__file__).resolve().parent
installed=Path.home()/'Library/Application Support/BluetoothBridgeController'
if root!=installed and (installed/'config.json').exists():
    subprocess.run([sys.executable,str(installed/'launch.py')],check=True,cwd=installed)
    raise SystemExit(0)
cfg = json.loads((root / "config.json").read_text())
runtime = (root / cfg["runtime_dir"]).resolve()
base=f'http://{cfg["ui_bind"]}:{cfg["ui_port"]}'
try:
    with urllib.request.urlopen(base,timeout=2):
        pass
except urllib.error.HTTPError as error:
    if error.code!=401:
        raise SystemExit('本地端口被其他服务占用，请检查控制器状态。')
except urllib.error.URLError:
    runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(runtime,0o700)
    log=open(runtime/'controller.log','ab',buffering=0)
    subprocess.Popen([sys.executable,'-u',str(root/'controller.py')],cwd=root,
                     stdout=log,stderr=log,start_new_session=True)
    for attempt in range(30):
        time.sleep(0.3)
        try:
            with urllib.request.urlopen(base,timeout=1):
                break
        except urllib.error.HTTPError as error:
            if error.code==401:break
        except urllib.error.URLError:pass
    else:
        raise SystemExit('后台未启动，请检查 Tailscale 和 runtime/controller.log。')
token = (runtime / "admin_token").read_text().strip()
request=urllib.request.Request(base+'/api/launch-token',data=b'{}',headers={
    'X-Admin-Token':token,'Origin':base,'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(request,timeout=5) as response:
        launch_token=json.load(response)['launch_token']
except urllib.error.HTTPError:
    raise SystemExit('控制器版本需要更新，请先重启控制器。')
url = base+'/?launch='+quote(launch_token)
webbrowser.open(url)
