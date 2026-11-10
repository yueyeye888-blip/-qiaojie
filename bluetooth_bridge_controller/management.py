"""Persistent, serial deployment queue and deduplicated incident delivery."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from pathlib import Path

from integrations import CloudClient, CloudError, notify_telegram, read_secret
from source_probe import inspect_usb


TERMINAL = {"succeeded", "failed", "skipped", "unknown"}
PHASES = {"queued", "waking", "preflight", "running", "waiting_heartbeat"}
SLEEPING = {"Stopped", "Hibernated", "Hibernating", "Stopping"}


def health_for(agent, lease, cloud_status="Unknown"):
    report = agent.get("last_report") or {}
    if isinstance(report, str):
        report = json.loads(report)
    active = lease.get("target_id") == agent["id"]
    if not agent.get("machine_id"):
        return {"code": "pending", "level": "info", "message": "等待接入", "action": "执行接入任务"}
    if not agent.get("online"):
        if cloud_status in SLEEPING and not active:
            return {"code": "sleeping", "level": "info", "message": "电脑已休眠或关机", "action": "使用时唤醒"}
        return {"code": "agent_offline", "level": "critical" if active else "warning",
                "message": "代理心跳中断" if cloud_status == "Running" or active else "代理离线，云状态待确认",
                "action": "读取诊断；确认云状态后修复代理"}
    if report.get("error"):
        return {"code": "agent_fault", "level": "critical" if active else "warning",
                "message": "代理回报故障", "action": "先结束共享，再运行诊断"}
    if active and lease.get("state") == "active" and report.get("bluetooth_status") != "ready":
        return {"code": "usb_unavailable", "level": "critical", "message": "共享中的蓝牙设备异常",
                "action": "结束共享并检查适配器连接"}
    if active and lease.get("state") == "releasing":
        return {"code": "releasing", "level": "info", "message": "等待释放确认", "action": "确认释放后再切换"}
    return {"code": "healthy", "level": "info", "message": "共享正常" if active else "在线待用", "action": ""}


class Management:
    def __init__(self, app, root: Path, cloud=None, sender=None):
        self.app, self.store, self.root = app, app.store, root
        self.cloud = cloud or CloudClient(app.cfg.get("cloud_region", "ap-southeast-1"))
        self.sender = sender or notify_telegram
        self.stop = threading.Event()
        self.last_sync_at = 0
        self.last_health_at=int(time.time())
        self.source_hardware={"count":None,"checked_at":0}
        self.cloud_ready = self._has("aliyun.credentials")
        self.telegram_ready = self._has("telegram.credentials")
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cloud_inventory (
                    desktop_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    region TEXT NOT NULL, cloud_status TEXT NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, desktop_id TEXT NOT NULL,
                    operation TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                    invoke_id TEXT, error_code TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    deadline INTEGER NOT NULL, expected_hash TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(desktop_id)
                    WHERE state IN ('queued','waking','preflight','running','waiting_heartbeat');
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_key TEXT PRIMARY KEY, agent_id INTEGER, level TEXT NOT NULL,
                    code TEXT NOT NULL, message TEXT NOT NULL,
                    opened_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, resolved_at INTEGER,
                    acknowledged_at INTEGER, notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    due_at INTEGER NOT NULL, error_code TEXT NOT NULL DEFAULT ''
                );
            """)
            if 'notified' not in {r[1] for r in db.execute('PRAGMA table_info(incidents)')}:
                db.execute('ALTER TABLE incidents ADD COLUMN notified INTEGER NOT NULL DEFAULT 0')
            # Lost submission responses are never blindly re-run on restart.
            db.execute("UPDATE jobs SET state='unknown',error_code='ControllerRestartDuringSubmission' WHERE state='submitting'")

    @staticmethod
    def _has(*names):
        try:
            return all(read_secret(n) for n in names)
        except Exception:
            return False

    def sync(self):
        if not self.cloud_ready:
            raise ValueError("请先配置阿里云受限 RAM 凭据")
        rows = self.cloud.desktops()
        current = int(time.time())
        with self.store.lock, self.store.connect() as db:
            for row in rows:
                desktop_id = row.get("DesktopId", "")
                if not re.fullmatch(r"ecd-[A-Za-z0-9]+", desktop_id):
                    continue
                db.execute("INSERT INTO cloud_inventory VALUES(?,?,?,?,?) ON CONFLICT(desktop_id) DO UPDATE SET display_name=excluded.display_name,cloud_status=excluded.cloud_status,updated_at=excluded.updated_at",
                           (desktop_id, str(row.get("DesktopName") or desktop_id)[:80], self.cloud.region,
                            str(row.get("DesktopStatus", "Unknown")), current))
            # Preserve history but distinguish resources absent from the latest complete inventory.
            db.execute("UPDATE cloud_inventory SET cloud_status='NotReturned' WHERE updated_at<?", (current,))
        self.last_sync_at=current
        return len(rows)

    def create_batch(self, ids, operation, batch_id):
        if operation not in {"install", "diagnose", "repair"}:
            raise ValueError("不支持的任务类型")
        maximum=min(50,int(self.app.cfg.get("max_batch_size",3)))
        if not isinstance(ids, list) or not 1 <= len(ids) <= maximum or len(set(ids)) != len(ids):
            raise ValueError(f"每批选择 1 到 {maximum} 台不同的云电脑")
        if not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", batch_id):
            raise ValueError("无效批次标识")
        if not self.cloud_ready:
            raise ValueError("阿里云接口尚未配置")
        current = int(time.time())
        with self.store.lock, self.store.connect() as db:
            existing = db.execute("SELECT id,desktop_id,operation FROM jobs WHERE batch_id=?", (batch_id,)).fetchall()
            if existing:
                if {x["desktop_id"] for x in existing} != set(ids) or any(x["operation"] != operation for x in existing):
                    raise ValueError("批次标识已用于另一组任务")
                return [x["id"] for x in existing]
            lease = db.execute("SELECT target_id FROM lease WHERE singleton=1").fetchone()
            for desktop_id in ids:
                row = db.execute("SELECT * FROM cloud_inventory WHERE desktop_id=?", (desktop_id,)).fetchone()
                if not row or row["region"] != self.cloud.region or row["cloud_status"] == "NotReturned" or current-row["updated_at"] > 600:
                    raise ValueError("请先刷新云电脑列表，再选择目标")
                agent = db.execute("SELECT id,machine_id FROM agents WHERE desktop_id=?", (desktop_id,)).fetchone()
                if agent and agent["id"] == lease["target_id"]:
                    raise ValueError("目标正在共享蓝牙，请先结束共享")
                if operation == "repair" and (not agent or not agent["machine_id"]):
                    raise ValueError("修复仅适用于已登记电脑，请先接入")
                if db.execute("SELECT 1 FROM jobs WHERE desktop_id=? AND state NOT IN ('succeeded','failed','skipped')", (desktop_id,)).fetchone():
                    raise ValueError("目标已有任务或结果待确认，不能重复提交")
            result = []
            for desktop_id in ids:
                job_id = secrets.token_hex(12)
                db.execute("INSERT INTO jobs(id,batch_id,desktop_id,operation,state,created_at,updated_at,deadline) VALUES(?,?,?,?,?,?,?,?)",
                           (job_id,batch_id,desktop_id,operation,"queued",current,current,current+1800))
                result.append(job_id)
        return result

    def update_job(self, job_id, **fields):
        allowed = {"state", "stage", "invoke_id", "error_code", "expected_hash", "deadline"}
        if not fields or set(fields)-allowed:
            raise ValueError("invalid job fields")
        fields["updated_at"] = int(time.time())
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(k+"=?" for k in fields) + " WHERE id=?", (*fields.values(),job_id))

    def _submit(self, job, script, state, stage, expected_hash=""):
        # Commit before the network mutation. Crash/timeout requires result reconciliation.
        self.update_job(job["id"], state="submitting",stage=stage,expected_hash=expected_hash)
        try:
            invoke_id = self.cloud.run(job["desktop_id"], "# BRIDGE-JOB:" + job["id"] + "\n" + script)
        except CloudError as exc:
            if (stage=="preflight" and exc.code=="CloudAssistant.NotReady" and
                not exc.uncertain and int(time.time()) < job["deadline"]-1500):
                # The cloud explicitly rejected this read-only probe; no command
                # exists to reconcile. Allow five minutes from job start for boot.
                self.update_job(job["id"],state="waking",stage="cloudassistant_wait",error_code="")
            else:
                self.update_job(job["id"], state="unknown" if exc.uncertain else "failed",error_code=exc.code)
            return
        except Exception:
            self.update_job(job["id"],state="unknown",error_code="SubmissionResultUnknown")
            return
        self.update_job(job["id"],state=state,stage=stage,invoke_id=invoke_id)

    def _result(self, job):
        result = self.cloud.invocation(job["invoke_id"])
        for invocation in result.get("Invocations", []):
            if invocation.get("InvokeId") != job["invoke_id"]:
                continue
            for item in invocation.get("InvokeDesktops", []):
                if item.get("DesktopId") == job["desktop_id"]:
                    return item
        return None

    def tick_jobs(self):
        if not self.cloud_ready:
            return
        with self.store.connect() as db:
            # One computer at a time keeps partial failures contained.
            row = db.execute("SELECT * FROM jobs WHERE state IN ('queued','waking','preflight','running','waiting_heartbeat') ORDER BY created_at,id LIMIT 1").fetchone()
        if not row:
            return
        job = dict(row)
        try:
            if job['operation']=='install' and job['state']=='queued' and not self.app.bulk_ready():
                self.update_job(job['id'],state='failed',error_code='SourceValidationRequired')
                return
            if job["state"]=="queued" and not job["stage"]:
                job["deadline"]=int(time.time())+1800
                self.update_job(job["id"],stage="preparing",deadline=job["deadline"])
            if int(time.time()) > job["deadline"]:
                self.update_job(job["id"],state="failed",error_code="JobDeadlineExceeded")
                return
            if job["state"] in {"queued", "waking"}:
                # Fetch current state, never infer hibernation from stale heartbeats.
                self.sync()
                with self.store.connect() as db:
                    desktop = dict(db.execute("SELECT * FROM cloud_inventory WHERE desktop_id=?",(job["desktop_id"],)).fetchone())
                    enrolled = db.execute("SELECT machine_id FROM agents WHERE desktop_id=?",(job["desktop_id"],)).fetchone()
                status = desktop["cloud_status"]
                if job["operation"] == "install" and enrolled and enrolled["machine_id"]:
                    self.update_job(job["id"],state="skipped",stage="already_registered")
                    return
                if status != "Running":
                    if job["state"] == "waking":
                        return
                    action = {"Hibernated": "WakeupDesktops", "Stopped": "StartDesktops"}.get(status)
                    if not action:
                        self.update_job(job["id"],state="failed",error_code="DesktopState."+status)
                        return
                    self.update_job(job["id"],state="waking",stage="wake_requested")
                    self.cloud.call(action,DesktopId=[job["desktop_id"]])
                    return
                self._submit(job, (self.root/"windows"/"diagnose.ps1").read_text(), "preflight", "preflight")
                return
            if job["state"] == "waiting_heartbeat":
                agents = self.store.snapshot()["agents"]
                agent = next((a for a in agents if a["desktop_id"]==job["desktop_id"]),None)
                if agent and agent["online"] and not agent["last_report"].get("error") and agent["last_report"].get("agent_version")==job["expected_hash"]:
                    self.update_job(job["id"],state="succeeded",stage="heartbeat_confirmed")
                return
            item = self._result(job)
            if not item or item.get("InvocationStatus") in {"Pending","Running","Stopping"}:
                return
            if item.get("InvocationStatus") != "Success" or item.get("ExitCode") != 0:
                self.update_job(job["id"],state="failed",error_code=str(item.get("ErrorCode") or "CommandFailed")[:100])
                return
            if job["state"] == "running":
                self.update_job(job["id"],state="waiting_heartbeat",stage="command_finished")
                return
            marker = "BRIDGE-DIAG:"
            lines = [line[len(marker):] for line in str(item.get("Output","")).splitlines() if line.startswith(marker)]
            if not lines:
                self.update_job(job["id"],state="failed",error_code="DiagnosticOutputMissing")
                return
            probe = json.loads(lines[-1])
            if job["operation"] == "diagnose":
                # Keep only allowlisted facts, never raw command output or agent.json.
                self.update_job(job["id"],state="succeeded",stage="diagnostic_complete",error_code=str(probe.get("code", ""))[:100])
                return
            if not probe.get("supported"):
                self.update_job(job["id"],state="failed",error_code="UnsupportedWindowsEnvironment")
                return
            with self.store.connect() as db:
                desktop=dict(db.execute("SELECT * FROM cloud_inventory WHERE desktop_id=?",(job["desktop_id"],)).fetchone())
                registered=db.execute("SELECT machine_id FROM agents WHERE desktop_id=?",(job["desktop_id"],)).fetchone()
            expected = hashlib.sha256((self.root/"windows"/"agent.ps1").read_bytes()).hexdigest()
            if job["operation"] == "install":
                if probe.get("configured"):
                    self.update_job(job["id"],state="failed",error_code="ExistingIdentityRequiresReview")
                    return
                package = self.app.onboarding_package(desktop["display_name"],job["desktop_id"])
                script = package["command"]
            else:
                expected_controller=f'http://{self.app.cfg["agent_bind"]}:{self.app.cfg["agent_port"]}/agent/poll'
                if not registered or probe.get("machine_hash")!=registered["machine_id"] or probe.get("controller")!=expected_controller:
                    self.update_job(job["id"],state="failed",error_code="RepairIdentityMismatch")
                    return
                script = self.app.repair_script()
            self._submit(job, script,"running",job["operation"],expected)
        except CloudError as exc:
            if exc.uncertain:
                self.update_job(job["id"],state="unknown",error_code=exc.code)
            else:
                # API permission failures need intervention, not a retry storm.
                self.update_job(job["id"],state="failed",error_code=exc.code)
        except Exception:
            self.update_job(job["id"],state="failed",error_code="ManagementStepFailed")

    def evaluate_health(self, snapshot):
        current = int(time.time())
        with self.store.lock, self.store.connect() as db:
            inventory = {r["desktop_id"]:dict(r) for r in db.execute("SELECT * FROM cloud_inventory")}
            observed = set()
            for agent in snapshot["agents"]:
                cloud_row=inventory.get(agent["desktop_id"],{})
                cloud_status=cloud_row.get("cloud_status","Unknown") if current-cloud_row.get("updated_at",0)<120 else "Unknown"
                health=health_for(agent,snapshot["lease"],cloud_status)
                if health["level"]=="info":
                    continue
                if health["code"]=="agent_offline" and snapshot["lease"]["target_id"]!=agent["id"] and current-(agent.get("last_seen") or 0)<90:
                    continue
                key=f"agent:{agent['id']}:{health['code']}"
                observed.add(key)
                old=db.execute("SELECT * FROM incidents WHERE incident_key=?",(key,)).fetchone()
                new=not old or old["resolved_at"] is not None
                db.execute("INSERT INTO incidents(incident_key,agent_id,level,code,message,opened_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(incident_key) DO UPDATE SET level=excluded.level,message=excluded.message,updated_at=excluded.updated_at,resolved_at=NULL,opened_at=CASE WHEN incidents.resolved_at IS NOT NULL THEN excluded.opened_at ELSE incidents.opened_at END",
                           (key,agent["id"],health["level"],health["code"],health["message"],current,current))
                if new:
                    db.execute("UPDATE incidents SET notified=0,acknowledged_at=NULL WHERE incident_key=?",(key,))
                if new and self.telegram_ready and (cloud_status=="Running" or snapshot["lease"]["target_id"]==agent["id"]):
                    message=f"蓝牙桥接告警\n电脑：{agent['display_name'][:80]}\n代码：{health['code']}\n建议：{health['action']}"
                    db.execute("INSERT INTO notification_outbox(message,due_at) VALUES(?,?)",(message,current))
                    db.execute("UPDATE incidents SET notified=1 WHERE incident_key=?",(key,))
                if snapshot["lease"]["target_id"]==agent["id"] and snapshot["lease"]["state"] in {"active","authorizing"}:
                    # Stop authorizing on faults; no automatic reconnect to an old session.
                    db.execute("UPDATE lease SET state='releasing',pending_target_id=NULL,release_grace_until=0,generation=generation+1,updated_at=? WHERE singleton=1",(current,))
                    self.store.event(db,"fault_release_requested",agent_id=agent["id"],level="warning",details={"code":health["code"]})
            for old in db.execute("SELECT * FROM incidents WHERE resolved_at IS NULL AND incident_key LIKE 'agent:%'").fetchall():
                if old["incident_key"] not in observed:
                    db.execute("UPDATE incidents SET resolved_at=?,updated_at=? WHERE incident_key=?",(current,current,old["incident_key"]))
                    if self.telegram_ready and old["notified"]:
                        db.execute("INSERT INTO notification_outbox(message,due_at) VALUES(?,?)",(f"蓝牙桥接恢复\n电脑编号：{old['agent_id']}\n已恢复：{old['code']}",current))
            for job in db.execute("SELECT * FROM jobs WHERE state IN ('failed','unknown')").fetchall():
                key="job:"+job["id"]
                if not db.execute("SELECT 1 FROM incidents WHERE incident_key=?",(key,)).fetchone():
                    message=f"部署任务异常：{job['desktop_id']} · {job['error_code']}"
                    db.execute("INSERT INTO incidents(incident_key,level,code,message,opened_at,updated_at) VALUES(?,?,?,?,?,?)",
                               (key,"warning",job["error_code"],message,current,current))
                    if self.telegram_ready:
                        db.execute("INSERT INTO notification_outbox(message,due_at) VALUES(?,?)",(message,current))
            if self.app.cfg.get("usb_proxy_enabled") and not getattr(self.app,"source_maintenance",False):
                proxy=getattr(self.app,"proxy",None)
                available=bool(proxy and proxy.ready())
                count=self.source_hardware["count"]
                problem=None
                if not available:
                    problem="源端 USB 服务不可达"
                elif self.source_hardware["checked_at"] and count!=1:
                    problem="指定蓝牙适配器未插入" if count==0 else "需要确认唯一蓝牙适配器"
                old=db.execute("SELECT * FROM incidents WHERE incident_key='source:usb'").fetchone()
                if problem:
                    new=not old or old["resolved_at"] is not None
                    db.execute("INSERT INTO incidents(incident_key,level,code,message,opened_at,updated_at) VALUES('source:usb','critical','SourceUnavailable',?,?,?) ON CONFLICT(incident_key) DO UPDATE SET message=excluded.message,updated_at=excluded.updated_at,resolved_at=NULL",(problem,current,current))
                    if new and self.telegram_ready:
                        db.execute("INSERT INTO notification_outbox(message,due_at) VALUES(?,?)",("蓝牙桥接告警\n"+problem+"\n请检查 Mac 控制台；当前设备不会分配给其他电脑。",current))
                    db.execute("UPDATE lease SET state='releasing',pending_target_id=NULL,release_grace_until=0,generation=generation+1,updated_at=? WHERE singleton=1 AND target_id IS NOT NULL AND state IN ('active','authorizing')",(current,))
                elif old and old["resolved_at"] is None:
                    db.execute("UPDATE incidents SET resolved_at=?,updated_at=? WHERE incident_key='source:usb'",(current,current))
                    if self.telegram_ready:
                        db.execute("INSERT INTO notification_outbox(message,due_at) VALUES(?,?)",("蓝牙桥接恢复\nMac 源端 USB 服务及唯一适配器已就绪。",current))
            reset_error=snapshot['lease'].get('reset_error','')
            resetter=getattr(self.app,'source_reset',None)
            if (not reset_error and self.app.cfg.get('source_reset_enabled') and resetter
                    and resetter.last_helper_check and not resetter.helper_ready
                    and not getattr(self.app,'source_maintenance',False)):
                reset_error='ResetHelperUnavailable'
            old=db.execute("SELECT * FROM incidents WHERE incident_key='source:reset'").fetchone()
            if reset_error:
                new=not old or old['resolved_at'] is not None
                db.execute("INSERT INTO incidents(incident_key,level,code,message,opened_at,updated_at) VALUES('source:reset','critical','SourceResetFailed',?,?,?) ON CONFLICT(incident_key) DO UPDATE SET message=excluded.message,updated_at=excluded.updated_at,resolved_at=NULL",('USB 自动复位失败：'+reset_error,current,current))
                if new and self.telegram_ready:
                    db.execute('INSERT INTO notification_outbox(message,due_at) VALUES(?,?)',('蓝牙桥接告警\nUSB 自动复位失败，已停止分配。\n请打开 Mac 管理台查看错误；不会自动重复重启。',current))
            elif old and old['resolved_at'] is None:
                db.execute("UPDATE incidents SET resolved_at=?,updated_at=? WHERE incident_key='source:reset'",(current,current))
                if self.telegram_ready:
                    db.execute('INSERT INTO notification_outbox(message,due_at) VALUES(?,?)',('蓝牙桥接恢复\nUSB 自动复位已确认成功。',current))
            watchdog=getattr(self.app,'watchdog',None)
            monitor=watchdog.status() if watchdog else {}
            if monitor.get('peer'):
                missing=current-max(monitor.get('last_seen',0),monitor.get('enrolled_at',0))>90
                old=db.execute("SELECT * FROM incidents WHERE incident_key='monitor:offline'").fetchone()
                if missing:
                    fresh=not old or old['resolved_at'] is not None
                    db.execute("INSERT INTO incidents(incident_key,level,code,message,opened_at,updated_at) VALUES('monitor:offline','warning','WatchdogOffline','独立监测服务器已失联，请检查其电源、Tailscale 和监测任务',?,?) ON CONFLICT(incident_key) DO UPDATE SET updated_at=excluded.updated_at,resolved_at=NULL",(current,current))
                    if fresh and self.telegram_ready:
                        db.execute('INSERT INTO notification_outbox(message,due_at) VALUES(?,?)',('蓝牙桥接告警\n独立监测服务器已失联，Mac 离线提醒暂时不可用。\n请检查监测服务器的 Tailscale 和计划任务。',current))
                elif old and old['resolved_at'] is None:
                    db.execute("UPDATE incidents SET resolved_at=?,updated_at=? WHERE incident_key='monitor:offline'",(current,current))
                    if self.telegram_ready:
                        db.execute('INSERT INTO notification_outbox(message,due_at) VALUES(?,?)',('蓝牙桥接恢复\n独立监测服务器已恢复心跳。',current))
        self.last_health_at=current

    def probe_source(self):
        count=inspect_usb(self.app.cfg["usb_vendor_id"],self.app.cfg["usb_product_id"])
        self.source_hardware={"count":count,"checked_at":int(time.time())}
        if self.app.cfg.get("usb_proxy_enabled"):
            self.store.source_ready=count==1

    def reconcile(self, job_id):
        with self.store.connect() as db:
            row=db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not row or row["state"]!="unknown":
            raise ValueError("仅核对结果未知的任务")
        job=dict(row)
        if job["stage"]=="wake_requested":
            self.sync()
            with self.store.connect() as db:
                status=db.execute("SELECT cloud_status FROM cloud_inventory WHERE desktop_id=?",(job["desktop_id"],)).fetchone()[0]
            if status=="Running":
                self.update_job(job_id,state="queued",error_code="")
                return
            raise ValueError("云电脑尚未运行，请先在阿里云确认唤醒结果")
        token=None
        matches=[]
        for _ in range(10):
            result=self.cloud.call("DescribeInvocations",DesktopIds=[job["desktop_id"]],ContentEncoding="PlainText",IncludeInvokeDesktops=True,MaxResults=50,NextToken=token)
            for invocation in result.get("Invocations",[]):
                if str(invocation.get("CommandContent","")).replace('\r\n','\n').startswith("# BRIDGE-JOB:"+job_id+"\n"):
                    matches.append(invocation["InvokeId"])
            token=result.get("NextToken")
            if not token:
                break
        if len(set(matches))!=1:
            raise ValueError("无法唯一确认远程任务，请按执行编号检查；不会自动重复下发")
        self.update_job(job_id,state="preflight" if job["stage"]=="preflight" else "running",invoke_id=matches[0],error_code="")

    def deliver_one(self):
        if not self.telegram_ready:
            return
        with self.store.connect() as db:
            row=db.execute("SELECT * FROM notification_outbox WHERE state='queued' AND due_at<=? ORDER BY id LIMIT 1",(int(time.time()),)).fetchone()
        if not row:
            return
        try:
            self.sender(row["message"])
            state,error="sent",""
        except Exception:
            state,error=("failed" if row["attempts"]>=2 else "queued"),"TelegramDeliveryFailed"
        with self.store.connect() as db:
            db.execute("UPDATE notification_outbox SET state=?,attempts=attempts+1,due_at=?,error_code=? WHERE id=?",
                       (state,int(time.time())+60*(2**row["attempts"]),error,row["id"]))

    def status(self, snapshot):
        with self.store.connect() as db:
            inventory=[dict(r) for r in db.execute("SELECT * FROM cloud_inventory ORDER BY display_name")]
            jobs=[dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC,id LIMIT 100")]
            incidents=[dict(r) for r in db.execute("SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY opened_at DESC")]
            failed_notices=db.execute("SELECT count(*) FROM notification_outbox WHERE state='failed'").fetchone()[0]
        by_id={r["desktop_id"]:r for r in inventory}
        for agent in snapshot["agents"]:
            agent["health"]=health_for(agent,snapshot["lease"],by_id.get(agent["desktop_id"],{}).get("cloud_status","Unknown"))
        return {"inventory":inventory,"jobs":jobs,"incidents":incidents,"failed_notifications":failed_notices,
                "cloud_configured":bool(self.cloud_ready),"telegram_configured":bool(self.telegram_ready),"region":self.cloud.region,"last_sync_at":self.last_sync_at,"source_hardware":self.source_hardware,"max_batch_size":min(50,int(self.app.cfg.get('max_batch_size',3)))}

    def start(self):
        def loop(fn, seconds):
            while not self.stop.is_set():
                try:
                    fn()
                except Exception:
                    pass  # Failures are represented by job/notification states, never print secrets.
                self.stop.wait(seconds)
        for name,fn,seconds in [("bridge-health",lambda:self.evaluate_health(self.store.snapshot()),5),
                                ("bridge-deploy",self.tick_jobs,10),("bridge-notify",self.deliver_one,5),
                                ("bridge-inventory",lambda:self.sync() if self.cloud_ready else None,30),
                                ("bridge-usb-check",self.probe_source,30)]:
            threading.Thread(target=loop,args=(fn,seconds),name=name,daemon=True).start()
        if self.app.cfg.get('source_reset_enabled'):
            threading.Thread(target=loop,args=(self.app.source_reset.tick,1),name='bridge-source-reset',daemon=True).start()
