# BLDC_G431

自研 STM32G431 单电机 Target。硬件事实见[板卡说明](../../../hardware/BLDC_G431/README.md)，软件沿以下路径阅读。

```text
Core/main.c
    → runtime/system_runtime.c          静态任务启动
    → runtime/target_runtime.c          硬件装配与实时入口
    → firmware/motor/control_runtime.c  sample → compute → actuation

communication/target_communication.c
    → UART / CAN Server → Device Binding
    → runtime/runtime_exchange.c → ADC 实时边界
```

上图 `Core/main.c` 指 [Core/Src/main.c](Core/Src/main.c)。ADC sample-ready 是唯一 fast tick，Motor / ControlRuntime 是实时状态单写者；控制 ISR 不调用 RTOS API。TIM6 保持独立守卫。

| 目录 | 职责 |
| --- | --- |
| [platform](platform) | 真实 TIM/ADC/PWM/SSI/保护及 CORDIC 后端 |
| [runtime](runtime/README.md) | 正式 Run 装配、交接、参数、发布和原生静态 FreeRTOS |
| [communication](communication/README.md) | UART DMA/FDCAN、Server 装配及设备绑定 |
| [validation](validation/README.md) | Observe、旧台架、专项诊断、模型与工具 |
| `Core/`、`Drivers/`、`MDK-ARM/`、`.ioc` | CubeMX / Keil 工程与供应驱动 |

## 打开哪个工程

| Keil 工程 | Target | 用途 |
| --- | --- | --- |
| `MDK-ARM/BLDC_control V1.uvprojx` | `BLDC_G431_Run` | 唯一正式 Run，默认 UART、原生静态 FreeRTOS |
| `MDK-ARM/BLDC_G431_Validation.uvprojx` | `BLDC_G431_Observe` | 默认关断观测与诊断，不允许 Start |
| 同上 | `BLDC_G431_Timing` | 无功率专项计时 |
| 同上 | `BLDC_control V1` | 旧台架流程及其 ABI |

Observe 使用 `validation/runner/target_observe_runtime.c`，正式 Run 使用 `runtime/target_runtime.c`；两者共享公共 Motor、ControlRuntime 和 Platform，不同时链接两套入口。Validation 不链接 FreeRTOS。

先按 [Runtime 构建说明](runtime/README.md#构建与入口)生成身份并 Rebuild。CubeMX 再生成时核对两份 Keil 工程，保持正式/Validation 成员隔离。当前能力限制与自动验证入口见 [Validation](validation/README.md)；镜像归属和阶段结论见 Git 检查点，详细测量保留本地。
