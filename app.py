"""
app.py —— 上位机软件启动入口（双击 / 命令行均可）

用法::

    python app.py                     # 启动图形界面（推荐）
    python app.py --port COM3         # 启动后自动连接 COM3
    python app.py --cli --help        # 转到命令行模式

也可以直接运行 gui.py / cli.py，效果相同。
"""

from __future__ import annotations

import os
import sys

# 打包的第三方依赖目录（pyserial），存在则优先使用
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")
if os.path.isdir(_VENDOR):
    sys.path.insert(0, _VENDOR)


def main(argv=None) -> int:
    """入口：默认启动 GUI，``--cli`` 时转交命令行。"""
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--cli" in argv:
        argv.remove("--cli")
        import cli
        return cli.main(argv)

    port = None
    baud = None
    rest = []
    for i, item in enumerate(argv):
        if item in ("--port", "-p") and i + 1 < len(argv):
            port = argv[i + 1]
        elif item in ("--baud", "-b") and i + 1 < len(argv):
            baud = argv[i + 1]
        elif item not in ("--port", "-p", "--baud", "-b"):
            rest.append(item)

    import gui
    app = gui.HostApp()
    if port:
        app.var_port.set(port)
        if not app.cmb_port["values"]:
            app.cmb_port["values"] = (port,)
        if baud:
            app.var_baud.set(str(baud))
        # 只预填端口，不自动连接：连接后会立刻下发 PING / 查询，
        # 若端口上挂的是真实设备，未确认就发指令可能让它动起来。
        app.after(200, lambda: app.prompt_connect(port))
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
