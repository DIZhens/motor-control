# interface

Firmware 与 PC Host 的共享合同区域。

长期区分两个层次；当前只落实默认关断入口实际使用的最小语义，闭环相关部分逐步补齐：

```text
Device Contract
    设备有哪些状态、命令、Fault、观测和参数，以及这些数据的单位与行为语义

Protocol Contract
    上述设备语义如何表示为请求、响应、事件和 Telemetry
```

V1 只定义 G431 单电机闭环与 UART / VOFA 调试实际需要的最小集合：启停、速度目标、状态与故障，以及关键观测的通道、单位、有效条件和发布频率。设备语义见 [DEVICE_CONTRACT.md](DEVICE_CONTRACT.md)，线格式与通道见 [VOFA_PROTOCOL.md](VOFA_PROTOCOL.md)；VOFA 配置与操作说明保存在 `host/`，不成为另一份合同。

先明确命令、失败和安全语义，再定义最小 UART 表示与观测格式；采用现成 VOFA 数据格式不要求预建通用协议框架。位置环、Modbus、CANopen 和多链路协议扩展后续再做，不沿用旧工程的对象字典作为默认前提。
