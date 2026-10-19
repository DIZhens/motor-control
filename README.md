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

## V1 主线

V1 收敛为自研 STM32G431 板、一台电机和 MT6701 编码器的 FOC 电流内环与速度外环，通过 UART 和现成 VOFA 工具观测关键数据、启停、设置速度并查看故障。具体阶段目标、期限、完成条件和下一步由本地 Git 检查点正文保存，恢复方法见 [开发规则](DEVELOPMENT.md)。

Qt Host、位置环、Modbus、CANopen、FreeRTOS 及 F405 同功能验收均在后续范围；保留现有目录与替换边界，出现实际职责时再增加实现。

## 实现与验证入口

现有工程包含 G431 和 F405 M0 的 Platform、公共硬件能力合同及台架验证入口。G431 另有默认关断的 Motor / UART 观察入口，已接入编码器速度估计、VOFA 和实时边界命令交接，启动请求仍被拒绝；闭环控制和标定尚未完成，具体实测范围见 Target 验证页。上述 V1 范围是交付目标。

- [固件入口](firmware/README.md)：Target、公共应用与构建边界。
- [G431 默认关断入口](firmware/targets/BLDC_G431/runtime/README.md)：独立构建、UART 和当前执行边界。
- [G431 验证结果与剩余门槛](firmware/targets/BLDC_G431/VALIDATION.md)：G431 验收结论、镜像与证据的唯一汇总入口。
- [F405 M0 说明](firmware/targets/ODRIVE_V36_405_M0/README.md)：独立的实现和验证范围。
- [Host 入口](host/README.md)：V1 的 VOFA 资料职责与后续 Qt Host。
- [硬件资料入口](hardware/README.md)：控制板事实与独立的电机、编码器资料。
- [共享接口入口](interface/README.md)：设备语义、UART 命令与观测合同。

完整本地日志不随仓库克隆分发；公开证据及其测量限制从对应 Target 验证页进入。
