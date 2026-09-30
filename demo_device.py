"""
demo_device.py —— 仿真 STM32 的 TCP 服务端（无硬件演示 / 自测用）

它把 tests/fake_device.py 里的仿真设备挂到一个 TCP 端口上，
于是「上位机软件」可以完全不碰硬件就完整跑一遍：

    # 终端 1：起一个仿真设备，监听 5000
    python demo_device.py --port 5000

    # 终端 2：用上位机软件连它（TCP）
    python cli.py all -p tcp://127.0.0.1:5000
    python cli.py set -p tcp://127.0.0.1:5000 --x 1000 --servo 1
    python cli.py monitor -p tcp://127.0.0.1:5000 -n 5
    python app.py --port tcp://127.0.0.1:5000

真实硬件场景不需要它，直接连 COM 口即可。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "tests"))
sys.path.insert(0, _HERE)

import protocol as P                                        # noqa: E402
from fake_device import FakeStm32                           # noqa: E402
from link import BaseLink                                   # noqa: E402


class AcceptedLink(BaseLink):
    """把一个已连接的 socket 包装成仿真设备可用的链路。"""

    def __init__(self, sock: socket.socket, timeout: float = 0.02) -> None:
        """:param sock: 已连接的 socket；:param timeout: 读超时（秒）"""
        self.sock = sock
        sock.settimeout(timeout)
        self._dead = False

    @property
    def alive(self) -> bool:
        """连接是否仍在（对端断开后读写失败会置为 False）。"""
        if self._dead:
            return False
        try:
            return self.sock.fileno() != -1
        except OSError:
            return False

    def read_any(self, max_bytes: int = 256) -> bytes:
        """读取上位机发来的字节（供仿真设备消费）。"""
        try:
            return self.sock.recv(max_bytes)
        except socket.timeout:
            return b""
        except OSError as exc:
            self._dead = True
            raise IOError(f"socket 读失败：{exc}") from exc

    def write(self, data: bytes) -> None:
        """仿真设备写下的应答，通过 socket 发给上位机。"""
        try:
            self.sock.sendall(data)
        except OSError as exc:
            self._dead = True                          # 对端已断开
            raise IOError(f"socket 写失败：{exc}") from exc

    def close(self) -> None:
        """关闭连接（可重复调用）。"""
        self._dead = True
        try:
            self.sock.close()
        except OSError:
            pass

    def __repr__(self) -> str:
        return f"<AcceptedLink {self.sock.getpeername() if not self._dead else 'closed'}>"

    def close(self) -> None:
        """关闭连接。"""
        try:
            self.sock.close()
        except OSError:
            pass


def serve(host: str, port: int) -> int:
    """
    启动仿真设备 TCP 服务。

    :param host: 绑定地址
    :param port: 绑定端口
    :return    : 进程退出码
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    print(f"仿真 STM32 已监听 tcp://{host}:{port}")
    print("上位机连接示例：python cli.py all -p tcp://%s:%d" % (host, port))
    print("按 Ctrl-C 退出。")

    try:
        while True:
            conn, addr = server.accept()
            print(f"[{time.strftime('%H:%M:%S')}] 上位机接入：{addr}", flush=True)
            link = AcceptedLink(conn)
            device = FakeStm32(link)
            device.on_command = lambda f: print(
                f"  ← {f}   DATA={f.payload_hex() or '—'}", flush=True)
            device.start()
            try:
                # 等对端断开：设备线程结束或链路失效即退出（最多 5 分钟空闲）
                deadline = time.time() + 300
                while device.running and link.alive and time.time() < deadline:
                    time.sleep(0.1)
                if time.time() >= deadline:
                    print("  空闲超时，主动断开", flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:                    # noqa: BLE001
                print(f"  连接处理异常：{exc!r}", flush=True)
            finally:
                device.stop()
                link.close()
            print(f"[{time.strftime('%H:%M:%S')}] 上位机断开：{addr}", flush=True)
    except KeyboardInterrupt:
        print("\n已停止仿真设备", flush=True)
        return 0
    except Exception as exc:                            # noqa: BLE001
        print(f"\n监听异常退出：{exc!r}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.close()
        print("监听已关闭", flush=True)


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="仿真 STM32 协议设备（TCP）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=5000, help="绑定端口，默认 5000")
    args = parser.parse_args()
    return serve(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
