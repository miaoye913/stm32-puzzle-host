"""
test_e2e_socket.py —— 真实 socket 全链路测试

与 test_client.py（内存链路）互补：这里让上位机通过真实的 TCP socket
连接 demo_device 里的仿真设备，覆盖 TcpLink、AcceptedLink、粘包/拆包、
多客户端顺序接入等只有真实字节流才会暴露的问题。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import protocol as P                                       # noqa: E402
from client import ProtoClient                             # noqa: E402
from demo_device import AcceptedLink                       # noqa: E402
from fake_device import FakeStm32                          # noqa: E402
from link import TcpLink                                  # noqa: E402


class SocketServer:
    """只服务一个客户端的仿真设备 TCP 服务端（测试夹具）。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.device_ready = threading.Event()
        self.device: FakeStm32 | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "SocketServer":
        """启动服务端线程。"""
        self._thread.start()
        return self

    def _serve(self) -> None:
        """接受一个连接并驱动仿真设备。"""
        self.sock.settimeout(5.0)
        conn, _ = self.sock.accept()
        link = AcceptedLink(conn)
        self.device = FakeStm32(link).start()
        self.device_ready.set()
        while self.device.running and not self._stop.is_set():
            time.sleep(0.05)
        self.device.stop()
        link.close()
        conn.close()

    def stop(self) -> None:
        """停止服务端。"""
        self._stop.set()
        if self.device is not None:
            self.device.stop()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class TestSocketE2E(unittest.TestCase):
    """TCP socket 端到端测试。"""

    def setUp(self) -> None:
        self.server = SocketServer().start()
        self.link = TcpLink("127.0.0.1", self.server.port)
        self.link.open()
        self.client = ProtoClient(self.link, timeout=1.0)
        self.client.start()
        self.assertTrue(self.server.device_ready.wait(3.0), "服务端未就绪")
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        """先关客户端，再关服务端。"""
        self.client.close()
        self.server.stop()

    def test_ping_over_socket(self):
        """真实 socket 上 PING 有 ACK。"""
        self.assertTrue(self.client.ping())

    def test_set_and_get_all_over_socket(self):
        """设置后 GET ALL 状态一致。"""
        self.assertTrue(self.client.set_x(12345))
        self.assertTrue(self.client.set_y(6789))
        self.assertTrue(self.client.set_rot(-45))
        self.assertTrue(self.client.set_servo(True))
        status = self.client.get_all()
        self.assertIsNotNone(status)
        self.assertEqual((status.pos_x, status.pos_y, status.pos_rot, status.servo),
                         (12345, 6789, -45, 1))

    def test_echo_over_socket(self):
        """ECHO 回环（含 14 字节最大长度）。"""
        payload = bytes(range(14))
        self.assertEqual(self.client.echo(payload), payload)

    def test_long_run_no_desync(self):
        """连续 30 轮请求不错配（验证真实流式拆包）。"""
        for i in range(30):
            self.client.set_x(i)
            status = self.client.get_all()
            self.assertIsNotNone(status)
            self.assertEqual(status.pos_x, i)
        self.assertEqual(self.client.stats["timeouts"], 0)
        self.assertEqual(self.client.stats["nacks"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
