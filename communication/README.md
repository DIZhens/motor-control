# Communication

设备语义、可移植协议及 PC 工具统一放在这里；STM32 外设适配仍属于具体 Target。

| 目录 | 回答的问题 |
| --- | --- |
| [spec](spec/README.md) | 命令、对象、单位、状态以及 UART/CAN 帧应如何解释？ |
| [protocol](protocol/README.md) | 如何编解码、管理会话/事务、去重和产生应答？ |
| [host](host/README.md) | PC 如何连接、控制、记录、分析和向 VOFA 转发？ |

`protocol/device/` 保存共享设备编号、值与 Device Port；`uart/`、`can/` 各自实现链路映射；`transport/` 只定义字节流/帧端口。协议不包含 Motor、HAL 或 FreeRTOS。

[G431 communication](../firmware/targets/BLDC_G431/communication/README.md)提供 UART DMA / FDCAN 和设备绑定，将完整意图交给 Runtime。会话与请求身份不进入控制 ISR。

Host 保留已有 `protocol/` 和 `vofa/` 模块。CAN 目前只有 codec，没有实物 CAN Client。正式 Run 使用自研协议，PC 的 VOFA 桥单向输出本机 UDP；MCU 旧 VOFA 仅用于 Target Validation。
