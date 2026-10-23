# interface

Firmware 与 PC Host 的共享合同区域。

长期区分两个层次；现有有限闭环基线与新的二进制接口分别按实际实现和验证范围说明：

```text
Device Contract
    设备有哪些状态、命令、Fault、观测和参数，以及这些数据的单位与行为语义

Protocol Contract
    上述设备语义如何表示为请求、响应、事件和 Telemetry
```

当前设备语义见[DEVICE_CONTRACT.md](DEVICE_CONTRACT.md)：启停、速度目标、状态与故障，
以及关键观测的单位、有效条件和参数生命周期。正式Run的线上表示使用Core Protocol V1；
[VOFA_PROTOCOL.md](VOFA_PROTOCOL.md)保留独立验证构建的旧格式，配置与操作说明位于`host/`。

[MOTOR_NODE_PROFILE_V1.md](MOTOR_NODE_PROFILE_V1.md)拥有共享业务编号、结果与能力子集；
[UART_MAPPING_V1.md](UART_MAPPING_V1.md)拥有UART字段、布局与Session/Sequence规则；
[CAN_MAPPING_V1.md](CAN_MAPPING_V1.md)拥有Classical CAN ID、8字节布局与独立事务规则；
[CORE_PROTOCOL_V1.md](CORE_PROTOCOL_V1.md)只作为历史兼容入口。
[PROTOCOL_DESIGN.md](PROTOCOL_DESIGN.md)解释共享边界，不重复维护字段表。CAN采用独立帧映射，
不能把UART的COBS/CRC/Header当作共享设备语义。合同冻结不代表实现或实物通过。

线格式已有[C / Python 离线编解码及验证入口](../protocol/README.md)，
双方分别对照[固定黄金向量](test_vectors/core_protocol_v1.json)。Run的会话、DMA、RTOS和
有限运动已有独立证据，实际验收范围见[G431验证页](../firmware/targets/BLDC_G431/VALIDATION.md)，
不以合同冻结或库模型推断硬件通过。位置环、标准 Modbus / CANopen 栈和完整工业协议生态后续再做，
不沿用旧工程的对象字典作为默认前提。
