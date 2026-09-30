# 协议实现细节与固件对齐说明

本文记录 `protocol.py` 与 STM32 端 `app/protocol/protocol.c` / `protocol.h` 的对应关系，
以及文档里不显眼、但实现时必须处理的边界行为。

---

## 1. 帧格式

```
[0xAA] [0x55] [CMD] [LEN] [DATA ...] [CHECKSUM] [0x0D]
 帧头1  帧头2  命令  长度   参数（小端）   校验      帧尾
```

| 字段 | 字节 | 说明 |
|------|------|------|
| HEAD1 | 1 | 固定 `0xAA` |
| HEAD2 | 1 | 固定 `0x55` |
| CMD | 1 | 命令码 / 响应码 |
| LEN | 1 | DATA 段长度，合法范围 0 ~ 14 |
| DATA | 0~14 | 参数，**小端序** |
| CHECKSUM | 1 | `CMD ^ LEN ^ DATA[0] ^ … ^ DATA[n-1]` |
| TAIL | 1 | 固定 `0x0D` |

固件侧限制（`protocol.h`）：`PROTO_DATA_MAX = 14`。

---

## 2. 命令码（上位机 → STM32）

| 命令 | CMD | LEN | DATA | 说明 |
|------|-----|-----|------|------|
| SET X | 0x01 | 4 | uint32 小端 | 设置 X 轴坐标 |
| SET Y | 0x02 | 4 | uint32 小端 | 设置 Y 轴坐标 |
| SET R | 0x03 | 2 | int16 小端 | 设置旋转角度 |
| SET SERVO | 0x04 | 1 | 0/1 | 舵机开关 |
| SET EM | 0x05 | 1 | 0/1 | 电磁铁开关 |
| REINIT | 0x06 | 0 | — | 触发重新初始化（找零） |
| PING | 0x07 | 0 | — | 心跳，纯应答 |
| ECHO | 0x08 | 0~14 | 任意 | 原样回显 DATA |
| GET ALL | 0x10 | 0 | — | 查询全部状态 |
| GET X | 0x11 | 0 | — | 查询 X |
| GET Y | 0x12 | 0 | — | 查询 Y |
| GET R | 0x13 | 0 | — | 查询旋转 |
| GET S | 0x14 | 0 | — | 查询舵机 |
| GET E | 0x15 | 0 | — | 查询电磁铁 |

## 3. 响应码（STM32 → 上位机）

| 响应 | CMD | LEN | DATA |
|------|-----|-----|------|
| ACK | 0x80 | 0 | — |
| NACK | 0x81 | 0 | — |
| DATA: ALL | 0x90 | 14 | X(4)+Y(4)+R(2)+S(1)+E(1)+H(1)+RI(1) |
| DATA: X | 0x91 | 4 | uint32 小端 |
| DATA: Y | 0x92 | 4 | uint32 小端 |
| DATA: R | 0x93 | 2 | int16 小端 |
| DATA: S | 0x94 | 1 | 0/1 |
| DATA: E | 0x95 | 1 | 0/1 |
| ECHO 回显 | **0x08** | 0~14 | 原样返回 |

> ⚠️ `ECHO` 的响应码沿用请求码 `0x08`（见 `protocol.c` 的
> `case PROTO_CMD_ECHO: Proto_SendData(0x08, data, len);`），
> 不属于 0x80 系列，容易看漏。

### GET ALL 的 14 字节布局

| 偏移 | 长度 | 字段 | 类型 |
|------|------|------|------|
| 0 | 4 | posX | uint32 小端 |
| 4 | 4 | posY | uint32 小端 |
| 8 | 2 | posRot | int16 小端 |
| 10 | 1 | servoState | 0/1 |
| 11 | 1 | emState | 0/1 |
| 12 | 1 | homing | 0/1 |
| 13 | 1 | reinitPending | 0/1 |

对应 `Python` 解包格式：`struct.unpack("<IIhBBBB", data)`。

---

## 4. 必须处理的固件边界行为

这些都是读 `protocol.c` 才能发现的细节，本项目的解析器与之逐条对齐：

1. **NACK 出现在多种时机**：未知命令、参数长度不符（如 `SET X` 的 LEN ≠ 4）、
   **收到的帧校验失败**、**帧尾错误**、`LEN > 14`。
   所以「收到 NACK」不等于「命令码写错了」。
2. **校验顺序**：固件先比对 CHECKSUM，通过后才等帧尾；两者任一失败都会回 NACK。
   本项目的 `FrameParser` 按同样顺序校验，保证与之行为一致。
3. **LEN 超限直接丢弃**：固件在 `RX_STATE_LEN` 阶段就回到起点（并回 NACK），
   不会尝试读取超长数据。
4. **`PING` 是纯应答**：不做任何操作，非常适合做链路存活检测与压力测试。
5. **`SET SERVO` / `SET EM` 归一化**：固件用 `(data[0] != 0) ? 1 : 0`，
   即任何非 0 值都被当作 1。
6. **ACK 只代表"指令已入队"**：协议层只写 `g_sysCfg`，真正的电机动作由
   `Motor_Task` 在主循环里轮询消费。因此**收到 ACK 不等于动作已完成**，
   界面必须持续轮询 `GET ALL` 才能反映真实状态。
7. **状态字段的语义**（来自 `sys_ctrl.h`）：
   * `homing = 1`：正在找零 / 归零；
   * `reinitPending = 1`：已请求重新初始化，等待 `Motor_Task` 消费；
   * `posX/posY` 为逻辑坐标（脉冲，上电找零后为 0，只增不减）；
   * `posRot` 为角度，固件约定可用范围 -180 ~ +180。

---

## 5. 与上游文档的差异（勘误）

STM32 工程的 `app/protocol/README.md`「使用示例」一节中：

```
SET X=1000:  AA 55 01 04 E8 03 00 00 EC 0D      ← 文档所写
```

按协议逐字节计算校验：

```
0x01 ^ 0x04 ^ 0xE8 ^ 0x03 ^ 0x00 ^ 0x00 = 0xEE
```

固件 `protocol.c` 的 `Proto_SendData()` / `Proto_SendAck()` 使用 XOR 累积实现，
实际发出的校验字节是 **`0xEE`**：

```
正确帧：AA 55 01 04 E8 03 00 00 EE 0D
```

**本项目以固件为准按 `0xEE` 实现**，并在
`tests/test_protocol.py::TestBuildFrame::test_set_x_checksum` 中把这一点固定下来，
避免以后有人"照着文档改回去"。建议把上游 README 的该行改为 `EE`。

对照：该 README 里 `PING`（`AA 55 07 00 07 0D`）与 `GET ALL`（`AA 55 10 00 10 0D`）
两个示例的校验都是正确的，仅 `SET X` 这一行有误。

---

## 6. 上位机侧的实现映射

| 固件 | 上位机 |
|------|--------|
| `Proto_SendAck()` | `protocol.build_frame()` / `Frame.to_bytes()` |
| `Proto_SendData()` | 同上，带 DATA 段 |
| `Protocol_Task()` 状态机 | `protocol.FrameParser`（增量流式解析，含重同步） |
| `Proto_Execute()` | `tests/fake_device.py` 的 `FakeStm32._execute()`（用于离线测试） |
| `Proto_ReadU32/ReadI16` | `protocol.decode_u32()` / `decode_i16()` |
| `g_sysCfg` | `protocol.Status` |

`FrameParser` 会统计 `checksum_errors` / `tail_errors` / `length_errors` /
`resyncs`，出现异常帧时可据此判断是线路噪声还是协议不匹配。
