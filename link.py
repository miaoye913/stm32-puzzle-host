"""
link.py —— 字节链路抽象层（串口 / TCP / 仿真）

上位机协议的编解码在 protocol.py，收发字节的「管道」在 link.py，
把两者拼起来并管理线程的是 client.py。这样拆分的好处：

* protocol.py 纯逻辑，可单测；
* link.py 只有 read/write，可换成串口、TCP 或内存仿真；
* 单元测试用 FakeLink 就能完整跑通「上位机 <-> 仿真 STM32」的闭环。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import List, Optional, Tuple

try:                                        # 允许把 vendor/ 作为可选依赖目录
    import serial                           # type: ignore
    from serial.tools import list_ports     # type: ignore
    HAVE_PYSERIAL = True
except ImportError:                         # pragma: no cover - 取决于环境
    serial = None                           # type: ignore
    list_ports = None                       # type: ignore
    HAVE_PYSERIAL = False

# 本项目默认的串口参数：USART2，115200-8N1
DEFAULT_BAUDRATE = 115200
DEFAULT_SERIAL_KW = dict(
    bytesize=8,
    parity="N",
    stopbits=1,
    timeout=0.02,           # 单次 read 最长阻塞 20ms，保证读线程能及时看到停止标志
    write_timeout=0.5,
)


class LinkError(IOError):
    """链路层的读写错误（打开失败 / 断开 / 写超时等）。"""


# ==========================================================
# 串口枚举
# ==========================================================

BY_ID_DIR = "/dev/serial/by-id"


def list_serial_ports() -> List[Tuple[str, str]]:
    """
    枚举本机串口（跨平台）。

    :return: [(设备名, 描述), ...]，例如 [("COM3", "USB-SERIAL CH340 (COM3)")]
             或 [("/dev/ttyUSB0", "USB-SERIAL CH340")]
             pyserial 不可用时返回空列表。
    """
    if not HAVE_PYSERIAL:
        return []
    ports: List[Tuple[str, str]] = []
    for info in list_ports.comports():
        desc = info.description or "未知设备"
        if info.hwid and info.hwid != "n/a":
            desc = f"{desc} [{info.hwid}]"
        ports.append((info.device, desc))

    # Linux：额外列出 /dev/serial/by-id/ 里的稳定别名，
    # 它们在重新插拔后不会变成 ttyUSB1，接固定设备时更好用。
    if os.name == "posix" and os.path.isdir(BY_ID_DIR):
        try:
            for name in sorted(os.listdir(BY_ID_DIR)):
                alias = os.path.join(BY_ID_DIR, name)
                if os.path.islink(alias):
                    target = os.path.basename(os.path.realpath(alias))
                    ports.append((alias, f"稳定别名 -> {target}"))
        except OSError:
            pass
    return ports


def port_names() -> List[str]:
    """只取串口设备名列表。"""
    return [dev for dev, _ in list_serial_ports()]


# ==========================================================
# 链路接口
# ==========================================================

class BaseLink:
    """链路接口：能被 ProtoClient 驱动的「字节管道」。"""

    name = "link"

    @property
    def alive(self) -> bool:
        """链路当前是否可用（可继续 read/write）。"""
        raise NotImplementedError

    def read_any(self, max_bytes: int = 256) -> bytes:
        """
        读取「当前已到达」的字节。

        :param max_bytes: 单次最多读取的字节数
        :return         : 读到的字节；无数据时返回 b""（不应长时间阻塞）
        :raises LinkError: 链路已断开
        """
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        """
        发送字节（应尽量写完）。

        :raises LinkError: 链路已断开或写失败
        """
        raise NotImplementedError

    def close(self) -> None:
        """关闭链路（必须可重复调用）。"""
        raise NotImplementedError


# ==========================================================
# 串口链路
# ==========================================================

class SerialLink(BaseLink):
    """
    基于 pyserial 的串口链路。

    支持 ``open()`` 后由 ProtoClient 反复读写；断开时可通过
    ``reopen()`` 重连（``allow_reopen=True``）。
    """

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE,
                 timeout: float = DEFAULT_SERIAL_KW["timeout"],
                 allow_reopen: bool = True,
                 serial_instance=None) -> None:
        """
        :param port        : 串口名，如 "COM3"
        :param baudrate    : 波特率，默认 115200
        :param timeout     : 单次读超时（秒）
        :param allow_reopen: 断开后是否允许自动重连
        :param serial_instance: 已打开的 pyserial 对象（测试可注入，传入则不再自行打开）
        """
        if not HAVE_PYSERIAL:
            raise LinkError("未安装 pyserial，无法打开串口（pip install pyserial）")
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self.allow_reopen = allow_reopen
        self._ser: Optional["serial.Serial"] = serial_instance   # type: ignore[name-defined]
        self._lock = threading.Lock()
        self._closed = False

    # ---------- 生命周期 ----------

    def open(self) -> None:
        """
        打开串口（若已注入 serial_instance，则视为已打开）。

        :raises LinkError: 打开失败（端口不存在 / 被占用）
        """
        if self._ser is not None:
            self.reset_input()
            return
        kw = dict(DEFAULT_SERIAL_KW)
        kw["timeout"] = self.timeout
        try:
            # serial_for_url：既支持 "COM3" / "/dev/ttyUSB0"，
            # 也支持 "socket://host:port"、"loop://" 这类 URL
            self._ser = serial.serial_for_url(  # type: ignore[union-attr]
                self.port, baudrate=self.baudrate, **kw)
        except Exception as exc:                        # pyserial 异常种类多，统一包一层
            self._ser = None
            raise LinkError(f"打开 {self.port} 失败：{exc}") from exc
        # 上电/连接瞬间的残留字节清掉，避免解析到半个旧帧
        self.reset_input()

    def reopen(self) -> bool:
        """
        重新打开串口（用于断线重连）。

        :return: True = 重连成功
        """
        if not self.allow_reopen or self._closed:
            return False
        try:
            self._close_port()
        except Exception:
            pass
        for _ in range(3):
            try:
                self.open()
                return True
            except LinkError:
                time.sleep(0.2)
        return False

    def reset_input(self) -> None:
        """丢弃输入缓冲区里的残留数据。"""
        ser = self._ser
        if ser is None:
            return
        try:
            ser.reset_input_buffer()
        except Exception:
            pass

    def close(self) -> None:
        """关闭串口（可重复调用）。"""
        self._closed = True
        self._close_port()

    def _close_port(self) -> None:
        """内部：真正关闭底层句柄。"""
        ser, self._ser = self._ser, None
        if ser is None:
            return
        try:
            ser.close()
        except Exception:
            pass

    # ---------- 读写 ----------

    @property
    def alive(self) -> bool:
        """串口是否已打开。"""
        return self._ser is not None and getattr(self._ser, "is_open", False)

    def read_any(self, max_bytes: int = 256) -> bytes:
        """读当前已到达的字节（无数据返回 b""）。"""
        ser = self._ser
        if ser is None:
            raise LinkError("串口未打开")
        try:
            return ser.read(max_bytes)
        except Exception as exc:
            raise LinkError(f"串口读失败：{exc}") from exc

    def write(self, data: bytes) -> None:
        """写字节。"""
        ser = self._ser
        if ser is None:
            raise LinkError("串口未打开")
        try:
            ser.write(data)
        except Exception as exc:
            raise LinkError(f"串口写失败：{exc}") from exc

    def __repr__(self) -> str:
        state = "open" if self.alive else "closed"
        return f"<SerialLink {self.port}@{self.baudrate} {state}>"


# ==========================================================
# TCP 链路（可选：配合 tools/tcp_bridge.py 做无线/远程调试）
# ==========================================================

class TcpLink(BaseLink):
    """基于 socket 的 TCP 链路，用法与 SerialLink 完全一致。"""

    def __init__(self, host: str, port: int, timeout: float = 0.02) -> None:
        """:param host/port: 目标地址；:param timeout: 读超时（秒）"""
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def open(self) -> None:
        """:raises LinkError: 连接失败"""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3.0)
        except OSError as exc:
            raise LinkError(f"连接 {self.host}:{self.port} 失败：{exc}") from exc
        sock.settimeout(self.timeout)
        self._sock = sock

    def close(self) -> None:
        """
        关闭连接。

        必须先 ``shutdown`` 再 ``close``：Windows 上只 close 不保证发出 FIN，
        对端会一直阻塞在 recv 上（表现为「第二次连接没人应答」）。
        """
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def shutdown(self) -> None:
        """只停读写（不发 FIN），用于打断阻塞中的读线程。"""
        sock = self._sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def alive(self) -> bool:
        """连接是否可用。"""
        return self._sock is not None

    def read_any(self, max_bytes: int = 256) -> bytes:
        """读当前已到达的字节。"""
        sock = self._sock
        if sock is None:
            raise LinkError("TCP 未连接")
        try:
            return sock.recv(max_bytes)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise LinkError(f"TCP 读失败：{exc}") from exc

    def write(self, data: bytes) -> None:
        """写字节。"""
        sock = self._sock
        if sock is None:
            raise LinkError("TCP 未连接")
        try:
            sock.sendall(data)
        except OSError as exc:
            raise LinkError(f"TCP 写失败：{exc}") from exc

    def __repr__(self) -> str:
        state = "open" if self.alive else "closed"
        return f"<TcpLink {self.host}:{self.port} {state}>"


# ==========================================================
# 内存仿真链路（单测 / 离线演示）
# ==========================================================

class FakeLink(BaseLink):
    """
    单向内存链路：写入的字节进入 ``tx``，``read_any`` 从 ``rx`` 取。

    ``take_tx`` 供上层取走本端发出的字节。双向通信请用
    :class:`LoopbackLink`。
    """

    def __init__(self) -> None:
        self.tx = bytearray()               # 本端 -> 对端
        self.rx = bytearray()               # 对端 -> 本端
        self._open = True
        self._lock = threading.Lock()

    # 供仿真设备使用
    def feed(self, data: bytes) -> None:
        """把「对端」要发的字节压入 rx。"""
        with self._lock:
            self.rx.extend(data)

    def take_tx(self) -> bytes:
        """取出本端发出的字节（供对端消费）。"""
        with self._lock:
            data = bytes(self.tx)
            self.tx.clear()
            return data

    # BaseLink 接口
    @property
    def alive(self) -> bool:
        """是否处于打开状态。"""
        return self._open

    def read_any(self, max_bytes: int = 256) -> bytes:
        """从 rx 取最多 max_bytes 字节；无数据时短暂让出 CPU。"""
        with self._lock:
            data = bytes(self.rx[:max_bytes])
            del self.rx[:max_bytes]
        if not data:
            time.sleep(0.002)
        return data

    def write(self, data: bytes) -> None:
        """写入 tx。"""
        if not self._open:
            raise LinkError("FakeLink 已关闭")
        with self._lock:
            self.tx.extend(data)

    def close(self) -> None:
        """关闭。"""
        self._open = False


class LoopbackLink(FakeLink):
    """
    双向内存「回环线」：一端显式给上位机，另一端给仿真设备。

    语义等价于一根真实串口线：上位机 ``write`` 的字节 -> 设备读到；
    设备 ``write`` 的字节 -> 上位机读到。两端**互不串扰**——这一点很关键，
    若设备能读到自己的应答，就会形成「收到 ACK -> 不认识 -> 回 NACK ->
    再收到 NACK」的死循环。

    * 上位机侧：直接使用本对象（``read_any`` / ``write``）；
    * 设备侧：用 :meth:`device_side` 取得 ``DeviceView`` 视图。
    """

    def __init__(self) -> None:
        super().__init__()
        self._to_host = bytearray()             # 设备 -> 上位机
        self._to_device = bytearray()           # 上位机 -> 设备
        self._device_view: Optional["_DeviceView"] = None

    # ---- 上位机侧 ----
    def read_any(self, max_bytes: int = 256) -> bytes:
        """上位机读取设备发来的字节。"""
        with self._lock:
            data = bytes(self._to_host[:max_bytes])
            del self._to_host[:max_bytes]
        if not data:
            time.sleep(0.002)                   # 无数据时让出 CPU
        return data

    def write(self, data: bytes) -> None:
        """上位机发送字节给设备。"""
        if not self._open:
            raise LinkError("LoopbackLink 已关闭")
        with self._lock:
            self._to_device.extend(data)

    def feed(self, data: bytes) -> None:
        """设备（或测试代码）把字节交给上位机。"""
        with self._lock:
            self._to_host.extend(data)

    def take_tx(self) -> bytes:
        """取出「上位机发给设备」的字节（供设备视图/测试使用）。"""
        with self._lock:
            data = bytes(self._to_device)
            self._to_device.clear()
            return data

    # ---- 设备侧 ----
    def device_side(self) -> "_DeviceView":
        """取得设备侧视图（多次调用返回同一对象）。"""
        if self._device_view is None:
            self._device_view = _DeviceView(self)
        return self._device_view


class _DeviceView(LoopbackLink):
    """
    回环线的「设备端」视图，供 :class:`tests.fake_device.FakeStm32` 使用。

    与上位机端的收发方向刚好相反，两边共用同一条线的两个缓冲区。
    """

    def __init__(self, pair: LoopbackLink) -> None:
        """:param pair: 上位机侧的 LoopbackLink 对象"""
        super().__init__()
        self._pair = pair

    @property
    def alive(self) -> bool:
        """跟随上位机侧的开关状态。"""
        return self._pair.alive

    def read_any(self, max_bytes: int = 256) -> bytes:
        """设备读取上位机发来的字节。"""
        return self._pair.take_tx()

    def write(self, data: bytes) -> None:
        """设备把应答写回上位机。"""
        self._pair.feed(data)

    def device_side(self) -> "_DeviceView":
        """设备视图自身即设备端。"""
        return self

    def __repr__(self) -> str:
        return "<LoopbackLink 设备端>"


def make_link(target: str, baudrate: int = DEFAULT_BAUDRATE) -> BaseLink:
    """
    按字符串创建链路。

    :param target  : ``"COM3"`` / ``"tcp://192.168.4.1:5000"``
    :param baudrate: 串口波特率（TCP 忽略）
    :return        : BaseLink 实例（尚未打开）
    :raises ValueError: target 格式无法识别
    """
    target = target.strip()
    if target.lower().startswith("tcp://"):
        body = target[6:]
        if ":" not in body:
            raise ValueError(f"TCP 目标应形如 tcp://host:port，实际 {target}")
        host, _, port = body.rpartition(":")
        return TcpLink(host, int(port))
    if target.lower().startswith("serial://"):
        target = target[9:]
    return SerialLink(target, baudrate=baudrate)
