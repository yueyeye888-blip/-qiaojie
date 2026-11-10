import socket
import socketserver
import threading
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from source_probe import count_matches
from usb_proxy import UsbProxy
from controller import Store
from test_controller import cfg


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections+=1
        while True:
            try:
                data=self.request.recv(65536)
                if not data:return
                self.request.sendall(data)
            except OSError:return


class Gate:
    identity=None
    def usb_authorization(self,peer):
        return self.identity if peer=='127.0.0.1' else None
    def usb_connection_authorized(self,peer,identity):
        return identity is not None and self.usb_authorization(peer)==identity


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.echo=socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo)
        self.echo.daemon_threads=True
        self.echo.connections=0
        threading.Thread(target=self.echo.serve_forever,daemon=True).start()
        self.gate=Gate()
        self.proxy=UsbProxy(self.gate,{'agent_bind':'127.0.0.1','usb_proxy_port':0,'usb_backend_port':self.echo.server_address[1]})
        self.proxy.start()
    def tearDown(self):
        self.proxy.close();self.echo.shutdown();self.echo.server_close()
    def connect(self):return socket.create_connection(self.proxy.server.server_address,timeout=2)
    def test_denied_client_never_reaches_usb_backend(self):
        with self.connect() as client:
            try:self.assertEqual(client.recv(10),b'')
            except ConnectionResetError:pass
        self.assertEqual(self.echo.connections,0)
    def test_revocation_closes_an_existing_tunnel(self):
        self.gate.identity=('session',1,1)
        with self.connect() as client:
            client.sendall(b'test-usb-frame')
            self.assertEqual(client.recv(100),b'test-usb-frame')
            self.gate.identity=None
            client.settimeout(3)
            self.assertEqual(client.recv(100),b'')
    def test_changed_generation_cannot_reuse_old_connection(self):
        self.gate.identity=('session',1,1)
        with self.connect() as client:
            client.sendall(b'first');self.assertEqual(client.recv(100),b'first')
            self.gate.identity=('session',2,1)
            client.settimeout(3)
            self.assertEqual(client.recv(100),b'')
    def test_controller_shutdown_closes_tunnels(self):
        self.gate.identity=('session',1,1)
        with self.connect() as client:
            client.sendall(b'first');self.assertEqual(client.recv(100),b'first')
            self.proxy.close()
            client.settimeout(3)
            self.assertEqual(client.recv(100),b'')

    def test_normal_release_delivers_cleanup_then_closes_and_denies_new_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.db',cfg())
            task=store.new_enrollment('Trial','ecd-trial')
            agent=store.enroll(task['enrollment_token'],'machine-test','TRIAL','127.0.0.1')
            store.acquire(agent['agent_id']);self.proxy.store=store
            with self.connect() as client:
                client.sendall(b'usb-frame');self.assertEqual(client.recv(100),b'usb-frame')
                store.release()
                with self.connect() as second:
                    try:self.assertEqual(second.recv(100),b'')
                    except ConnectionResetError:pass
                self.assertEqual(self.echo.connections,1)
                client.sendall(b'normal-stop-using');self.assertEqual(client.recv(100),b'normal-stop-using')
                generation=store.snapshot()['lease']['generation']
                store.poll(agent['agent_token'],{'vh_status':'released','bluetooth_status':'absent','applied_action':'release','applied_generation':generation})
                client.settimeout(3);self.assertEqual(client.recv(100),b'')


class ProbeTests(unittest.TestCase):
    def test_status_refresh_reuses_probe_but_checks_marker_permissions(self):
        proxy=UsbProxy(Gate(),{})
        proxy.server=object()
        marker=MagicMock()
        marker.read_text.return_value='{"backend":"127.0.0.1","port":7576}'
        marker.stat.return_value=SimpleNamespace(st_uid=0,st_mode=0o644)
        with patch('usb_proxy.MARKER',marker),patch('usb_proxy.socket.create_connection') as connect,patch('usb_proxy.time.monotonic',return_value=100):
            for _ in range(10):self.assertTrue(proxy.ready())
            self.assertEqual(connect.call_count,1)
            marker.stat.return_value=SimpleNamespace(st_uid=0,st_mode=0o666)
            self.assertFalse(proxy.ready())
            proxy.stopped.set()
            self.assertFalse(proxy.ready())

    def test_only_target_usb_is_counted_and_duplicates_detected(self):
        tree=[{'idVendor':0x10d7,'idProduct':0xb012,'children':[{'idVendor':1,'idProduct':2}]}]
        self.assertEqual(count_matches(tree,0x10d7,0xb012),1)
        self.assertEqual(count_matches(tree+tree,0x10d7,0xb012),2)
        self.assertEqual(count_matches(tree,3,4),0)


if __name__=='__main__':unittest.main()
