# STM32 拼图机上位机（puzzle-host）

[![tests](https://github.com/miaoye913/stm32-puzzle-host/actions/workflows/tests.yml/badge.svg)](https://github.com/miaoye913/stm32-puzzle-host/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

用 Python 写的 PC 端上位机，通过串口按自定义二进制帧协议远程操控 STM32F103 拼图机：
设置 X / Y 坐标、旋转角度、舵机、电磁铁，触发重新初始化，并实时轮询显示全部状态。

配套固件端协议实现见 STM32 工程 `app/protocol/`（`protocol.c` / `protocol.h`）。

* **图形界面**（tkinter，零额外 GUI 依赖）与**命令行工具**两种形态
* 串口自动扫描、波特率可选、定时轮询状态、十六进制收发日志、手动发帧与离线解析
* **75 项自动化测试，不接硬件也能全流程验证**（内存仿真 / 真实 TCP socket / 真实 pyserial 链路）
* 跨平台：Windows、Linux、macOS 均可运行

---

## 界面预览

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Puzzle 上位机 —— STM32 串口控制台                                          ✕ │
├────────────────────────────┬─────────────────────────────────────────────────┤
│ ┌ 串口连接 ──────────────┐ │ ┌ 收发日志 ───────────────────────────────────┐ │
│ │ 端口 [COM3      ▾] 刷新│ │ │ ☐ 显示原始字节  ☐ 显示状态数据帧  解析 | 清空│ │
│ │ 波特率 [115200  ▾] 连接│ │ │ [13:41:19.319] ⚡ 连接：up                  │ │
│ │ 发现：COM3 = CH340 …   │ │ │ [13:41:19.515] → PING(0x07)                 │ │
│ └────────────────────────┘ │ │ [13:41:19.519] ← ACK(0x80)                  │ │
│ ┌ 通用 ──────────────────┐ │ │ [13:41:19.545] ✔ 状态：X=1000 Y=256 R=90°   │ │
│ │ [PING 心跳][重新初始化]│ │ │              S=1 E=1 H=0 RI=0               │ │
│ │ [查询全部]             │ │ │                                             │ │
│ │ ☑ 自动轮询状态 间隔1000│ │ │                                             │ │
│ │ 回环数据 11 22 33 [ECHO]│ │ │                                             │ │
│ └────────────────────────┘ │ │                                             │ │
│ ┌ 设置（SET 命令）───────┐ │ │                                             │ │
│ │ X 坐标  [1000     ] 脉冲│ │ │                                             │ │
│ │ Y 坐标  [0        ] 脉冲│ │ │                                             │ │
│ │ 旋转角度 [-90      ] 度 │ │ │                                             │ │
│ │ ☐ 舵机开  ☐ 电磁铁吸合  │ │ │                                             │ │
│ │ [下发 X / Y / 旋转]     │ │ │                                             │ │
│ └────────────────────────┘ │ │                                             │ │
│ ┌ 实时状态（GET ALL）────┐ │ │                                             │ │
│ │ X 1000    Y 256        │ │ │                                             │ │
│ │ 旋转 -90° 找零中 否    │ │ │                                             │ │
│ │ 舵机 开  电磁铁 释放    │ │ │                                             │ │
│ │ 已发 12 帧 / 已收 12 帧 │ │ └─────────────────────────────────────────────┘ │
│ └────────────────────────┘ │  手动帧 [AA 55 07 00 07 0D    ] [发送][不等待应答]│
└────────────────────────────┴─────────────────────────────────────────────────┘
```

---

## 快速开始

```bash
# 1) 安装依赖（仅 pyserial 一个第三方库）
python -m pip install -r requirements.txt

# 2) 启动图形界面
python app.py

# 3) 或使用命令行
python cli.py ports                  # 列出本机串口
python cli.py ping  -p COM3          # 心跳测试
python cli.py all   -p COM3          # 查询全部状态
```

> 端口名：Windows `COM3`；Linux `/dev/ttyUSB0`（CDC/ACM 设备为 `/dev/ttyACM0`）。
> Linux 还需 `sudo usermod -aG dialout $USER` 并重新登录，否则报 Permission denied。
> 详见 [docs/LINUX.md](docs/LINUX.md)。

---

## 功能

### 图形界面（`python app.py`）

| 区域 | 能力 |
|------|------|
| 串口连接 | 自动扫描串口并显示设备描述、可选波特率（9600~460800）、一键连接/断开 |
| 通用 | PING 心跳、重新初始化（带二次确认）、查询全部、自动轮询（间隔可调） |
| 设置 | X / Y 坐标（uint32）、旋转角度（int16）、舵机开关、电磁铁开关，可整批下发 |
| 实时状态 | 坐标（十进制 + 十六进制）、角度、舵机、电磁铁、找零中、待重初始化 |
| 收发日志 | 带时间戳的帧级日志、十六进制原始字节开关、状态帧过滤、手动发帧、离线解析任意帧 |

连接后会自动 PING 握手并查询一次状态；所有请求走后台收包线程，界面不会卡死。

### 命令行（`python cli.py --help`）

```
ports                              列出串口
ping   -p PORT                     心跳（显示往返时延）
all    -p PORT                     查询全部状态
set    -p PORT --x 1000 --y 200 --rot -90 --servo 1 --em 0
reinit -p PORT                     触发重新初始化（找零）
echo   -p PORT -d "11 22 33"       回环测试
monitor -p PORT -n 20 -i 0.5       周期轮询（-n 0 = 一直轮询）
bench  -p PORT -n 200              PING 压力测试（丢包率 + 时延统计）
shell  -p PORT                     交互式命令行
```

也支持 TCP（便于远程/无线调试）：`-p tcp://192.168.4.1:5000`。

---

## 无硬件也能验证

```bash
# 跑全部测试（75 项，不需要接板子）
python -m unittest discover -s tests -t .

# 起一个仿真 STM32（TCP），让上位机连它
python demo_device.py --port 5000
python cli.py all -p tcp://127.0.0.1:5000
python app.py --port tcp://127.0.0.1:5000
```

| 测试文件 | 覆盖内容 |
|----------|----------|
| `test_protocol.py` | 组帧逐字节比对、校验错/帧尾错/长度超限、拆包粘包噪声重同步、14 字节布局 |
| `test_client.py` | 全命令请求-应答、NACK / 超时 / 重试、事件流、连续 50 轮轮询不错配 |
| `test_e2e_socket.py` | 真实 TCP socket 全链路（含 30 轮长跑） |
| `test_serial_link.py` | **真实 pyserial** 链路（`socket://` + TCP 中转模拟串口线） |
| `test_gui_smoke.py` | 界面构造、控件回调、输入校验、日志过滤、字体选择 |

---

## 协议速查

帧格式：`AA 55 | CMD | LEN | DATA... | XOR | 0D`，`XOR = CMD ^ LEN ^ DATA[0] ^ …`

| 命令 | CMD | LEN | DATA | 说明 |
|------|-----|-----|------|------|
| SET X | 0x01 | 4 | uint32 小端 | 设置 X 坐标 |
| SET Y | 0x02 | 4 | uint32 小端 | 设置 Y 坐标 |
| SET R | 0x03 | 2 | int16 小端 | 设置旋转角度 |
| SET SERVO | 0x04 | 1 | 0/1 | 舵机 |
| SET EM | 0x05 | 1 | 0/1 | 电磁铁 |
| REINIT | 0x06 | 0 | — | 重新初始化（找零） |
| PING | 0x07 | 0 | — | 心跳，纯应答 |
| ECHO | 0x08 | 0~14 | 任意 | 原样回显 |
| GET ALL | 0x10 | 0 | — | 查询全部状态 |
| GET X/Y/R/S/E | 0x11~0x15 | 0 | — | 查询单项 |

响应：`ACK=0x80`、`NACK=0x81`、`DATA ALL=0x90`（14 字节）、`DATA X/Y/R/S/E=0x91~0x95`。

`GET ALL` 数据布局：`X(4) Y(4) R(2) S(1) E(1) homing(1) reinitPending(1)`

完整说明（含固件边界行为、与上游文档的差异）见 **[docs/PROTOCOL-NOTES.md](docs/PROTOCOL-NOTES.md)**。

---

## 项目结构

```
puzzle-host/
├── app.py                 统一入口（默认 GUI，--cli 转命令行）
├── gui.py                 tkinter 图形界面
├── cli.py                 命令行工具
├── protocol.py            协议编解码（纯逻辑、零依赖）
├── link.py                字节链路：SerialLink / TcpLink / LoopbackLink
├── client.py              协议客户端：收包线程 + 请求应答 + 事件回调
├── demo_device.py         仿真 STM32（TCP），无硬件演示用
├── tests/                 75 项自动化测试
└── docs/                  协议细节、Linux 部署
```

分层原则：`protocol.py` 只做字节 ↔ 帧转换，`link.py` 只管收发字节，
`client.py` 拼成「一问一答」，界面层只调 `client.py` —— 收发逻辑只有一份实现，
因此 GUI 与 CLI 行为完全一致。

---

## 已验证范围（诚实声明）

* ✅ **CI 每次提交自动在 Linux / Windows / macOS × Python 3.9 / 3.12 上跑全部测试**
  （见 [.github/workflows/tests.yml](.github/workflows/tests.yml) 与页首徽章）；
* ✅ 其中有一个**刻意不装 Tk 的 ubuntu 作业**，专门验证「无桌面环境 / SSH 服务器」
  场景下 CLI 可用（GUI 会自动提示改用命令行）；
* ✅ 75 项测试本地全部通过，且包含真实 TCP socket 与真实 pyserial 链路
  （后两条路径与平台无关，Linux 上走同一个 `serialposix` 后端）；
* ✅ 已用「从 GitHub 全新 clone → 装依赖 → 跑测试」验证过发布版本可用，
  仓库不含 vendor 依赖；
* ⚠️ 唯一未在真实硬件上验证的是 **GUI 的观感**（字体、中文字形、窗口布局在
  各发行版上的细微差异）；代码层面的跨平台处理（等宽字体探测、UTF-8 输出、
  `/dev/serial/by-id` 枚举）都已就位，详见 [docs/LINUX.md](docs/LINUX.md)。

---

## 已知问题 / 上游文档勘误

STM32 工程的 `app/protocol/README.md` 中 `SET X=1000` 示例的校验字节有误：
文档写 `… 00 EC 0D`，按 XOR 计算应为 `0xEE`（`01^04^E8^03^00^00`），
固件 `protocol.c` 实际发出的也是 `0xEE`。本项目以固件为准实现，并在
`tests/test_protocol.py::test_set_x_checksum` 中固定了该行为。
详见 [docs/PROTOCOL-NOTES.md](docs/PROTOCOL-NOTES.md)。

---

## 安全提示

* `REINIT` 会触发电机回零运动，GUI 有二次确认，命令行没有 —— 真实设备上执行前请确认机械空间；
* 上位机只负责发协议帧，不做行程保护，限位仍由设备端 endstop 模块负责；
* `SET X/Y` 是逻辑坐标目标值，单位脉冲（uint32）；`SET R` 为 int16，固件约定可用范围 -180 ~ +180。

## License

[MIT](LICENSE)
