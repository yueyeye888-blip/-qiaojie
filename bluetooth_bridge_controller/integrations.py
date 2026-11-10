"""Narrow cloud/notification clients. Credentials never enter the job database."""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ACCOUNT = "bluetooth-bridge-controller"
PREFIX = "com.lumen.bluetoothbridge."


def read_secret(name):
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", ACCOUNT,
         "-s", PREFIX + name, "-w"], capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise ValueError("尚未在 Mac 钥匙串配置所需凭据")
    return result.stdout.strip()


def write_secret(name, value):
    # Interactive stdin keeps the value out of process argument listings.
    args = ["add-generic-password", "-U", "-a", ACCOUNT, "-s", PREFIX + name, "-w", value]
    # security's interactive parser uses double quotes, not POSIX shell quoting.
    if any(any(ord(c) < 32 for c in item) for item in args):
        raise ValueError("凭据包含不支持的控制字符")
    quote = lambda item: '"' + item.replace('\\', '\\\\').replace('"', '\\"') + '"'
    result = subprocess.run(["/usr/bin/security", "-i"],
                            input=" ".join(quote(x) for x in args) + "\n",
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError("写入 macOS 钥匙串失败，请检查本机授权")
    if not hmac.compare_digest(read_secret(name),value):
        raise ValueError("钥匙串写入回读校验失败")


class CloudError(Exception):
    def __init__(self, code, request_id="", uncertain=False):
        self.code = re.sub(r"[^A-Za-z0-9_.-]", "", str(code))[:100] or "CloudError"
        self.request_id = re.sub(r"[^A-Za-z0-9-]", "", str(request_id))[:100]
        self.uncertain = uncertain
        super().__init__(self.code + (" · " + self.request_id if self.request_id else ""))


def rpc_parameters(action, region, access_id, secret, params, version="2020-09-30"):
    values = {"Action": action, "RegionId": region, "Format": "JSON", "Version": version,
              "AccessKeyId": access_id, "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
              "SignatureNonce": str(uuid.uuid4()),
              "Timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for key, value in params.items():
        if isinstance(value, list):
            for index, item in enumerate(value, 1):
                values[f"{key}.{index}"] = str(item)
        elif value is not None:
            values[key] = str(value).lower() if isinstance(value, bool) else str(value)
    encode = lambda x: urllib.parse.quote(str(x), safe="~")
    canonical = "&".join(encode(k) + "=" + encode(v) for k, v in sorted(values.items()))
    signed = "POST&%2F&" + encode(canonical)
    values["Signature"] = base64.b64encode(hmac.new((secret + "&").encode(), signed.encode(), hashlib.sha1).digest()).decode()
    return values


class CloudClient:
    ACTIONS = {"DescribeDesktops", "RunCommand", "DescribeInvocations", "StartDesktops", "WakeupDesktops"}

    def __init__(self, region, credentials=None):
        if not re.fullmatch(r"[a-z]{2}-[a-z]+(?:-\d+)?", region):
            raise ValueError("无效地域")
        self.region = region
        self.credentials = credentials

    def call(self, action, **params):
        if action not in self.ACTIONS:
            raise ValueError("未允许的云操作")
        stored=json.loads(read_secret("aliyun.credentials")) if self.credentials is None else None
        access_id, secret = self.credentials or (stored["access_id"],stored["access_secret"])
        body = urllib.parse.urlencode(rpc_parameters(action, self.region, access_id, secret, params)).encode()
        req = urllib.request.Request(f"https://ecd.{self.region}.aliyuncs.com/", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read(65536))
            except Exception:
                result = {"Code": "HTTP" + str(exc.code)}
            raise CloudError(result.get("Code", "CloudError"), result.get("RequestId", ""),
                             uncertain=exc.code>=500 and action not in {"DescribeDesktops","DescribeInvocations"}) from None
        except (OSError, ValueError):
            # RunCommand has no ClientToken: a lost response MUST NOT be retried.
            raise CloudError("TransportUnknown", uncertain=action not in {"DescribeDesktops", "DescribeInvocations"}) from None
        if result.get("Code"):
            raise CloudError(result["Code"], result.get("RequestId", ""))
        return result

    def desktops(self):
        rows, token = [], None
        for _ in range(100):
            result = self.call("DescribeDesktops", MaxResults=100, NextToken=token)
            rows.extend(result.get("Desktops", []))
            token = result.get("NextToken")
            if not token:
                return rows
        raise CloudError("InventoryPaginationLimit")

    def invocation(self, invoke_id):
        return self.call("DescribeInvocations", InvokeId=invoke_id, IncludeOutput=True,
                         IncludeInvokeDesktops=True, ContentEncoding="PlainText", MaxResults=50)

    def run(self, desktop_id, script):
        encoded = base64.b64encode(script.encode()).decode()
        if len(encoded) > 16 * 1024:
            raise ValueError("安装脚本超过阿里云 16KB 编码限制")
        return self.call("RunCommand", Type="RunPowerShellScript", CommandContent=encoded,
                         ContentEncoding="Base64", DesktopId=[desktop_id], Timeout=600)["InvokeId"]


def telegram_call(token, method, payload):
    # Only these methods are needed, and exception text must never contain the URL/token.
    if method not in {"getMe", "getChat", "getUpdates", "sendMessage"}:
        raise ValueError("未允许的 Telegram 操作")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise ValueError("Telegram Bot Token 格式不正确")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise ValueError()
        return result["result"]
    except Exception:
        raise ValueError("Telegram 请求失败，请检查 Token、私聊 Chat ID，以及是否已向机器人发送 /start") from None


def discover_telegram_chat(token, pairing_code):
    if not re.fullmatch(r"bridge-[a-f0-9]{24}",pairing_code):
        raise ValueError("请先生成新的配对指令")
    updates=telegram_call(token,"getUpdates",{"timeout":0,"limit":100,"allowed_updates":["message"]})
    matches=set()
    for update in updates:
        message=update.get("message") or {}
        chat=message.get("chat") or {}
        sender=message.get("from") or {}
        if (message.get("text")=="/start "+pairing_code and
            message.get("date",0)>=time.time()-600 and chat.get("type")=="private" and
            chat.get("id")==sender.get("id") and not sender.get("is_bot") and
            re.fullmatch(r"[1-9]\d*",str(chat.get("id","")))):
            matches.add(str(chat["id"]))
    if len(matches)!=1:
        raise ValueError("尚未找到唯一的配对消息。请用本人账号向新机器人发送页面上的完整指令，再点击读取；指令有效期为十分钟。")
    return matches.pop()


def configure_telegram(token, chat_id):
    if not re.fullmatch(r"[1-9]\d*", str(chat_id)):
        raise ValueError("仅支持正整数 Chat ID 的 Telegram 私聊")
    telegram_call(token, "getMe", {})
    chat = telegram_call(token, "getChat", {"chat_id": chat_id})
    if chat.get("type") != "private" or str(chat.get("id")) != str(chat_id):
        raise ValueError("必须配置本人 Telegram 私聊")
    write_secret("telegram.credentials",json.dumps({"token":token,"chat_id":str(chat_id)}))


def notify_telegram(text):
    credentials=json.loads(read_secret("telegram.credentials"))
    return telegram_call(credentials["token"], "sendMessage", {
        "chat_id": credentials["chat_id"], "text": text[:3500],
        "disable_web_page_preview": True,
    })
