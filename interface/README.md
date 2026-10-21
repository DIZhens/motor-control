# interface

Firmware 与 PC Host 的共享合同区域。

长期区分两个层次；当前只落实默认关断入口实际使用的最小语义，闭环相关部分逐步补齐：

```text
Device Contract
    设备有哪些状态、命令、Fault、观测和参数，以及这些数据的单位与行为语义

Protocol Contract
    上述设备语义如何表示为请求、响应、事件和 Telemetry
```

当前生效合同定义 G431 单电机闭环与 UART / VOFA 调试需要的最小集合：启停、速度目标、状态与故障，以及关键观测的通道、单位、有效条件和发布频率。设备语义见 [DEVICE_CONTRACT.md](DEVICE_CONTRACT.md)，线格式与通道见 [VOFA_PROTOCOL.md](VOFA_PROTOCOL.md)；VOFA 配置与操作说明保存在 `host/`，不成为另一份合同。

展示目标新增 UART 自研设备协议和 FreeRTOS 后台集成。[PROTOCOL_DESIGN.md](PROTOCOL_DESIGN.md)保存协议初步方案、现有合同的适配边界和待定问题，尚非正式 Wire Protocol，不代表已实现。协议语义和编解码保持不依赖 RTOS；CAN 实物接入后续再做，共用设备语义但允许独立映射。

先明确命令、失败和恢复语义，再冻结最小 UART 对象、事务与字节格式。位置环、标准 Modbus / CANopen 栈和完整工业协议生态后续再做，不沿用旧工程的对象字典作为默认前提。
