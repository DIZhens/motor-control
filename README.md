# motor-control

基于 STM32 的单电机编码器 BLDC/PMSM 控制器项目，覆盖嵌入式固件、功率硬件调试与电机控制工程。

项目主线是完成可复现的单电机控制系统：从板级采样、PWM 与保护，到闭环控制、参数配置和数据观测。成果以实际实现、测量条件、验证结果和故障分析为依据。

既有公共合同继续隔离硬件与通信差异：

- 更换电机控制板时，尽量只修改板级 Platform 实现；
- 更换 UART / USB / CAN / TCP 等通信通道时，尽量不改变设备语义和 Host 上层逻辑。

## 仓库结构

```text
control/      可移植电机控制算法
interface/    Firmware / Host 共享的设备与协议合同
firmware/     MCU 固件、Hardware Capability Contract 与各板 Target
host/         PC 调试工具配置与说明，后续 Qt6 Host
hardware/     板级硬件事实与电机、编码器参考资料
```

长期架构见 `ARCHITECTURE.md`，开发、协作和验证方式见 `DEVELOPMENT.md`。

## 展示目标与交付边界

展示版本以自研 STM32G431 板、一台电机和 MT6701 编码器为基础，完成 FOC 电流内环与速度外环、UART 自研设备协议及 FreeRTOS 后台任务集成，覆盖嵌入式控制、通信和实时软件组织。

交付按控制基线、自研 UART 协议、FreeRTOS 集成依次验收。控制基线先沿用现有 UART 命令和 VOFA，提供可重复的有限范围启停、设速、观测及故障关断；协议与调度迁移分别形成后续增量。具体活动分支的范围、完成条件和下一步由本地 Git 检查点正文保存，恢复方法见 [开发规则](DEVELOPMENT.md)。

CAN 实物联调、Qt Host、位置环、标准 Modbus / CANopen 栈及 F405 同功能验收在后续范围。自研协议统一设备语义，UART / CAN 分别映射；本阶段字段与编号见[Core Protocol V1](interface/CORE_PROTOCOL_V1.md)，冻结合同不表示已经实现或实机通过。

## 实现与验证入口

当前G431控制基线使用公共Motor / ControlRuntime，已取得有限空载UART正反向启动、调速、显式Stop和独立1.5秒关断的实物证据；原生FreeRTOS、UART DMA、会话事务及Python客户端另有逻辑供电证据。实测绑定固定板卡、电机、接线和镜像，不代表持续运行、全部保护或性能验收。Telemetry/Event与通信压力、独立电流精度、持久标定及故障恢复仍未验收，具体范围见Target验证页。

日常开发打开 `firmware/targets/BLDC_G431/MDK-ARM/BLDC_control V1.uvprojx`，唯一正式 Target 为 `BLDC_G431_Run`。观察、专项计时和旧台架另存于同目录 `BLDC_G431_Validation.uvprojx`，共享核心源码；它们不是不同的应用版本。F405 M0 保留独立实现与验证范围。

- [固件入口](firmware/README.md)：Target、公共应用与构建边界。
- [G431 Runtime 入口](firmware/targets/BLDC_G431/runtime/README.md)：正式构建、运行边界、模块职责与验证工程。
- [G431 验证结果与剩余门槛](firmware/targets/BLDC_G431/VALIDATION.md)：G431 验收结论、镜像与证据的唯一汇总入口。
- [F405 M0 说明](firmware/targets/ODRIVE_V36_405_M0/README.md)：独立的实现和验证范围。
- [Host 入口](host/README.md)：V1 的 VOFA 资料职责与后续 Qt Host。
- [硬件资料入口](hardware/README.md)：控制板事实与独立的电机、编码器资料。
- [共享接口入口](interface/README.md)：设备语义、UART 命令与观测合同。

完整本地日志不随仓库克隆分发；公开证据及其测量限制从对应 Target 验证页进入。
