# firmware

MCU 电机控制固件。

## 实现边界

G431 的正式工程只有 `BLDC_G431_Run`，装配公共 Motor / ControlRuntime 的有限空载配置；现有 UART 的正反启停、调速及独立时限关断已有实物证据。观察、计时与旧台架保留在独立验证工程，共享 Platform、Motor 与控制算法，不复制业务实现。独立电流精度、故障恢复与持续运行仍未完成。公共 HCC 已定义不等于全部 Target 已完成实现或实机验收；各 Target 验收结论由对应验证入口维护。

| 区域 | 职责与当前入口 | 验证或实现边界 |
| --- | --- | --- |
| `platform_contract/` | 实时电流/驱动、位置、母线、ControlTime 和可选 Hardware Trip 的能力合同 | 逐个 Target 核对实现、失败语义与物理验收 |
| `targets/BLDC_G431/` | 正式 Run 装配、Platform、独立验证工程与工具 | [G431 验证结果与剩余门槛](targets/BLDC_G431/VALIDATION.md) |
| `targets/ODRIVE_V36_405_M0/` | Platform 与功率关闭观测入口 | [F405 M0 说明](targets/ODRIVE_V36_405_M0/README.md)；不属于当前 V1 同功能验收 |
| `app/` | Motor 生命周期、受门控实时控制链、VOFA 编解码和快照映射 | 模型与实物分别验收，故障恢复尚未实现 |

公共应用使用 HCC，不直接依赖某一 Target 的 HAL、调试邮箱或保护注入命令。
两板验证结果互不替代；模型、静态检查、目标编译和实物验证分别记录。
Motor 内部有限静态定向核心及其尚未装配的执行边界见 [Motor 说明](app/motor/README.md)。
协议无关ticket/Completion、参数生命周期和观测发布的当前边界见
[SystemRuntime说明](app/system_runtime/README.md)。

展示目标还包含自研 UART 协议与 FreeRTOS 后台任务；两者尚未接入，按合同、纯Codec、实时交接、RTOS启动、UART DMA及事务逐项集成。调度与中断适配属于Target，编解码不依赖RTOS，FOC保持硬件同步执行。长期边界见[总体架构](../ARCHITECTURE.md)，唯一Wire Contract见[Core Protocol V1](../interface/CORE_PROTOCOL_V1.md)。

控制基线使用 G431、一台电机和 MT6701 的 FOC 电流内环与速度外环，以 UART 对接现成 VOFA 工具。正式构建、验证工程及限制见 [G431 runtime](targets/BLDC_G431/runtime/README.md)。CAN 实物接入、位置环、标准 Modbus / CANopen 栈和 F405 同功能验收在后续范围。

## 长期结构

```text
firmware/
├── app/
│   ├── motor/
│   ├── communication/
│   └── system_runtime/
├── platform_contract/
└── targets/
```

`app/` 保存与具体板卡尽量无关的 Firmware 业务与运行时。上图表达职责边界，目录和模块在有实际实现时建立，不要求先建全目录树。

`platform_contract/` 保存当前 Control Profile 对硬件提出的 Hardware Capability Contract；它只定义公共能力语义，不包含任何具体 Target 的 HAL / Timer / ADC 实现。

`targets/<target>/` 保存某个确定 Target Configuration 的完整 CubeMX / Keil 工程和 `platform/` 实现。Target Configuration 由 MCU、功率级、相电流采样、位置源及静态时序/保护配置共同定义，不要求所有硬件位于同一块 PCB。

正式 Target 保留 `*.ioc`、`Core/`、`Drivers/`、`MDK-ARM/` 与 `platform/`；新增参与固件构建的源码要同步加入 Keil 工程并完成真实 Rebuild，具体规则见 [DEVELOPMENT.md](../DEVELOPMENT.md)。

具体工程入口：

- [BLDC_G431](targets/BLDC_G431/)：G431 自制板 Target；验收统一从 [VALIDATION.md](targets/BLDC_G431/VALIDATION.md) 进入。
- [ODRIVE_V36_405_M0](targets/ODRIVE_V36_405_M0/README.md)：保留的 F405 M0 Target，独立记录实现与验收范围。
