"""
gui.py —— 上位机图形界面（tkinter + pyserial）

对应 STM32 侧 app/protocol/README.md 的二进制帧协议，功能：

* 串口自动扫描 / 刷新，波特率可选；
* 连接、心跳（PING）、重新初始化（REINIT）；
* 设置 X / Y 坐标、旋转角度、舵机、电磁铁；
* 定时轮询 GET ALL，实时显示全部状态（含 homing / reinitPending）；
* 收发日志（带时间戳与十六进制），可手动发帧、可解析任意帧。

线程模型：tkinter 只能在主线程操作，因此串口收发交给 ProtoClient 的后台
线程，界面通过队列 + ``after()`` 取事件，避免任何阻塞。
"""

from __future__ import annotations

import os
import queue
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")
if os.path.isdir(_VENDOR):
    sys.path.insert(0, _VENDOR)

import protocol as P                                                        # noqa: E402
from client import Event, ProtoClient, ProtocolError, ResponseError          # noqa: E402
from link import DEFAULT_BAUDRATE, LinkError, list_serial_ports, make_link   # noqa: E402

# 界面默认值
BAUD_CHOICES = ("9600", "19200", "38400", "57600", "115200", "230400", "460800")
UI_TICK_MS = 60                 # 事件队列轮询周期
LABEL_TICK_MS = 250             # 状态标签刷新周期
MAX_LOG_LINES = 3000            # 日志上限，超出自动截断

# 等宽字体候选：Windows / macOS / 常见 Linux 发行版依次尝试
MONO_CANDIDATES = ("Consolas", "Cascadia Mono", "Menlo", "Monaco",
                   "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono",
                   "Ubuntu Mono", "FreeMono", "Courier New")


def pick_mono_font(root: tk.Misc) -> str:
    """
    挑选本机可用的等宽字体。

    :param root: 任意 Tk 控件（用于查询字体列表）
    :return    : 字体族名；一个都找不到时退回 Tk 的 monospace 族
    """
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except tk.TclError:                         # pragma: no cover - 极端环境
        return "TkFixedFont"
    for name in MONO_CANDIDATES:
        if name.lower() in available:
            return name
    return "TkFixedFont"                        # Tk 内置的等宽逻辑字体


def ensure_utf8_stdout() -> None:
    """
    保证标准输出能打印中文。

    Windows 终端默认代码页可能是 GBK；Linux 在 LANG=C 等场景下 stdout 也可能是
    ASCII，直接 print 中文/符号会抛 UnicodeEncodeError。这里尽量切到 UTF-8，
    失败则退化为「无法编码的字符替换输出」，绝不让打印本身把程序搞崩。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


class HostApp(tk.Tk):
    """上位机主窗口。"""

    def __init__(self, start_timers: bool = True) -> None:
        """
        :param start_timers: 是否启动界面刷新定时器（自动化测试可传 False）
        """
        super().__init__()
        self.title("Puzzle 上位机 —— STM32 串口控制台")
        self.geometry("1120x720")
        self.minsize(940, 600)
        self._mono = pick_mono_font(self)       # 跨平台等宽字体

        self.client: Optional[ProtoClient] = None
        self._events: "queue.Queue[Event]" = queue.Queue()
        self._status: Optional[P.Status] = None
        self._pw_cache = ""                             # 上次显示的电流
        self._pending_poll = False

        # ---- 界面变量 ----
        self.var_port = tk.StringVar()
        self.var_baud = tk.StringVar(value=str(DEFAULT_BAUDRATE))
        self.var_x = tk.StringVar(value="0")
        self.var_y = tk.StringVar(value="0")
        self.var_rot = tk.StringVar(value="0")
        self.var_servo = tk.IntVar(value=0)
        self.var_em = tk.IntVar(value=0)
        self.var_poll = tk.BooleanVar(value=True)
        self.var_poll_ms = tk.StringVar(value="1000")
        self.var_raw_log = tk.BooleanVar(value=False)
        self.var_show_data = tk.BooleanVar(value=False)
        self.var_manual = tk.StringVar()
        self.var_conn = tk.StringVar(value="未连接")
        self.var_stat_x = tk.StringVar(value="—")
        self.var_stat_y = tk.StringVar(value="—")
        self.var_stat_rot = tk.StringVar(value="—")
        self.var_stat_homing = tk.StringVar(value="—")
        self.var_stat_reinit = tk.StringVar(value="—")
        self.var_stat_count = tk.StringVar(value="—")
        self.var_echo = tk.StringVar(value="11 22 33 44")
        self.var_bench = tk.StringVar(value="100")

        self._build_ui()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if start_timers:
            self.after(UI_TICK_MS, self._pump_events)
            self.after(LABEL_TICK_MS, self._refresh_labels)

    # ==================================================
    # 界面构建
    # ==================================================

    def _build_ui(self) -> None:
        """搭建整体布局：左侧控制区 + 右侧日志区。"""
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer)
        left.pack(side="left", fill="y", padx=(0, 8))

        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True)

        self._build_conn_box(left)
        self._build_action_box(left)
        self._build_set_box(left)
        self._build_status_box(left)
        self._build_log_box(right)

    # ---------- 串口 ----------

    def _build_conn_box(self, parent: tk.Widget) -> None:
        """串口连接区。"""
        box = ttk.LabelFrame(parent, text="串口连接", padding=8)
        box.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="端口").pack(side="left")
        self.cmb_port = ttk.Combobox(row, textvariable=self.var_port, width=16)
        self.cmb_port.pack(side="left", padx=4)
        ttk.Button(row, text="刷新", width=6,
                   command=self.refresh_ports).pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="波特率").pack(side="left")
        ttk.Combobox(row2, textvariable=self.var_baud, values=BAUD_CHOICES,
                     width=9, state="readonly").pack(side="left", padx=4)
        self.btn_conn = ttk.Button(row2, text="连接", width=8,
                                   command=self.toggle_connection)
        self.btn_conn.pack(side="left", padx=4)
        self.lbl_conn = ttk.Label(row2, textvariable=self.var_conn, foreground="#b00")
        self.lbl_conn.pack(side="left", padx=4)

        self.lbl_ports = ttk.Label(box, text="", foreground="#666",
                                   wraplength=340, justify="left")
        self.lbl_ports.pack(fill="x", pady=(6, 0))

    # ---------- 通用动作 ----------

    def _build_action_box(self, parent: tk.Widget) -> None:
        """心跳 / 重新初始化 / 查询。"""
        box = ttk.LabelFrame(parent, text="通用", padding=8)
        box.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Button(row, text="PING 心跳", width=11,
                   command=self.on_ping).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="重新初始化", width=11,
                   command=self.on_reinit).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="查询全部", width=11,
                   command=self.on_query_all).pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(row2, text="自动轮询状态", variable=self.var_poll,
                        command=self._on_poll_toggle).pack(side="left")
        ttk.Label(row2, text="间隔(ms)").pack(side="left", padx=(8, 2))
        ent = ttk.Entry(row2, textvariable=self.var_poll_ms, width=7)
        ent.pack(side="left")

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="回环数据").pack(side="left")
        ttk.Entry(row3, textvariable=self.var_echo, width=13).pack(side="left", padx=4)
        ttk.Button(row3, text="ECHO 测试", width=10,
                   command=self.on_echo).pack(side="left")
        ttk.Button(row3, text="压力测试", width=9,
                   command=self.on_bench).pack(side="left", padx=4)
        ttk.Entry(row3, textvariable=self.var_bench, width=5).pack(side="left")

    # ---------- 设置 ----------

    def _build_set_box(self, parent: tk.Widget) -> None:
        """坐标 / 姿态 / 执行器设置区。"""
        box = ttk.LabelFrame(parent, text="设置（SET 命令）", padding=8)
        box.pack(fill="x", pady=(0, 8))

        def entry_row(label: str, var: tk.StringVar, unit: str = "") -> ttk.Entry:
            """生成一行「标签 + 输入框 + 单位」。"""
            row = ttk.Frame(box)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=11).pack(side="left")
            ent = ttk.Entry(row, textvariable=var, width=13)
            ent.pack(side="left")
            if unit:
                ttk.Label(row, text=unit, foreground="#666").pack(side="left", padx=4)
            return ent

        entry_row("X 坐标", self.var_x, "脉冲 (uint32)")
        entry_row("Y 坐标", self.var_y, "脉冲 (uint32)")
        entry_row("旋转角度", self.var_rot, "度 (int16)")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(row, text="舵机开", variable=self.var_servo,
                        command=self.on_servo).pack(side="left")
        ttk.Checkbutton(row, text="电磁铁吸合", variable=self.var_em,
                        command=self.on_em).pack(side="left", padx=10)

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="下发 X / Y / 旋转", width=18,
                   command=self.on_send_motion).pack(side="left")
        ttk.Button(row, text="下发全部设置", width=14,
                   command=self.on_send_all).pack(side="left", padx=4)

    # ---------- 状态 ----------

    def _build_status_box(self, parent: tk.Widget) -> None:
        """实时状态面板。"""
        box = ttk.LabelFrame(parent, text="实时状态（GET ALL）", padding=8)
        box.pack(fill="x", pady=(0, 8))

        grid = ttk.Frame(box)
        grid.pack(fill="x")

        def item(row: int, col: int, label: str, var: tk.StringVar) -> None:
            """往状态网格里塞一个「名称 + 值」。"""
            ttk.Label(grid, text=label, foreground="#666").grid(
                row=row, column=col * 2, sticky="w", padx=(0, 4), pady=1)
            ttk.Label(grid, textvariable=var, font=(self._mono, 10, "bold")).grid(
                row=row, column=col * 2 + 1, sticky="w", padx=(0, 14), pady=1)

        item(0, 0, "X", self.var_stat_x)
        item(0, 1, "Y", self.var_stat_y)
        item(1, 0, "旋转", self.var_stat_rot)
        item(1, 1, "找零中", self.var_stat_homing)

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="舵机", foreground="#666").pack(side="left")
        self.lbl_servo = ttk.Label(row, text="关", width=6, font=(self._mono, 10, "bold"))
        self.lbl_servo.pack(side="left", padx=(2, 10))
        ttk.Label(row, text="电磁铁", foreground="#666").pack(side="left")
        self.lbl_em = ttk.Label(row, text="释放", width=6, font=(self._mono, 10, "bold"))
        self.lbl_em.pack(side="left", padx=(2, 10))
        ttk.Label(row, text="待重初始化", foreground="#666").pack(side="left")
        self.lbl_reinit = ttk.Label(row, text="—", width=4,
                                    font=(self._mono, 10, "bold"))
        self.lbl_reinit.pack(side="left", padx=2)

        ttk.Label(box, textvariable=self.var_stat_count,
                  foreground="#666").pack(fill="x", pady=(6, 0))

    # ---------- 日志 ----------

    def _build_log_box(self, parent: tk.Widget) -> None:
        """右侧日志区 + 手动发帧。"""
        box = ttk.LabelFrame(parent, text="收发日志", padding=8)
        box.pack(fill="both", expand=True)

        top = ttk.Frame(box)
        top.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(top, text="显示原始字节（raw）",
                        variable=self.var_raw_log).pack(side="left")
        ttk.Checkbutton(top, text="显示状态数据帧（0x90~0x95）",
                        variable=self.var_show_data).pack(side="left", padx=8)
        ttk.Button(top, text="清空日志", command=self.clear_log).pack(side="right")
        ttk.Button(top, text="解析该帧", command=self.on_parse_frame).pack(
            side="right", padx=4)

        self.txt = scrolledtext.ScrolledText(box, height=18, wrap="none",
                                             font=(self._mono, 9),
                                             state="disabled", background="#12161c",
                                             foreground="#d7dde5")
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_config("tx", foreground="#7fd1ff")
        self.txt.tag_config("rx", foreground="#9ee493")
        self.txt.tag_config("err", foreground="#ff8a80")
        self.txt.tag_config("info", foreground="#c9a227")
        self.txt.tag_config("dim", foreground="#8b97a6")
        self.txt.tag_config("raw", foreground="#6f7c8a")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="手动帧").pack(side="left")
        ent = ttk.Entry(row, textvariable=self.var_manual, font=(self._mono, 9))
        ent.pack(side="left", fill="x", expand=True, padx=4)
        ent.bind("<Return>", lambda _e: self.on_send_manual())
        ttk.Button(row, text="发送", width=6,
                   command=self.on_send_manual).pack(side="left")
        ttk.Button(row, text="不等待应答", width=11,
                   command=lambda: self.on_send_manual(wait_reply=False)).pack(
            side="left", padx=4)
        ttk.Label(box, text="手动帧填完整帧（AA 55 …），或只填负载后由程序补帧头帧尾；"
                            "「解析该帧」只解析不发送。",
                  foreground="#666", wraplength=620, justify="left").pack(
            fill="x", pady=(4, 0))

    # ==================================================
    # 日志
    # ==================================================

    def log(self, text: str, tag: str = "info") -> None:
        """向日志区追加一行。"""
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag)
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.txt.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def clear_log(self) -> None:
        """清空日志。"""
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    # ==================================================
    # 连接管理
    # ==================================================

    def refresh_ports(self) -> None:
        """重新扫描串口并刷新下拉框。"""
        ports = list_serial_ports()
        names = [dev for dev, _ in ports]
        self.cmb_port["values"] = names
        if names and self.var_port.get() not in names:
            self.var_port.set(names[0])
        # 只显示「端口 = 简短描述」，最多几条，避免长 hwid 把左侧撑爆
        if ports:
            shown = [f"{dev} = {self._short_desc(desc)}" for dev, desc in ports[:4]]
            text = "发现：" + "；".join(shown)
            if len(ports) > 4:
                text += f"；…另外 {len(ports) - 4} 个"
            self.lbl_ports.configure(text=text)
        else:
            self.lbl_ports.configure(text="未发现串口：检查 USB 转串口驱动或设备连接")

    @staticmethod
    def _short_desc(desc: str) -> str:
        """截断过长的串口描述（去掉 VID/PID 明细）。"""
        head = desc.split("[")[0].strip()
        if len(head) > 28:
            head = head[:28] + "…"
        return head or "未知设备"

    def prompt_connect(self, port: str) -> None:
        """
        带确认的自动连接（``app.py --port`` 用）。

        连接后软件会立刻下发 PING 与 GET ALL；若端口上挂的是真实设备，
        未经确认就发指令存在让机构动作的风险，因此这里先问一句。
        """
        if messagebox.askyesno(
                "确认连接",
                f"即将连接 {port} 并发送 PING / 状态查询命令。\n\n"
                "如果该端口接的是真实设备，请先确认机械空间安全。\n是否继续？",
                parent=self):
            self.connect()
        else:
            self.log(f"已取消自动连接 {port}；可手动点「连接」", "info")

    def toggle_connection(self) -> None:
        """连接 / 断开。"""
        if self.client is not None:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        """打开串口并启动客户端。"""
        port = self.var_port.get().strip()
        if not port:
            messagebox.showwarning("提示", "请先选择串口（可点「刷新」重新扫描）", parent=self)
            return
        try:
            baud = int(self.var_baud.get())
        except ValueError:
            messagebox.showwarning("提示", "波特率必须是整数", parent=self)
            return

        try:
            link = make_link(port, baud)
            link.open()
        except (LinkError, ValueError) as exc:
            messagebox.showerror("连接失败", str(exc), parent=self)
            self.log(f"连接 {port} 失败：{exc}", "err")
            return

        self.client = ProtoClient(link, on_event=self._events.put, timeout=0.3)
        self.client.start()
        self._status = None
        self._pending_poll = False

        self.btn_conn.configure(text="断开")
        self.var_conn.set(f"{port} @ {baud} 已连接")
        self.lbl_conn.configure(foreground="#0a0")
        self.log(f"已连接 {port} @ {baud} 8N1，帧格式 AA 55 CMD LEN DATA XOR 0D", "info")

        # 连上先握手 + 取一次完整状态
        self.after(50, self.on_ping)
        self.after(250, self.on_query_all)
        self._schedule_poll()

    def disconnect(self) -> None:
        """关闭串口。"""
        client, self.client = self.client, None
        if client is not None:
            client.close()
        self.btn_conn.configure(text="连接")
        self.var_conn.set("未连接")
        self.lbl_conn.configure(foreground="#b00")
        self.var_stat_count.set("—")
        self.log("已断开连接", "info")

    def on_close(self) -> None:
        """窗口关闭。"""
        try:
            if self.client is not None:
                self.client.close()
        finally:
            self.destroy()

    def _require(self) -> Optional[ProtoClient]:
        """取当前客户端；未连接则提示并返回 None。"""
        if self.client is None:
            messagebox.showinfo("提示", "请先连接串口", parent=self)
            return None
        return self.client

    # ==================================================
    # 事件处理
    # ==================================================

    def _pump_events(self) -> None:
        """定时把后台线程的事件搬到界面上（tkinter 只能在主线程操作控件）。"""
        try:
            while True:
                event = self._events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(UI_TICK_MS, self._pump_events)

    @staticmethod
    def _is_status_frame(event: Event) -> bool:
        """该事件是否为状态数据帧（0x90~0x95）。"""
        frame = event.frame
        return frame is not None and P.RSP_ALL <= frame.cmd <= P.RSP_E

    def _handle_event(self, event: Event) -> None:
        """处理一条客户端事件。"""
        if event.kind == "raw":
            if self.var_raw_log.get():
                self.log(event.text(), "raw")
            return
        if event.kind == "tx":
            self.log(event.text(), "tx")
        elif event.kind == "rx":
            if not self.var_show_data.get() and self._is_status_frame(event):
                return                      # 轮询回来的状态数据帧，默认不刷屏
            self.log(event.text(), "rx")
        elif event.kind == "status":
            self._status = event.status
        elif event.kind == "echo":
            self.log(event.text(), "rx")
        elif event.kind == "conn":
            self.log(event.text(), "info")
        elif event.kind == "error":
            self.log(event.text(), "err")
            if self.client is not None and not self.client.alive:
                self._on_link_lost()

    def _on_link_lost(self) -> None:
        """链路断开后的界面收尾（只做一次）。"""
        if self.client is None:
            return
        self.log("链路不可用，已停止轮询。请检查设备后重新连接。", "err")
        self.client.close()
        self.client = None
        self.btn_conn.configure(text="连接")
        self.var_conn.set("连接已断开")
        self.lbl_conn.configure(foreground="#b00")

    # ==================================================
    # 状态轮询与显示
    # ==================================================

    def _on_poll_toggle(self) -> None:
        """自动轮询开关。"""
        if self.var_poll.get():
            self._schedule_poll()

    def _poll_interval_ms(self) -> int:
        """轮询间隔（不小于单次请求超时，避免请求堆积）。"""
        try:
            value = max(100, int(self.var_poll_ms.get()))
        except ValueError:
            value = 1000
        return max(value, 500)

    def _schedule_poll(self) -> None:
        """安排下一次轮询。"""
        self.after(self._poll_interval_ms(), self._poll_once)

    def _poll_once(self) -> None:
        """执行一次 GET ALL 轮询。"""
        if self.client is None or not self.var_poll.get():
            return
        if self._pending_poll:
            return                                  # 上一次还没回来，跳过本轮
        self._pending_poll = True
        try:
            self.client.get_all()
        except (ProtocolError, LinkError) as exc:
            self.log(f"轮询失败：{exc}", "err")
        finally:
            self._pending_poll = False
            self._schedule_poll()

    def _refresh_labels(self) -> None:
        """按最新状态刷新界面标签。"""
        status = self._status
        if status is not None:
            self.var_stat_x.set(f"{status.pos_x}  (0x{status.pos_x:08X})")
            self.var_stat_y.set(f"{status.pos_y}  (0x{status.pos_y:08X})")
            self.var_stat_rot.set(f"{status.pos_rot}°")
            self.var_stat_homing.set("是" if status.homing else "否")
            self.lbl_servo.configure(text="开" if status.servo else "关",
                                     foreground="#0a0" if status.servo else "#888")
            self.lbl_em.configure(text="吸合" if status.electromagnet else "释放",
                                  foreground="#c00" if status.electromagnet else "#888")
            self.lbl_reinit.configure(text="是" if status.reinit_pending else "否",
                                      foreground="#c60" if status.reinit_pending else "#888")
        if self.client is not None:
            s = self.client.stats
            self.var_stat_count.set(
                f"已发 {s['tx_frames']} 帧 / 已收 {s['rx_frames']} 帧；"
                f"超时 {s['timeouts']}，NACK {s['nacks']}")
        self.after(LABEL_TICK_MS, self._refresh_labels)

    # ==================================================
    # 按钮回调
    # ==================================================

    def on_ping(self) -> None:
        """心跳测试。"""
        client = self._require()
        if client is None:
            return
        t0 = time.perf_counter()
        try:
            ok = client.ping()
            ms = (time.perf_counter() - t0) * 1000
            self.log(f"PING → {'ACK ✔' if ok else 'NACK ✘'}（{ms:.0f} ms）",
                     "rx" if ok else "err")
        except ProtocolError as exc:
            self.log(f"PING 失败：{exc}", "err")

    def on_reinit(self) -> None:
        """触发重新初始化。"""
        client = self._require()
        if client is None:
            return
        if not messagebox.askyesno("确认", "确定触发重新初始化（找零）吗？\n"
                                            "机器会开始回零运动。", parent=self):
            return
        try:
            ok = client.reinit()
            self.log(f"REINIT → {'ACK ✔ 已请求找零' if ok else 'NACK ✘'}",
                     "rx" if ok else "err")
        except ProtocolError as exc:
            self.log(f"REINIT 失败：{exc}", "err")

    def on_query_all(self) -> None:
        """主动查询一次全部状态。"""
        client = self._require()
        if client is None:
            return
        try:
            status = client.get_all()
            self._status = status
            self.log("超时（无响应）" if status is None else f"状态：{status}",
                     "err" if status is None else "rx")
        except ProtocolError as exc:
            self.log(f"查询失败：{exc}", "err")

    def _read_field(self, name: str, var: tk.StringVar,
                    low: int, high: int) -> Optional[int]:
        """读取并校验一个数值输入框。"""
        try:
            value = P.check_range(name, P.parse_int(var.get()), low, high)
            var.set(str(value))
            return value
        except ValueError as exc:
            messagebox.showwarning("输入错误", str(exc), parent=self)
            return None

    def _send_motion(self, client: ProtoClient) -> bool:
        """下发 X / Y / 旋转。"""
        x = self._read_field("X", self.var_x, P.X_MIN, P.X_MAX)
        if x is None:
            return False
        y = self._read_field("Y", self.var_y, P.Y_MIN, P.Y_MAX)
        if y is None:
            return False
        rot = self._read_field("旋转角度", self.var_rot, P.ROT_MIN, P.ROT_MAX)
        if rot is None:
            return False
        try:
            for label, func, value in (("X", client.set_x, x),
                                       ("Y", client.set_y, y),
                                       ("旋转", client.set_rot, rot)):
                ok = func(value)
                self.log(f"SET {label}={value} → {'ACK ✔' if ok else 'NACK ✘'}",
                         "rx" if ok else "err")
            return True
        except ProtocolError as exc:
            self.log(f"下发失败：{exc}", "err")
            return False

    def on_send_motion(self) -> None:
        """按钮：下发坐标与姿态。"""
        client = self._require()
        if client is not None:
            self._send_motion(client)
            self.on_query_all()

    def on_send_all(self) -> None:
        """按钮：下发坐标、姿态、舵机、电磁铁全部设置。"""
        client = self._require()
        if client is None:
            return
        if not self._send_motion(client):
            return
        self._send_actuators(client)

    def _send_actuators(self, client: ProtoClient) -> None:
        """下发舵机与电磁铁状态。"""
        try:
            servo = bool(self.var_servo.get())
            em = bool(self.var_em.get())
            ok_s = client.set_servo(servo)
            self.log(f"SET SERVO={1 if servo else 0} → {'ACK ✔' if ok_s else 'NACK ✘'}",
                     "rx" if ok_s else "err")
            ok_e = client.set_em(em)
            self.log(f"SET EM={1 if em else 0} → {'ACK ✔' if ok_e else 'NACK ✘'}",
                     "rx" if ok_e else "err")
        except ProtocolError as exc:
            self.log(f"下发执行器失败：{exc}", "err")

    def on_servo(self) -> None:
        """舵机开关勾选后立即下发。"""
        client = self._require()
        if client is None:
            self.var_servo.set(0 if self.var_servo.get() else 1)
            return
        try:
            on = bool(self.var_servo.get())
            ok = client.set_servo(on)
            self.log(f"SET SERVO={1 if on else 0} → {'ACK ✔' if ok else 'NACK ✘'}",
                     "rx" if ok else "err")
        except ProtocolError as exc:
            self.log(f"舵机设置失败：{exc}", "err")

    def on_em(self) -> None:
        """电磁铁开关勾选后立即下发。"""
        client = self._require()
        if client is None:
            self.var_em.set(0 if self.var_em.get() else 1)
            return
        try:
            on = bool(self.var_em.get())
            ok = client.set_em(on)
            self.log(f"SET EM={1 if on else 0} → {'ACK ✔' if ok else 'NACK ✘'}",
                     "rx" if ok else "err")
        except ProtocolError as exc:
            self.log(f"电磁铁设置失败：{exc}", "err")

    def on_echo(self) -> None:
        """回环测试。"""
        client = self._require()
        if client is None:
            return
        try:
            payload = P.parse_hex_bytes(self.var_echo.get())
        except ValueError as exc:
            messagebox.showwarning("输入错误", str(exc), parent=self)
            return
        try:
            got = client.echo(payload)
        except ProtocolError as exc:
            self.log(f"ECHO 失败：{exc}", "err")
            return
        if got is None:
            self.log("ECHO 超时（无响应）", "err")
        elif got == payload:
            self.log(f"ECHO 回环一致 ✔ {P.to_hex(payload)}", "rx")
        else:
            self.log(f"ECHO 不一致 ✘ 发送 {P.to_hex(payload)}，"
                     f"回显 {P.to_hex(got)}", "err")

    def on_bench(self) -> None:
        """PING 压力测试（会短暂阻塞界面）。"""
        client = self._require()
        if client is None:
            return
        try:
            count = max(1, int(self.var_bench.get()))
        except ValueError:
            messagebox.showwarning("输入错误", "压力测试次数必须是整数", parent=self)
            return
        self.update_idletasks()
        result = client.ping_bench(count)
        if result["ok"]:
            self.log(f"压力测试：发 {result['sent']}，成功 {result['ok']}，"
                     f"丢失 {result['lost']}；时延 min/avg/max = "
                     f"{result['min_ms']}/{result['avg_ms']}/{result['max_ms']} ms",
                     "rx" if result["lost"] == 0 else "err")
        else:
            self.log(f"压力测试：全部失败（{result['sent']} 次无响应）", "err")

    def on_send_manual(self, wait_reply: bool = True) -> None:
        """手动发帧：完整帧或纯负载。"""
        client = self._require()
        if client is None:
            return
        text = self.var_manual.get().strip()
        try:
            raw = P.parse_hex_bytes(text)
        except ValueError as exc:
            messagebox.showwarning("输入错误", str(exc), parent=self)
            return
        try:
            reply = client.send_raw(raw, expect_reply=wait_reply)
        except (ProtocolError, LinkError, ValueError) as exc:
            self.log(f"手动发帧失败：{exc}", "err")
            return
        if not wait_reply:
            self.log(f"已发送（不等待应答）：{P.to_hex(raw)}", "tx")
        elif reply is None:
            self.log(f"无响应：{P.to_hex(raw)}", "err")
        else:
            self.log(f"响应：{reply}  [{reply.hex()}]", "rx")

    def on_parse_frame(self) -> None:
        """只解析输入框里的帧，不发送（离线核对字节）。"""
        try:
            raw = P.parse_hex_bytes(self.var_manual.get())
        except ValueError as exc:
            messagebox.showwarning("输入错误", str(exc), parent=self)
            return
        has_head = raw[:2] == bytes([P.HEAD1, P.HEAD2])
        try:
            frame = P.parse_bytes(raw) if has_head else P.parse_frame(raw)
        except (ValueError, P.ChecksumError, P.TailError) as exc:
            self.log(f"解析失败：{exc}", "err")
            return
        note = ""
        if not has_head:
            note = "（未含帧头，按 CMD LEN ... 解析）"
        self.log(f"解析：{frame}  DATA={frame.payload_hex() or '—'} {note}", "info")


def main() -> int:
    """GUI 入口。"""
    ensure_utf8_stdout()
    try:
        app = HostApp()
    except tk.TclError as exc:                  # pragma: no cover - 取决于环境
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        print("Linux 上常见原因是缺少 Tk 或没有显示环境：", file=sys.stderr)
        print("  sudo apt install python3-tk       # 安装 Tk", file=sys.stderr)
        print("  无桌面 / SSH 场景请改用：python3 cli.py --help", file=sys.stderr)
        return 1
    except ImportError as exc:                  # pragma: no cover - 取决于环境
        print(f"缺少图形界面依赖：{exc}", file=sys.stderr)
        print("可改用命令行版本：python cli.py --help", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
