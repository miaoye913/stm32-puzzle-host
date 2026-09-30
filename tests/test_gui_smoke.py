"""
test_gui_smoke.py —— 图形界面冒烟测试

不连硬件、不弹窗，只验证界面能创建、控件回调不抛异常、
状态刷新与手动解析路径可用。需要能创建 tkinter 窗口；
无显示环境（如纯 SSH）会自动跳过。
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "vendor"))

import protocol as P                                        # noqa: E402


def _can_create_window() -> bool:
    """探测当前环境能否创建 tkinter 窗口。"""
    try:
        root = tk.Tk()
    except Exception:                                       # noqa: BLE001
        return False
    root.destroy()
    return True


@unittest.skipUnless(_can_create_window(), "当前环境无法创建 tkinter 窗口")
class TestGuiSmoke(unittest.TestCase):
    """界面构造与回调冒烟测试。"""

    def setUp(self) -> None:
        import gui

        # 不弹窗：把对话框换成记录器
        self.calls: list = []
        gui.messagebox.showinfo = lambda *a, **k: self.calls.append(("info", a))
        gui.messagebox.showwarning = lambda *a, **k: self.calls.append(("warn", a))
        gui.messagebox.showerror = lambda *a, **k: self.calls.append(("error", a))
        gui.messagebox.askyesno = lambda *a, **k: (self.calls.append(("ask", a)), True)[1]
        self.gui = gui

    def _make_app(self):
        """创建界面（不启动定时器），并注册销毁。"""
        app = self.gui.HostApp(start_timers=False)
        self.addCleanup(app.destroy)
        return app

    def test_window_creates(self):
        """主窗口可以正常创建。"""
        app = self._make_app()
        self.assertIn("Puzzle", app.title())
        self.assertIsNone(app.client)

    def test_log_and_clear(self):
        """日志写入与清空。"""
        app = self._make_app()
        app.log("测试一行", "info")
        content = app.txt.get("1.0", "end")
        self.assertIn("测试一行", content)
        app.clear_log()
        self.assertEqual(app.txt.get("1.0", "end").strip(), "")

    def test_status_labels_update(self):
        """状态标签按 Status 刷新。"""
        app = self._make_app()
        app._status = P.Status(pos_x=12, pos_y=34, pos_rot=-5, servo=1,
                               electromagnet=1, homing=1, reinit_pending=1)
        app._refresh_labels()                    # 手动跑一次刷新
        self.assertIn("12", app.var_stat_x.get())
        self.assertIn("34", app.var_stat_y.get())
        self.assertIn("-5", app.var_stat_rot.get())
        self.assertEqual(app.lbl_servo.cget("text"), "开")
        self.assertEqual(app.lbl_em.cget("text"), "吸合")

    def test_actions_without_connection_warn(self):
        """未连接时点按钮应提示而不是崩溃。"""
        app = self._make_app()
        app.on_ping()
        app.on_query_all()
        app.on_reinit()
        app.on_send_motion()
        app.on_send_all()
        app.on_echo()
        app.on_bench()
        self.assertTrue(any(kind == "info" for kind, _ in self.calls))

    def test_manual_parse_frame(self):
        """手动帧解析路径（合法帧与非法帧）。"""
        app = self._make_app()
        app.var_manual.set(P.ping_frame().hex().upper())
        app.on_parse_frame()
        self.assertIn("PING", app.txt.get("1.0", "end"))

        app.var_manual.set("AA 55 07 00 99 0D")          # 校验错
        app.on_parse_frame()
        self.assertIn("解析失败", app.txt.get("1.0", "end"))

    def test_manual_send_bad_hex_warns(self):
        """手动发帧输入非法十六进制时提示。"""
        app = self._make_app()
        app.var_manual.set("ZZ ZZ")
        app.on_send_manual()
        self.assertTrue(any(kind == "info" for kind, _ in self.calls))

    def test_input_validation_blocks_bad_range(self):
        """越界输入被拦下并提示。"""
        app = self._make_app()
        app.var_rot.set("99999")
        self.assertIsNone(app._read_field("旋转角度", app.var_rot, P.ROT_MIN, P.ROT_MAX))
        self.assertTrue(any(kind == "warn" for kind, _ in self.calls))

    def test_refresh_ports_no_crash(self):
        """串口扫描在无设备时也不应报错。"""
        app = self._make_app()
        app.refresh_ports()
        self.assertIsInstance(app.cmb_port["values"], tuple)

    def test_poll_interval_clamped(self):
        """轮询间隔下限保护（防止请求堆积）。"""
        app = self._make_app()
        app.var_poll_ms.set("10")
        self.assertGreaterEqual(app._poll_interval_ms(), 500)
        app.var_poll_ms.set("abc")
        self.assertEqual(app._poll_interval_ms(), 1000)

    def test_mono_font_is_available(self):
        """等宽字体挑选结果必须是本机真实存在的字体（跨平台关键）。"""
        import tkinter.font as tkfont
        app = self._make_app()
        chosen = app._mono
        self.assertTrue(chosen)
        if chosen != "TkFixedFont":
            families = {name.lower() for name in tkfont.families(app)}
            self.assertIn(chosen.lower(), families)

    def test_port_description_shortened(self):
        """过长的串口描述会被截断（否则左侧栏会被撑爆）。"""
        app = self._make_app()
        long_desc = "USB-SERIAL CH340 " + "X" * 200 + " [USB VID:PID=1A86:7523]"
        short = app._short_desc(long_desc)
        self.assertLessEqual(len(short), 29)
        self.assertNotIn("[", short)

    def test_utf8_stdout_helper_is_safe(self):
        """UTF-8 输出开关可重复调用且不抛异常。"""
        app = self._make_app()
        self.gui.ensure_utf8_stdout()
        self.gui.ensure_utf8_stdout()


if __name__ == "__main__":
    unittest.main(verbosity=2)
