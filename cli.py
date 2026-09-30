"""
cli.py —— 命令行上位机（无 GUI 环境 / 脚本化测试用）

用法示例::

    python cli.py ports                          # 列出串口
    python cli.py ping    -p COM3                # 心跳测试
    python cli.py all     -p COM3                # 查询全部状态
    python cli.py set     -p COM3 --x 1000 --y 2000 --rot -90 --servo 1 --em 0
    python cli.py reinit  -p COM3                # 触发重新初始化
    python cli.py echo    -p COM3 --data "11 22 33"
    python cli.py monitor -p COM3 -n 20 -i 0.5   # 轮询 20 次
    python cli.py shell   -p COM3                # 交互式命令行
    python cli.py bench   -p COM3 -n 200         # PING 压力测试
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

# 打包的第三方依赖目录（pyserial），存在则优先使用
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")
if os.path.isdir(_VENDOR):
    sys.path.insert(0, _VENDOR)

import protocol as P                                            # noqa: E402
from client import Event, ProtoClient, ProtocolError, ResponseError, ResponseTimeout  # noqa: E402
from link import (DEFAULT_BAUDRATE, LinkError, SerialLink, list_serial_ports,  # noqa: E402
                  make_link)

EPILOG = """
常用命令帧速查（十六进制）:
  PING         AA 55 07 00 07 0D
  GET ALL      AA 55 10 00 10 0D
  SET X=1000   AA 55 01 04 E8 03 00 00 EE 0D
  SET R=-90    AA 55 03 02 A6 FF 4C 0D
  REINIT       AA 55 06 00 06 0D

Linux 用法示例（串口通常是 /dev/ttyUSB0 或 /dev/ttyACM0）:
  python3 cli.py ports
  python3 cli.py ping -p /dev/ttyUSB0
  sudo usermod -aG dialout $USER    # 若无权限访问串口，加入 dialout 后重新登录
"""


def ensure_utf8_stdout() -> None:
    """
    保证标准输出能打印中文。

    Linux 在 LANG=C / POSIX locale 下 stdout 编码可能是 ASCII，
    直接 print 中文或 ✔ 会抛 UnicodeEncodeError；这里尽量切到 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


ensure_utf8_stdout()


# ==========================================================
# 打印辅助
# ==========================================================

def print_event(event: Event) -> None:
    """把客户端事件打到标准输出 / 标准错误。"""
    if event.kind == "raw":
        return                                  # CLI 默认不刷原始字节
    if event.kind in ("error",):
        print(event.text(), file=sys.stderr)
    else:
        print(event.text())


def print_ports() -> int:
    """列出本机串口。"""
    ports = list_serial_ports()
    if not ports:
        print("未发现可用串口（检查驱动 / 是否已插入设备）")
        return 1
    print(f"发现 {len(ports)} 个串口：")
    for dev, desc in ports:
        print(f"  {dev:<10} {desc}")
    return 0


def open_client(args) -> ProtoClient:
    """
    按命令行参数打开链路并启动客户端。

    :raises SystemExit: 打开失败
    """
    try:
        link = make_link(args.port, args.baud)
        link.open()
    except (LinkError, ValueError) as exc:
        print(f"打开 {args.port} 失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
    client = ProtoClient(link, on_event=None if args.quiet else print_event,
                         timeout=args.timeout)
    client.start()
    print(f"已连接 {args.port} @ {args.baud} 8N1")
    return client


# ==========================================================
# 各子命令
# ==========================================================

def cmd_ping(args) -> int:
    """心跳测试。"""
    client = open_client(args)
    try:
        t0 = time.perf_counter()
        ok = client.ping()
        ms = (time.perf_counter() - t0) * 1000
        print(f"PING → {'ACK' if ok else 'NACK'}，往返 {ms:.1f} ms")
        return 0 if ok else 1
    except ProtocolError as exc:
        print(f"PING 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def cmd_all(args) -> int:
    """查询全部状态。"""
    client = open_client(args)
    try:
        status = client.get_all()
        if status is None:
            print("GET ALL 超时", file=sys.stderr)
            return 1
        for key, value in status.as_dict().items():
            print(f"  {key:<14} {value}")
        return 0
    except ProtocolError as exc:
        print(f"GET ALL 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def cmd_set(args) -> int:
    """批量下发设置类命令。"""
    client = open_client(args)
    rc = 0
    try:
        jobs = []
        if args.x is not None:
            jobs.append(("X", client.set_x, args.x))
        if args.y is not None:
            jobs.append(("Y", client.set_y, args.y))
        if args.rot is not None:
            jobs.append(("旋转", client.set_rot, args.rot))
        if args.servo is not None:
            jobs.append(("舵机", client.set_servo, bool(args.servo)))
        if args.em is not None:
            jobs.append(("电磁铁", client.set_em, bool(args.em)))
        if not jobs:
            print("没有指定任何设置项（--x/--y/--rot/--servo/--em）", file=sys.stderr)
            return 2
        for name, func, value in jobs:
            try:
                ok = func(value)
                print(f"SET {name}={value} → {'ACK' if ok else 'NACK'}")
                rc |= 0 if ok else 1
            except (ProtocolError, LinkError, ValueError) as exc:
                print(f"SET {name} 失败：{exc}", file=sys.stderr)
                rc |= 1
        return rc
    finally:
        client.close()


def cmd_reinit(args) -> int:
    """触发重新初始化（找零）。"""
    client = open_client(args)
    try:
        ok = client.reinit()
        print(f"REINIT → {'ACK' if ok else 'NACK'}")
        return 0 if ok else 1
    except ProtocolError as exc:
        print(f"REINIT 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def cmd_echo(args) -> int:
    """回环测试。"""
    client = open_client(args)
    try:
        payload = P.parse_hex_bytes(args.data)
        got = client.echo(payload)
        if got is None:
            print("ECHO 超时", file=sys.stderr)
            return 1
        print(f"发送 {P.to_hex(payload)}")
        print(f"回显 {P.to_hex(got)}")
        if got != payload:
            print("回显内容不一致！", file=sys.stderr)
            return 1
        print("回环一致 ✔")
        return 0
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    finally:
        client.close()


def cmd_monitor(args) -> int:
    """周期性轮询状态。"""
    client = open_client(args)
    rc = 0
    try:
        count = 0
        while args.count == 0 or count < args.count:
            t0 = time.perf_counter()
            try:
                status = client.get_all()
                error = ""
            except (ProtocolError, LinkError) as exc:
                status, error = None, str(exc)
            ms = (time.perf_counter() - t0) * 1000
            stamp = time.strftime("%H:%M:%S")
            if status is None:
                print(f"[{stamp}] 失败：{error or '超时'}", file=sys.stderr)
                rc |= 1
                if not client.alive:
                    print("链路已断开，停止轮询", file=sys.stderr)
                    break
            else:
                print(f"[{stamp}] {status}   ({ms:.0f} ms)")
            count += 1
            if args.count == 0 or count < args.count:
                time.sleep(max(0.0, args.interval))
        return rc
    finally:
        client.close()


def cmd_bench(args) -> int:
    """PING 压力测试。"""
    client = open_client(args)
    try:
        print(f"连续 PING {args.count} 次 …")
        result = client.ping_bench(args.count, timeout=args.timeout)
        print(f"  发送 {result['sent']}，成功 {result['ok']}，丢失 {result['lost']}")
        if result["ok"]:
            print(f"  往返时延 min/avg/max = "
                  f"{result['min_ms']} / {result['avg_ms']} / {result['max_ms']} ms")
        return 0 if result["lost"] == 0 else 1
    finally:
        client.close()


# ==========================================================
# 交互式 shell
# ==========================================================

SHELL_HELP = """
可用命令：
  ping                     心跳测试
  all                      查询全部状态
  x                        查询 X
  y                        查询 Y
  r                        查询旋转
  s                        查询舵机
  e                        查询电磁铁
  x <值>                   设置 X（支持 0x 前缀）
  y <值>                   设置 Y
  r <值>                   设置旋转（-180 ~ 180）
  servo <0|1>              舵机开/关
  em <0|1>                 电磁铁开/关
  reinit                   重新初始化（找零）
  echo <十六进制字节>      回环测试，如 echo 11 22 33
  raw <十六进制帧>         直接发原始帧，如 raw AA 55 07 00 07 0D
  hex <十六进制帧>         解析并打印一帧（不发送）
  watch                    打印一次状态（同 all）
  help                     显示本帮助
  quit / exit              退出
"""


def _shell_send(client: ProtoClient, text: str) -> None:
    """执行一条交互命令。"""
    parts = text.split()
    op = parts[0].lower()
    arg = parts[1:] if len(parts) > 1 else []

    if op == "ping":
        print("ACK ✔" if client.ping() else "NACK ✘")

    elif op == "all":
        status = client.get_all()
        print("超时 ✘" if status is None else f"{status}")

    elif op in ("x", "y", "r", "s", "e") and not arg:
        value = {"x": client.get_x, "y": client.get_y, "r": client.get_rot,
                 "s": client.get_servo, "e": client.get_em}[op]()
        print("超时 ✘" if value is None else f"{op.upper()} = {value}")

    elif op in ("x", "y") and arg:
        print("ACK ✔" if {"x": client.set_x, "y": client.set_y}[op](P.parse_int(arg[0])) else "NACK ✘")

    elif op == "r" and arg:
        print("ACK ✔" if client.set_rot(P.parse_int(arg[0])) else "NACK ✘")

    elif op in ("servo", "em") and arg:
        on = P.parse_int(arg[0]) != 0
        func = client.set_servo if op == "servo" else client.set_em
        print("ACK ✔" if func(on) else "NACK ✘")

    elif op == "reinit":
        print("ACK ✔（已请求重新初始化）" if client.reinit() else "NACK ✘")

    elif op == "echo":
        payload = P.parse_hex_bytes(" ".join(arg))
        got = client.echo(payload)
        print("超时 ✘" if got is None else
              f"回显 {P.to_hex(got)}" + ("  ✔ 一致" if got == payload else "  ✘ 不一致"))

    elif op == "raw":
        raw = P.parse_hex_bytes(" ".join(arg))
        reply = client.send_raw(raw)
        print("超时 ✘（无响应）" if reply is None else f"← {reply}  [{reply.hex()}]")

    elif op == "hex":
        frame = P.parse_bytes(P.parse_hex_bytes(" ".join(arg)))
        print(f"{frame}  DATA={frame.payload_hex() or '—'}")

    elif op in ("watch",):
        status = client.get_all()
        print("超时 ✘" if status is None else f"{status}")

    elif op in ("help", "?"):
        print(SHELL_HELP)

    elif op in ("quit", "exit", "q"):
        raise EOFError

    else:
        print(f"未知命令：{op}（输入 help 查看帮助）")


def cmd_shell(args) -> int:
    """交互式命令行。"""
    client = open_client(args)
    print(SHELL_HELP)
    try:
        while True:
            try:
                text = input("puzzle> ").strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("\n（Ctrl-C 退出，或输入 quit）")
                continue
            if not text:
                continue
            try:
                _shell_send(client, text)
            except EOFError:
                break
            except (ProtocolError, LinkError, ValueError) as exc:
                print(f"错误：{exc}", file=sys.stderr)
        return 0
    finally:
        client.close()


# ==========================================================
# 入口
# ==========================================================

def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="puzzle 上位机命令行工具（对应 app/protocol/README.md 的二进制帧协议）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        """给子命令加上串口公共参数。"""
        p.add_argument("-p", "--port", required=True,
                       help='串口名（如 COM3）或 "tcp://host:port"')
        p.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUDRATE,
                       help=f"波特率，默认 {DEFAULT_BAUDRATE}")
        p.add_argument("-t", "--timeout", type=float, default=0.3,
                       help="单次请求等待响应的秒数，默认 0.3")
        p.add_argument("-q", "--quiet", action="store_true", help="不打印事件日志")

    sub.add_parser("ports", help="列出本机串口").set_defaults(func=lambda a: print_ports())

    p = sub.add_parser("ping", help="心跳测试"); add_common(p)
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("all", help="查询全部状态"); add_common(p)
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("set", help="下发设置类命令"); add_common(p)
    p.add_argument("--x", type=int, help="X 轴坐标（uint32）")
    p.add_argument("--y", type=int, help="Y 轴坐标（uint32）")
    p.add_argument("--rot", type=int, help="旋转角度（int16，-180~180）")
    p.add_argument("--servo", type=int, choices=(0, 1), help="舵机 0/1")
    p.add_argument("--em", type=int, choices=(0, 1), help="电磁铁 0/1")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("reinit", help="触发重新初始化（找零）"); add_common(p)
    p.set_defaults(func=cmd_reinit)

    p = sub.add_parser("echo", help="回环测试"); add_common(p)
    p.add_argument("-d", "--data", required=True, help='要回环的十六进制字节，如 "11 22 33"')
    p.set_defaults(func=cmd_echo)

    p = sub.add_parser("monitor", help="周期性轮询状态"); add_common(p)
    p.add_argument("-n", "--count", type=int, default=0, help="轮询次数，0 = 一直轮询")
    p.add_argument("-i", "--interval", type=float, default=0.5, help="轮询间隔秒，默认 0.5")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("bench", help="PING 压力测试"); add_common(p)
    p.add_argument("-n", "--count", type=int, default=100, help="次数，默认 100")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("shell", help="交互式命令行"); add_common(p)
    p.set_defaults(func=cmd_shell)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
