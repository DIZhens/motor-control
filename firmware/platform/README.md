# Platform 公共接口

这里只保存跨 Target 的硬件能力头文件，不包含 HAL 或某块板的实现。

| 头文件 | 能力 |
| --- | --- |
| [realtime_motor_io.h](realtime_motor_io.h) | 同步相电流、调制提交、使能和 safe-off |
| [position_observation.h](position_observation.h) | 带采集时间和有效性的机械位置观测 |
| [bus_observation.h](bus_observation.h) | 带采集时间和有效性的母线观测 |
| [control_time.h](control_time.h) | 控制域时间、间隔和截止约定 |
| [hardware_trip.h](hardware_trip.h) | 可选、独立于 CPU 的保护能力 |

实现分别位于 [G431 platform](../targets/BLDC_G431/platform) 和 [F405 platform](../targets/ODRIVE_V36_405_M0/platform)。物理语义、采样/施加时序和失败处理见[架构](../../ARCHITECTURE.md#43-platform-与-hardware-capability-contract)。
