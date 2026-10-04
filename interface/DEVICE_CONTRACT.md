# Device Contract

状态：草案。

本文件将在第一阶段 Firmware 与 Host 并行开发前冻结最小设备语义。

当前只确定需要覆盖以下类别，不提前固定 wire ID 或对象编号：

```text
Lifecycle
    Start
    Stop
    Clear Fault
    State

Control
    Control Mode
    Target

Status
    Active Command
    Fault

Observation
    位置 / 速度
    FOC 关键观测
    母线等必要状态

Parameter
    只开放真实调试/实验需要的参数

Telemetry
    关键连续观测
```

约束：

- Device Contract 描述业务语义，不描述 UART/USB/CAN/TCP；
- 控制模式和目标必须作为完整 Command 语义处理；
- Fault 当前条件、锁存故障、Clear 和 Restart 不混为同一件事；
- 参数单位、范围、有效状态和应用时机属于合同的一部分；
- Telemetry 不是 Motor 真值所有者；
- 具体字段只有在 Firmware/Host 第一阶段需求确认后再冻结。
