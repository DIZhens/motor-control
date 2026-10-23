# G431 Runtime

正式 Run 的运行装配、实时交接、参数和发布，以及原生静态 FreeRTOS。UART/FDCAN 与设备绑定位于 [Communication](../communication/README.md)，硬件能力实现在 [Platform](../platform)。

| 文件 | 职责 |
| --- | --- |
| [target_runtime.c](target_runtime.c) | 硬件装配、ADC 入口、快照和独立守卫 |
| [g431_control_profile.c](g431_control_profile.c) | 固定控制配置、单位及角度后端 |
| [runtime_exchange.h](runtime_exchange.h) | 普通/Stop 请求、ticket 和 Completion |
| [runtime_parameters.h](runtime_parameters.h) | Default / Staged / Active 及遥测配置 |
| [system_runtime.c](system_runtime.c)、[target_system_runtime.c](target_system_runtime.c) | 静态任务、后台调度和 Target 接入 |
| [telemetry_mailbox.h](telemetry_mailbox.h)、[motor_telemetry.h](motor_telemetry.h) | 通用订阅、单槽 Mailbox 和内部观测值 |
| [FreeRTOSConfig.h](FreeRTOSConfig.h) | 项目内核配置 |

## 所有权

ADC sample-ready 是唯一 fast tick，Motor / ControlRuntime 独占实时状态。Communication 只经 RuntimeExchange 交接请求和消费 Completion；SystemRuntime 拥有通用遥测订阅，Server 独占 Session 与线上序号。TIM6 独立守卫先 safe-off，再交接内部 Stop。控制 ISR 不调用 RTOS API。

详细合同见 [SystemRuntime](SYSTEM_RUNTIME.md)与 [RTOS 集成](RTOS_INTEGRATION.md)。

## 固定边界速查

| 项目 | 当前配置 |
| --- | --- |
| PWM / 电流 / 速度环 | 20 / 10 / 1 kHz |
| 速度目标 / 估计排歧上界 | ±80 rpm / 300 rpm |
| VBUS | 10..14 V |
| 软件相电流撤销 | 0.65 名义 A |
| 有限运行 | 1.5 s / ±2 机械圈 |
| 默认速度 PI | Kp=0.001、Ki=0.004 |

具体配置权威为 [g431_control_profile.c](g431_control_profile.c)及独立守卫实现；Hardware Trip 见 [hardware_trip_config.h](../platform/hardware_trip_config.h)，Wire 见[通信规范](../../../../communication/spec/README.md)。参数绑定原电机与接线，名义电流尚无独立绝对精度验收。

## 构建与入口

打开 `MDK-ARM/BLDC_control V1.uvprojx` 的 `BLDC_G431_Run`。从仓库根目录生成源码身份并全量构建：

```powershell
.venv/Scripts/python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tools/generate_build_identity.py
& 'D:/keil5/UV4/UV4.exe' -r 'firmware/targets/BLDC_G431/MDK-ARM/BLDC_control V1.uvprojx' -t BLDC_G431_Run -j0 -o "$env:TEMP/g431-run-build.log"
```

身份覆盖正式工程及配置闭包；生成身份和 Rebuild 均不烧录。历史实测仍属于原镜像。模型入口及当前能力限制见 [Validation](../validation/README.md)，Target 选择见 [G431](../README.md#打开哪个工程)。
