#!/usr/bin/env python3
"""Install the controller as this user's login agent; default only previews."""
import argparse
import json
import os
import plistlib
import socket
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
LABEL='com.lumen.bluetoothbridge.controller'
DEPLOYMENT=Path.home()/'Library/Application Support/BluetoothBridgeController'


def stage(cfg):
    """Keep the login service outside macOS protected Desktop folders."""
    if ROOT==DEPLOYMENT:
        return ROOT
    DEPLOYMENT.mkdir(parents=True,exist_ok=True,mode=0o700)
    DEPLOYMENT.chmod(0o700)
    for path in ROOT.iterdir():
        if path.is_file() and path.suffix in {'.py','.md','.command'}:
            shutil.copy2(path,DEPLOYMENT/path.name)
    for name in ('windows','static'):
        shutil.copytree(ROOT/name,DEPLOYMENT/name,dirs_exist_ok=True)
    shutil.copy2(ROOT/'ram-policy.json',DEPLOYMENT/'ram-policy.json')
    target_config=DEPLOYMENT/'config.json'
    if not target_config.exists():
        target_config.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n')
        target_config.chmod(0o600)
        source_runtime=(ROOT/cfg['runtime_dir']).resolve()
        target_runtime=DEPLOYMENT/'runtime'
        target_runtime.mkdir(mode=0o700,exist_ok=True)
        for name in ('admin_token','enrollment_secret'):
            if (source_runtime/name).exists():
                shutil.copy2(source_runtime/name,target_runtime/name)
                (target_runtime/name).chmod(0o600)
        if (source_runtime/'bridge.sqlite3').exists():
            with sqlite3.connect(source_runtime/'bridge.sqlite3') as source:
                with sqlite3.connect(target_runtime/'bridge.sqlite3') as target:
                    source.backup(target)
            (target_runtime/'bridge.sqlite3').chmod(0o600)
        deployed_cfg=dict(cfg,runtime_dir='runtime')
        target_config.write_text(json.dumps(deployed_cfg,ensure_ascii=False,indent=2)+'\n')
    return DEPLOYMENT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if os.geteuid()==0:
        raise SystemExit('此步骤以 Mac 当前用户运行，不要使用 sudo。')
    cfg=json.loads((ROOT/'config.json').read_text())
    path=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    if not args.apply:
        print('审核：为当前 Mac 用户添加登录启动与异常退出重启。')
        print('后台监听范围保持 config.json 中的本机和 Tailscale 地址。')
        print('执行：python3 install_login_service.py --apply')
        return
    domain='gui/'+str(os.getuid())
    loaded=subprocess.run(['/bin/launchctl','print',domain+'/'+LABEL],capture_output=True).returncode==0
    if loaded:
        subprocess.run(['/bin/launchctl','bootout',domain+'/'+LABEL],capture_output=True,check=True)
    try:
        with socket.create_connection((cfg['ui_bind'],int(cfg['ui_port'])),timeout=1):
            raise SystemExit('控制器仍由其他进程运行；请先停止手动进程，避免启动两个后台。')
    except OSError:
        pass
    deployment=stage(cfg)
    deployed_cfg=json.loads((deployment/'config.json').read_text())
    runtime=(deployment/deployed_cfg['runtime_dir']).resolve()
    logs=Path.home()/'Library/Logs/BluetoothBridge'
    logs.mkdir(parents=True,exist_ok=True,mode=0o700)
    definition={'Label':LABEL,'ProgramArguments':[sys.executable,'-u',str(deployment/'controller.py')],
                'WorkingDirectory':str(deployment),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,
                'StandardOutPath':str(logs/'controller.log'),'StandardErrorPath':str(logs/'controller.log')}
    runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(runtime,0o700)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        old=plistlib.loads(path.read_bytes())
        known_scripts={str(ROOT/'controller.py'),str(DEPLOYMENT/'controller.py')}
        if old.get('Label')!=LABEL or not known_scripts.intersection(old.get('ProgramArguments',[])):
            raise SystemExit('已有不同来源的登录服务，未修改。')
    path.write_bytes(plistlib.dumps(definition))
    path.chmod(0o600)
    result=subprocess.run(['/bin/launchctl','bootstrap',domain,str(path)],capture_output=True)
    if result.returncode:
        raise SystemExit('服务定义已保存，但加载失败；请在当前 Mac 登录会话中执行。')
    print('登录启动服务已加载。关闭网页后，健康检查和已配置通知仍在后台运行。')
    print('停用：launchctl bootout '+domain+'/'+LABEL)
    print('完全移除时，再删除 '+str(path))


if __name__=='__main__':main()
