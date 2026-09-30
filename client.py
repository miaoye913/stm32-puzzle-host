"""
client.py —— 协议客户端：把 link.py 的字节管道变成「一问一答」的接口

职责：

1. 后台收包线程：从链路读字节 -> FrameParser 拆帧 -> 放入响应队列 + 抛事件；
2. 线程安全的请求：加锁发送 -> 等待匹配的响应（可重试）-> 返回解析结果；
3. 把 ACK / NACK 翻译成返回值或异常，把 GET ALL 翻译成 Status 对象。

GUI 和 CLI 都只依赖本文件，因此收发逻辑只有一份实现。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import protocol as P
from link import BaseLink, LinkError

# 超时 / 重试默认值
DEFAULT_TIMEOUT = 0.3           # 单次请求等待响应的秒数
DEFAULT_RETRIES = 0             # 默认不重试（设置类命令重发有副作用风险）
MAX_RETRIES = 3


class ProtocolError(Exception):
    """协议层错误基类。"""


class ResponseError(ProtocolError):
    """设备回了 NACK（未知命令 / 校验失败 / 参数错误）。"""


class ResponseTimeout(ProtocolError):
    """等待响应超时。"""


@dataclass
class Event:
    """
    客户端向界面抛出的统一事件。

    kind 取值：

    ==========  ==========================================
    kind        含义 / extra
    ==========  ==========================================
    tx          已发送一帧，frame = Frame
    rx          收到一帧，frame = Frame
    status      收到 GET ALL，status = Status
    echo        收到 ECHO 回显，data = bytes
    error       出错，message = str
    conn        连接状态变化，state = "up"/"down"/"lost"
    raw         原始字节（收发都走这里，便于做 raw 日志）
    ==========  ==========================================
    """

    kind: str
    frame: Optional[P.Frame] = None
    status: Optional[P.Status] = None
    data: bytes = b""
    message: str = ""
    state: str = ""
    direction: str = ""             # "tx" / "rx"，用于 raw 事件
    ts: float = field(default_factory=time.time)

    def text(self) -> str:
        """转成一行可读日志。"""
        stamp = time.strftime("%H:%M:%S", time.localtime(self.ts))
        ms = int((self.ts % 1) * 1000)
        head = f"[{stamp}.{ms:03d}]"
        if self.kind == "tx":
            return f"{head} → {self.frame}"
        if self.kind == "rx":
            return f"{head} ← {self.frame}"
        if self.kind == "echo":
            return f"{head} ↺ ECHO 回显：{P.to_hex(self.data)}"
        if self.kind == "status":
            return f"{head} ✔ 状态：{self.status}"
        if self.kind == "error":
            return f"{head} ✘ {self.message}"
        if self.kind == "conn":
            return f"{head} ⚡ 连接：{self.state}"
        if self.kind == "raw":
            arrow = "发" if self.direction == "tx" else "收"
            return f"{head} {arrow} 原始 {len(self.data)} 字节：{P.to_hex(self.data)}"
        return f"{head} {self.kind} {self.message}"


# 命令 -> 期望的响应码集合
EXPECTED: Dict[int, Tuple[int, ...]] = {
    P.CMD_SET_X: (P.RSP_ACK, P.RSP_NACK),
    P.CMD_SET_Y: (P.RSP_ACK, P.RSP_NACK),
    P.CMD_SET_R: (P.RSP_ACK, P.RSP_NACK),
    P.CMD_SET_SERVO: (P.RSP_ACK, P.RSP_NACK),
    P.CMD_SET_EM: (P.RSP_ACK, P.RSP_NACK),
    P.CMD_REINIT: (P.RSP_ACK, P.RSP_NACK),
    P.CMD_PING: (P.RSP_ACK, P.RSP_NACK),
    P.CMD_ECHO: (P.RSP_ECHO, P.RSP_NACK),
    P.CMD_GET_ALL: (P.RSP_ALL, P.RSP_NACK),
    P.CMD_GET_X: (P.RSP_X, P.RSP_NACK),
    P.CMD_GET_Y: (P.RSP_Y, P.RSP_NACK),
    P.CMD_GET_R: (P.RSP_R, P.RSP_NACK),
    P.CMD_GET_S: (P.RSP_S, P.RSP_NACK),
    P.CMD_GET_E: (P.RSP_E, P.RSP_NACK),
}


class ProtoClient:
    """
    协议客户端。

    典型用法::

        link = SerialLink("COM3")
        link.open()
        cli = ProtoClient(link, on_event=print_event)
        print(cli.get_all())            # Status(...)
        cli.set_x(1000)                 # 成功返回 Status/True，NACK 抛 ResponseError
        cli.close()

    线程约定：``close()`` 之外的公开方法可在任意线程调用；
    但同一时刻只建议有一个线程做「请求-等待」，因为响应是全局队列。
    """

    def __init__(self, link: BaseLink, on_event=None,
                 timeout: float = DEFAULT_TIMEOUT,
                 auto_reopen: bool = True) -> None:
        """
        :param link       : 已打开的链路
        :param on_event   : 事件回调 ``fn(Event)``，可为 None
        :param timeout    : 单次请求等待响应的秒数
        :param auto_reopen: 读线程出错时是否尝试自动重连
        """
        self.link = link
        self.timeout = timeout
        self.auto_reopen = auto_reopen
        self._on_event = on_event

        self._parser = P.FrameParser()
        self._rx: List[P.Frame] = []            # 已收到但未被请求消费的帧
        self._cv = threading.Condition()
        self._tx_lock = threading.Lock()
        self._req_lock = threading.RLock()      # 保证同一时刻只有一次「请求-等待」

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.state = "closed"                   # closed / up / down / lost
        self.last_status: Optional[P.Status] = None
        self.stats = {"tx_frames": 0, "rx_frames": 0, "timeouts": 0, "nacks": 0}

    # ==================================================
    # 生命周期
    # ==================================================

    def start(self) -> None:
        """启动后台收包线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader_loop,
                                        name="proto-reader", daemon=True)
        self._thread.start()
        self._set_state("up")

    def close(self) -> None:
        """停止收包线程并关闭链路（可重复调用）。"""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        # 先 shutdown（若链路支持）打阻塞中的 read，避免线程卡在 recv 上
        shutdown = getattr(self.link, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        try:
            self.link.close()
        except Exception:
            pass
        self._set_state("closed")

    def __enter__(self) -> "ProtoClient":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        """链路是否仍然可用。"""
        return self.link.alive and self.state == "up"

    # ==================================================
    # 事件
    # ==================================================

    def _emit(self, event: Event) -> None:
        """回调事件（回调异常不影响主流程）。"""
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            pass

    def _set_state(self, state: str) -> None:
        """更新连接状态并抛事件。"""
        if state == self.state:
            return
        self.state = state
        if state != "closed":
            self._emit(Event(kind="conn", state=state))

    # ==================================================
    # 读线程
    # ==================================================

    def _reader_loop(self) -> None:
        """后台线程：读字节 -> 拆帧 -> 入队 + 抛事件。"""
        while not self._stop.is_set():
            link = self.link
            if not link.alive:
                if not self._try_reopen():
                    self._set_state("lost")
                    self._emit(Event(kind="error", message="链路已断开"))
                    return
                continue
            try:
                chunk = link.read_any(256)
            except LinkError as exc:
                if self._stop.is_set():
                    return
                if not self._try_reopen():
                    self._set_state("lost")
                    self._emit(Event(kind="error", message=f"链路读失败：{exc}"))
                    return
                continue
            if not chunk:
                continue
            self._emit(Event(kind="raw", data=chunk, direction="rx"))
            for frame in self._parser.feed(chunk):
                self.stats["rx_frames"] += 1
                self._emit(Event(kind="rx", frame=frame))
                if frame.cmd == P.RSP_ALL and len(frame.data) == 14:
                    try:
                        self.last_status = P.Status.from_payload(frame.data)
                        self._emit(Event(kind="status", frame=frame,
                                         status=self.last_status))
                    except ValueError as exc:       # pragma: no cover - 长度已判断
                        self._emit(Event(kind="error", message=str(exc)))
                elif frame.cmd == P.RSP_ECHO:
                    self._emit(Event(kind="echo", frame=frame, data=frame.data))
                with self._cv:
                    self._rx.append(frame)
                    self._cv.notify_all()

    def _try_reopen(self) -> bool:
        """尝试重连（仅串口链路支持）。"""
        if self._stop.is_set() or not self.auto_reopen:
            return False
        reopen = getattr(self.link, "reopen", None)
        if reopen is None:
            return False
        ok = bool(reopen())
        if ok:
            self._parser.reset()
            self._set_state("up")
            self._emit(Event(kind="conn", state="up（已自动重连）"))
        return ok

    # ==================================================
    # 请求-响应
    # ==================================================

    def _wait_frame(self, expected: Sequence[int],
                    timeout: float) -> Optional[P.Frame]:
        """
        等待一帧响应。

        :param expected: 可接受的响应码集合；空 = 接受任意帧
        :param timeout : 超时秒数
        :return        : Frame 或 None（超时）
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for idx, frame in enumerate(self._rx):
                    if not expected or frame.cmd in expected:
                        return self._rx.pop(idx)
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return None
                self._cv.wait(remain)

    def request(self, cmd: int, data: bytes = b"", expected: Sequence[int] = (),
                timeout: Optional[float] = None, retries: int = DEFAULT_RETRIES,
                check_nack: bool = True) -> Optional[P.Frame]:
        """
        发送一帧并等待响应。

        :param cmd      : 命令码
        :param data     : DATA 段
        :param expected : 期望的响应码；默认按 EXPECTED 表查
        :param timeout  : 超时秒数，默认 self.timeout
        :param retries  : 超时后的重发次数（0 ~ 3）
        :param check_nack: 收到 NACK 时是否抛 ResponseError
        :return         : 响应 Frame；超时返回 None
        :raises ResponseError : 设备回 NACK（check_nack=True）
        :raises LinkError     : 链路不可用
        """
        if not self.link.alive:
            raise LinkError("链路不可用，请先连接")
        if not expected:
            expected = EXPECTED.get(cmd, ())
        timeout = self.timeout if timeout is None else timeout
        retries = max(0, min(int(retries), MAX_RETRIES))

        frame = P.Frame(cmd=cmd, data=bytes(data))
        reply: Optional[P.Frame] = None

        with self._req_lock:
            for attempt in range(retries + 1):
                with self._tx_lock:
                    # 丢弃本次请求之前的历史残留，避免错配
                    self._drain_rx()
                    raw = frame.to_bytes()
                    self.link.write(raw)
                    self.stats["tx_frames"] += 1
                    self._emit(Event(kind="raw", data=raw, direction="tx"))
                    self._emit(Event(kind="tx", frame=frame))
                reply = self._wait_frame(expected, timeout)
                if reply is not None:
                    break
                self.stats["timeouts"] += 1
                if attempt < retries:
                    self._emit(Event(kind="error",
                                     message=f"{frame.name()} 超时，重发第 {attempt + 1} 次"))

        if reply is None:
            return None

        if check_nack and reply.cmd == P.RSP_NACK:
            self.stats["nacks"] += 1
            raise ResponseError(f"{frame.name()} 被设备拒绝（NACK）")
        return reply

    def _drain_rx(self) -> None:
        """清空未消费的响应队列。"""
        with self._cv:
            self._rx.clear()

    # ==================================================
    # 设置类命令
    # ==================================================

    def set_x(self, value: int, **kw) -> bool:
        """:return: True = 收到 ACK；:raises ResponseError: NACK"""
        P.check_range("X", value, P.X_MIN, P.X_MAX)
        return self._send_set(P.set_x_frame(value), **kw)

    def set_y(self, value: int, **kw) -> bool:
        """:return: True = 收到 ACK"""
        P.check_range("Y", value, P.Y_MIN, P.Y_MAX)
        return self._send_set(P.set_y_frame(value), **kw)

    def set_rot(self, value: int, **kw) -> bool:
        """:return: True = 收到 ACK"""
        P.check_range("旋转角度", value, P.ROT_MIN, P.ROT_MAX)
        return self._send_set(P.set_rot_frame(value), **kw)

    def set_servo(self, on: bool, **kw) -> bool:
        """:return: True = 收到 ACK"""
        return self._send_set(P.set_servo_frame(on), **kw)

    def set_em(self, on: bool, **kw) -> bool:
        """:return: True = 收到 ACK"""
        return self._send_set(P.set_em_frame(on), **kw)

    def reinit(self, **kw) -> bool:
        """触发重新初始化（找零）。:return: True = 收到 ACK"""
        return self._send_set(P.reinit_frame(), **kw)

    def ping(self, **kw) -> bool:
        """心跳。:return: True = 收到 ACK"""
        return self._send_set(P.ping_frame(), **kw)

    def _send_set(self, raw: bytes, **kw) -> bool:
        """发送一个设置类帧，把 ACK/NACK 转成 bool/异常。"""
        frame = P.parse_bytes(raw)
        reply = self.request(frame.cmd, frame.data, **kw)
        if reply is None:
            raise ResponseTimeout(f"{frame.name()} 无响应（超时）")
        return reply.cmd == P.RSP_ACK

    def echo(self, data: bytes, **kw) -> Optional[bytes]:
        """
        回环测试：设备原样返回 DATA。

        :param data: 要回环的数据（0 ~ 14 字节）
        :return    : 回显数据；超时返回 None
        """
        frame = P.parse_bytes(P.echo_frame(data))
        reply = self.request(frame.cmd, frame.data, **kw)
        if reply is None:
            return None
        return reply.data

    # ==================================================
    # 查询类命令
    # ==================================================

    def get_all(self, **kw) -> Optional[P.Status]:
        """
        查询全部状态。

        :return: Status 对象；超时返回 None
        :raises ResponseError: 设备回 NACK
        """
        reply = self.request(P.CMD_GET_ALL, b"", **kw)
        if reply is None:
            return None
        if len(reply.data) != 14:
            raise ProtocolError(
                f"GET ALL 响应长度异常：{len(reply.data)}（应为 14）")
        status = P.Status.from_payload(reply.data)
        self.last_status = status
        return status

    def get_x(self, **kw) -> Optional[int]:
        """:return: X 坐标；超时返回 None"""
        reply = self.request(P.CMD_GET_X, b"", **kw)
        return None if reply is None else P.decode_u32(reply.data)

    def get_y(self, **kw) -> Optional[int]:
        """:return: Y 坐标；超时返回 None"""
        reply = self.request(P.CMD_GET_Y, b"", **kw)
        return None if reply is None else P.decode_u32(reply.data)

    def get_rot(self, **kw) -> Optional[int]:
        """:return: 旋转角度；超时返回 None"""
        reply = self.request(P.CMD_GET_R, b"", **kw)
        return None if reply is None else P.decode_i16(reply.data)

    def get_servo(self, **kw) -> Optional[int]:
        """:return: 舵机状态 0/1；超时返回 None"""
        reply = self.request(P.CMD_GET_S, b"", **kw)
        return None if reply is None else P.decode_u8(reply.data)

    def get_em(self, **kw) -> Optional[int]:
        """:return: 电磁铁状态 0/1；超时返回 None"""
        reply = self.request(P.CMD_GET_E, b"", **kw)
        return None if reply is None else P.decode_u8(reply.data)

    # ==================================================
    # 原始帧
    # ==================================================

    def send_raw(self, raw: bytes, expect_reply: bool = True,
                 timeout: Optional[float] = None) -> Optional[P.Frame]:
        """
        直接发送一整帧原始字节（用于手动调试）。

        :param raw         : 完整帧字节（含帧头帧尾），或仅 DATA 之前的载荷
        :param expect_reply: 是否等待任意响应
        :return            : 响应 Frame；不等待或超时返回 None
        """
        if raw[:2] == bytes([P.HEAD1, P.HEAD2]):
            frame = P.parse_bytes(raw, strict=False)
        else:
            frame = P.Frame(cmd=raw[0], data=bytes(raw[1:]))
        if not expect_reply:
            return self._write_only(frame)
        return self.request(frame.cmd, frame.data, expected=(), timeout=timeout)

    def _write_only(self, frame: P.Frame) -> None:
        """只发不等。"""
        raw = frame.to_bytes()
        with self._tx_lock:
            self.link.write(raw)
        self.stats["tx_frames"] += 1
        self._emit(Event(kind="raw", data=raw, direction="tx"))
        self._emit(Event(kind="tx", frame=frame))
        return None

    # ==================================================
    # 压力测试
    # ==================================================

    def ping_bench(self, count: int = 100, timeout: float = 0.5) -> dict:
        """
        连续 PING，统计往返时延与丢包率（用来验证串口链路质量）。

        :param count  : 次数
        :param timeout: 单次超时
        :return       : {"sent", "ok", "lost", "min_ms", "avg_ms", "max_ms"}
        """
        lats: List[float] = []
        lost = 0
        for _ in range(max(1, int(count))):
            t0 = time.perf_counter()
            try:
                reply = self.request(P.CMD_PING, b"", timeout=timeout)
            except (LinkError, ProtocolError):
                reply = None
            if reply is None:
                lost += 1
            else:
                lats.append((time.perf_counter() - t0) * 1000.0)
        return {
            "sent": int(count),
            "ok": len(lats),
            "lost": lost,
            "min_ms": round(min(lats), 2) if lats else None,
            "avg_ms": round(sum(lats) / len(lats), 2) if lats else None,
            "max_ms": round(max(lats), 2) if lats else None,
        }
