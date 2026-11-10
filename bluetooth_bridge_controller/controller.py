#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tailscale_oauth import create_one_time_auth_key, credentials_available
from integrations import CloudClient, configure_telegram, discover_telegram_chat, notify_telegram, write_secret
from management import Management
from usb_proxy import UsbProxy, MARKER
from source_reset import Coordinator
from watchdog import Watchdog


ROOT = Path(__file__).resolve().parent


def now() -> int:
    return int(time.time())


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path, cfg: dict, enrollment_secret=None):
        self.path = path
        self.cfg = cfg
        self.enrollment_secret=enrollment_secret or secrets.token_bytes(32)
        self.source_ready=True
        self.lock = threading.RLock()
        self._init()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init(self):
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    desktop_id TEXT UNIQUE,
                    display_name TEXT NOT NULL,
                    machine_id TEXT UNIQUE,
                    hostname TEXT,
                    tailscale_ip TEXT,
                    token_hash TEXT,
                    enrolled_at INTEGER,
                    last_seen INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_report TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS enrollments (
                    token_hash TEXT PRIMARY KEY,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS lease (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    session_id TEXT,
                    target_id INTEGER REFERENCES agents(id),
                    pending_target_id INTEGER REFERENCES agents(id),
                    generation INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'idle',
                    expires_at INTEGER,
                    release_grace_until INTEGER NOT NULL DEFAULT 0,
                    reset_nonce TEXT,
                    reset_error TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO lease(singleton, generation, state, updated_at)
                VALUES(1, 0, 'idle', strftime('%s','now'));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    agent_id INTEGER,
                    details TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            if 'release_grace_until' not in {row[1] for row in db.execute('PRAGMA table_info(lease)')}:
                db.execute('ALTER TABLE lease ADD COLUMN release_grace_until INTEGER NOT NULL DEFAULT 0')
            columns={row[1] for row in db.execute('PRAGMA table_info(lease)')}
            if 'reset_nonce' not in columns:
                db.execute('ALTER TABLE lease ADD COLUMN reset_nonce TEXT')
            if 'reset_error' not in columns:
                db.execute("ALTER TABLE lease ADD COLUMN reset_error TEXT NOT NULL DEFAULT ''")

    def event(self, db, event: str, *, agent_id=None, level="info", details=None):
        db.execute(
            "INSERT INTO events(created_at,level,event,agent_id,details) VALUES(?,?,?,?,?)",
            (now(), level, event, agent_id, json.dumps(details or {}, ensure_ascii=False)),
        )

    def recover_after_restart(self):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT target_id,state FROM lease WHERE singleton=1").fetchone()
            if row["target_id"]:
                db.execute("UPDATE lease SET state='releasing',pending_target_id=NULL,release_grace_until=0,generation=generation+1,updated_at=? WHERE singleton=1", (now(),))
                self.event(db,"controller_restart_release",agent_id=row["target_id"],level="warning")
            elif row['state']=='resetting':
                nonce=db.execute('SELECT reset_nonce FROM lease WHERE singleton=1').fetchone()[0]
                self._fail_source_reset(db,nonce,'ControllerRestartDuringReset')

    def _queue_source_reset(self, db, target):
        db.execute("UPDATE lease SET session_id=NULL,target_id=NULL,pending_target_id=?,state='resetting',reset_nonce=?,release_grace_until=0,generation=generation+1,expires_at=?,updated_at=? WHERE singleton=1",
                   (target,secrets.token_hex(16),now()+60,now()))
        self.event(db,'source_reset_requested',agent_id=target)

    def _fail_source_reset(self, db, nonce, error):
        row=db.execute('SELECT state,reset_nonce,pending_target_id FROM lease WHERE singleton=1').fetchone()
        if row['state']=='resetting' and row['reset_nonce']==nonce:
            db.execute("UPDATE lease SET state='idle',session_id=NULL,target_id=NULL,pending_target_id=NULL,reset_nonce=NULL,expires_at=NULL,reset_error=?,updated_at=? WHERE singleton=1",(error,now()))
            self.event(db,'source_reset_failed',agent_id=row['pending_target_id'],level='critical',details={'code':error})

    def fail_source_reset(self, nonce, error):
        with self.lock,self.connect() as db:
            self._fail_source_reset(db,nonce,error)

    def finish_source_reset(self, nonce, result):
        with self.lock,self.connect() as db:
            self.expire_lease(db)
            lease=db.execute('SELECT * FROM lease WHERE singleton=1').fetchone()
            if lease['state']!='resetting' or lease['reset_nonce']!=nonce:
                return False
            target=db.execute('SELECT * FROM agents WHERE id=?',(lease['pending_target_id'],)).fetchone()
            report=json.loads(target['last_report'] or '{}') if target else {}
            if (not target or not target['last_seen'] or now()-target['last_seen']>int(self.cfg['agent_offline_seconds'])
                    or report.get('error') or report.get('bluetooth_status')!='absent' or report.get('vh_status')!='released'):
                self._fail_source_reset(db,nonce,'TargetNotReadyAfterReset')
                return False
            db.execute("UPDATE lease SET state='authorizing',session_id=?,target_id=?,pending_target_id=NULL,reset_nonce=NULL,reset_error='',generation=generation+1,expires_at=?,updated_at=? WHERE singleton=1",
                       (secrets.token_urlsafe(18),target['id'],now()+int(self.cfg['lease_seconds']),now()))
            self.event(db,'source_reset_completed',agent_id=target['id'],details={'old_pid':result['old_pid'],'new_pid':result['new_pid']})
            return True

    def usb_authorization(self, peer_ip):
        if not self.source_ready:
            return None
        with self.lock, self.connect() as db:
            row = db.execute("SELECT l.*,a.tailscale_ip,a.last_seen,a.status AS agent_status FROM lease l JOIN agents a ON a.id=l.target_id WHERE l.singleton=1").fetchone()
            if not row or row["state"] not in ("authorizing","active") or row["tailscale_ip"] != peer_ip:
                return None
            if not row["expires_at"] or row["expires_at"] <= now() or not row["last_seen"] or now()-row["last_seen"] > int(self.cfg["agent_offline_seconds"]):
                return None
            if row['agent_status']=='fault':
                return None
            return row["session_id"], row["generation"], row["target_id"]

    def usb_connection_authorized(self, peer_ip, identity):
        if identity is None or not self.source_ready:
            return False
        if self.usb_authorization(peer_ip)==identity:
            return True
        with self.lock,self.connect() as db:
            row=db.execute('SELECT l.*,a.tailscale_ip,a.last_seen,a.status AS agent_status FROM lease l JOIN agents a ON a.id=l.target_id WHERE l.singleton=1').fetchone()
            return bool(row and row['state']=='releasing' and row['release_grace_until']>now()
                        and row['session_id']==identity[0] and row['target_id']==identity[2]
                        and row['generation']==identity[1]+1 and row['tailscale_ip']==peer_ip
                        and row['agent_status']!='fault' and row['last_seen']
                        and now()-row['last_seen']<=int(self.cfg['agent_offline_seconds']))

    def release_deadline(self, lease):
        seconds=min(30,max(0,int(self.cfg.get('release_drain_seconds',30))))
        return min(now()+seconds,lease['expires_at'] or now())

    def expire_lease(self, db):
        lease = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
        if lease['state']=='resetting' and lease['expires_at']<=now():
            self._fail_source_reset(db,lease['reset_nonce'],'SourceResetExpired')
            return
        if lease["target_id"] and lease["expires_at"] and lease["expires_at"] <= now() and lease["state"] not in ("releasing", "idle"):
            db.execute(
                "UPDATE lease SET state='releasing',pending_target_id=NULL,release_grace_until=0,generation=generation+1,updated_at=? WHERE singleton=1",
                (now(),),
            )
            self.event(db, "lease_expired", agent_id=lease["target_id"], level="warning")

    def snapshot(self):
        with self.lock, self.connect() as db:
            self.expire_lease(db)
            lease = dict(db.execute("SELECT * FROM lease WHERE singleton=1").fetchone())
            agents = []
            offline_after = int(self.cfg["agent_offline_seconds"])
            for row in db.execute("SELECT * FROM agents ORDER BY display_name COLLATE NOCASE"):
                item = dict(row)
                item.pop("token_hash", None)
                item["online"] = bool(item["last_seen"] and now() - item["last_seen"] <= offline_after)
                try:
                    item["last_report"] = json.loads(item["last_report"] or "{}")
                except json.JSONDecodeError:
                    item["last_report"] = {}
                agents.append(item)
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80")]
            for item in events:
                try:
                    item["details"] = json.loads(item["details"])
                except json.JSONDecodeError:
                    item["details"] = {}
            db.commit()
            return {"now": now(), "lease": lease, "agents": agents, "events": events, "config": {
                "lease_seconds": self.cfg["lease_seconds"],
                "usb": f'{self.cfg["usb_vendor_id"]}:{self.cfg["usb_product_id"]}',
                "hub": self.cfg["virtualhere_hub"],
            }}

    def new_enrollment(self, display_name: str, desktop_id: str | None):
        display_name = display_name.strip()
        desktop_id = (desktop_id or "").strip() or f"manual-{secrets.token_hex(6)}"
        if not display_name or len(display_name) > 80:
            raise ValueError("电脑名称不能为空且不能超过 80 个字符")
        raw = secrets.token_urlsafe(32)
        with self.lock, self.connect() as db:
            row = db.execute("SELECT id,machine_id FROM agents WHERE desktop_id=?", (desktop_id,)).fetchone()
            if row:
                if row["machine_id"]:
                    raise ValueError("该电脑已登记，请使用诊断或修复，不能重复初始化身份")
                agent_id = row["id"]
                db.execute("UPDATE agents SET display_name=? WHERE id=?", (display_name, agent_id))
            else:
                cur = db.execute("INSERT INTO agents(desktop_id,display_name) VALUES(?,?)", (desktop_id, display_name))
                agent_id = cur.lastrowid
            db.execute("DELETE FROM enrollments WHERE agent_id=? OR expires_at<?", (agent_id, now()))
            db.execute(
                "INSERT INTO enrollments(token_hash,agent_id,expires_at) VALUES(?,?,?)",
                (token_hash(raw), agent_id, now() + 3600),
            )
            self.event(db, "enrollment_created", agent_id=agent_id)
        return {"agent_id": agent_id, "desktop_id": desktop_id, "enrollment_token": raw, "expires_at": now() + 3600}

    def enroll(self, enrollment_token: str, machine_id: str, hostname: str, tailscale_ip: str):
        if not machine_id or len(machine_id) > 200:
            raise ValueError("invalid machine_id")
        with self.lock, self.connect() as db:
            row = db.execute(
                "SELECT * FROM enrollments WHERE token_hash=? AND expires_at>?",
                (token_hash(enrollment_token), now()),
            ).fetchone()
            if not row:
                raise PermissionError("enrollment token invalid or expired")
            agent_id = row["agent_id"]
            raw_agent_token=hmac.new(self.enrollment_secret,(str(agent_id)+"|"+machine_id+"|"+enrollment_token).encode(),hashlib.sha256).hexdigest()
            if row["used_at"] is not None:
                bound=db.execute("SELECT * FROM agents WHERE id=?",(agent_id,)).fetchone()
                if bound["machine_id"]!=machine_id or bound["tailscale_ip"]!=tailscale_ip or not hmac.compare_digest(bound["token_hash"] or "",token_hash(raw_agent_token)):
                    raise PermissionError("enrollment token already bound to another identity")
                return self.enrollment_response(agent_id,raw_agent_token)
            existing = db.execute("SELECT id FROM agents WHERE machine_id=? AND id<>?", (machine_id, agent_id)).fetchone()
            if existing:
                raise ValueError("machine identity already belongs to another desktop")
            db.execute("UPDATE enrollments SET used_at=? WHERE token_hash=?", (now(), row["token_hash"]))
            db.execute(
                "UPDATE agents SET machine_id=?,hostname=?,tailscale_ip=?,token_hash=?,enrolled_at=?,last_seen=?,status='ready' WHERE id=?",
                (machine_id, hostname[:100], tailscale_ip[:64], token_hash(raw_agent_token), now(), now(), agent_id),
            )
            self.event(db, "agent_enrolled", agent_id=agent_id, details={"hostname": hostname, "tailscale_ip": tailscale_ip})
        return self.enrollment_response(agent_id,raw_agent_token)

    def enrollment_response(self,agent_id,raw_agent_token):
        return {
            "agent_id": agent_id,
            "agent_token": raw_agent_token,
            "poll_url": f'http://{self.cfg["agent_bind"]}:{self.cfg["agent_port"]}/agent/poll',
            "hub": self.cfg["virtualhere_hub"],
            "usb_name": self.cfg["usb_name"],
            "usb_vid": self.cfg["usb_vendor_id"],
            "usb_pid": self.cfg["usb_product_id"],
            "poll_seconds": int(self.cfg["renew_interval_seconds"]),
        }

    def authenticate_agent(self, raw_token: str, peer_ip=None):
        digest = token_hash(raw_token)
        with self.connect() as db:
            for row in db.execute("SELECT * FROM agents WHERE token_hash IS NOT NULL"):
                if hmac.compare_digest(row["token_hash"], digest):
                    if peer_ip is not None and row["tailscale_ip"] != peer_ip:
                        raise PermissionError("agent network identity mismatch")
                    return dict(row)
        raise PermissionError("invalid agent token")

    def poll(self, raw_token: str, report: dict, peer_ip=None):
        agent = self.authenticate_agent(raw_token,peer_ip)
        with self.lock, self.connect() as db:
            self.expire_lease(db)
            clean = {
                "vh_status": str(report.get("vh_status", "unknown"))[:40],
                "device_address": str(report.get("device_address", ""))[:200],
                "bluetooth_status": str(report.get("bluetooth_status", "unknown"))[:40],
                "applied_generation": int(report.get("applied_generation", 0) or 0),
                "applied_action": str(report.get("applied_action", ""))[:20],
                "error": str(report.get("error", ""))[:500],
                "agent_version": str(report.get("agent_version", ""))[:64],
            }
            status = "fault" if clean["error"] else clean["vh_status"]
            db.execute(
                "UPDATE agents SET last_seen=?,status=?,last_report=? WHERE id=?",
                (now(), status, json.dumps(clean, ensure_ascii=False), agent["id"]),
            )
            if clean['error']:
                db.execute('UPDATE lease SET release_grace_until=0 WHERE singleton=1 AND target_id=?',(agent['id'],))
            lease = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
            if lease["target_id"] == agent["id"] and clean["applied_generation"] == lease["generation"] and not clean["error"]:
                if lease["state"] == "authorizing" and clean["vh_status"] == "attached" and clean["applied_action"] == "use" and clean["bluetooth_status"] == "ready":
                    db.execute("UPDATE lease SET state='active',updated_at=? WHERE singleton=1", (now(),))
                    self.event(db, "lease_active", agent_id=agent["id"], details={"session_id": lease["session_id"]})
                elif lease["state"] == "releasing" and clean["vh_status"] == "released" and clean["applied_action"] == "release" and clean["bluetooth_status"] == "absent":
                    pending = lease["pending_target_id"]
                    if pending:
                        if self.cfg.get('source_reset_enabled'):
                            self._queue_source_reset(db,pending)
                        else:
                            db.execute(
                            "UPDATE lease SET target_id=?,pending_target_id=NULL,state='authorizing',release_grace_until=0,generation=generation+1,expires_at=?,updated_at=? WHERE singleton=1",
                            (pending, now() + int(self.cfg["lease_seconds"]), now()),
                            )
                        self.event(db, "lease_handoff", agent_id=pending)
                    else:
                        db.execute(
                            "UPDATE lease SET session_id=NULL,target_id=NULL,pending_target_id=NULL,state='idle',release_grace_until=0,expires_at=NULL,updated_at=? WHERE singleton=1",
                            (now(),),
                        )
                        self.event(db, "lease_released", agent_id=agent["id"])
            lease = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
            action = "release"
            if lease["target_id"] == agent["id"] and lease["state"] in ("authorizing", "active"):
                action = "use"
            return {
                "action": action,
                "generation": lease["generation"],
                "session_id": lease["session_id"],
                "lease_expires_at": lease["expires_at"],
                "hub": self.cfg["virtualhere_hub"],
                "usb_name": self.cfg["usb_name"],
            }

    def acquire(self, agent_id: int):
        with self.lock, self.connect() as db:
            self.expire_lease(db)
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise ValueError("目标电脑不存在")
            if not agent["last_seen"] or now() - agent["last_seen"] > int(self.cfg["agent_offline_seconds"]):
                raise ValueError("目标电脑代理离线，不能分配设备")
            lease = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
            expires = now() + int(self.cfg["lease_seconds"])
            if lease["state"] in ('releasing','resetting'):
                raise ValueError("正在等待旧连接释放，请等待确认后重试")
            if not lease["target_id"] or lease["state"] == "idle":
                if self.cfg.get('source_reset_enabled'):
                    self._queue_source_reset(db,agent_id)
                    return dict(db.execute('SELECT * FROM lease WHERE singleton=1').fetchone())
                sid = secrets.token_urlsafe(18)
                db.execute(
                    "UPDATE lease SET session_id=?,target_id=?,pending_target_id=NULL,release_grace_until=0,generation=generation+1,state='authorizing',expires_at=?,updated_at=? WHERE singleton=1",
                    (sid, agent_id, expires, now()),
                )
                self.event(db, "lease_requested", agent_id=agent_id, details={"session_id": sid})
            elif lease["target_id"] == agent_id:
                db.execute("UPDATE lease SET expires_at=?,updated_at=? WHERE singleton=1", (expires, now()))
            else:
                db.execute(
                    "UPDATE lease SET pending_target_id=?,state='releasing',release_grace_until=?,generation=generation+1,expires_at=?,updated_at=? WHERE singleton=1",
                    (agent_id, self.release_deadline(lease), expires, now()),
                )
                self.event(db, "lease_switch_requested", agent_id=agent_id, details={"from_agent_id": lease["target_id"]})
        return self.snapshot()["lease"]

    def renew(self, agent_id: int, session_id: str, generation: int):
        with self.lock, self.connect() as db:
            self.expire_lease(db)
            lease = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
            if lease["target_id"] != agent_id or lease["state"] not in ("authorizing", "active") or lease["session_id"] != session_id or lease["generation"] != generation:
                raise ValueError("当前电脑没有可续期租约")
            db.execute("UPDATE lease SET expires_at=?,updated_at=? WHERE singleton=1", (now() + int(self.cfg["lease_seconds"]), now()))

    def release(self):
        with self.lock, self.connect() as db:
            lease = db.execute("SELECT * FROM lease WHERE singleton=1").fetchone()
            if lease['state']=='resetting':
                db.execute("UPDATE lease SET state='idle',session_id=NULL,target_id=NULL,pending_target_id=NULL,reset_nonce=NULL,expires_at=NULL,generation=generation+1,updated_at=? WHERE singleton=1",(now(),))
                self.event(db,'source_reset_cancelled')
            elif lease["target_id"] and lease["state"] == "releasing":
                # Repeated clicks must not invalidate an in-flight release ACK.
                # Ending sharing also cancels a pending handoff, without changing
                # the already issued release generation.
                if lease["pending_target_id"]:
                    db.execute("UPDATE lease SET pending_target_id=NULL,updated_at=? WHERE singleton=1",(now(),))
                    self.event(db,"lease_switch_cancelled",agent_id=lease["target_id"])
            elif lease["target_id"]:
                db.execute(
                    "UPDATE lease SET pending_target_id=NULL,state='releasing',release_grace_until=?,generation=generation+1,updated_at=? WHERE singleton=1",
                    (self.release_deadline(lease),now()),
                )
                self.event(db, "release_requested", agent_id=lease["target_id"])
            else:
                db.execute("UPDATE lease SET state='idle',pending_target_id=NULL,updated_at=? WHERE singleton=1", (now(),))


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.source_maintenance=False
        self.launch_tokens={}
        self.launch_lock=threading.Lock()
        runtime = (ROOT / cfg["runtime_dir"]).resolve()
        runtime.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        token_file = runtime / "admin_token"
        if token_file.exists():
            self.admin_token = token_file.read_text().strip()
        else:
            self.admin_token = secrets.token_urlsafe(36)
            token_file.write_text(self.admin_token)
            os.chmod(token_file, 0o600)
        key_file=runtime/"enrollment_secret"
        if not key_file.exists():
            fd=os.open(key_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"wb") as stream:
                stream.write(secrets.token_bytes(32))
        self.store = Store(runtime / "bridge.sqlite3", cfg,key_file.read_bytes())
        self.store.source_ready=not cfg.get("usb_proxy_enabled")
        self.store.recover_after_restart()
        self.proxy = UsbProxy(self.store,cfg) if cfg.get("usb_proxy_enabled") else None
        self.management = Management(self,ROOT)
        self.source_reset=Coordinator(self)
        self.watchdog=Watchdog(runtime)

    def repair_script(self):
        digest=self.agent_artifact()
        base=f'http://{self.cfg["agent_bind"]}:{self.cfg["agent_port"]}'
        return (ROOT/"windows"/"repair.ps1").read_text().replace("{{AGENT_SHA256}}",digest).replace("{{CONTROLLER_BASE}}",base)

    def agent_artifact(self):
        data=(ROOT/"windows"/"agent.ps1").read_bytes()
        digest=hashlib.sha256(data).hexdigest()
        release=ROOT/self.cfg["runtime_dir"]/"releases"/digest
        release.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=release/"agent.ps1"
        if not path.exists():
            path.write_bytes(data)
            os.chmod(path,0o600)
        elif hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError("本地发布文件摘要异常")
        return digest

    def status(self):
        snapshot=self.store.snapshot()
        snapshot["management"]=self.management.status(snapshot)
        snapshot["management"]["source_isolation_ready"]=self.source_ready()
        snapshot["management"]["source_isolation_configured"]=bool(self.cfg.get("usb_proxy_enabled"))
        snapshot["management"]["bulk_ready"]=self.bulk_ready()
        snapshot['management']['usb_active_tunnels']=self.proxy.active_tunnels if self.proxy else 0
        snapshot['management']['source_reset_configured']=bool(self.cfg.get('source_reset_enabled'))
        snapshot['management']['source_reset_ready']=self.source_reset.helper_ready
        snapshot['management']['watchdog']=self.watchdog.status()
        return snapshot

    def source_ready(self):
        if (self.cfg.get('source_reset_enabled') and self.source_reset.last_helper_check
                and not self.source_reset.helper_ready):
            return False
        return bool(self.proxy and self.proxy.ready() and self.store.source_ready and not self.source_maintenance)

    def bulk_ready(self):
        if self.cfg.get('bulk_validation_hold'):
            return False
        if not self.source_ready():
            return False
        try:
            since=int(json.loads(MARKER.read_text())["installed_at"])
        except (OSError,ValueError,KeyError):
            return False
        with self.store.connect() as db:
            if db.execute('SELECT reset_error FROM lease WHERE singleton=1').fetchone()[0]:
                return False
            return bool(db.execute("SELECT 1 FROM events a JOIN events r ON r.agent_id=a.agent_id AND r.id>a.id WHERE a.event='lease_active' AND r.event='lease_released' AND a.created_at>=? AND a.id>COALESCE((SELECT MAX(id) FROM events WHERE event IN ('fault_release_requested','source_reset_failed')),0) LIMIT 1",(since,)).fetchone())

    def create_launch_token(self):
        token=secrets.token_urlsafe(32)
        with self.launch_lock:
            self.launch_tokens={k:v for k,v in self.launch_tokens.items() if v>now()}
            self.launch_tokens[token]=now()+60
        return token

    def consume_launch_token(self,token):
        with self.launch_lock:
            expiry=self.launch_tokens.pop(token,0)
        return expiry>now()

    def onboarding_package(self, display_name: str, desktop_id: str | None):
        if not credentials_available():
            raise ValueError("Tailscale OAuth 尚未配置到 macOS 钥匙串")
        enrollment = self.store.new_enrollment(display_name, desktop_id)
        safe_host = "wuying-" + "".join(c.lower() if c.isalnum() else "-" for c in enrollment["desktop_id"])[-40:].strip("-")
        safe_host = safe_host or f'wuying-{enrollment["agent_id"]}'
        auth = create_one_time_auth_key(f'wuying-{enrollment["agent_id"]}')
        auth_key = auth.get("key")
        if not auth_key:
            raise RuntimeError("Tailscale 未返回一次性授权密钥")
        agent_hash = self.agent_artifact()
        bootstrap = (ROOT / "windows" / "bootstrap.ps1").read_text()
        base = f'http://{self.cfg["agent_bind"]}:{self.cfg["agent_port"]}'
        def ps(value):
            return "'" + str(value).replace("'", "''") + "'"
        command = (
            "# One-time bootstrap: join Tailscale before contacting the Mac.\n"
            "& {\n" + bootstrap + "\n} "
            f"-ControllerBase {ps(base)} -TailscaleAuthKey {ps(auth_key)} "
            f"-EnrollmentToken {ps(enrollment['enrollment_token'])} -ComputerName {ps(safe_host)} -AgentSha256 {ps(agent_hash)}"
        )
        return {"command": command, "agent_id": enrollment["agent_id"], "desktop_id": enrollment["desktop_id"], "expires_at": enrollment["expires_at"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "BluetoothBridgeController/0.1"

    @property
    def app(self):
        return self.server.app

    @property
    def role(self):
        return self.server.role

    def log_message(self, fmt, *args):
        path = urlparse(self.path).path
        status = args[1] if len(args) > 1 else "-"
        if path in ("/api/status","/agent/poll") and str(status)=="200":
            return
        print(f"{self.address_string()} {self.command} {path} {status}")

    def json_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 1024 * 1024:
            raise ValueError("request too large")
        raw = self.rfile.read(size) if size else b"{}"
        return json.loads(raw.decode("utf-8"))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc, status=400):
        self.send_json({"ok": False, "error": str(exc)}, status)

    def admin_ok(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        supplied = self.headers.get("X-Admin-Token", "")
        if "bridge_admin" in cookie:
            supplied = cookie["bridge_admin"].value
        return hmac.compare_digest(supplied, self.app.admin_token)

    def valid_origin(self):
        origin = self.headers.get("Origin")
        expected = f'http://{self.app.cfg["ui_bind"]}:{self.app.cfg["ui_port"]}'
        return origin == expected

    def bearer(self):
        value = self.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if self.role == "ui":
                query = parse_qs(parsed.query)
                if parsed.path == "/" and query.get("launch") and self.app.consume_launch_token(query["launch"][0]):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f"bridge_admin={self.app.admin_token}; HttpOnly; SameSite=Strict; Path=/")
                    self.end_headers()
                    return
                if not self.admin_ok():
                    self.send_error_json("unauthorized", 401)
                    return
                if parsed.path == "/api/status":
                    self.send_json({"ok": True, **self.app.status()})
                    return
                if parsed.path=="/api/diagnostics":
                    snapshot=self.app.status()
                    for agent in snapshot["agents"]:
                        agent.pop("machine_id",None)
                    self.send_json({"ok":True,**snapshot})
                    return
                self.serve_static(parsed.path)
                return
            if self.role == "agent" and parsed.path == "/agent/agent.ps1":
                self.serve_file(ROOT / "windows" / "agent.ps1", "text/plain; charset=utf-8")
                return
            if self.role == 'agent' and parsed.path == '/agent/watchdog/install.ps1':
                self.serve_file(ROOT/'windows'/'install-watchdog.ps1', 'text/plain; charset=utf-8')
                return
            if self.role=="agent" and parsed.path=="/agent/health":
                healthy=now()-self.app.management.last_health_at<=20
                if self.app.cfg.get("usb_proxy_enabled"):
                    healthy=healthy and self.app.source_ready()
                self.send_json({"ok":healthy,"service":"bluetooth-bridge","checked_at":now()},200 if healthy else 503)
                return
            if self.role == "agent" and parsed.path == "/agent/bootstrap.ps1":
                self.serve_file(ROOT / "windows" / "bootstrap.ps1", "text/plain; charset=utf-8")
                return
            if self.role=="agent" and parsed.path.startswith("/agent/releases/"):
                import re
                match=re.fullmatch(r"/agent/releases/([a-f0-9]{64})/agent\.ps1",parsed.path)
                if not match:
                    raise ValueError("invalid release path")
                self.serve_file(ROOT/self.app.cfg["runtime_dir"]/"releases"/match[1]/"agent.ps1","text/plain; charset=utf-8")
                return
            self.send_error_json("not found", 404)
        except Exception as exc:
            self.send_error_json(exc, 500)

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if self.role == "ui":
                if not self.admin_ok():
                    self.send_error_json("unauthorized", 401)
                    return
                if not self.valid_origin():
                    self.send_error_json("invalid origin", 403)
                    return
                body = self.json_body()
                if path=="/api/launch-token":
                    self.send_json({"ok":True,"launch_token":self.app.create_launch_token()})
                elif path == '/api/watchdog/enrollment':
                    self.send_json({'ok':True, **self.app.watchdog.create(str(body.get('peer',''))),
                                    'installer_sha256':hashlib.sha256((ROOT/'windows'/'install-watchdog.ps1').read_bytes()).hexdigest()})
                elif path == "/api/enrollment":
                    self.send_json({"ok": True, **self.app.store.new_enrollment(body.get("display_name", ""), body.get("desktop_id"))})
                elif path == "/api/onboarding/package":
                    self.send_json({"ok": True, **self.app.onboarding_package(body.get("display_name", ""), body.get("desktop_id"))})
                elif path == "/api/settings/cloud":
                    access_id=str(body.get("access_id", "")).strip()
                    secret=str(body.get("access_secret", "")).strip()
                    if not access_id or not secret:
                        raise ValueError("请填写受限 RAM 凭据")
                    client=CloudClient(self.app.management.cloud.region,credentials=(access_id,secret))
                    client.call("DescribeDesktops",MaxResults=1)
                    write_secret("aliyun.credentials",json.dumps({"access_id":access_id,"access_secret":secret}))
                    self.app.management.cloud_ready=True
                    self.send_json({"ok":True})
                elif path == "/api/settings/telegram/discover":
                    chat_id=discover_telegram_chat(str(body.get("token", "")).strip(),str(body.get("pairing_code", "")).strip())
                    self.send_json({"ok":True,"chat_id":chat_id})
                elif path == "/api/settings/telegram":
                    configure_telegram(str(body.get("token", "")).strip(),str(body.get("chat_id", "")).strip())
                    self.app.management.telegram_ready=True
                    self.send_json({"ok":True})
                elif path == "/api/settings/telegram/test":
                    notify_telegram("Mac 蓝牙桥接：Telegram 私聊通知连接测试成功。")
                    self.send_json({"ok":True})
                elif path == "/api/inventory/sync":
                    self.send_json({"ok":True,"count":self.app.management.sync()})
                elif path == "/api/jobs":
                    if body.get("operation")=="install" and not self.app.bulk_ready():
                        raise ValueError("批量接入前需启用源端隔离，并在 Yorke 完成一次新版接管和释放复测")
                    ids=self.app.management.create_batch(body.get("desktop_ids"),body.get("operation"),body.get("batch_id",""))
                    self.send_json({"ok":True,"job_ids":ids})
                elif path == "/api/jobs/reconcile":
                    self.app.management.reconcile(str(body.get("job_id","")))
                    self.send_json({"ok":True})
                elif path=="/api/incidents/ack":
                    key=str(body.get("incident_key",""))
                    with self.app.store.connect() as db:
                        db.execute("UPDATE incidents SET acknowledged_at=?,resolved_at=CASE WHEN incident_key LIKE 'job:%' THEN ? ELSE resolved_at END WHERE incident_key=?",(now(),now(),key))
                    self.send_json({"ok":True})
                elif path in ("/api/source/prepare-rollback","/api/source/maintenance"):
                    with self.app.store.lock:
                        lease=self.app.store.snapshot()["lease"]
                        if lease["state"]!="idle" or lease["target_id"]:
                            raise ValueError("必须先确认释放，才能维护源端配置")
                        self.app.source_maintenance=True
                    if self.app.proxy:
                        self.app.proxy.close()
                        self.app.proxy=None
                    self.send_json({"ok":True})
                elif path == "/api/lease/acquire":
                    with self.app.store.lock:
                        if self.app.source_maintenance:
                            raise ValueError("源端正在维护，请完成配置并重启控制器")
                        if self.app.cfg.get("usb_proxy_enabled") and not self.app.source_ready():
                            raise ValueError("源端 USB 服务尚未就绪")
                        lease=self.app.store.acquire(int(body["agent_id"]))
                    self.send_json({"ok": True, "lease":lease})
                elif path == "/api/lease/renew":
                    self.app.store.renew(int(body["agent_id"]), body["session_id"], int(body["generation"]))
                    self.send_json({"ok": True})
                elif path == "/api/lease/release":
                    self.app.store.release()
                    self.send_json({"ok": True})
                else:
                    self.send_error_json("not found", 404)
                return
            if self.role == "agent":
                body = self.json_body()
                if path == '/agent/watchdog/enroll':
                    self.send_json({'ok':True, **self.app.watchdog.enroll(self.bearer(),self.client_address[0])})
                elif path == '/agent/watchdog/report':
                    self.app.watchdog.report(self.bearer(),self.client_address[0],body)
                    self.send_json({'ok':True})
                elif path == "/agent/enroll":
                    peer=self.client_address[0]
                    if ipaddress.ip_address(peer) not in ipaddress.ip_network("100.64.0.0/10") or body.get("tailscale_ip") != peer:
                        raise PermissionError("enrollment network identity mismatch")
                    result = self.app.store.enroll(
                        body.get("enrollment_token", ""), body.get("machine_id", ""), body.get("hostname", ""), body.get("tailscale_ip", "")
                    )
                    self.send_json({"ok": True, **result})
                elif path == "/agent/poll":
                    self.send_json({"ok": True, **self.app.store.poll(self.bearer(), body,self.client_address[0])})
                else:
                    self.send_error_json("not found", 404)
                return
        except PermissionError as exc:
            self.send_error_json(exc, 401)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_error_json(exc, 400)
        except Exception as exc:
            self.send_error_json(exc, 500)

    def serve_static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        base = (ROOT / "static").resolve()
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            self.send_error_json("invalid path", 400)
            return
        self.serve_file(target, mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def serve_file(self, path: Path, content_type: str):
        if not path.is_file():
            self.send_error_json("not found", 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app, role):
        self.app = app
        self.role = role
        super().__init__(address, Handler)


def load_config(path: Path):
    cfg = json.loads(path.read_text())
    required = ["ui_bind", "ui_port", "agent_bind", "agent_port", "virtualhere_hub", "usb_vendor_id", "usb_product_id", "usb_name", "lease_seconds", "renew_interval_seconds", "agent_offline_seconds", "runtime_dir"]
    missing = [key for key in required if key not in cfg]
    if missing:
        raise SystemExit(f"Missing config keys: {', '.join(missing)}")
    if cfg["ui_bind"] not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("ui_bind must be loopback only")
    if cfg["agent_bind"] in ("0.0.0.0", "::"):
        raise SystemExit("agent_bind must be the explicit Tailscale IP, not a wildcard")
    try:
        address=ipaddress.ip_address(cfg["agent_bind"])
        if address.version!=4 or address not in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError()
    except ValueError:
        raise SystemExit("agent_bind 必须填写本机实际 Tailscale IPv4 地址，不能保留示例占位符")
    cfg.setdefault("cloud_region","ap-southeast-1")
    cfg.setdefault("usb_proxy_enabled",False)
    return cfg


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    args = parser.parse_args()
    cfg = load_config(Path(args.config).resolve())
    runtime=(ROOT/cfg["runtime_dir"]).resolve()
    runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
    controller_lock=open(runtime/"controller.lock","a")
    try:
        fcntl.flock(controller_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Bluetooth bridge controller is already running")
    app = App(cfg)
    ui = Server((cfg["ui_bind"], int(cfg["ui_port"])), app, "ui")
    agent = Server((cfg["agent_bind"], int(cfg["agent_port"])), app, "agent")
    if app.proxy:
        app.proxy.start()
    app.management.start()
    threading.Thread(target=agent.serve_forever, name="agent-api", daemon=True).start()
    print(f'Bluetooth bridge UI: http://{cfg["ui_bind"]}:{cfg["ui_port"]}/ (run launch.py to authenticate)')
    print(f'Agent API: http://{cfg["agent_bind"]}:{cfg["agent_port"]}')
    try:
        ui.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.management.stop.set()
        if app.proxy:
            app.proxy.close()
        ui.server_close()
        agent.shutdown()
        agent.server_close()


if __name__ == "__main__":
    main()
