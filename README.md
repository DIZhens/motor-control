# motor-control

基于自研 STM32G431 板、2804 电机与 MT6701 编码器的单电机控制项目。展示主线是 FOC 电流内环与速度外环、自研 UART 设备协议，以及原生静态 FreeRTOS 后台任务。

## 从哪里开始读

仓库只有三个业务目录：

```text
hardware/         板卡、电机、编码器与测试器材的物理事实
firmware/         控制算法、Motor、硬件能力接口及具体 MCU 工程
communication/    Firmware / Host 共享规范、可移植协议与 PC 工具
```

建议沿以下顺序学习：

1. [硬件入口](hardware/README.md)：板卡、电机与编码器。
2. [G431 工程入口](firmware/targets/BLDC_G431/README.md)：Keil Target、启动与主运行路径。
3. [Motor / ControlRuntime](firmware/motor/README.md)及[控制算法](firmware/control/README.md)：状态、采样到控制输出。
4. [G431 Runtime](firmware/targets/BLDC_G431/runtime/README.md)：实时交接、静态任务与运行装配。
5. [通信入口](communication/README.md)：共享语义、UART/CAN 映射、Target 接入与 Host。

跨模块约束见[总体架构](ARCHITECTURE.md)，Git、工具链及验证方式见[开发规则](DEVELOPMENT.md)。根目录 `project_context.ps1` 可只读恢复本地分支、检查点和文档入口。

## 实现与验证范围

正式工程只有 `BLDC_G431_Run`，使用公共 Motor / ControlRuntime、原生静态 FreeRTOS、自研协议及 UART DMA；默认 UART，同一 Target 可静态选择 CAN。观察、专项计时与旧台架保存在独立 Validation 工程。F405 M0 是实现同一 Platform API 的第二个 Target，独立记录验收范围。

已有固定空载的有限启停、设速及关断证据；正式通信工作点为 250 Hz。500 Hz 遥测损失 1.92% 仍未通过原小于 1% 门槛。原 P6 速度性能、外部 CAN 总线、独立电流精度、持续运行和完整 WCET 尚未验收。

全部测量条件、源码/镜像身份、公开证据及限制统一从 [G431 验证页](firmware/targets/BLDC_G431/validation/README.md)进入。目录重构后的构建身份单独生成，历史实物证据继续属于原镜像。

- [Firmware 导航](firmware/README.md)
- [F405 M0 Target](firmware/targets/ODRIVE_V36_405_M0/README.md)
- [Host 客户端与 VOFA 桥](communication/host/README.md)
