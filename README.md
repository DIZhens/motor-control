# motor-control

基于自研 STM32G431 板、2804 电机与 MT6701 编码器的单电机控制项目。当前实现主线为 FOC 电流内环与速度外环、UART/CAN 自研设备协议，以及原生静态 FreeRTOS 后台任务。

```text
hardware/         板卡、电机、编码器与测试器材的物理事实
firmware/         控制算法、Motor、硬件能力接口及具体 MCU 工程
communication/    Firmware / Host 共享规范、可移植协议与 PC 工具
```

## 推荐阅读顺序

1. [硬件](hardware/README.md)：板卡、执行器与器材。
2. [G431 工程](firmware/targets/BLDC_G431/README.md)：Keil Target、启动和运行入口。
3. [Motor](firmware/motor/README.md)与[控制算法](firmware/control/README.md)：状态所有权和采样到控制输出。
4. [Runtime](firmware/targets/BLDC_G431/runtime/README.md)：实时交接、静态任务及运行装配。
5. [通信](communication/README.md)：共享合同、UART/CAN、Target 接入和 Host。

长期职责见[架构](ARCHITECTURE.md)，工具、Git 和验证规则见[开发说明](DEVELOPMENT.md)。从本地 Git 状态和最近检查点恢复阶段历史。

## 当前边界

正式 G431 Run 默认 UART，可静态选择 CAN；Observe、专项计时和旧台架位于独立 Validation 工程。[F405 M0](firmware/targets/ODRIVE_V36_405_M0/README.md)是同一 Platform API 的第二套实现，单独验收。

已有固定空载的有限启停、设速和关断实测；正式通信工作点为 250 Hz。500 Hz 历史遥测损失 1.92%，仍未通过原小于 1% 门槛。速度性能、外部 CAN 总线、独立电流精度、持续/带载运行和完整 WCET 尚未验收。能力边界与自动验证入口见 [G431 Validation](firmware/targets/BLDC_G431/validation/README.md)。

详细测量与日志仅保留本地，阶段结果保存在 Git 检查点。源码整理和离线构建不产生新镜像的实物验收。
