# G431 Runtime

本目录保存正式 `BLDC_G431_Run` 的运行装配、后台到实时边界交接、参数和发布，以及原生静态 FreeRTOS。UART/FDCAN 和设备绑定在 [communication](../communication/README.md)，CORDIC 在 [platform](../platform)。

## 构建与入口

正式工程为 `../MDK-ARM/BLDC_control V1.uvprojx`，唯一 Target 是 `BLDC_G431_Run`。从仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tools/generate_build_identity.py --manifest "$env:TEMP\g431-build-identity.json"
& 'D:\keil5\UV4\UV4.exe' -r 'firmware\targets\BLDC_G431\MDK-ARM\BLDC_control V1.uvprojx' -t BLDC_G431_Run -j0 -o "$env:TEMP\g431-run-build.log"
```

这些命令只构建。完整 Target 清单见 [G431 入口](../README.md#打开哪个工程)，观察和专项诊断说明见 [Validation](../validation/README.md)。

## 主运行路径与所有权

1. `Core/Src/main.c` 调用 `system_runtime_start()`；Bootstrap 核对时基、创建静态 Mailbox 和后台任务，再调用 Target 装配。
2. `target_runtime.c` 先 safe-off，绑定 sample/trip、位置/母线及实时 I/O，最后启动驱动时序。启动时序不等于使能功率。
3. ADC sample-ready 调用 `control_runtime_step()`，处理反馈、Stop/请求和控制计算；该边界独占 Motor、估计器及 PI 状态。
4. Communication 经 `target_runtime_submit_request()` 提交完整请求，`runtime_exchange` 是唯一命令/参数/Stop 交接；ADC 消费并产生准确 ticket 的 Completion。Telemetry 只读发布值，后台不直接写实时历史。
5. TIM6 独立检查心跳和有限运行边界。时限到达先 safe-off，再调用 `runtime_exchange_request_internal_stop()`；由下个 fast tick 消费 Stop 和完成旧请求取消。内部 Stop 不分配外部 ticket，已排队的外部 Stop 仍保留自身身份，不等待任务关断。

| 文件 | 职责 |
| --- | --- |
| `target_runtime.*` | 正式 Run 硬件装配、采样入口、快照发布、交接临界区与守卫 |
| `g431_control_profile.*` | 固定控制配置及 Target 私有角度后端上下文 |
| `runtime_exchange.*` | 普通/Stop 请求槽、ticket 和 SPSC Completion |
| `runtime_parameters.*` | Default / Staged / Active 与遥测配置值生命周期 |
| `system_runtime.*`、`target_system_runtime.c` | 静态任务、后台调度和 Target 接入 |
| `telemetry_mailbox.h`、`speed_commissioning.h` | 一致发布的内部值及后台待发 Mailbox |
| `FreeRTOSConfig.h` | 项目内核配置；Kernel/Port 供应源码保持原样 |

交接和参数的详细语义见 [SystemRuntime](SYSTEM_RUNTIME.md)，任务栈、中断优先级、时基及 UART/CAN 静态选择见 [RTOS 集成合同](RTOS_INTEGRATION.md)。控制 ISR 不调用 RTOS API。

## 有限运行入口

正式 Run 的 `ControlRuntimeConfig` 位于 `g431_control_profile.c/.h`，集中持有采样节拍、
零偏窗口、电角映射、默认电流/速度 PI 和数值限制。该 Target 装配还持有配置绑定的私有
CORDIC 上下文；启动时初始化后端，运行期继续由原实时所有者串行使用。`target_runtime.c`
仍负责硬件装配、交接、发布和独立守卫，从同一配置初始化 `RuntimeParameters` 的默认速度 PI。
Observe 在入口内保留独立的关断配置，不链接 Run Profile，也不获得运行许可。

当前开发候选在 Profile 中单独启用 1 kHz alpha/beta 电流控制反馈低通，系数
`0.4558867801` 按固定 10 kHz 采样计算；设为 0 可恢复旁路。滤波不进入校零、原始相电流
过流检查、Hardware Trip 或独立 Guard，也不改变采样/提交边界、PI、目标限幅和电角度预测。
RUN 发布控制器实际采用的滤后 DQ，IDLE/Stop 的观测 DQ 仍来自未滤波电流。
该候选的静态降噪和合成对象验证不代替实机时序、稳定性或 P6 性能验收，证据见
[Target 验证页](../validation/README.md)。

Run 使用既有实物记录中的 7 极对、编码器正方向和 0.0244434178 圈电角度偏置，
B/C 电流在本启动零偏校正后取反、重构 A 相。静态记录的约 1.236 V / 0.394 名义 A
对应约 3.14 Ω，与独立绕组测得的每相约 3.1 Ω 相符，因此用于受限功能试验；
这没有完成独立电流幅值精度标定。相序、符号与电角度记录仍绑定原硬件和接线，不能换机照用。

| 配置 | Run 的固定边界 |
| --- | --- |
| 频率 | PWM 20 kHz，电流环 10 kHz，速度环 1 kHz |
| 异步观测 | SSI / 母线实际采集 1 kHz；每个 fast tick 读取完整缓存，仅新时间戳更新估计 |
| 启动校零 | 健康 IDLE、MOE 关闭、编码器静止连续 100 拍，再采 4096 点；无外部电流注入 |
| 速度目标 | 绝对值不超过 80 rpm；估计运动排歧上限 300 rpm |
| 速度 PI | 默认Kp=0.001、Ki=0.004；内部健康IDLE可整组暂存，Start锁存Active；Iq限幅±0.10名义A、Id=0 |
| 电压 / 母线 | 调制利用系数为 1.8/14，母线允许 10..14 V；电压矢量请求不超过 1.8 V |
| 软件相电流撤销 | 任一校正相绝对值超过 0.65 名义 A |
| 独立有限时长 | 从 Start 被接受起最多 1.5 秒，调速不会续期；TIM6 先 safe-off，再交接 Stop |
| 净位移 | 接受 Start 后按新编码器观测解绕累加；绝对值达到 2 圈立即关断并锁存 bit 6 |


## 构建身份

`tools/generate_build_identity.py` 对正式工程成员、配置及保守头文件依赖闭包计算身份，生成 `firmware_build_identity.h`。源码路径属于身份输入，因此移动文件后必须重新生成并 Rebuild。历史实测继续属于原 HEX；新目录的构建没有自动获得旧实测资格。

## 离线验证

模型集中在 [validation/tests/runtime](../validation/tests/runtime)。从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/validation/tests/runtime/verify_runtime.py --finite-run --exchange
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/validation/tests/runtime/verify_system_runtime.py
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/validation/tests/runtime/verify_foc_runtime.py
```

Observe、旧 VOFA、计算探针和时序预算的原有解释保存在 [Validation 历史说明](../validation/history/runtime-diagnostics.md)。当前验证结论、构建与实物证据统一见 [Validation](../validation/README.md)。
