"""Lease-gated TCP forwarding for the one allowed VirtualHere USB server.

The real server MUST bind loopback only. Closing this process also closes its
tunnels, so a stopped controller cannot leave a remotely reachable raw server.
"""
from __future__ import annotations

import json
import select
import socket
import socketserver
import threading
import time
from pathlib import Path


MARKER = Path("/Library/Application Support/BluetoothBridge/source-isolation.json")


class Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        proxy = self.server.proxy
        identity = proxy.store.usb_authorization(self.client_address[0])
        if identity is None:
            return
        with proxy.tunnel_lock:
            proxy.active_tunnels+=1
        try:
            with socket.create_connection(proxy.upstream, timeout=3) as upstream:
                endpoints = [self.request, upstream]
                for endpoint in endpoints:
                    # VirtualHere depends on low-latency USB request/response
                    # exchanges. Preserve the transport behaviour on both legs.
                    endpoint.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    endpoint.settimeout(2)
                while not proxy.stopped.is_set():
                    if not proxy.store.usb_connection_authorized(self.client_address[0],identity):
                        return
                    readable, _, _ = select.select(endpoints, [], [], 0.5)
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = upstream if source is self.request else self.request
                        destination.sendall(data)
        except OSError:
            return
        finally:
            with proxy.tunnel_lock:
                proxy.active_tunnels-=1


class TunnelServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class UsbProxy:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.upstream = ("127.0.0.1", int(cfg.get("usb_backend_port", 7576)))
        self.stopped = threading.Event()
        self.server = None
        self.probe_lock=threading.Lock()
        self.probe_at=None
        self.probe_ok=False
        self.tunnel_lock=threading.Lock()
        self.active_tunnels=0

    def start(self):
        self.server = TunnelServer((self.cfg["agent_bind"], int(self.cfg.get("usb_proxy_port", 7575))), Tunnel)
        self.server.proxy = self
        threading.Thread(target=self.server.serve_forever,name="bridge-usb-proxy",daemon=True).start()

    def ready(self):
        if not self.server or self.stopped.is_set():
            return False
        try:
            marker = json.loads(MARKER.read_text())
            stat = MARKER.stat()
            if stat.st_uid != 0 or stat.st_mode & 0o022:
                return False
            if marker.get("backend") != "127.0.0.1" or marker.get("port") != self.upstream[1]:
                return False
            with self.probe_lock:
                current=time.monotonic()
                if self.probe_at is not None and current-self.probe_at<5:
                    return self.probe_ok
                self.probe_at=current
                try:
                    with socket.create_connection(self.upstream,timeout=1):
                        self.probe_ok=True
                except OSError:
                    self.probe_ok=False
                return self.probe_ok
        except (OSError,ValueError):
            return False

    def close(self):
        self.stopped.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
