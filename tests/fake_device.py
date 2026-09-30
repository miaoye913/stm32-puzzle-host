"""
fake_device.py —— 纯软件仿真 STM32（严格照抄 protocol.c 的行为）

用途：在没有硬件的情况下跑通「上位机软件」的全链路测试。

* 协议解析用 firmware 同款状态机语义（这里复用 protocol.FrameParser，
  其错误分支与 protocol.c 一一对应）；
* 命令执行逐条对照 protocol.c 的 Proto_Execute；
* 通过后台线程读写 FakeLink，等价于一台真实的 USART2 设备。
"""

from __future__ import annotations

import struct
import threading
import time
from typing import List, Optional

import protocol as P
from link import FakeLink


class FakeStm32:
    """
    仿真设备：绑定一个 :class:`FakeLink`，自动应答上位机的帧。

    :param link      : FakeLink 实例
    :param resp_delay: 每帧响应前的延迟（秒），用于测试超时逻辑
    """

    def __init__(self, link: FakeLink, resp_delay: float = 0.0) -> None:
        self.link = link.device_side() if hasattr(link, "device_side") else link
        self.resp_delay = resp_delay

        # ---- 对应 g_sysCfg ----
        self.pos_x = 0
        self.pos_y = 0
        self.pos_rot = 0
        self.servo_state = 0
        self.em_state = 0
        self.homing = 0
        self.reinit_pending = 0

        # ---- 运行状态 ----
        self._parser = P.FrameParser()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.received: List[P.Frame] = []       # 收到的所有帧（供测试断言）
        self.sent: List[P.Frame] = []           # 发出的所有帧
        self.on_command = None                  # 可选钩子 fn(frame)

    # ==================================================
    # 生命周期
    # ==================================================

    def start(self) -> "FakeStm32":
        """启动仿真设备线程。"""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="fake-stm32",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """停止仿真设备线程。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    @property
    def running(self) -> bool:
        """仿真设备线程是否在运行。"""
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self) -> "FakeStm32":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def _loop(self) -> None:
        """主循环：收帧 -> 执行 -> 回应答（链路无关，串口/TCP/内存都能跑）。"""
        while not self._stop.is_set():
            chunk = self._recv()
            if not chunk:
                continue
            for frame in self._parser.feed(chunk):
                self.received.append(frame)
                if self.on_command is not None:
                    self.on_command(frame)
                if self.resp_delay:
                    time.sleep(self.resp_delay)
                try:
                    self._execute(frame)
                except (IOError, OSError):
                    self._stop.set()            # 链路断了，安静退出

    def _recv(self) -> bytes:
        """
        取一批「上位机发来的字节」。

        ``self.link`` 已经通过 ``device_side()`` 切到设备端视角，
        因此这里直接调 ``read_any`` 即可；串口/TCP 场景同样适用。
        """
        try:
            return self.link.read_any(256)
        except (IOError, OSError):
            self._stop.set()
            return b""

    # ==================================================
    # 应答
    # ==================================================

    def _reply(self, cmd: int, data: bytes = b"") -> None:
        """发送一帧应答（链路断开时静默放弃）。"""
        frame = P.Frame(cmd, data)
        self.sent.append(frame)
        try:
            self.link.write(frame.to_bytes())
        except (IOError, OSError, AttributeError):
            self._stop.set()

    def _ack(self) -> None:
        """回 ACK。"""
        self._reply(P.RSP_ACK)

    def _nack(self) -> None:
        """回 NACK。"""
        self._reply(P.RSP_NACK)

    # ==================================================
    # 命令执行（逐条对照 protocol.c）
    # ==================================================

    def _execute(self, frame: P.Frame) -> None:
        """执行命令并应答。"""
        cmd, data = frame.cmd, frame.data

        if cmd == P.CMD_SET_X:
            if len(data) != 4:
                return self._nack()
            self.pos_x = struct.unpack("<I", data)[0]
            self._ack()

        elif cmd == P.CMD_SET_Y:
            if len(data) != 4:
                return self._nack()
            self.pos_y = struct.unpack("<I", data)[0]
            self._ack()

        elif cmd == P.CMD_SET_R:
            if len(data) != 2:
                return self._nack()
            self.pos_rot = struct.unpack("<h", data)[0]
            self._ack()

        elif cmd == P.CMD_SET_SERVO:
            if len(data) != 1:
                return self._nack()
            self.servo_state = 1 if data[0] else 0
            self._ack()

        elif cmd == P.CMD_SET_EM:
            if len(data) != 1:
                return self._nack()
            self.em_state = 1 if data[0] else 0
            self._ack()

        elif cmd == P.CMD_REINIT:
            self.reinit_pending = 1
            self._ack()

        elif cmd == P.CMD_PING:
            self._ack()

        elif cmd == P.CMD_ECHO:
            self._reply(P.RSP_ECHO, data)

        elif cmd == P.CMD_GET_X:
            self._reply(P.RSP_X, struct.pack("<I", self.pos_x))

        elif cmd == P.CMD_GET_Y:
            self._reply(P.RSP_Y, struct.pack("<I", self.pos_y))

        elif cmd == P.CMD_GET_R:
            self._reply(P.RSP_R, struct.pack("<h", self.pos_rot))

        elif cmd == P.CMD_GET_S:
            self._reply(P.RSP_S, bytes([self.servo_state]))

        elif cmd == P.CMD_GET_E:
            self._reply(P.RSP_E, bytes([self.em_state]))

        elif cmd == P.CMD_GET_ALL:
            self._reply(P.RSP_ALL, self.status().to_payload())

        else:
            self._nack()

    # ==================================================
    # 状态
    # ==================================================

    def status(self) -> P.Status:
        """当前状态（与固件 GET ALL 的 DATA 段一致）。"""
        return P.Status(pos_x=self.pos_x, pos_y=self.pos_y, pos_rot=self.pos_rot,
                        servo=self.servo_state, electromagnet=self.em_state,
                        homing=self.homing, reinit_pending=self.reinit_pending)
