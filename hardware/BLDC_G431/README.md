# BLDC_G431

本目录记录自研 BLDC_control V1 控制板（STM32G431CBT6）的物理硬件事实。

| 入口 | 内容 |
| --- | --- |
| [HARDWARE_BASELINE.md](HARDWARE_BASELINE.md) | 长期硬件约束、关键引脚、当前实测范围与待验证边界 |
| [HARDWARE_DESIGN.md](HARDWARE_DESIGN.md) | 详细硬件设计与制造基线说明 |
| [schematic/](schematic) | 原理图与相关原始资料 |
| [PCB/](PCB) | PCB 与制造相关原始资料 |
| [2804 电机与 MT6701 编码器](../actuators/2804-mt6701/README.md) | 独立整理的用户提供商家资料，不属于本板实测 |
| [台架验证](../../firmware/targets/BLDC_G431/validation/README.md) | 固件验证入口与能力限制 |

本目录只记录板级硬件；电机、编码器商家标称与板级实测分开维护。全部硬件资料入口见 [hardware/README.md](../README.md)。
