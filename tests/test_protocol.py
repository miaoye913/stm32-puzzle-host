"""
test_protocol.py —— 协议编解码层单元测试

对照 README.md 的示例字节 + protocol.c 的边界行为，验证：

* 组帧结果与 README 示例逐字节一致；
* 流式解析能正确处理「拆包 / 粘包 / 噪声 / 校验错 / 帧尾错 / 长度超限」；
* GET ALL 的 14 字节布局与固件一致。
"""

from __future__ import annotations

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol as P  # noqa: E402


class TestBuildFrame(unittest.TestCase):
    """组帧测试。"""

    def test_ping_matches_firmware_example(self):
        """README: PING → AA 55 07 00 07 0D"""
        self.assertEqual(P.build_frame(P.CMD_PING).hex().upper(), "AA550700070D")

    def test_get_all_matches_firmware_example(self):
        """README: GET ALL → AA 55 10 00 10 0D"""
        self.assertEqual(P.get_all_frame().hex().upper(), "AA551000100D")

    def test_set_x_checksum(self):
        """
        SET X=1000 的校验应为 EE，不是 README 里写的 EC。

        逐字节算：01 ^ 04 ^ E8 ^ 03 ^ 00 ^ 00 = 0xEE（README 该行示例有笔误，
        固件 protocol.c 用的是正确的 XOR 累积，故以固件为准）。
        """
        self.assertEqual(P.set_x_frame(1000).hex().upper(), "AA550104E8030000EE0D")

    def test_ack_frame(self):
        """ACK 帧固定为 AA 55 80 00 80 0D。"""
        self.assertEqual(P.Frame(P.RSP_ACK).to_bytes(),
                         bytes([0xAA, 0x55, 0x80, 0x00, 0x80, 0x0D]))

    def test_checksum_xor(self):
        """校验 = CMD ^ LEN ^ DATA..."""
        self.assertEqual(P.checksum(0x01, bytes([0xE8, 0x03, 0x00, 0x00])), 0xEE)
        self.assertEqual(P.checksum(0x07), 0x07)
        self.assertEqual(P.checksum(0x10), 0x10)

    def test_echo_frame(self):
        """ECHO 帧携带 DATA，校验随之变化。"""
        raw = P.echo_frame(bytes([0x11, 0x22, 0x33]))
        self.assertEqual(raw, bytes([0xAA, 0x55, 0x08, 0x03, 0x11, 0x22, 0x33,
                                     0x08 ^ 0x03 ^ 0x11 ^ 0x22 ^ 0x33, 0x0D]))

    def test_set_rot_negative(self):
        """SET R=-90 → int16 小端 A6 FF。"""
        self.assertEqual(P.set_rot_frame(-90)[4:6], bytes([0xA6, 0xFF]))

    def test_reject_oversize_data(self):
        """DATA 段超过 14 字节必须报错。"""
        with self.assertRaises(ValueError):
            P.build_frame(P.CMD_ECHO, bytes(15))

    def test_reject_bad_cmd(self):
        """CMD 必须落在 0~255。"""
        with self.assertRaises(ValueError):
            P.build_frame(0x100)


class TestParseFrame(unittest.TestCase):
    """单帧解析测试。"""

    def test_roundtrip_all_commands(self):
        """所有命令帧编解码往返一致。"""
        cases = [
            (P.CMD_SET_X, struct.pack("<I", 123456)),
            (P.CMD_SET_Y, struct.pack("<I", 0)),
            (P.CMD_SET_R, struct.pack("<h", -180)),
            (P.CMD_SET_SERVO, bytes([1])),
            (P.CMD_SET_EM, bytes([0])),
            (P.CMD_REINIT, b""),
            (P.CMD_PING, b""),
            (P.CMD_GET_ALL, b""),
        ]
        for cmd, data in cases:
            raw = P.build_frame(cmd, data)
            frame = P.parse_bytes(raw)
            self.assertEqual(frame.cmd, cmd)
            self.assertEqual(frame.data, data)

    def test_checksum_error_detected(self):
        """篡改校验字节必须抛 ChecksumError。"""
        raw = bytearray(P.build_frame(P.CMD_PING))
        raw[4] ^= 0xFF
        with self.assertRaises(P.ChecksumError):
            P.parse_bytes(bytes(raw))

    def test_tail_error_detected(self):
        """篡改帧尾必须抛 TailError（校验和正确时）。"""
        raw = bytearray(P.build_frame(P.CMD_PING))
        raw[5] = 0x0A
        with self.assertRaises(P.TailError):
            P.parse_bytes(bytes(raw))

    def test_bad_header(self):
        """帧头错误必须报错。"""
        with self.assertRaises(ValueError):
            P.parse_bytes(bytes([0xAB, 0x55, 0x07, 0x00, 0x07, 0x0D]))

    def test_short_frame(self):
        """数据不足必须报错。"""
        with self.assertRaises(ValueError):
            P.parse_bytes(bytes([0xAA, 0x55, 0x07]))


class TestFrameParser(unittest.TestCase):
    """流式解析器测试。"""

    def test_split_chunks(self):
        """逐字节喂入也能解析出完整帧（拆包）。"""
        raw = P.set_x_frame(1000)
        parser = P.FrameParser()
        frames = []
        for i in range(len(raw)):
            frames += parser.feed(raw[i:i + 1])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].cmd, P.CMD_SET_X)
        self.assertEqual(P.decode_u32(frames[0].data), 1000)

    def test_sticky_packets(self):
        """一次喂入多帧（粘包）全部解析出来。"""
        raw = P.ping_frame() + P.get_all_frame() + P.set_rot_frame(45)
        frames = P.FrameParser().feed(raw)
        self.assertEqual([f.cmd for f in frames],
                         [P.CMD_PING, P.CMD_GET_ALL, P.CMD_SET_R])
        self.assertEqual(P.decode_i16(frames[2].data), 45)

    def test_noise_before_frame(self):
        """帧头之前的噪声被丢弃，不影响后续解析。"""
        parser = P.FrameParser()
        frames = parser.feed(bytes([0x00, 0xAA, 0x11, 0xFF]) + P.ping_frame())
        self.assertEqual([f.cmd for f in frames], [P.CMD_PING])
        self.assertGreaterEqual(parser.stats.discarded, 3)

    def test_bad_checksum_resync(self):
        """校验错误的帧被丢弃，且能重新同步到下一帧。"""
        bad = bytearray(P.build_frame(P.CMD_SET_X, struct.pack("<I", 7)))
        bad[4] ^= 0xFF
        parser = P.FrameParser()
        frames = parser.feed(bytes(bad) + P.ping_frame())
        self.assertEqual([f.cmd for f in frames], [P.CMD_PING])
        self.assertEqual(parser.stats.checksum_errors, 1)
        self.assertTrue(parser.take_pending_nack())

    def test_tail_error_resync(self):
        """帧尾错误的帧被丢弃并重新同步。"""
        bad = bytearray(P.build_frame(P.CMD_PING))
        bad[5] = 0x00
        parser = P.FrameParser()
        frames = parser.feed(bytes(bad) + P.ping_frame())
        self.assertEqual([f.cmd for f in frames], [P.CMD_PING])
        self.assertEqual(parser.stats.tail_errors, 1)

    def test_length_over_max(self):
        """LEN 超过 14 必须被丢弃（固件同样直接丢弃）。"""
        parser = P.FrameParser()
        frames = parser.feed(bytes([0xAA, 0x55, 0x91, 0x0F]) + P.ping_frame())
        self.assertEqual([f.cmd for f in frames], [P.CMD_PING])
        self.assertEqual(parser.stats.length_errors, 1)

    def test_unsolicited_burst(self):
        """连续 100 帧不丢帧。"""
        raw = b"".join(P.ping_frame() for _ in range(100))
        frames = P.FrameParser().feed(raw)
        self.assertEqual(len(frames), 100)


class TestStatus(unittest.TestCase):
    """GET ALL 数据布局测试。"""

    def test_layout_matches_firmware(self):
        """14 字节布局：X(4) Y(4) R(2) S(1) E(1) H(1) RI(1)。"""
        status = P.Status(pos_x=1000, pos_y=2500, pos_rot=-90,
                          servo=1, electromagnet=0, homing=1, reinit_pending=0)
        payload = status.to_payload()
        self.assertEqual(len(payload), 14)
        self.assertEqual(payload[0:4], struct.pack("<I", 1000))
        self.assertEqual(payload[4:8], struct.pack("<I", 2500))
        self.assertEqual(payload[8:10], struct.pack("<h", -90))
        self.assertEqual(payload[10], 1)        # servo
        self.assertEqual(payload[11], 0)        # electromagnet
        self.assertEqual(payload[12], 1)        # homing
        self.assertEqual(payload[13], 0)        # reinitPending

    def test_roundtrip(self):
        """编解码往返一致（含极值）。"""
        for status in [
            P.Status(),
            P.Status(pos_x=0xFFFFFFFF, pos_y=0, pos_rot=-32768,
                     servo=1, electromagnet=1, homing=1, reinit_pending=1),
            P.Status(pos_x=1, pos_y=2, pos_rot=32767),
        ]:
            self.assertEqual(P.Status.from_payload(status.to_payload()), status)

    def test_bad_length(self):
        """长度不是 14 字节必须报错。"""
        with self.assertRaises(ValueError):
            P.Status.from_payload(bytes(13))


class TestHelpers(unittest.TestCase):
    """输入解析辅助函数测试。"""

    def test_parse_int_dec_hex(self):
        """支持十进制与 0x 十六进制。"""
        self.assertEqual(P.parse_int("1000"), 1000)
        self.assertEqual(P.parse_int("0x10"), 16)
        self.assertEqual(P.parse_int("  -90 "), -90)
        with self.assertRaises(ValueError):
            P.parse_int("abc")

    def test_parse_hex_bytes(self):
        """容忍空格 / 冒号 / 连字符。"""
        self.assertEqual(P.parse_hex_bytes("AA 55 07 00 07 0D"),
                         bytes.fromhex("AA550700070D"))
        self.assertEqual(P.parse_hex_bytes("aa550700070d"),
                         bytes.fromhex("AA550700070D"))
        self.assertEqual(P.parse_hex_bytes("AA:55-07"), bytes([0xAA, 0x55, 0x07]))
        with self.assertRaises(ValueError):
            P.parse_hex_bytes("AA5")
        with self.assertRaises(ValueError):
            P.parse_hex_bytes("ZZ")

    def test_check_range(self):
        """范围校验。"""
        self.assertEqual(P.check_range("X", 10, 0, 100), 10)
        with self.assertRaises(ValueError):
            P.check_range("X", 101, 0, 100)

    def test_hex_format(self):
        """十六进制显示格式。"""
        self.assertEqual(P.to_hex(bytes([0xAA, 0x0D])), "AA 0D")


if __name__ == "__main__":
    unittest.main(verbosity=2)
