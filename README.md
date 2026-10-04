# motor-control

面向 BLDC/PMSM 的电机控制系统。

当前目标不是预先建设“通用实验平台”，而是先完成一套可靠、可理解、可持续演进的电机控制系统，同时把两个已经明确存在的变化隔离开：

- 更换电机控制板时，尽量只修改板级 Platform 实现；
- 更换 UART / USB / CAN / TCP 等通信通道时，尽量不改变设备语义和 Host 上层逻辑。

## 仓库结构

```text
control/      可移植电机控制算法
interface/    Firmware / Host 共享的设备与协议合同
firmware/     MCU 固件、Hardware Capability Contract 与各板 Target
host/         Qt6 PC Host
hardware/     已确认的物理硬件事实
```

长期架构见 `ARCHITECTURE.md`，开发、协作和验证方式见 `DEVELOPMENT.md`。

## 当前第一阶段

第一块目标硬件为自研 STM32G431 BLDC 控制板，第一条 Host 链路优先使用 UART。

第一阶段只要求形成一条完整闭环：

```text
Qt Host
   ↓
Device API
   ↓
Protocol
   ↓
UART
   ↓
STM32 Firmware
   ↓
Motor Control
   ↓
Telemetry
   ↓
Qt Host
```

暂不实现 ODrive Target、USB/CAN/TCP、多设备、CANopen、ESP32 Gateway 或通用插件平台。它们只有在真实需求出现后再进入工程。
