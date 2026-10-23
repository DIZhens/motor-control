# Protocol

可移植的设备值、线格式编解码与事务 Server。协议不依赖 HAL、FreeRTOS 或 Motor 私有类型，不直接操作功率。

| 目录 | 职责与入口 |
| --- | --- |
| [device](device) | [Motor Node 值](device/motor_node_profile.h)及 [Device Port](device/protocol_device_port.h) |
| [uart](uart) | COBS / CRC、固定值 Codec、Parser 及 [UART Server](uart/core_server.h) |
| [can](can) | Classical CAN 紧凑 Codec 及 [CAN Server](can/can_server.h) |
| [transport](transport) | 链接期字节流与 CAN 帧 Port |
| [tests](tests) | 独立黄金向量、ARM C / Python 对照与事务模型 |

## 边界

Server 是 Session、Sequence、去重和发送状态的唯一所有者，经 Device Port 读一致对象、提交完整设备请求及消费 Completion；Target Binding 负责与 Runtime 转换。Server 直接调用 Transport Port，纯 Codec 不拥有 Session。

SystemRuntime 提供协议无关的单消费者订阅和单槽 Mailbox。新 Session 替换订阅、失效结束订阅，幂等 OPEN 保留当前域。UART 保留 publication 覆盖缺口与 BUSY 身份；CAN Motion 使用实时采样序号低 16 位，恢复不重放旧运动或旧遥测。

共享业务以 [Motor Node Profile](../spec/MOTOR_NODE_PROFILE_V1.md)为准，精确 Wire 与事务规则见 [UART Mapping](../spec/UART_MAPPING_V1.md)和 [CAN Mapping](../spec/CAN_MAPPING_V1.md)。`core_* / Core* / CORE_*` 是 UART V1 历史兼容名称，C 类型不是线上内存布局。

[G431 接入](../../firmware/targets/BLDC_G431/communication/README.md)在构建期选择唯一 UART/CAN 前端；[Host](../host/README.md)使用同一合同。

## 离线验证

从仓库根目录执行：

```powershell
.venv/Scripts/python.exe -X utf8 communication/protocol/tests/uart/verify_core_protocol.py
.venv/Scripts/python.exe -X utf8 communication/protocol/tests/uart/verify_core_server.py
.venv/Scripts/python.exe -X utf8 communication/protocol/tests/can/verify_can.py
.venv/Scripts/python.exe -X utf8 communication/protocol/tests/verify_boundaries.py
```

依赖见[开发规则](../../DEVELOPMENT.md#44-本地验证依赖)。模型执行正式 Codec/Server 和绑定，替换硬件收发与实时推进；不证明外部物理总线、真实调度、吞吐或电机运动。实物能力边界见 [G431 Validation](../../firmware/targets/BLDC_G431/validation/README.md)。
