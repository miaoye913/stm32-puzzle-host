"""
test_serial_link.py —— 串口链路（pyserial SerialLink）测试

真实硬件走的是串口，而串口不易自动化。这里用 pyserial 的
``socket://`` URL + 一个 TCP 中转，构造一根「软件串口线」：

    SerialLink("socket://127.0.0.1:P")  ←→  中转线程  ←→  仿真 STM32 串口

于是 SerialLink 的 open / read_any / write / close 与 ProtoClient 的配合
（发送、收包线程、应答匹配、GET ALL 解析）都能在无硬件时被真实验证。
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "vendor"))

import serial                                       # noqa: E402
import protocol as P                                # noqa: E402
from client import ProtoClient                      # noqa: E402
from link import SerialLink                         # noqa: E402


class RelayLine:
    """TCP 中转：把两条连接拼成一根双向「串口线」。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "RelayLine":
        """启动中转线程。"""
        self._thread.start()
        return self

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        """单向搬运字节。"""
        src.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data = src.recv(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            try:
                dst.sendall(data)
            except OSError:
                break

    def _loop(self) -> None:
        """接受两路连接并双向转发。"""
        self.sock.settimeout(5.0)
        conn_a = conn_b = None
        try:
            conn_a, _ = self.sock.accept()
            conn_b, _ = self.sock.accept()
        except (socket.timeout, OSError):
            return
        finally:
            if self._stop.is_set():
                for conn in (conn_a, conn_b):
                    if conn is not None:
                        try:
                            conn.close()
                        except OSError:
                            pass
                return
        t1 = threading.Thread(target=self._pump, args=(conn_a, conn_b), daemon=True)
        t2 = threading.Thread(target=self._pump, args=(conn_b, conn_a), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for conn in (conn_a, conn_b):
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        """停止中转。"""
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class DeviceOnSerial:
    """挂在「串口」另一端的仿真 STM32（直接用 pyserial 收发）。"""

    def __init__(self, url: str) -> None:
        """:param url: pyserial URL，如 socket://127.0.0.1:1234"""
        self.ser = serial.serial_for_url(url, timeout=0.05)
        self._parser = P.FrameParser()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.received: list = []
        self.x = 0
        self.y = 0
        self.rot = 0
        self.servo = 0
        self.em = 0
        self.reinit = 0

    def start(self) -> "DeviceOnSerial":
        """启动应答线程。"""
        self._thread.start()
        return self

    def _loop(self) -> None:
        """收帧 -> 执行 -> 应答。"""
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(256)
            except (OSError, AttributeError, serial.SerialException):  # type: ignore[attr-defined]
                return                      # 串口已关闭（socket:// 关闭后内部为 None）
            if not chunk:
                continue
            for frame in self._parser.feed(chunk):
                self.received.append(frame)
                self._execute(frame)

    def _execute(self, frame: P.Frame) -> None:
        """按固件语义执行命令。"""
        cmd, data = frame.cmd, frame.data
        if cmd == P.CMD_SET_X:
            self.x = P.decode_u32(data)
        elif cmd == P.CMD_SET_Y:
            self.y = P.decode_u32(data)
        elif cmd == P.CMD_SET_R:
            self.rot = P.decode_i16(data)
        elif cmd == P.CMD_SET_SERVO:
            self.servo = data[0]
        elif cmd == P.CMD_SET_EM:
            self.em = data[0]
        elif cmd == P.CMD_REINIT:
            self.reinit = 1

        if cmd in (P.CMD_PING, P.CMD_SET_X, P.CMD_SET_Y, P.CMD_SET_R,
                   P.CMD_SET_SERVO, P.CMD_SET_EM, P.CMD_REINIT):
            self._write(P.Frame(P.RSP_ACK))
        elif cmd == P.CMD_ECHO:
            self._write(P.Frame(P.RSP_ECHO, data))
        elif cmd == P.CMD_GET_ALL:
            status = P.Status(self.x, self.y, self.rot, self.servo, self.em,
                              0, self.reinit)
            self._write(P.Frame(P.RSP_ALL, status.to_payload()))
        elif cmd == P.CMD_GET_X:
            self._write(P.Frame(P.RSP_X, struct.pack("<I", self.x)))
        else:
            self._write(P.Frame(P.RSP_NACK))

    def _write(self, frame: P.Frame) -> None:
        """发回应答（链路断了就静默退出）。"""
        try:
            self.ser.write(frame.to_bytes())
        except (OSError, serial.SerialException):           # type: ignore[attr-defined]
            self._stop.set()

    def stop(self) -> None:
        """停止并关闭串口。"""
        self._stop.set()
        try:
            self.ser.close()
        except Exception:
            pass


class TestSerialLink(unittest.TestCase):
    """SerialLink + ProtoClient 串口链路测试。"""

    def setUp(self) -> None:
        self.relay = RelayLine().start()
        url = f"socket://127.0.0.1:{self.relay.port}"
        self.device = DeviceOnSerial(url).start()
        # 让设备先连上中转（中转按连接先后配对）
        time.sleep(0.25)
        self.link = SerialLink(url, timeout=0.05)
        self.link.open()
        self.addCleanup(self._teardown)
        self.client = ProtoClient(self.link, timeout=0.8)
        self.client.start()
        time.sleep(0.1)

    def _teardown(self) -> None:
        """清理顺序：客户端 -> 设备 -> 中转。"""
        try:
            self.client.close()
        finally:
            self.device.stop()
            self.relay.stop()

    def test_link_reports_open(self):
        """串口链路打开后 alive 为真。"""
        self.assertTrue(self.link.alive)
        self.assertIn("socket://", repr(self.link))

    def test_ping(self):
        """串口上 PING 有 ACK。"""
        self.assertTrue(self.client.ping())
        self.assertEqual(len(self.device.received), 1)
        self.assertEqual(self.device.received[0].cmd, P.CMD_PING)

    def test_full_set_and_query(self):
        """通过串口下发全部设置，再读回状态。"""
        self.assertTrue(self.client.set_x(4321))
        self.assertTrue(self.client.set_y(1000))
        self.assertTrue(self.client.set_rot(-77))
        self.assertTrue(self.client.set_servo(True))
        self.assertTrue(self.client.set_em(True))
        self.assertTrue(self.client.reinit())
        status = self.client.get_all()
        self.assertIsNotNone(status)
        self.assertEqual(status.pos_x, 4321)
        self.assertEqual(status.pos_y, 1000)
        self.assertEqual(status.pos_rot, -77)
        self.assertEqual(status.servo, 1)
        self.assertEqual(status.electromagnet, 1)
        self.assertEqual(status.reinit_pending, 1)

    def test_echo(self):
        """串口上 ECHO 原样回显。"""
        self.assertEqual(self.client.echo(b"\xDE\xAD\xBE\xEF"), b"\xDE\xAD\xBE\xEF")

    def test_query_single_value(self):
        """单项查询（GET X）解析正确。"""
        self.client.set_x(777)
        self.assertEqual(self.client.get_x(), 777)

    def test_close_then_use_raises(self):
        """关闭链路后再发请求应抛 LinkError，而不是卡死。"""
        from link import LinkError
        self.client.close()
        with self.assertRaises(LinkError):
            self.client.ping()

    def test_no_timeouts_in_burst(self):
        """串口上连续 10 轮设置 + 查询无超时。"""
        for i in range(10):
            self.assertTrue(self.client.set_x(i))
            status = self.client.get_all()
            self.assertIsNotNone(status)
            self.assertEqual(status.pos_x, i)
        self.assertEqual(self.client.stats["timeouts"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
