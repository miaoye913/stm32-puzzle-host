"""
test_client.py —— 客户端全链路测试（上位机软件 <-> 仿真 STM32）

不接硬件即可验证：组帧发送、应答匹配、ACK/NACK 语义、超时、
GET ALL 状态同步、ECHO 回环、命令长度校验、压力测试等。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))      # 项目根目录
sys.path.insert(0, _HERE)                       # tests 目录

import protocol as P                                                    # noqa: E402
from client import (Event, ProtoClient, ProtocolError, ResponseError,   # noqa: E402
                    ResponseTimeout)
from fake_device import FakeStm32                                       # noqa: E402
from link import LinkError, LoopbackLink, make_link                      # noqa: E402

FakeLink = LoopbackLink                     # 仿真设备需要可收发的回环链路


class ClientTestCase(unittest.TestCase):
    """公共夹具：配对好的 FakeLink + 仿真设备 + 客户端。"""

    resp_delay = 0.0

    def setUp(self) -> None:
        self.link = FakeLink()
        self.device = FakeStm32(self.link, resp_delay=self.resp_delay).start()
        self.events = []
        self.client = ProtoClient(self.link, on_event=self.events.append,
                                  timeout=0.5)
        self.client.start()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        """按顺序清理客户端与仿真设备。"""
        try:
            self.client.close()
        finally:
            self.device.stop()

    def kinds(self, kind: str) -> list:
        """取出某类事件。"""
        return [e for e in self.events if e.kind == kind]


class TestBasicCommands(ClientTestCase):
    """设置 / 查询类命令测试。"""

    def test_ping(self):
        """PING 收到 ACK。"""
        self.assertTrue(self.client.ping())
        self.assertEqual(self.client.stats["tx_frames"], 1)
        self.assertEqual(self.client.stats["rx_frames"], 1)

    def test_set_and_get_x(self):
        """SET X 之后 GET X 返回同值。"""
        self.assertTrue(self.client.set_x(1000))
        self.assertEqual(self.client.get_x(), 1000)

    def test_set_and_get_y(self):
        """SET Y 之后 GET Y 返回同值。"""
        self.assertTrue(self.client.set_y(250000))
        self.assertEqual(self.client.get_y(), 250000)

    def test_set_and_get_rot_negative(self):
        """负角度（int16）往返正确。"""
        self.assertTrue(self.client.set_rot(-90))
        self.assertEqual(self.client.get_rot(), -90)

    def test_set_and_get_servo(self):
        """舵机开关状态同步。"""
        self.assertTrue(self.client.set_servo(True))
        self.assertEqual(self.client.get_servo(), 1)
        self.assertTrue(self.client.set_servo(False))
        self.assertEqual(self.client.get_servo(), 0)

    def test_set_and_get_em(self):
        """电磁铁开关状态同步。"""
        self.assertTrue(self.client.set_em(True))
        self.assertEqual(self.client.get_em(), 1)
        self.assertTrue(self.client.set_em(False))
        self.assertEqual(self.client.get_em(), 0)

    def test_reinit_sets_flag(self):
        """REINIT 会让设备的 reinitPending 置 1。"""
        self.assertTrue(self.client.reinit())
        self.assertEqual(self.device.reinit_pending, 1)
        self.assertEqual(self.client.get_all().reinit_pending, 1)

    def test_echo_roundtrip(self):
        """ECHO 原样返回数据（含空数据与最大长度）。"""
        for payload in (b"", bytes([0x11, 0x22, 0x33]), bytes(range(14))):
            self.assertEqual(self.client.echo(payload), payload)

    def test_unknown_command_nack(self):
        """未知命令（0x7F）→ NACK → ResponseError。"""
        with self.assertRaises(ResponseError):
            self.client.request(0x7F, b"")
        self.assertEqual(self.client.stats["nacks"], 1)

    def test_bad_length_nack(self):
        """SET X 的 DATA 长度不是 4 → NACK（固件 len 校验）。"""
        with self.assertRaises(ResponseError):
            self.client.request(P.CMD_SET_X, bytes([0x01, 0x02]))

    def test_range_validation(self):
        """超范围参数在本地就被拦下。"""
        with self.assertRaises(ValueError):
            self.client.set_x(-1)
        with self.assertRaises(ValueError):
            self.client.set_rot(40000)

    def test_get_all_updates_cache(self):
        """GET ALL 会更新 last_status 缓存并抛 status 事件。"""
        self.client.set_x(7)
        self.client.set_y(8)
        self.client.set_rot(45)
        self.client.set_servo(True)
        status = self.client.get_all()
        self.assertEqual(status.pos_x, 7)
        self.assertEqual(status.pos_y, 8)
        self.assertEqual(status.pos_rot, 45)
        self.assertEqual(status.servo, 1)
        self.assertEqual(self.client.last_status, status)
        self.assertEqual(len(self.kinds("status")), 1)


class TestRawAndEvents(ClientTestCase):
    """原始帧与事件测试。"""

    def test_send_raw_full_frame(self):
        """手动发完整帧（PING）能拿到 ACK。"""
        reply = self.client.send_raw(P.ping_frame())
        self.assertIsNotNone(reply)
        self.assertEqual(reply.cmd, P.RSP_ACK)

    def test_send_raw_no_reply(self):
        """不等待应答模式立即返回 None，但帧确实发出去了。"""
        self.assertIsNone(self.client.send_raw(P.ping_frame(), expect_reply=False))
        time.sleep(0.15)
        self.assertEqual(len(self.device.received), 1)

    def test_tx_rx_events(self):
        """tx / rx / raw 事件都被抛出。"""
        self.client.ping()
        self.assertEqual(len(self.kinds("tx")), 1)
        self.assertEqual(len(self.kinds("rx")), 1)
        self.assertGreaterEqual(len(self.kinds("raw")), 2)

    def test_echo_event(self):
        """ECHO 回显会抛 echo 事件。"""
        self.client.echo(bytes([0xAA]))
        echo_events = self.kinds("echo")
        self.assertEqual(len(echo_events), 1)
        self.assertEqual(echo_events[0].data, bytes([0xAA]))

    def test_event_text_is_readable(self):
        """事件文本包含方向箭头与命令名（GUI 日志直接用它）。"""
        self.client.ping()
        texts = [e.text() for e in self.kinds("tx")] + [e.text() for e in self.kinds("rx")]
        self.assertTrue(any("PING" in t for t in texts))
        self.assertTrue(any("ACK" in t for t in texts))


class TestTimeouts(ClientTestCase):
    """超时与重试测试。"""

    resp_delay = 0.4

    def test_timeout_returns_none(self):
        """响应慢于超时 → None（并按类型抛 ResponseTimeout）。"""
        self.assertIsNone(self.client.request(P.CMD_PING, b"", timeout=0.05))
        self.assertEqual(self.client.stats["timeouts"], 1)
        self.assertIsNone(self.client.request(P.CMD_GET_ALL, b"", timeout=0.05))
        self.assertIsNone(self.client.request(P.CMD_GET_ALL, b"", timeout=0.05))


class TestRetry(unittest.TestCase):
    """重试路径（用可控链路模拟第一次发送丢失）。"""

    def test_retry_succeeds(self):
        """首帧丢失 → 重发第 2 次收到 ACK。"""

        class FlakyLink(FakeLink):
            """第一次 write 直接丢包，用于验证重试。"""

            def __init__(self) -> None:
                super().__init__()
                self.dropped = 0

            def write(self, data: bytes) -> None:
                """首次写入丢弃，之后正常。"""
                if self.dropped == 0:
                    self.dropped += 1
                    return
                super().write(data)

        link = FlakyLink()
        device = FakeStm32(link).start()
        self.addCleanup(device.stop)
        client = ProtoClient(link, timeout=0.1)
        client.start()
        self.addCleanup(client.close)

        reply = client.request(P.CMD_PING, b"", timeout=0.1, retries=1)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.cmd, P.RSP_ACK)
        self.assertEqual(client.stats["timeouts"], 1)
        self.assertEqual(link.dropped, 1)


class TestUnsolicited(unittest.TestCase):
    """非请求响应（噪声/残留）不应污染后续请求。"""

    def test_stale_frame_is_drained(self):
        """队列里的历史帧在下次请求前被清掉，避免错配。"""
        link = FakeLink()
        client = ProtoClient(link, timeout=0.3)
        client.start()
        self.addCleanup(client.close)
        link.feed(P.Frame(P.RSP_ACK).to_bytes())        # 模拟历史残留
        time.sleep(0.05)
        # SET X 期望的响应是 ACK，但残留的也是 ACK -> 会被清掉后重新请求
        device = FakeStm32(link).start()
        self.addCleanup(device.stop)
        self.assertTrue(client.set_x(123))
        self.assertEqual(device.pos_x, 123)


class TestLinkFailure(unittest.TestCase):
    """链路异常处理。"""

    def test_write_on_closed_link_raises(self):
        """链路关闭后请求必须抛 LinkError，而不是卡死。"""
        link = FakeLink()
        client = ProtoClient(link, timeout=0.2, auto_reopen=False)
        client.start()
        self.addCleanup(client.close)
        link.close()
        time.sleep(0.05)
        with self.assertRaises(LinkError):
            client.set_x(1)

    def test_make_link_parsing(self):
        """make_link 能识别串口名与 tcp:// 形式。"""
        link = make_link("tcp://127.0.0.1:5000")
        self.assertEqual(link.host, "127.0.0.1")
        self.assertEqual(link.port, 5000)
        with self.assertRaises(ValueError):
            make_link("tcp://127.0.0.1")


class TestBench(ClientTestCase):
    """压力测试统计。"""

    def test_ping_bench(self):
        """PING 压力测试 20 次全中。"""
        result = self.client.ping_bench(20)
        self.assertEqual(result["sent"], 20)
        self.assertEqual(result["ok"], 20)
        self.assertEqual(result["lost"], 0)
        self.assertIsNotNone(result["avg_ms"])


class TestMultiFrameBurst(ClientTestCase):
    """连续多帧请求的稳定性（模拟界面长时间轮询）。"""

    def test_50_get_all(self):
        """连续 50 次 GET ALL 都能正确应答（无错配、无丢帧）。"""
        for i in range(50):
            self.client.set_x(i)
            status = self.client.get_all()
            self.assertIsNotNone(status)
            self.assertEqual(status.pos_x, i)
        self.assertEqual(self.client.stats["nacks"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
