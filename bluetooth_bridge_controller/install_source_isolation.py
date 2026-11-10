#!/usr/bin/env python3
"""One-time, reversible change to the existing VirtualHere launch daemon.

Default is a read-only review. --apply requires macOS administrator privileges.
No drivers, extensions, default routes or cloud resources are installed here.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT=Path(__file__).resolve().parent
PLIST=Path('/Library/LaunchDaemons/com.virtualhere.vhusbd.plist')
SUPPORT=Path('/Library/Application Support/BluetoothBridge')
LABEL='com.virtualhere.vhusbd'


def write_atomic(path,data,mode,owner=None):
    fd,name=tempfile.mkstemp(prefix=path.name+'.bridge-',dir=path.parent)
    tmp=Path(name)
    os.fchmod(fd,mode)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if owner:
            os.chown(tmp,*owner)
        os.replace(tmp,path)
    finally:
        if tmp.exists():
            tmp.unlink()


def ensure_idle(cfg):
    token=(ROOT/cfg['runtime_dir']/'admin_token').read_text().strip()
    req=urllib.request.Request(f"http://{cfg['ui_bind']}:{cfg['ui_port']}/api/status",headers={'X-Admin-Token':token})
    with urllib.request.urlopen(req,timeout=5) as response:
        snapshot=json.load(response)
    if snapshot['lease']['state']!='idle' or snapshot['lease']['target_id']:
        raise SystemExit('请先恢复 Yorke 心跳并确认释放；当前租约尚未空闲，未更改服务。')


def prepare_maintenance(cfg):
    token=(ROOT/cfg['runtime_dir']/'admin_token').read_text().strip()
    base=f"http://{cfg['ui_bind']}:{cfg['ui_port']}"
    req=urllib.request.Request(base+'/api/source/maintenance',data=b'{}',headers={'X-Admin-Token':token,'Origin':base,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=10) as response:
        json.load(response)


def launch(action,*args,check=True):
    result=subprocess.run(['/bin/launchctl',action,*args],capture_output=True,text=True)
    if check and result.returncode:
        detail=(result.stderr or result.stdout or '').strip()[:1000]
        raise RuntimeError(f'launchctl {action} failed ({result.returncode}): {detail}')
    return result


def bootstrap_service():
    # bootout may still be completing when launchctl returns; retry only loading
    # the same local daemon definition, never repeat a remote install command.
    for attempt in range(10):
        result=launch('bootstrap','system',str(PLIST),check=False)
        if result.returncode==0:
            return
        time.sleep(1)
    raise RuntimeError(f'launchctl bootstrap failed ({result.returncode}): '+(result.stderr or result.stdout or '').strip()[:1000])


def stop_service():
    # bootout acknowledges the request before launchd finishes removing the job.
    # In particular VirtualHere can spend the full exit timeout releasing USB.
    if launch('print','system/'+LABEL,check=False).returncode:
        return
    result=launch('bootout','system/'+LABEL,check=False)
    for _ in range(60):
        if launch('print','system/'+LABEL,check=False).returncode:
            return
        time.sleep(0.5)
    detail=(result.stderr or result.stdout or '').strip()[:1000]
    raise RuntimeError('旧 VirtualHere 服务尚未退出，未继续加载新配置。'+detail)


def wait_for_server(host,port):
    for _ in range(15):
        try:
            with socket.create_connection((host,port),timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f'VirtualHere did not listen on {host}:{port}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true')
    mode.add_argument('--rollback',type=Path)
    mode.add_argument('--restore-service',action='store_true')
    mode.add_argument('--restart-service',action='store_true')
    args=parser.parse_args()
    cfg_path=ROOT/'config.json'
    cfg=json.loads(cfg_path.read_text())
    existing=plistlib.loads(PLIST.read_bytes())
    argv=existing.get('ProgramArguments',[])
    known_binaries=('/Applications/VirtualHereServerUniversal.app/Contents/MacOS/vhusbdosx',str(SUPPORT/'VirtualHereServer.app/Contents/MacOS/vhusbdosx'))
    if not argv or argv[0] not in known_binaries or '-c' not in argv:
        raise SystemExit('VirtualHere 服务配置与已知安装不符，未作修改。')
    if argv[0]==known_binaries[1]:
        binary=Path(argv[0])
        if binary.is_symlink() or binary.stat().st_uid!=0 or binary.stat().st_mode&0o022:
            raise SystemExit('管理员保护的 USB 服务程序权限异常，未作修改。')
    if not args.apply and not args.rollback and not args.restore_service and not args.restart_service:
        print('审核：仅共享 10d7/b012；VirtualHere 改为 127.0.0.1:7576；')
        print('控制器在现有 Tailscale 地址的 7575 端口按单台租约转发。')
        print('控制器退出、租约到期或目标离线时断开转发；备份后只重启现有 USB 服务。')
        print('执行：sudo python3 install_source_isolation.py --apply')
        return
    if os.geteuid()!=0:
        raise SystemExit('此步骤需要 macOS 管理员执行 sudo；未作修改。')
    ensure_idle(cfg)
    if args.restart_service:
        if not cfg.get('usb_proxy_enabled'):
            raise SystemExit('先完成源端隔离，未重启旧模式服务。')
        if Path(argv[argv.index('-c')+1])!=SUPPORT/'virtualhere.ini':
            raise SystemExit('源服务未使用已知受保护配置，未作修改。')
        prepare_maintenance(cfg)
        if launch('print','system/'+LABEL,check=False).returncode:
            bootstrap_service()
        else:
            launch('kickstart','-k','system/'+LABEL)
        wait_for_server('127.0.0.1',int(cfg.get('usb_backend_port',7576)))
        print('现有 VirtualHere USB 源服务已重启并验证本机监听；配置未修改。')
        return
    if args.restore_service:
        if cfg.get('usb_proxy_enabled'):
            raise SystemExit('源端隔离已启用，未按旧模式启动服务。')
        if launch('print','system/'+LABEL,check=False).returncode:
            bootstrap_service()
        host,port=cfg['virtualhere_hub'].rsplit(':',1)
        wait_for_server(host,int(port))
        print('原 VirtualHere 服务已恢复，监听端口已验证。')
        return
    if SUPPORT.exists() and (SUPPORT.is_symlink() or SUPPORT.stat().st_uid!=0 or SUPPORT.stat().st_mode&0o022):
        raise SystemExit('安装目录权限异常，未作修改。')
    SUPPORT.mkdir(mode=0o755,parents=True,exist_ok=True)
    if args.rollback:
        backup=args.rollback.resolve()
        if backup.parent!=SUPPORT/'backups' or backup.stat().st_uid!=0:
            raise SystemExit('回退目录不是本程序创建的管理员备份。')
        token=(ROOT/cfg['runtime_dir']/'admin_token').read_text().strip()
        base=f"http://{cfg['ui_bind']}:{cfg['ui_port']}"
        req=urllib.request.Request(base+'/api/source/prepare-rollback',data=b'{}',headers={'X-Admin-Token':token,'Origin':base,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=10) as response:
            json.load(response)
        stop_service()
        owner=cfg_path.stat()
        write_atomic(PLIST,(backup/'daemon.plist').read_bytes(),0o644)
        write_atomic(cfg_path,(backup/'controller.json').read_bytes(),0o600,(owner.st_uid,owner.st_gid))
        (SUPPORT/'source-isolation.json').unlink(missing_ok=True)
        bootstrap_service()
        print('已恢复原服务配置；请重启本地控制器。')
        return
    if cfg.get('usb_proxy_enabled'):
        raise SystemExit('源端隔离已配置，无需重复初始化。')
    source=Path(argv[argv.index('-c')+1])
    # Read only whitelisted settings for review; never print or log License.
    lines=source.read_text().splitlines()
    settings={line.split('=',1)[0].lower():line.split('=',1)[1] for line in lines if '=' in line}
    if settings.get('alloweddevices','').lower()!='10d7/b012' or not settings.get('license'):
        raise SystemExit('未确认单设备限制或已购许可，未作修改。')
    if launch('print','system/'+LABEL,check=False).returncode:
        print('检测到原 USB 服务未加载，先恢复并验证原端口。',flush=True)
        bootstrap_service()
        host,port=cfg['virtualhere_hub'].rsplit(':',1)
        wait_for_server(host,int(port))
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1',7576))
        except OSError:
            raise SystemExit('本机 7576 端口已占用，未作修改。')
    prepare_maintenance(cfg)
    backups=SUPPORT/'backups'
    backups.mkdir(mode=0o700,exist_ok=True)
    backup=backups/(str(int(time.time()))+'-'+str(os.getpid()))
    backup.mkdir(mode=0o700)
    (backup/'daemon.plist').write_bytes(PLIST.read_bytes())
    (backup/'controller.json').write_bytes(cfg_path.read_bytes())
    (backup/'source.ini').write_bytes(source.read_bytes())
    for path in backup.iterdir():
        path.chmod(0o600)
    replacements={'networkinterface':'NetworkInterface=127.0.0.1','tcpport':'TCPPort=7576',
                  'alloweddevices':'AllowedDevices=10d7/b012','useavahi':'UseAVAHI=0',
                  'reverseclients':'ReverseClients='}
    updated=[line for line in lines if line.split('=',1)[0].lower() not in replacements]
    updated.extend(replacements.values())
    private_config=SUPPORT/'virtualhere.ini'
    write_atomic(private_config,('\n'.join(updated)+'\n').encode(),0o600)
    new_plist=dict(existing)
    new_args=list(argv)
    new_args[new_args.index('-c')+1]=str(private_config)
    new_plist['ProgramArguments']=new_args
    new_plist['KeepAlive']=True
    owner=cfg_path.stat()
    try:
        stop_service()
        write_atomic(PLIST,plistlib.dumps(new_plist),0o644)
        bootstrap_service()
        wait_for_server('127.0.0.1',7576)
        marker={'backend':'127.0.0.1','port':7576,'allowed_devices':'10d7/b012','installed_at':int(time.time())}
        write_atomic(SUPPORT/'source-isolation.json',json.dumps(marker).encode(),0o644)
        cfg.update(usb_proxy_enabled=True,usb_backend_port=7576,usb_proxy_port=7575)
        write_atomic(cfg_path,(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode(),0o600,(owner.st_uid,owner.st_gid))
    except Exception as failure:
        stop_service()
        write_atomic(PLIST,(backup/'daemon.plist').read_bytes(),0o644)
        write_atomic(cfg_path,(backup/'controller.json').read_bytes(),0o600,(owner.st_uid,owner.st_gid))
        (SUPPORT/'source-isolation.json').unlink(missing_ok=True)
        try:
            bootstrap_service()
            host,port=cfg['virtualhere_hub'].rsplit(':',1)
            wait_for_server(host,int(port))
            recovery='原服务已恢复并验证监听。'
        except Exception as restore_failure:
            recovery='原服务尚未恢复：'+str(restore_failure)
        raise SystemExit('升级失败原因：'+str(failure)+'\n'+recovery+'\n管理员备份：'+str(backup))
    print('源端仅本机监听已验证。请重启本地控制器以启用租约转发。')
    print('管理员回退备份：'+str(backup))


if __name__=='__main__':
    try:
        main()
    except RuntimeError as failure:
        raise SystemExit(str(failure))
