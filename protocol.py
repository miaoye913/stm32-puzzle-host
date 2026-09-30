"""
protocol.py —— 上位机通信协议编解码层（纯逻辑，零依赖）

对应 STM32 侧 app/protocol/protocol.c / protocol.h。

帧格式::

    [0xAA] [0x55] [CMD] [LEN] [DATA ...] [CHECKSUM] [0x0D]
     帧头1  帧头2  命令  长度   参数（小端）   校验      帧尾

本文件只做「字节 <-> 帧对象」的转换，不碰串口、不碰线程，
因此可以被单元测试完整覆盖（见 tests/test_protocol.py）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ==========================================================
# 协议常量
# ==========================================================

HEAD1 = 0xAA                    # 帧头第一字节
HEAD2 = 0x55                    # 帧头第二字节
TAIL = 0x0D                     # 帧尾
DATA_MAX = 14                   # DATA 段最大长度（GET ALL 响应）

# ---- 命令码：上位机 -> STM32 ----
CMD_SET_X = 0x01                # 设置 X 轴坐标（uint32 小端）
CMD_SET_Y = 0x02                # 设置 Y 轴坐标（uint32 小端）
CMD_SET_R = 0x03                # 设置旋转角度（int16 小端）
CMD_SET_SERVO = 0x04            # 舵机开关（1 字节，0/1）
CMD_SET_EM = 0x05               # 电磁铁开关（1 字节，0/1）
CMD_REINIT = 0x06               # 触发重新初始化（找零）
CMD_PING = 0x07                 # 心跳/测试，纯应答
CMD_ECHO = 0x08                 # 回环测试，DATA 原样返回
CMD_GET_ALL = 0x10              # 查询所有状态
CMD_GET_X = 0x11                # 查询 X
CMD_GET_Y = 0x12                # 查询 Y
CMD_GET_R = 0x13                # 查询旋转
CMD_GET_S = 0x14                # 查询舵机
CMD_GET_E = 0x15                # 查询电磁铁

# ---- 响应码：STM32 -> 上位机 ----
RSP_ACK = 0x80                  # 设置成功
RSP_NACK = 0x81                 # 错误（未知命令 / 校验失败 / 参数错误）
RSP_ALL = 0x90                  # 所有状态数据（14 字节）
RSP_X = 0x91                    # X 坐标数据（4 字节）
RSP_Y = 0x92                    # Y 坐标数据（4 字节）
RSP_R = 0x93                    # 旋转角度数据（2 字节）
RSP_S = 0x94                    # 舵机状态数据（1 字节）
RSP_E = 0x95                    # 电磁铁状态数据（1 字节）
RSP_ECHO = CMD_ECHO             # 回环响应沿用 0x08（见 protocol.c）

# ---- 取值范围（与 SysConfig_t 字段类型一致）----
X_MIN, X_MAX = 0, 0xFFFFFFFF            # uint32_t posX / posY
Y_MIN, Y_MAX = 0, 0xFFFFFFFF
ROT_MIN, ROT_MAX = -32768, 32767        # int16_t posRot

# ==========================================================
# 名称表（供 GUI / CLI / 日志显示）
# ==========================================================

CMD_NAMES = {
    CMD_SET_X: "SET X",
    CMD_SET_Y: "SET Y",
    CMD_SET_R: "SET R",
    CMD_SET_SERVO: "SET SERVO",
    CMD_SET_EM: "SET EM",
    CMD_REINIT: "REINIT",
    CMD_PING: "PING",
    CMD_ECHO: "ECHO",
    CMD_GET_ALL: "GET ALL",
    CMD_GET_X: "GET X",
    CMD_GET_Y: "GET Y",
    CMD_GET_R: "GET R",
    CMD_GET_S: "GET S",
    CMD_GET_E: "GET E",
}

RSP_NAMES = {
    RSP_ACK: "ACK",
    RSP_NACK: "NACK",
    RSP_ALL: "DATA: ALL",
    RSP_X: "DATA: X",
    RSP_Y: "DATA: Y",
    RSP_R: "DATA: R",
    RSP_S: "DATA: S",
    RSP_E: "DATA: E",
    RSP_ECHO: "ECHO 回显",
}


def cmd_name(cmd: int) -> str:
    """命令码转可读名称。"""
    return CMD_NAMES.get(cmd, RSP_NAMES.get(cmd, "UNKNOWN"))


# ==========================================================
# 数据类
# ==========================================================

@dataclass
class Frame:
    """一帧已解析的报文（收发通用）。"""

    cmd: int
    data: bytes = b""

    @property
    def length(self) -> int:
        """DATA 段长度。"""
        return len(self.data)

    def to_bytes(self) -> bytes:
        """按协议编码为完整帧字节串。"""
        return build_frame(self.cmd, self.data)

    def hex(self) -> str:
        """完整帧的十六进制大写字符串，例如 'AA 55 80 00 80 0D'。"""
        return to_hex(self.to_bytes())

    def payload_hex(self) -> str:
        """DATA 段的十六进制字符串。"""
        return to_hex(self.data)

    def name(self) -> str:
        """命令/响应名。"""
        return cmd_name(self.cmd)

    def __str__(self) -> str:                       # 日志友好
        if self.data:
            return f"{self.name()}(0x{self.cmd:02X}) DATA[{len(self.data)}]={self.payload_hex()}"
        return f"{self.name()}(0x{self.cmd:02X})"


@dataclass
class Status:
    """GET ALL 的解析结果（对应 g_sysCfg 的可观测字段）。"""

    pos_x: int = 0              # uint32 逻辑 X 坐标（脉冲）
    pos_y: int = 0              # uint32 逻辑 Y 坐标（脉冲）
    pos_rot: int = 0            # int16 旋转角度（度，-180 ~ +180）
    servo: int = 0              # 1 = 舵机开（拉满），0 = 关
    electromagnet: int = 0      # 1 = 电磁铁吸合，0 = 释放
    homing: int = 0             # 1 = 正在找零/归零
    reinit_pending: int = 0     # 1 = 已请求重新初始化（找零）

    @classmethod
    def from_payload(cls, data: bytes) -> "Status":
        """
        解析 14 字节 DATA 段：X(4)+Y(4)+R(2)+S(1)+E(1)+H(1)+RI(1)。

        :raises ValueError: 长度不是 14 字节
        """
        if len(data) != 14:
            raise ValueError(f"GET ALL 响应的 DATA 段应为 14 字节，实际 {len(data)}")
        x, y, r, s, e, h, ri = struct.unpack("<IIhBBBB", data)
        return cls(pos_x=x, pos_y=y, pos_rot=r, servo=s,
                   electromagnet=e, homing=h, reinit_pending=ri)

    def to_payload(self) -> bytes:
        """编码回 14 字节 DATA 段（用于仿真设备）。"""
        return struct.pack("<IIhBBBB", self.pos_x, self.pos_y, self.pos_rot,
                           self.servo, self.electromagnet,
                           self.homing, self.reinit_pending)

    def as_dict(self) -> dict:
        """转普通字典（便于打印 / 序列化）。"""
        return {
            "posX": self.pos_x,
            "posY": self.pos_y,
            "posRot": self.pos_rot,
            "servo": self.servo,
            "electromagnet": self.electromagnet,
            "homing": self.homing,
            "reinitPending": self.reinit_pending,
        }

    def __str__(self) -> str:
        return (f"X={self.pos_x} Y={self.pos_y} R={self.pos_rot} "
                f"S={self.servo} E={self.electromagnet} "
                f"H={self.homing} RI={self.reinit_pending}")


# ==========================================================
# 编码
# ==========================================================

def checksum(cmd: int, data: bytes = b"") -> int:
    """
    计算 XOR 校验：CMD ^ LEN ^ DATA[0] ^ DATA[1] ^ ...

    :param cmd : 命令码
    :param data: DATA 段
    :return    : 校验字节
    """
    xor = (cmd & 0xFF) ^ (len(data) & 0xFF)
    for b in data:
        xor ^= b
    return xor & 0xFF


def build_frame(cmd: int, data: bytes = b"") -> bytes:
    """
    编码一帧。

    :param cmd : 命令码（0x00 ~ 0xFF）
    :param data: DATA 段，长度 0 ~ 14
    :return    : 完整帧字节串
    :raises ValueError: cmd 或 data 非法
    """
    if not 0 <= int(cmd) <= 0xFF:
        raise ValueError(f"CMD 超出 1 字节范围：{cmd}")
    data = bytes(data)
    if len(data) > DATA_MAX:
        raise ValueError(f"DATA 段最长 {DATA_MAX} 字节，实际 {len(data)}")
    return bytes([HEAD1, HEAD2, cmd & 0xFF, len(data)]) + data + \
        bytes([checksum(cmd, data), TAIL])


def to_hex(data: bytes, sep: str = " ") -> str:
    """字节串转大写十六进制字符串。"""
    return sep.join(f"{b:02X}" for b in data)


# ==========================================================
# 解码
# ==========================================================

class ChecksumError(ValueError):
    """校验和不匹配（对应固件的 NACK 分支）。"""


class TailError(ValueError):
    """帧尾不是 0x0D。"""


def parse_frame(raw: bytes, strict: bool = True) -> Frame:
    """
    解析一帧完整报文（不含帧头）。

    :param raw   : 形如 ``CMD LEN DATA... CHECKSUM TAIL`` 的字节串
    :param strict: True = 严格复刻固件顺序（先校验校验和，再校验帧尾）；
                   False = 只做结构解析
    :return      : Frame 对象
    :raises ValueError: 长度不足 / LEN 与数据不符
    :raises ChecksumError: 校验和不匹配
    :raises TailError: 帧尾错误
    """
    if len(raw) < 4:
        raise ValueError(f"帧长度不足：{len(raw)} 字节")

    cmd = raw[0]
    length = raw[1]
    if len(raw) < 4 + length:
        raise ValueError(f"LEN={length} 需要 {4 + length} 字节，实际 {len(raw)}")

    data = raw[2:2 + length]
    got_sum = raw[2 + length]
    got_tail = raw[3 + length]

    if strict:
        if checksum(cmd, data) != got_sum:
            raise ChecksumError(
                f"校验失败：CMD=0x{cmd:02X} LEN={length} "
                f"期望 0x{checksum(cmd, data):02X} 实际 0x{got_sum:02X}")
        if got_tail != TAIL:
            raise TailError(f"帧尾错误：期望 0x{TAIL:02X} 实际 0x{got_tail:02X}")

    return Frame(cmd=cmd, data=data)


def parse_bytes(raw: bytes, strict: bool = True) -> Frame:
    """
    解析一整帧（含帧头 ``AA 55``）。

    :raises ValueError: 帧头错误或结构非法
    """
    if len(raw) < 2 or raw[0] != HEAD1 or raw[1] != HEAD2:
        head = to_hex(raw[:2]) if len(raw) >= 2 else to_hex(raw)
        raise ValueError(f"帧头错误：期望 AA 55，实际 {head}")
    return parse_frame(raw[2:], strict=strict)


# ==========================================================
# 流式解析器
# ==========================================================

@dataclass
class ParseStats:
    """解析统计，便于测试与诊断。"""

    frames: int = 0                 # 成功解析的帧数
    discarded: int = 0              # 丢弃的字节数（噪声 / 出错后的重同步）
    resyncs: int = 0                # 重新同步次数
    checksum_errors: int = 0
    tail_errors: int = 0
    length_errors: int = 0


class FrameParser:
    """
    增量流式解析器：喂任意长度的字节，吐出完整的帧。

    与固件 protocol.c 的状态机一一对应，包含同样的错误处理：
    校验和错误、帧尾错误、LEN 超限都会导致「丢弃并重同步」，
    LEN 超限与帧尾错误在固件里还会额外回一帧 NACK。
    """

    def __init__(self, strict: bool = True) -> None:
        """:param strict: 是否校验校验和与帧尾（默认 True）"""
        self.strict = strict
        self.stats = ParseStats()
        self._buf = bytearray()
        self._pending_nack = False      # 需要上位机回 NACK 的情况（LEN 超限 / 帧尾错）

    # ---------- 内部工具 ----------

    def _resync(self, consume: int) -> None:
        """从缓冲区头部丢弃 consume 字节，并尝试重新定位帧头。"""
        del self._buf[:consume]
        self.stats.discarded += consume
        self.stats.resyncs += 1
        # 在剩余数据里寻找下一个可能的帧头，跳过无效噪声
        while self._buf:
            if self._buf[0] == HEAD1:
                if len(self._buf) == 1 or self._buf[1] == HEAD2:
                    return
                del self._buf[0]
                self.stats.discarded += 1
            else:
                del self._buf[0]
                self.stats.discarded += 1

    # ---------- 对外接口 ----------

    def feed(self, chunk: bytes) -> List[Frame]:
        """
        喂入新收到的字节。

        :param chunk: 任意长度的字节串
        :return     : 本次新解析出的完整帧列表（可能为空）
        """
        if chunk:
            self._buf.extend(chunk)

        frames: List[Frame] = []
        while True:
            result = self._try_parse_one()
            if result is None:                  # 数据不足，等更多字节
                break
            if result is False:                 # 出错并已重同步，继续尝试
                continue
            frames.append(result)
            self.stats.frames += 1
        return frames

    def _try_parse_one(self):
        """
        尝试解析缓冲区头部的一帧。

        :return: Frame = 解析成功；None = 数据不足，等更多字节；
                 False = 解析出错已重同步
        """
        buf = self._buf
        if len(buf) < 2:
            return None
        if buf[0] != HEAD1:
            self._resync(1)
            return False
        if buf[1] != HEAD2:
            self._resync(1)                     # 固件此时回到等待 HEAD1
            return False
        if len(buf) < 4:
            return None                         # 还差 CMD / LEN

        cmd = buf[2]
        length = buf[3]

        if length > DATA_MAX:                   # 长度超限，固件直接丢弃并回 NACK
            self.stats.length_errors += 1
            self._pending_nack = True
            self._resync(2)
            return False

        need = 4 + length + 2                   # HEAD1 HEAD2 CMD LEN DATA SUM TAIL
        if len(buf) < need:
            return None                         # 数据不足

        data = bytes(buf[4:4 + length])
        got_sum = buf[4 + length]
        got_tail = buf[5 + length]

        if self.strict:
            if checksum(cmd, data) != got_sum:
                # 固件在校验失败处会立即回 NACK
                self.stats.checksum_errors += 1
                self._pending_nack = True
                self._resync(2)
                return False
            if got_tail != TAIL:
                self.stats.tail_errors += 1
                self._pending_nack = True
                self._resync(need)
                return False

        del buf[:need]
        return Frame(cmd=cmd, data=data)

    def take_pending_nack(self) -> bool:
        """
        取出并清除「需要回复 NACK」标志。

        固件在校验失败 / LEN 超限 / 帧尾错误时会回一帧 NACK；上位机若在做
        从机仿真或压力测试，可以借此同步行为。正常使用无需理会。
        """
        flag = self._pending_nack
        self._pending_nack = False
        return flag

    def reset(self) -> None:
        """清空缓冲区（例如串口重连后调用）。"""
        self._buf.clear()
        self._pending_nack = False


# ==========================================================
# 命令编码辅助
# ==========================================================

def set_x_frame(value: int) -> bytes:
    """SET X 帧（uint32 小端）。"""
    return build_frame(CMD_SET_X, struct.pack("<I", int(value)))


def set_y_frame(value: int) -> bytes:
    """SET Y 帧（uint32 小端）。"""
    return build_frame(CMD_SET_Y, struct.pack("<I", int(value)))


def set_rot_frame(value: int) -> bytes:
    """SET R 帧（int16 小端）。"""
    return build_frame(CMD_SET_R, struct.pack("<h", int(value)))


def set_servo_frame(on: bool) -> bytes:
    """SET SERVO 帧（1 字节）。"""
    return build_frame(CMD_SET_SERVO, bytes([1 if on else 0]))


def set_em_frame(on: bool) -> bytes:
    """SET EM 帧（1 字节）。"""
    return build_frame(CMD_SET_EM, bytes([1 if on else 0]))


def reinit_frame() -> bytes:
    """REINIT 帧。"""
    return build_frame(CMD_REINIT)


def ping_frame() -> bytes:
    """PING 帧。"""
    return build_frame(CMD_PING)


def echo_frame(data: bytes) -> bytes:
    """ECHO 帧。"""
    return build_frame(CMD_ECHO, data)


def get_all_frame() -> bytes:
    """GET ALL 帧。"""
    return build_frame(CMD_GET_ALL)


def get_x_frame() -> bytes:
    """GET X 帧。"""
    return build_frame(CMD_GET_X)


def get_y_frame() -> bytes:
    """GET Y 帧。"""
    return build_frame(CMD_GET_Y)


def get_rot_frame() -> bytes:
    """GET R 帧。"""
    return build_frame(CMD_GET_R)


def get_servo_frame() -> bytes:
    """GET S 帧。"""
    return build_frame(CMD_GET_S)


def get_em_frame() -> bytes:
    """GET E 帧。"""
    return build_frame(CMD_GET_E)


# ---- 响应 DATA 段解码 ----

def decode_u32(data: bytes) -> int:
    """解码 4 字节 uint32 小端。"""
    if len(data) != 4:
        raise ValueError(f"uint32 需要 4 字节，实际 {len(data)}")
    return struct.unpack("<I", data)[0]


def decode_i16(data: bytes) -> int:
    """解码 2 字节 int16 小端。"""
    if len(data) != 2:
        raise ValueError(f"int16 需要 2 字节，实际 {len(data)}")
    return struct.unpack("<h", data)[0]


def decode_u8(data: bytes) -> int:
    """解码 1 字节状态量。"""
    if len(data) != 1:
        raise ValueError(f"uint8 需要 1 字节，实际 {len(data)}")
    return data[0]


def check_range(name: str, value: int, low: int, high: int) -> int:
    """
    校验取值范围。

    :raises ValueError: 超范围
    """
    value = int(value)
    if not low <= value <= high:
        raise ValueError(f"{name} 超出范围 [{low}, {high}]：{value}")
    return value


def parse_int(text: str) -> int:
    """
    解析用户输入的整数，支持十进制 / 十六进制（0x 前缀）。

    :raises ValueError: 格式非法
    """
    text = text.strip().replace("_", "")
    if not text:
        raise ValueError("输入为空")
    return int(text, 16) if text.lower().startswith(("0x", "-0x")) else int(text, 10)


def parse_hex_bytes(text: str) -> bytes:
    """
    解析用户输入的十六进制字节串，例如 ``"AA 55 07 00 07 0D"`` 或 ``"AA550700 070D"``。

    :raises ValueError: 含非十六进制字符或长度为奇数
    """
    cleaned = "".join(ch for ch in text if ch not in " \t,;:-_")
    cleaned = cleaned.removeprefix("0x").removeprefix("0X")
    if not cleaned:
        raise ValueError("输入为空")
    if len(cleaned) % 2:
        raise ValueError(f"十六进制字符个数应为偶数：{len(cleaned)}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"非法十六进制字符：{text}") from exc
