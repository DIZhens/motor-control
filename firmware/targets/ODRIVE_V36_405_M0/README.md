# ODRIVE_V36_405_M0

STM32F405RGT6 / 8 MHz HSE，第三方 ODrive V3.6 风格板的 M0，位置源为 MT6701 / SPI3。
硬件事实以 [HARDWARE_BASELINE.md](../../../hardware/ODRIVE_V36_405/HARDWARE_BASELINE.md) 为准。

## 工程入口

- CubeMX：ODRIVE_V36_405_M0.ioc，CubeMX 6.18.1 / STM32Cube FW_F4 V1.28.3。
- Keil：MDK-ARM/ODRIVE_V36_405_M0.uvprojx，ARM Compiler 5 / STM32F4xx_DFP。
- Core / Drivers 由 CubeMX 维护；业务接入只写在 USER CODE 区。
- platform 兑现现有三项公共 HCC，没有 Hardware Trip 实现。

当前 main.c 是 Target-local 观测入口：绑定电流回调，启动三项观测及持续 Drive Timing，
在调试器中查看 s_target_probe。默认保持功率关闭，不调用 Power Enable，
不实现 Motor / Measurement / ControlRuntime / SystemRuntime。

## 静态配置与实现

| 能力 | 配置 |
| --- | --- |
| M0 PWM | TIM1 CH1/2/3 + CH1N/2N/3N；PA8/9/10、PB13/14/15 |
| 电流 | ADC2 PC0 / B、ADC3 PC1 / C；同一 TIM1 TRGO rising，单 injected rank |
| 采样时序 | 168 MHz TIM1、center-aligned ARR=4200、20 kHz；CH4 PWM2 CCR=4116，无输出引脚 |
| 提交边界 | RCR=0，峰/谷均更新；峰后采样回调 stage，下一谷提交，至少保留 10 us 提交余量 |
| 正常包络 | 三相 duty 各 10%～90%，名义电压利用因子 0.45；dead-time 500 ns |
| 电流单位 | 负向 20 mV/A 外部前端的名义换算；只发布 B/C，不重构 A、不标定 offset/gain |
| Control Time | 独立 TIM2 1 MHz，自由运行并扩展为 64 bit；三项观测共用 |
| 位置 | SPI3 PC10/11/12、PA0 CS；约 1 kHz、异步 IRQ，CRC6/status 检查；机械 turn [0,1) |
| 母线 | ADC1 PA6 / VBUS + VREFINT，两 rank DMA；约 1 kHz，39 kΩ / 2.2 kΩ 换算为 V |

电流采集以 ADC3 completion 为唯一入口，检查 ADC2 同次完成和 PWM 周期连续性。
任何不可信事务保持 invalid，不以旧值或补零冒充两相观测。缺失电流事件超过 75 us
由 TIM1 update IRQ 触发 software safe-off。SPI IRQ 无 BSY 等待；事务超时发布 invalid。
Bus 的两 rank DMA 完成后才发布整份观测。latest getter 只做有界快照，不等待外设。

Safe-off 清 MOE，保留 CCxE/CCxNE，并配置 OSSI/OSSR、OISx/OISxN LOW：
六路定时器输出被请求进入 inactive。TIM1 counter、CH4 TRGO 和 TIM2 继续运行。
Safe-off 使旧 epoch、pending duty、enable 条件失效。重新 enable 需要当前 epoch 的
新 actuation 跨过更新边界，且当前有效采样回调再次成功 stage；没有自动恢复旧 duty。
M1 的六路输入和 AUX 两路只配置为 GPIO LOW，不提供相应功能。

## 验证边界

机器静态检查与 C 编译检查不能证明真实功率关断、采样窗口、传感器 framing 或物理精度。
10%～90% / 0.45 是首版待实测包络；VREFINT 使用 F405 的 1.21 V 名义值，
不套用 G431 的 factory calibration。MT6701 的 32 clock framing 需本板无功率 L1 核对。

交付后先由用户对正式 Keil 工程 Rebuild，检查 0 Error / 0 Warning。
配置与工程成员可以重复静态核对（Python 标准库，无额外依赖）：

    python firmware/targets/ODRIVE_V36_405_M0/verify_target.py

后续 L1 分别核对持续 20 kHz 采样、三项时间戳、六路 EG2134 输入及六个 MOS Gate 的关断；
之后才安排限压限流条件下的 L2。当前没有 powered L1/L2 或 Hardware Trip PASS。
