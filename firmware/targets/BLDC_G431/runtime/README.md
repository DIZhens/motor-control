# G431 Runtime 入口

`BLDC_G431_Run` 是日常开发的正式入口，装配公共 Motor / ControlRuntime、Platform 和
原生静态FreeRTOS后台。正式Run关闭旧UART/VOFA入口，使用Core Protocol V1。
USART1以460800/8N1接入RX Circular DMA及单帧TX DMA；Communication唯一持有Codec、
Session/事务Server，提供对象读写、完整命令应答及Telemetry/Event。无功率50/250Hz遥测与
每秒状态请求通过，按250Hz完成本阶段通信验收；500Hz损失1.92%仍未通过原<1%门槛，
作为容量边界保留，详见Target验证页。
同一协议/RTOS镜像已完成一次固定空载`+60→+40 rpm→无会话Stop`，重复同身份Start只执行
一次；457帧遥测连续、225帧RUN全有效，状态Event及停机独立读回通过。范围与镜像见验证页。
Validation裸机入口仍保持115200，不能与Run混用串口设置。
既有实测基线执行固定硬件、空载条件下的有限启停与调速；上电始终关断，必须显式Start。
独立电流精度、持续运行与故障恢复尚未完成；实测镜像、证据及限制统一见
[Target 验证页](../VALIDATION.md)。

`BLDC_G431_Observe` 只用于观察与诊断，装配 `control.enabled=false` 并拒绝所有 Start。
它与专项 Timing、旧台架入口均在独立验证工程，不能用其配置替代 Run 的运行许可。
本页先说明当前正式入口；后半部分单独保留验证入口与冻结历史诊断的解释。

## 构建与入口

主工程为 `../MDK-ARM/BLDC_control V1.uvprojx`，其中只保留 `BLDC_G431_Run`。
独立验证工程为 `../MDK-ARM/BLDC_G431_Validation.uvprojx`：

| 工程 | Keil Target | 入口与用途 | 输出 |
| --- | --- | --- | --- |
| 主工程 | `BLDC_G431_Run` | `system_runtime` Bootstrap调用`target_runtime.c`，正式有限控制装配 | `build/run/` |
| 验证工程 | `BLDC_G431_Observe` | 相同 Runtime，关闭控制并编入专用诊断 | `build/observe/` |
| 验证工程 | `BLDC_G431_Timing` | `timing_entry.c` / `runtime_timing.c`，无功率计时 | `build/timing/` |
| 验证工程 | `BLDC_control V1` | `validation/target_validation.c`，保留旧台架 ABI | `BLDC_control V1/` |

选择 Target 后使用 Keil Rebuild。仓库根目录的 CLI 等价命令如下；工具路径按本机安装调整，
日志使用绝对路径。这些命令仅构建，不连接 ST-Link 或烧录：

```powershell
.\.venv\Scripts\python.exe firmware/targets/BLDC_G431/runtime/tools/generate_build_identity.py --manifest "$env:TEMP\g431-build-identity.json"
& 'D:\keil5\UV4\UV4.exe' -r 'firmware\targets\BLDC_G431\MDK-ARM\BLDC_control V1.uvprojx' -t BLDC_G431_Run -j0 -o "$env:TEMP\g431-run-build.log"
& 'D:\keil5\UV4\UV4.exe' -r 'firmware\targets\BLDC_G431\MDK-ARM\BLDC_G431_Validation.uvprojx' -t BLDC_G431_Observe -j0 -o "$env:TEMP\g431-observe-build.log"
```

Observe / Run 的 `Control`、`Application/User/Motor`、`Application/User/Runtime` 三个组显式使用
ARMCC `-O3 -Otime`，其余组保留原继承配置。按速度优化使短数值检查能够内联；源内
`#pragma Otime` 不能替代本机编译器的翻译单元内联策略。没有开启 fast-math 或改写
有限值检查、除法和归一化公式。正式 Runtime 与私有同源副本采用相同策略。
核对实际链接对象的编译记录及机器码，不按目录中残留的同名 `.o` 推断当前镜像。
相关 ARM 模型显式使用 `-Otime`；模型、历史计时和当前镜像的实测分别判定。

Observe / Run 用 `BLDC_G431_RUNTIME_ENTRY` 选择同一 Core 装配入口；Run 再定义
`BLDC_G431_FINITE_RUN`及`BLDC_G431_FREERTOS`，Observe用`BLDC_G431_DIAGNOSTICS`编入实验钩子，并保留
`BLDC_G431_OBSERVE_ENTRY` 兼容标记。Run 不编入专项诊断邮箱或中断测量钩子。
Timing 使用 `BLDC_G431_TIMING_ENTRY`。`main()` 和 IRQ 的 USER CODE 选择互斥入口。
两份工程共用 CubeMX 配置、Platform、Motor 和控制算法源，不复制实现或同时绑定两个 consumer。

## 有限运行入口

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

既有VOFA实测基线串口为115200 / 8N1，当前Run已移除这条装配，以下命令仅解释历史证据。
`start:60\n`、`speed:40\n`和`stop\n`
分别走真实 Motor 请求和实时边界，表中的 `\n` 表示实际 LF。启动仍跨两拍提交中性 duty，
成功使能才进入 RUN；有效 Id/Iq 来自相同正式控制反馈。运行不执行 Observe 的隔离计算、
提交预算或电流诊断窗口，后台只处理 UART 和一致性遥测。未重新定义 VOFA 通道。

正常 Stop 与时长上限都撤销输出并进入 IDLE，不代表主动减速至零，停机后可能继续滑行。
`clear` 仍不能恢复锁存故障或重启；故障后需现场确认并复位重装配。
`g_target_runtime_run_status` 是停止后读取的 32 B 私有记录，保存接受次数、限制原因及净位移，
不是可写命令邮箱或公共协议。TIM6 保留采样心跳守卫，Platform 保留原 slot 绝对截止与首因锁存。

本入口已完成正式 UART 正反向启动、目标更新、显式Stop及独立1.5秒时限实测，范围见
Target验证页；2圈边界实机触发、正式RUN故障注入及长期运行尚未验证。
本入口只为固定空载台架的有限功能验收。烧录前使用逻辑供电检查默认关断；恢复母线供电后
在默认关断状态重新启动一次，使零偏在本次供电条件下形成，再由操作者确认本轮参数。
编译、HCC 替身模型及历史台架时序均不能冒充新 Run 镜像的实机验收或 WCET。
原速度峰值误差筛选失败仍是性能项，不能写成满足全部性能指标。
VOFA连接、Run单次命令、校零失败后的现场复位及Observe区别见
[Host操作说明](../../../../host/vofa/README.md#有限-run-操作)。

离线检查：

```powershell
.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py --finite-run
.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py
```

## 内部事务交接与参数

Run已装配[SystemRuntime交接](../../../app/system_runtime/README.md)：非零ticket、独立Stop槽、
深度4静态Completion、参数写的ADC权威结果及Staged/Active生命周期。后台不能直接修改PI实例。
速度调参publication只复制已有状态，RUN按真实速度更新边界发布；观测读取不推进样本序号。
内部可信性丢失立即safe-off并撤销Session逻辑资格，下一ADC把bit7合入Motor；
首因与计数单独保存，不改变Clear或现有有限保护。API边界见`target_runtime.h`。

当前请求由唯一Communication任务的Server使用，`target_core_server.c`完成对象快照映射、
准确ticket交接及协议无关Fail-closed连接；不从后台修改PI实例。原VOFA装配只保留在验证
入口与兼容模型中，不是Run的第二个后台所有者。新协议已完成对象/事务、名义通信及一次
固定空载正向启停调速，仍按各自HEX保存证据；持续运行、带载和完整保护尚未验收。

```powershell
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py --finite-run --exchange
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tests/verify_system_runtime.py
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tests/verify_uart_dma.py
.\.venv\Scripts\python.exe -X utf8 firmware/app/communication/tests/verify_core_server.py
```

任务职责、栈预算、四个独立时间域及异常归属见[RTOS集成合同](RTOS_INTEGRATION.md)。
任务模型替代异常上下文切换边界，不等同于真实调度器、实机栈余量或中断延迟通过。

`uart_dma_transport.c`独占USART1及DMA1通道2/3，事件只唤醒Communication。接收按DMA
位置与边界旗标核对进度，不能按通知次数推导字节；连续性断开时返回0字节及新epoch。
发送最多130 B、内部复制且单帧在途，DMA TC后须等待USART TC；10ms超时或内部不变量
失败锁存关断。具体时序界限见[字节流边界](RTOS_INTEGRATION.md#字节流与后台边界)。
ARM模型使用模拟MMIO与字节复制替身，另以生产C库的真实串口回环核对复制结果。
`tests/uart_transport_loopback.c`仅供逻辑供电临时镜像：替代后台service执行原始回环、
有限暂停读取及故意冻结TX。它不含Motor命令、不加入任一正式工程，也不新增长期Target；
实测完成恢复正式Run，故障测试不能作为业务恢复入口。
正式Server通过正常引用保留TX/统计API，不使用强制keep或测试请求作为业务入口。

## 构建身份

修改正式Run源码、配置或依赖头文件后，先运行上面的`generate_build_identity.py`，再从Keil
Rebuild；它只在内容变化时更新`firmware_build_identity.h`，不使用编译时间。正式工程成员、
配置及保守头文件依赖闭包按仓库相对路径和去BOM/LF内容排序计算SHA256，取前64 bit为
DEVICE_INFO的build ID，排除生成头本身以避免递归。条件分支头也纳入保守闭包，编译器系统
头不入摘要，工具链版本另记；`--manifest`保存完整来源摘要，短ID不替代HEX/AXF核验。

`--check`只核对身份是否过期，用于冻结和烧录前检查；直接Keil构建不会替代生成步骤。
源改变后遗漏该步骤会被检查拒绝，不能把旧ID附会为新源码已验证。Python依赖安装见
[开发规则](../../../../DEVELOPMENT.md#44-本地验证依赖)。

## 实时频率与作用时刻

TIM1 载波保持 20 kHz；RCR=3 使三相 preload 每100 µs在指定谷底同时生效。
每个slot仅采首个峰值，电流环10 kHz，速度环每10个样本更新一次即1 kHz。
Platform 的 SSI / 母线实际采集为 1 kHz。Run 每个 100 µs fast tick 读取完整缓存，
只在新采集时间戳到达时更新估计；Observe 仍按 10 个样本分频读取。
测量到下一有效更新约75 µs；若以随后100 µs电压保持区间的中心作为逆Park参考，
名义施加角预测为125 µs。电流PI dt为100 µs、速度PI dt为1 ms。
当前Observe控制配置仍关闭；这些时序参数不构成电流增益/角度标定或功率许可。
完整slot、预热、截止和失败语义见[时序合同](../validation/PWM_TIMING.md)。

## 执行职责

- 装配先 safe-off，再核对时钟、绑定 sample / trip、arm position / bus / realtime / protection，
  最后启动持续运行的 Drive Timing；这不等于功率使能。装配失败保持 FAULT 和关断。
- `target_runtime.c` 保留硬件装配、板级时序/采样检查、独立守卫、命令交接临界区与快照发布。
  ADC sample callback 调用 Motor 内部 `control_runtime_step()`；该实时边界是运行期
  Motor、观测缓存和估计器的唯一写者。故障和 Stop 优先，随后更新反馈并消费其余完整命令。
  Stop 优先于普通排队请求，并丢弃更早的待处理命令。
- TIM6 每 50 µs 检查独立 DWT 时钟和采样心跳；间隔超过 125 µs 锁存故障并 safe-off。
  最迟在后续一次 TIM6 检查发现；它不是完整 ISR WCET 或未来带载控制截止证明。
- 裸机Observe及既有实测基线的`target_vofa.c`拥有接收行、片段超时、命令映射与遥测发送缓存；通过协议无关的
  完整 `MotorCommand` 提交和一致性 `MotorSnapshot` 读取与实时装配交接。
  `target_uart.c` 封装 USART1 寄存器轮询及硬件错误，不解释 Motor 语义。
  后台每次至多收、发各一字节，44 字节帧发送完毕前不被新快照覆盖；UART 不进入 ADC 回调。
- 实时侧publication保存协议无关的`MotorSnapshot`；裸机后台经`motor_vofa`映射到
  原十通道格式。publication 按 100 Hz 或命令/故障/反馈有效性变化生成，后台约 100 Hz
  编码。Run 可发布活动目标、控制时间、母线、机械速度和有效的名义 Id/Iq；Observe 的
  目标保持零、Id/Iq 无效。旧快照失效资格仍由 Runtime 判断，不交给协议编码器猜测。

`MotorSnapshot.command_result` 是最近一次处理结果，不具有逐请求关联或可靠应答语义。
自研协议通过独立ticket Completion关联实际结果，不能从这个最近值推断某请求已经完成，
也不能把提交成功解释为已经启转。事务属于Communication，不增加Motor或控制ISR的协议状态。

## FreeRTOS 的接入边界

本阶段已固定[RTOS集成合同](RTOS_INTEGRATION.md)及[Core Protocol V1](../../../../interface/CORE_PROTOCOL_V1.md)，
按纯Codec、实时交接、RTOS启动、UART DMA、事务和压力逐项接通。

当前Run已使用静态Bootstrap、Communication、Telemetry和Idle；Validation保持裸机轮询。
RTOS只承接后台通信、遥测与维护；
电流环、速度环仍在硬件同步 ADC 边界执行，保持 Motor / ControlRuntime 单写者，不改成独立任务。
控制 ISR 不等待任务、队列或互斥锁，不调用内核 API。

Run的TIM7提供HAL tick，SysTick/PendSV/SVC由原始Port独占，TIM6独立守卫不变；
USART1由DMA Adapter独占，协议事务在Communication任务。优先级、栈及静态RAM预算见
RTOS集成合同，真实启动、请求负载高水位及默认关断证据见Target验证页。串口压力和有限运行工作点需
分别补验，空闲启动结果与静态调用图不能证明最坏栈深度。

## Observe 反馈与校零配置

以下两节说明验证工程的 Observe 装配；Run 的极对数、方向、偏置与
校零点数以“有限运行入口”为准。Observe 故意不确认控制标定，不表示整项目仍未完成有限 FOC。

### 编码器反馈

当前仅支持 Platform 提供的周期单圈位置，每 10 个 fast tick 读取一次 latest observation。
只在采集时间戳前进时更新纯 C `PositionEstimator`，以实际采集间隔计算最近周期差，
不把重复轮询的旧位置当作新采集。速度正负沿编码器机械坐标，尚未标定为电机正转方向。

首份合法位置只建立基线，第二份开始给出速度；低通时间常数为 10 ms。观测排歧界限暂设
3000 rpm，仅说明估计器的运动假设，不是电机运行限值，也不能保证识别超过该假设的混叠。
位置年龄超过 100 tick（5 ms）、时间回退/超前、采集无效、未 referenced 或坐标非法时，
丢弃估计历史。恢复需要两份新的合法位置。暂时读不到 coherent publication 时保留尚未
过期的缓存，不伪造采集失败。母线也保留尚未过期的完整缓存。

反馈有效性变化立即更新 publication，不等到下一次 100 Hz 周期；已经开始发送的完整旧帧
仍按历史快照发送完毕。缺少速度反馈时保持观察状态 IDLE，不把尚未具备运行条件伪装成
硬件 Fault。该入口仍未完成电角度标定或 FOC，获得有效速度也不能接受 Start。

ControlRuntime 已接入纯 C 电角度映射与短时预测：缓存合法原始机械位置，以其采集时间
计算年龄，在每个同步 fast tick 更新角度。极对数、方向和偏置通过数值检查后，还需
装配方独立确认实际标定。Observe 的该确认标志为 false、参数为空，电角度始终无效。
采样时间重复、跳拍或回退时清位置与角度历史、重置观测分频，异常拍不消费新位置。
位置失效后恢复需要两份新的合法观测；单纯预测超时只撤销角度，仍保留尚新鲜的速度。

预测使用当前低通后的编码器坐标速度；加减速误差、SSI 实际延迟与 PWM 生效时刻的关系
仍需在实际控制前验收。电角度方向确认后，速度反馈也按该方向统一坐标；未确认时保留
当前编码器坐标。仅电角度配置不能开放功率或有效 Id/Iq，还需独立的电流和时序确认。

### 无功率电流零偏

当前 Observe 构建的启动校零许可仅适用于电机母线电源、电机三相及外部注入源均已断开的现场条件。
该许可不是硬件检测结果，不能照搬为正式带电入口的通用启动策略。
Motor 内部 ControlRuntime 在健康 IDLE、当拍 MOE 关闭、有效速度绝对值不超过 5 rpm
持续 100 拍（10 ms）后，收集连续 2048 拍 B/C 名义电流；10 kHz 下窗口约 204.8 ms。
ControlTime 保持 20000 tick/s，相邻采样时间必须相差 2 tick，重复、跳拍或回退均撤销零偏资格。

每相窗口均值绝对值上限暂为 0.10 A，峰峰值上限 0.05 A；这两个数是本次工程候选门限，
不是传感器精度、运行电流限值或已通过实测的统计范围。合格窗口后冻结 offset，再逐拍
校正 B/C、重构 A 并计算 αβ；历史零偏不硬编码到本次窗口。
校零窗口中失去静止反馈、离开 IDLE 或功率关闭条件，会锁定 FAILED，不自动重试。
READY 后只校正，不重新学习；正常 RUN 与 Stop 保留冻结偏置，坏采样、时间异常和故障撤销校准资格。

`s_control_runtime.current_measurement` 保存状态、首因、样本数、窗口均值/极值、
冻结 offset 和相电流/αβ 有效性，供本地诊断。它不改变十通道 VOFA 格式；当前实机的
控制配置和可信标定尚未开放，Id/Iq 仍为 NaN。READY 只证明本次零偏窗口满足软件门限，增益、极性和实际采样窗口仍待实测。

可移植电流 PI、速度 PI、Park 和 SVPWM 已接入公共 Runtime，当前装配仍关闭控制配置；
最新构建和已完成的无功率诊断实测身份见 Target 验证页；这不开放实际控制链。

## 公共受门控控制链

`ControlRuntimeConfig.control` 装配电流/速度 PI、慢环分频、母线范围、相电流阈值和电压作用
时延。数值参数与三项实物确认分别检查：电流增益/方向、电角度、采样/调制时序均可信，
且本拍反馈完整、新鲜、在范围内，才允许接受 Start。控制关闭时整组参数不参与观察流程。
电压利用因子、控制时基与周期还须符合 HCC facts。

Start 当拍先 safe-off 清旧 epoch，并保持 IDLE/零活动目标；下一拍 stage 三相 0.5 duty，
再下一拍跨过确定 update boundary、重新 stage 中性 duty 后调用 enable，成功才提交 RUN。
RUN 每拍执行电流环并 stage；速度环在首次运行拍计算，其后整数分频，输出在两次计算间保持。
Stop 清目标、待启动请求和两环积分，保留正常冻结零偏；任何活动反馈、计算、提交或功率错误
均主动关断、锁故障，不自动重启。普通非法运动目标只拒绝命令，保留已有合法 RUN 目标。

电流预处理按实测相执行 `signed_gain*(nominal-offset)`，再重构缺相和 Clarke；未启用增益
修正时按 1 处理。零偏窗口及其门限始终使用名义 A，修正不会回写窗口。每相系数的数值检查
与实物标定确认分离。
本拍 Park 使用同步采样角；inverse Park 与限幅回算使用独立的电压作用参考角。
`actuation_prediction_time_s` 由装配显式提供，不能从“下一周期”推定准确物理延迟。
速度 PI 只跟踪自身 Iq 限值，电流 PI 跟踪调制器电压限幅；实际电压饱和下的级联动态仍需整体验收。

Observe 的控制配置为零、运行许可关闭，仍把意外 MOE 视为故障；Run 使用前述有限装配。
三角函数、PI、SVPWM 及提交必须满足 Platform 给出的本 slot
绝对截止；当前首峰采样到下一有效谷底约 75 µs，另留 10 µs 事务预留，实际可用预算还须
扣除 ADC 和入口开销，不能沿用旧半周约 15 µs 的窗口。模型与编译不证明实机执行预算。

## 历史后台无功率计算计时

本节及下一节保留旧 50 µs 候选的工具语义与结果解释，不是正式 Run 的日常操作。
历史工具仍按其冻结镜像、布局和许可检查，不能用于新镜像直接发诊断请求。

`compute_probe.h/.c` 调用正式电流预处理、电角度、速度 PI 与电流 PI；
`compute_comparison.h/.c` 为软件 sin/cos 和 G431 CORDIC 各持有一份独立算法状态。
六例覆盖正常、速度保持、独立施加角、限幅及两条预期拒绝路径，每例每后端 16 次，
共 192 个计时算例。同一轮交替运行软件和硬件，输入相同、积分历史各自推进。
之后逐轮扫描 256 个等分电角度和 1 的前一浮点数，核对分量、单位圆及配对 duty 误差。
主循环每轮至多执行一个算例或一个扫描点，duty 只保存诊断，不提交硬件。
它不调用真实 ControlRuntime，不改实物标定确认，不 stage、不 enable、不清故障。

电角度的默认 API 继续使用软件后端；纯 C 的 `ElectricalAngleTrig` 仅接收已经预测、
映射并归一化的角度，Target 私有的 `cordic_math` 拥有唯一硬件实例。CubeMX 的 `.ioc`
开启 CORDIC 并生成时钟初始化，但本机模式库不配置计算功能。适配器启动时一次配置
COSINE、6 cycles、scale 0、两个 32 位输入输出；每拍显式写角度和模长，读 cos 后读 sin。
有界等待、重入或配置异常均拒绝，外设失败隔离到 MCU 重启，不静默回退软件。
Observe 装配显式选择该后端，但电角度物理确认和控制许可关闭，正常观察不计算
未标定角度；历史诊断由下述隔离试具执行合成计算。Run 也使用真实 CORDIC 后端，
`cordic_math` 因而属于正式 Target 数学适配，不是可随诊断删除的测量程序。
共享栈由 `.ioc` 配置为 4 KB，再生成 startup，
用于容纳新增后台调用链、函数指针回调与中断嵌套；静态调用图仍不能证明最坏栈占用。

主机只向 `g_target_runtime_compute_request` 写一次非零新身份。后台是实例、身份消费和
结果发布的唯一写者；开始及终态通过 `g_target_runtime_compute_sequence` 发布
`g_target_runtime_compute_result`。ACTIVE 只含开始状态，COMPLETE 才含完整统计。
重复身份不重放；活动请求遇新身份锁存 BUSY，不隐式运行替代请求。

ADC 同拍发布 IDLE/OFF、控制关闭且母线有效、处于 0..3.6 V 的诊断许可；许可变化立即
发布。后台在计算前后另查 MOE、guard、邮箱、DWT 与采样心跳；只在极短临界区读取心跳
时间对，计算期间不屏蔽中断、不清零 DWT。Stop 在后台直接取消，即使 ADC 已消费并
清掉 Stop pending，也不能漏取消或同轮重开。最后一步后失去健康条件会撤销 COMPLETE。

每例独立计量 total、电流处理、测量角、施加角、速度更新或保持、电流控制六段。
未执行段保持零次；total 包含分段边界、控制流及读数开销，不能用不同算例极值相减
推算函数成本。八个可返回异常 handler 各自维护单写计数槽，stamp 前后求和夹住 DWT
读取，冲突最多重取三次，不屏蔽计算期间的中断。段内进入计数不变的样本另记 clean；
其余样本保留总耗时和 IRQ 进入数，不估算或扣除 ISR 周期。
clean 仍包含仪器化开销、Flash 与外设访问延迟，不是函数净耗时或 WCET。
按 170 MHz 配置换算；200 µs 的单例诊断上限只用于拒绝异常读数，不是实际控制预算。
该诊断不是完整 ISR 或 stage 截止验收，也会延后 UART 轮询，不能同时验收串口高负荷。

历史控制器分段候选定义 `CONTROL_CURRENT_PROFILE`，在正式电流控制函数内记录七个
边界，区分输入校验、相量准备与 Park、PI 与逆 Park、SVPWM、施加 Park 与积分、输出提交。
算法仅在该编译宏下调用通用私有钩子；默认算法构建没有探针调用，也不引入 Target 头文件。
后台独占 `controller_profile` 实例：128 次有效调用形成七行统计，32 次预期拒绝只计数；
完成后才冻结整窗。每行分别保留全部与 clean 样本，不能把不同 clean 集合的均值相加。
正式控制预算验收还需使用对应的无探针构建，不能用插桩结果扣一个估计常数代替。

主算法完整冻结后，同一请求才启动 `irq_timing` 的 256 点被动窗口，避免两类仪器化
同时争用 CPU。ADC IRQ 是该统计的唯一写者；平台继续使用原来的 cycle 安全锚点，
没有移动 stage 截止或屏蔽计算中断。四行分别为 IRQ USER CODE 区间、锚点到 consumer、
consumer 同步调用和 Observe 回调，后三行嵌套而非顺序相加；regular-only ADC IRQ
只进入第一行。首个 IRQ 用于准备，后续才计点。读数包含抢占与部分插桩，排除异常入栈、
出栈及末尾统计，且当前控制关闭，因而不证明正式 RUN 下的 ISR 或 stage 预算。
控制器和 IRQ 各有独立奇偶版本号，只发布启动及终态；任一失败都不能当作完整窗口。

CORDIC 独占采用最多四次 exclusive 尝试；普通异常清除 reservation 时仅重取该锁，
发现 busy 已占用立即拒绝，不等待持有者。四次均失败仍拒绝，不屏蔽中断或回退软件；
配置、时钟、残留结果及就绪超时继续隔离外设，锁重试不等于重试硬件事务。

当前 Observe 候选已取消 `CONTROL_CURRENT_PROFILE`，控制器内部无上述七个探针调用。
旧后台工具需要这些统计，因此本候选在请求写入前明确撤销旧 compute 许可；历史窗口
继续绑定其冻结镜像。100 µs slot统一使用独立Timing入口，不能把不同测量布局或验收结论互换。

[`tools/compute_probe.py`](tools/compute_probe.py) 默认只冻结并核对同次 HEX/AXF/MAP、
真实 DWARF 字段、短枚举和对照 schema；旧单后端布局不能套用到新镜像。
只有显式 `--measure --logic-only-confirmed --vbus-dmm-v <实测值>`
才 HOTPLUG 进行一次请求；失败不补发，不烧录、不复位。现场须由操作者确认母线电源、
电池、三相和外部注入源断开。实测结果及解释见 [验证页](../VALIDATION.md)。

```powershell
.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tools/compute_probe.py --build-dir <同次构建目录> --expected-hex-sha256 <完整SHA256> --output <新目录>
```

## 历史无功率提交预算诊断

以下诊断实现和独立模型仅保留原50 µs采样候选的解释。当前100 µs入口明确撤销
旧compute/stage/IRQ诊断许可；旧工具离线核对fast_tick_period_ticks，仅接受原值1，
新值2会拒绝并提示使用`BLDC_G431_Timing`，不会套用旧半周期期限给出PASS。


`stage_budget.c` 拥有独立 Motor/ControlRuntime 试具；`stage_budget_runtime.c` 仅通过符号
重命名编译同一正式源码，不复制算法，也不在公共 Runtime 增加诊断业务模式。合成电流、
位置、12 V 母线、校正参数、标定标志与模拟 RUN/power 输入仅属于该试具，真实业务对象
保持未标定、控制关闭。测量/施加角使用实际 CORDIC；刷新拍先执行真实 HCC latest 读取，
再提供合成数值。SSI 与母线采集仍按原 Platform 尾部调度，不把异步采集全耗时算入前段。

试具的初始化、两点合成校零和历史构造在 Drive Timing 启动前执行；ADC 准备拍只复制
固定模板。新请求由 ADC 单写者消费，准备拍不计量；随后最多 80 拍覆盖普通快拍、速度
更新、观测刷新及调制限幅。原锚点、当前方向、CNT、DWT 和真实 HCC stage 返回值均保留。
使能端口永远拒绝，正常结束或首个失败均实际 safe-off，冻结完整统计或失败拍原始读数。
Stop 优先取消；其他命令拒绝并中止窗口。重复身份不重放，失败不自动重试。

Target 先做原有保护与命令检查，再运行该诊断；真实 OFF 观察保持逐拍运行，安排在诊断
stage 后，防止跳过采样或伪造真实 Runtime 时间历史。该尾部是诊断附加开销，不能据此
声称完整正式 RUN ISR/WCET。六行是不同分类与嵌套区间，不能相加。固定窗口通过也不
构成最坏执行时间或实物闭环验收；本轮实测提交超时，详见 [验证页](../VALIDATION.md)。

[`tools/stage_budget.py`](tools/stage_budget.py) 默认只冻结和核对同次 HEX/AXF/MAP 及真实
DWARF；仅显式 `--measure --logic-only-confirmed --vbus-dmm-v <参考读数>` 执行一次请求。
前检要求真实 IDLE/OFF，前后核对 Flash、CPU、guard、时基和低母线，后检记录真实状态
与故障。FAILED 仍保存完整结果、
首因与后健康，不变成 PASS；超时或应答丢失不补发、复位或清故障。
MAP 压缩 RW 时核对明确的 `COMPRESSED[...]` 物理加载长度，HEX 与 ELF 仍逐字节相同。

```powershell
.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tools/stage_budget.py --build-dir <同次构建目录> --expected-hex-sha256 <完整SHA256> --output firmware/targets/BLDC_G431/validation/local-evidence/<新目录>
```

## 独立的无功率调用与IRQ计时

`BLDC_G431_Timing` 只用于区分计算、真实提交与外围开销。它绑定一个采样 consumer，
保留真实电流采样、SSI、母线采集、硬件保护和独立 TIM6 守卫，每拍最多执行一次
私有同源 Runtime；不再追加 Observe 的 OFF Runtime，不装配 UART 业务或功率使能。
正式 `app/motor/control_runtime.c` 与算法源没有为此修改。

`runtime_timing.c` 启动时构造固定合成历史，准备拍只复制模板，随后测量 80 个真实 ADC 拍。
32 位请求为 `(nonce << 2) | mode`，nonce 非零；同一完整请求不重放，活动时新请求只中止
旧窗口。显式新 nonce 才能另开窗口，故障与超时均不自动重试。

| 模式 | 被直接 DWT 读数包围的唯一调用 | 成功统计的含义 |
| --- | --- | --- |
| `stage`（1） | 真实 `realtime_motor_io_stage_phase_duty()`，交替不等三相合法 duty | 80 次真实调用、80 次接受，无 Runtime、sink 或 enable |
| `compute`（2） | 同源 `control_runtime_step()`，合成 RUN、电流和反馈 | 80 次计算，8 次速度更新、8 次观测刷新；80 次数值 sink，真实 stage/enable 均为零 |
| `compute-stage`（3） | 同源 Runtime 内原提交点直接调用真实 HCC | 80 次计算与真实提交全部接受，sink/enable 为零；保留 8 次速度与观测刷新 |

sink 只检查归一化 duty 的有限值与正常包络，不能被解释为硬件提交接受。compute 模式
允许跨越中间峰/谷计算，但必须在当前slot的绝对截止以前完成IRQ末标记。
stage/IRQ预算从Platform只读getter取得，不按CNT/DIR推算；新slot不会刷新旧请求的预算。
真实MOE始终须为零。合成标定、RUN 和 12 V 输入不是现场测量或控制许可。

`compute-stage` 复用相同隔离试具，在同一次 Runtime 调用的原提交位置进入真实 HCC，
不在计算返回后另补 stage；直接调用计时因此包含真实提交。每拍最多一次提交，HCC
拒绝保留 STAGE 首因，任何 enable 尝试仍永远拒绝。第 80 拍立即关断会取消最后一组
pending，因此 80 次 accepted 不代表全部请求均已跨 UEV 装载，也不代表带电闭环通过。

结果 schema=2，布局为 256 B，9 行分别记录：直接调用、原 Platform 锚点至调用返回、IRQ 首末 USER
标记、锚点至调用入口、调用返回至末标记、速度拍、观测刷新拍、普通快拍和慢拍。
许可、模板绑定、合成输入和额外 MMIO stamp 在直接调用区间外；调用区间仍含 DWT
及调用本身的测量开销。各行包含分类与嵌套区间，不能相加推算函数净成本。

IRQ 标记区间包含 HAL、健康 publication 及 Platform SSI/母线尾部；末标记后的统计、
即时 safe-off 和异常入出栈未计入。前 79 点逐拍聚合；第 80 拍只保存固定计时及分类
快照并立即关断，保持 active。下一采样空拍先核对真实许可，再聚合末点、核对完整覆盖，
复核关断并发布 256 B 结果；不执行第 81 次控制，也不覆盖第 80 拍原始读数。
若末拍完整 IRQ 被判为迟到，或收尾前许可丢失、Stop/BUSY 取消，冻结为 79 个已确认点；
真实调用计数与末拍原始读数仍保留，不补成成功。准备和终态收尾拍不属于 80 点。regular-only IRQ
不充当控制拍，缺失或未配对IRQ标记不能COMPLETE。结果另存同一slot的stage/IRQ预算，
超过绝对截止即失败；成功的有限窗口也不等于未插桩完整ISR已满足该预算或WCET。完整结果与独立健康后检须分别判定。

真实健康快照schema=2，为64 B，按 100 Hz 或许可/故障变化发布；不伪装成 Motor IDLE 快照。
独立心跳仍逐拍更新，TIM6 故障单独锁存。前后检查 CPU、MOE、guard、推进的时基、
低母线及有效参考位置；末标记前再读权威 Hardware Trip，不能只凭拍首许可发布完成。

[`tools/runtime_timing.py`](tools/runtime_timing.py) 默认只冻结并核对同次 HEX/AXF/MAP
和 DWARF。实测须明确 `--measure --mode stage|compute|compute-stage --logic-only-confirmed
--vbus-dmm-v <参考读数>`，每次只写一个完整请求。工具不烧录、复位、清故障或补发；
失败保留首因、原始读数与后健康。现场只由 ST-Link 提供逻辑供电，母线电源、电池、
电机三相及外部注入源断开。有限诊断结果不授予带电 FOC 许可。
三种模式继续使用 schema=2 / 256 B；新工具同时核对 `COMPUTE_STAGE=3` 的 DWARF 枚举，
旧 AXF 或旧 prepared 缺少该能力时在连接设备前拒绝，不通过猜测旧布局发送 mode 3。

```powershell
.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tools/runtime_timing.py --build-dir <同次Timing产物目录> --expected-hex-sha256 <完整SHA256> --output firmware/targets/BLDC_G431/validation/local-evidence/<新目录>
```

## 已知电流诊断采集

`current_capture.h/.c` 是此 Target 的无功率诊断统计器，不属于 Motor 命令、控制算法或
UART 协议。仅在健康 IDLE、当拍功率关闭、零偏 READY 且位置速度有效静止时，接受一次
固定 2048 点的原始 B/C 电流窗口。请求当拍是首点，10 kHz 下窗口约 204.8 ms。
start 时复制冻结零偏；窗口只计算原始均值、极值及无偏样本方差，不学习或应用增益。
主机另以原始均值/极值减冻结零偏得到校正统计，方差保持不变。

调试入口仅允许主机向 `g_target_runtime_current_request` 写一个非零新序号；保持同一
请求序号不会重放。开始、完成或失败时，用奇偶版本号锁存
`g_target_runtime_current_capture`，并同拍更新现有业务快照。
ACTIVE 的锁存内容不逐拍变化，只有 COMPLETE 且 count=2048 才是完整结果。
Stop 优先撤销窗口；采样、时间、位置、静止条件或功率/故障状态异常会失败锁存。
活动窗口收到另一新请求时丢弃旧统计并返回 BUSY，不开始替代窗口。
ADC 停滞时结果可能留在 ACTIVE，主机仍须检查独立 guard 并有界超时。

配套 CLI 为 [`tools/current_capture.py`](tools/current_capture.py)。默认仅离线冻结并核对
HEX/AXF/MAP、派生类型布局，不接设备；`--acquire` 才执行 HOTPLUG 前检和一次 request 写入。
它核对镜像、CPU running、MOE、guard、健康时基/母线、冻结零偏及请求空闲状态，
采后复核完整结果和前后状态，保存原始数据与参考电流来源；失败不补发请求。
版本竞争只允许有界的只读复核，不重新采集。ARMCC 枚举宽度从同次 AXF 派生，不固定假设四字节。

`--reference-source` 默认 `dmm`，兼容既有调用；`supply-display` 表示使用独立注入电源的
实际显示电流作基础幅值／方向粗对照，不是限流设定值或万用表测量，也不证明绝对精度。
结果保存 `reference_source`、`reference_quality` 与测量限制；两种来源均不自动判定增益
合格、不安装校准或开放运行许可。`--vbus-dmm-v` 始终是万用表对板上 VBUS/GND 的
独立测量，不能填注入电源的低压读数。

实际采集需先确认电机母线电源及电机三相断开，启动校零时外部注入源断开；
零偏 READY 后再接入限流注入支路。注入期间不得复位或重新启动自动校零。
脚本不烧录、不复位，也不提供功率命令；该统计入口的非零电流采集尚未执行，器材与焊点确认后再做针对性实测。
参考方向、接点与既有台架记录见 [电流复核](../validation/CURRENT_CHECK.md#后续已知电流对照准备)。

## 通信与运行监视

UART 错误、超长/非法行和 100 ms 未完成片段均丢弃至下一 LF；UART 无连接会话检测，
不会因重连自动启动。邮箱容纳一个普通请求，拥塞或非法帧记录 BAD_COMMAND；Stop
覆盖排队请求。收到 Stop 后、消费前到达的普通请求也丢弃，调用方不要把遥测当可靠逐条应答。

启动成功但 ADC 没有首拍，以及 ADC 停滞后尚未合并的 guard 原因，都会使旧遥测失效。
已在途的 UART 帧是过去的快照，接收端还需留意数据是否持续更新。当前故障恢复未实现，
Clear 不会清锁存；独立 guard 原因可在 Watch 中查看 `g_target_runtime_guard_faults`。

## 离线验证与证据复用

从仓库根目录，使用 [开发依赖](../../../../DEVELOPMENT.md#44-本地验证依赖) 中的 Python：

```powershell
python firmware/app/motor/tests/verify_motor.py
python firmware/app/motor/tests/verify_rotor_alignment.py
python firmware/app/communication/tests/verify_vofa.py
python firmware/app/communication/tests/verify_motor_vofa.py
python control/tests/verify_position_estimator.py
python control/tests/verify_current_measurement.py
python control/tests/verify_electrical_angle.py
python control/tests/verify_speed_controller.py
python firmware/targets/BLDC_G431/runtime/tests/verify_cordic_math.py
python firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py --finite-run
python firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py
python firmware/targets/BLDC_G431/runtime/tests/verify_foc_runtime.py
```

上列是各自独立的模块与装配检查，按修改影响选择，不要求每次全跑。
专项诊断改变时再执行相应检查；旧 50 µs 探针保持冻结用途：

```powershell
python firmware/targets/BLDC_G431/runtime/tests/verify_current_capture_model.py
python firmware/targets/BLDC_G431/runtime/tests/test_current_capture_tool.py
python firmware/targets/BLDC_G431/runtime/tests/verify_compute_probe_model.py
python firmware/targets/BLDC_G431/runtime/tests/verify_controller_profile.py
python firmware/targets/BLDC_G431/runtime/tests/verify_irq_timing.py
python firmware/targets/BLDC_G431/runtime/tests/verify_stage_budget.py
python firmware/targets/BLDC_G431/runtime/tests/test_stage_budget_tool.py
python firmware/targets/BLDC_G431/runtime/tests/verify_runtime_timing.py
python firmware/targets/BLDC_G431/runtime/tests/verify_timing_entry.py
python firmware/targets/BLDC_G431/runtime/tests/test_runtime_timing_tool.py
python firmware/targets/BLDC_G431/runtime/tests/test_compute_probe_tool.py
```

测试编译并执行实际 ARM C，覆盖默认关断、完整命令、Stop 优先、错误丢弃、启动失败、
ADC 丢失、旧快照失效、低优先级代码被 ADC 抢占的交错，以及位置跨圈、真实采集间隔、
重复/过期/无效观测、两点恢复和无命令干预的有效性发布。电流增量覆盖显式许可、
静止等待、有限零偏窗口、冻结校正及故障/运动/时间异常下的失效锁定。电角度增量覆盖
未确认标定、延迟消费、真实采集间隔、低通后外推、预测超时与观测过期的不同恢复条件，
以及时间异常和故障撤销角度。独立 FOC Runtime 模型使用真实 ARM C 和 HCC 边界替身，
覆盖受控启停、内外环分频、正反目标、有符号电流修正、测量/施加角和失败关断。
它通过实际跨拍 duty 驱动独立双精度 PMSM，合成对象包含反电势、坐标耦合和机械负载，
采用固定理想母线；不包含真实回灌、死区、采样噪声或指令执行周期。
同一套完整 Runtime 检查保留旧 50 µs 节拍，并覆盖当前 100 µs 电流／1 ms 速度节拍。
当前对象每拍先沿用旧 duty 积分 75 µs，经显式有效更新边界后再以新 duty 积分 25 µs；
125 µs 是新电压保持区间的中点预测，不是输出延迟。Stop／故障取消待生效请求，后续
边界不能恢复旧 epoch；该替身不模拟真实 ISR 截止或硬件 watchdog。
诊断采集增量覆盖冻结偏置后的连续原始统计、
锁存/请求身份、重复请求、BUSY、Stop 与异常中止；主机替身验证前检失败零写入、
单地址一次写、应答丢失不补发和采后复核。模型不代替 UART 链路、
实机速度精度或 ISR WCET；软件与实机结论分别记录在 Target 验证页。
既有 UART、手转反馈、逻辑供电零偏及后台计算计时各有有限实测；软件/CORDIC 对照的
各次执行身份和范围见验证页。20 / 10 / 1 kHz 下的合成输入计算与真实 HCC 提交计时，
以及后来的正式 Run 正反向 UART 有限实测，是两类不同证据；前者不能替代后者，
后者也不证明完整WCET或全部保护。新协议请求应答、FreeRTOS后台、50/250Hz通信及一次
协议下有限运动已有独立证据；500Hz原门槛仍FAIL，电流精度和性能保留独立范围。
各轮镜像与实测范围、既有台架证据仍从
[VALIDATION.md](../VALIDATION.md) 进入，按具体修改影响选择复测。

可为独立 MATLAB 复算导出轨迹：

```powershell
python firmware/targets/BLDC_G431/runtime/tests/verify_foc_runtime.py --trace-output "$env:TEMP\motor-foc-model\closed-loop.csv"

# 导出当前 20 / 10 / 1 kHz 时序的分段对象轨迹；默认仍执行两套测试。
python firmware/targets/BLDC_G431/runtime/tests/verify_foc_runtime.py --trace-timing slot --trace-output "$env:TEMP\motor-foc-model\slot-closed-loop.csv"
```

同目录同时生成 `closed-loop.metadata.json`，记录合成对象、控制配置和每行积分前状态／
随后 ZOH 电压的时间语义。MATLAB 从仓库根目录调用
`verify_foc_trace(csv_path, metadata_path, report_path)`，先将本目录 `tests/` 加入 path。
仅依赖基础 `ode45`，不需要 Simulink；抽取起步、降速、反转切换和 Stop 附近的连续区段，
每段只取首状态，随后累计独立积分比较。误差表示两个数值对象的一致性，不能解释为传感器精度。
旧默认导出保持 schema 1；`--trace-timing slot` 使用 schema 2，记录生效边界与边界后的实际
电压／duty，MATLAB 按同一边界分段续积分，不把一拍的两段电压平均后代替时序。
[当前节拍的软件模型摘要与曲线](../validation/evidence/slot-foc-model/summary.json) 保存
30 组 ARM 检查、两套独立复算及损坏分段电压应拒绝的反例；不替代实物闭环验收。
[集成检查点 130a410 的软件模型摘要与曲线](../validation/evidence/foc-runtime-model/summary.json)
保留原源码身份，明确区分模型与实物范围；后续后台诊断的实测不改写这份模型证据。
