# Linux 部署与使用

本项目是纯 Python 实现，Windows / Linux / macOS 通用。以下是在 Linux 上从零跑起来的步骤。

---

## 1. 安装依赖

```bash
# Python 3.8+（多数发行版自带，用 python3 --version 确认）
python3 --version

# 图形界面需要 Tk —— 很多精简系统默认没装，这是最常见的坑
sudo apt install python3-tk          # Debian / Ubuntu / 树莓派
sudo dnf install python3-tkinter     # Fedora / RHEL / CentOS Stream
sudo pacman -S tk                    # Arch

# 串口库（唯一第三方依赖）
python3 -m pip install -r requirements.txt
# 或使用发行版包：sudo apt install python3-serial

# 可选：支持 tcp:// 远程连接时无需额外依赖（已内置）
```

> 若 `pip` 不可用或无法联网，也可以直接 `sudo apt install python3-serial`，
> 效果等价。

---

## 2. 串口权限（必做）

Linux 下串口设备属于 `dialout` 组（部分发行版是 `uucp`），普通用户默认无权访问，
否则会报 `打开 /dev/ttyUSB0 失败：Permission denied`。

```bash
# 查看自己是否已在组里
groups | grep -E 'dialout|uucp'

# 加入 dialout 组（推荐，比每次 sudo 安全）
sudo usermod -aG dialout $USER
#   之后必须重新登录（或执行 newgrp dialout）才生效

# 临时方案（不推荐长期使用）
sudo python3 cli.py ping -p /dev/ttyUSB0
```

---

## 3. 找到串口

```bash
python3 cli.py ports
```

输出示例：

```
发现 3 个串口：
  /dev/ttyUSB0            USB-SERIAL CH340 [USB VID:PID=1A86:7523 ...]
  /dev/ttyACM0            STMicroelectronics STLink Virtual COM Port
  /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0   稳定别名 -> ttyUSB0
```

* USB 转串口（CH340 / CP2102 / FT232）通常是 **`/dev/ttyUSB0`**；
* CDC/ACM 类设备（ST-Link VCP、J-Link VCOM、Arduino）是 **`/dev/ttyACM0`**；
* 也可以直接看系统设备名：

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
dmesg | tail -20                 # 刚插上设备后看内核分配了哪个口
```

### 为什么推荐用 `/dev/serial/by-id/...`

普通设备名是按插入顺序分配的。同时插两个 USB 转串口时，`ttyUSB0` / `ttyUSB1`
可能在重新插拔后互换，导致连错设备。`by-id` 别名基于设备序列号，永远指向同一个设备：

```bash
python3 cli.py all -p /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

`cli.py ports` 会自动把这些别名列出来。

---

## 4. 运行

```bash
# 命令行
python3 cli.py ping -p /dev/ttyUSB0
python3 cli.py all  -p /dev/ttyUSB0
python3 cli.py set  -p /dev/ttyUSB0 --x 1000 --rot -90 --servo 1
python3 cli.py shell -p /dev/ttyUSB0          # 交互式

# 图形界面
python3 app.py
python3 app.py --port /dev/ttyUSB0            # 预填端口（会先弹确认框）
```

中文界面在缺字体时可能显示成方块，装个中文字体即可：

```bash
sudo apt install fonts-noto-cjk        # 或 fonts-wqy-zenhei
```

---

## 5. 跑测试（不需要接硬件）

```bash
python3 -m unittest discover -s tests -t .
```

预期结果：**75 项全部 OK（Ran 75 tests ... OK）**。

测试不需要真实串口：`test_serial_link.py` 用 pyserial 的 `socket://` URL
配合一个 TCP 中转，在用户态构造出一根"软件串口线"，因此能真实覆盖
`SerialLink` 的读写路径。

可选：起一个仿真设备，用上位机连它（完全不碰硬件）：

```bash
python3 demo_device.py --port 5000                  # 终端 1
python3 cli.py all -p tcp://127.0.0.1:5000          # 终端 2
python3 app.py --port tcp://127.0.0.1:5000
```

---

## 6. 无桌面环境（纯 SSH / 服务器 / 树莓派 Lite）

GUI 起不来是正常的，程序会提示安装 `python3-tk` 并建议改用命令行 —— **功能完全一致**。

若确实需要图形界面：

```bash
# 方案 A：SSH X11 转发（本地需装 X Server，Windows 可用 VcXsrv / MobaXterm）
ssh -X user@host
python3 app.py

# 方案 B：树莓派等设备接显示器直接跑
```

---

## 7. 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| `打开 /dev/ttyUSB0 失败：Permission denied` | 没加入 `dialout` 组，见第 2 节 |
| `FileNotFoundError: /dev/ttyUSB0` | 设备名不对，用 `python3 cli.py ports` 确认 |
| `could not open port ... Device or resource busy` | 端口被占用：`minicom` / `screen` / `ModemManager` 正抓着它。可停掉 ModemManager：`sudo systemctl stop ModemManager` |
| PING 一直超时 | 接线（TX/RX 要交叉、共地）、波特率、或设备端 `Protocol_Task()` 未被主循环调用 |
| 界面中文显示为方块 | 缺中文字体，`sudo apt install fonts-noto-cjk` |
| `ModuleNotFoundError: No module named 'serial'` | 执行 `python3 -m pip install pyserial` |
| `ModuleNotFoundError: No module named 'tkinter'` | 执行 `sudo apt install python3-tk` |
| 终端输出乱码 | 缺 UTF-8 locale：`export LANG=C.UTF-8`（程序已尽量自动切 UTF-8） |

---

## 8. 平台兼容性说明

* 代码中**没有** Windows-only 调用；仅 pyserial 自带的 `serialwin32.py` 是平台实现，
  Linux 上不会导入（走 `serialposix.py`）；
* 仓库内所有文本文件统一 **LF** 换行（见 `.gitattributes`），Linux 下不会看到 `^M`；
* 等宽字体按 `Consolas → Cascadia Mono → Menlo → Monaco → DejaVu Sans Mono →
  Liberation Mono → Noto Sans Mono → Ubuntu Mono → Courier New` 顺序探测，
  都找不到则退回 Tk 内置等宽字体；
* 标准输出在启动时自动切换到 UTF-8，避免 `LANG=C` 下打印中文报 `UnicodeEncodeError`。

> 开发验证环境为 Windows + Python 3.13。GUI 未在 Linux 实机验证；
> 若你跑 `python3 -m unittest discover -s tests -t .` 全绿，即说明实机可用。
