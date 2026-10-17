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

## 当前实现

仓库目前处于硬件适配与基础验证阶段。已有自研 STM32G431 板和 ODrive V3.6 / STM32F405 M0 的 Target 工程、公共硬件能力接口及台架验证入口。

- **G431**：完成首板修复后的无电机基础波形、受控保护注入与恢复、静态电流统计和编码器手动转动检查。限于已记录的测量条件；尚未完成带载控制验收，且占空比提交的硬件截止检查仍有实现缺口。见 [验证结果与剩余门槛](firmware/targets/BLDC_G431/VALIDATION.md)。
- **F405 M0**：已有 Platform 适配和功率关闭的观测入口，当前工程的实机验收尚未完成。见 [Target 说明](firmware/targets/ODRIVE_V36_405_M0/README.md)。
- **公共应用与 Host**：正式 Motor / ControlRuntime、控制算法、协议实现和 Qt Host 尚未落地；当前 `main()` 运行的是 Target 台架程序。

两个 Target 的实现与验收范围见 [固件入口](firmware/README.md)。少量原始波形及测量限制见 [公开证据索引](firmware/targets/BLDC_G431/validation/evidence/20261018-baseline/README.md)；完整本地日志不随仓库克隆分发。

## 后续系统目标

第一块目标硬件为自研 STM32G431 BLDC 控制板，第一条 Host 链路优先使用 UART。

后续目标是形成下面的系统链路；此图表示设计目标，不代表当前已实现：

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

已有 F405 Target 保留为第二种硬件适配。USB/CAN/TCP、多设备、CANopen、ESP32 Gateway 和通用插件平台暂不扩展，优先完成单电机系统及其可复现验证。
