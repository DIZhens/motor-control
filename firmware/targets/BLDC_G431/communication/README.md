# G431 Communication

本目录把可移植协议接入 G431 的外设与 Runtime。正式 Run 默认 UART，同一 Target 可按[集成合同](../runtime/RTOS_INTEGRATION.md#静态链路选择)静态选择 CAN。

| 文件 | 职责 |
| --- | --- |
| `target_communication.*`、`target_communication_config.h` | 选中链路的初始化、后台服务与通知接入 |
| `uart_dma_transport.*` | USART1 / RX Circular DMA / 单帧 TX DMA、IRQ 和恢复 |
| `fdcan_frame_transport.c` | FDCAN 帧收发、过滤与错误处理 |
| `target_core_server.*`、`target_can_server.*` | 链路 Server 的静态装配、服务与统计映射 |
| `motor_node_binding.c`、`motor_node_binding_target.h` | Device Port 到 Runtime 请求/完成和值的规范化 |
| `target_motor_node_binding.c` | 本机身份、发布值、提交与关断端口的链接期绑定 |

## 事务Server

Communication 是 Parser、Session/Sequence、事务和发送状态的后台唯一所有者。普通请求、Stop、去重、超时及应答由 [可移植 Server](../../../../communication/protocol/README.md)管理。Binding 只转换完整设备意图，不操作 PWM/MOE 或 PI 历史；实际执行经过 [Runtime ticket/Completion](../runtime/SYSTEM_RUNTIME.md)。

共享对象与线格式分别由 [Motor Node Profile](../../../../communication/spec/MOTOR_NODE_PROFILE_V1.md)、[UART](../../../../communication/spec/UART_MAPPING_V1.md)和 [CAN](../../../../communication/spec/CAN_MAPPING_V1.md)规范定义。Target 不复制协议源码。

旧裸机 `vofa`、`motor_vofa`、`target_vofa`、`target_uart` 位于 [validation/communication](../validation/communication)，不进入正式 Run。PC 的 [VOFA UDP 桥](../../../../communication/host/vofa/README.md)属于 Host。

从仓库根目录运行 `communication/protocol/tests/uart/verify_core_server.py`、`communication/protocol/tests/can/verify_can.py` 以及 `firmware/targets/BLDC_G431/validation/tests/runtime/verify_uart_dma.py`。模型与实物结论分别记录，统一见 [Validation](../validation/README.md)。
