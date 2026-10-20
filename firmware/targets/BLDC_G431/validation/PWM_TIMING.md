# G431 PWM 提交时序验证

本流程验证 `realtime_motor_io_stage_phase_duty()` 对下一确定更新边界的承诺，
覆盖真实 Platform 的提交、使能与关断。公共 HCC 和 CubeMX 外设配置保持不变；
`main()` 的 SysInit 用户区在时钟初始化后开启 Flash 预取。
当前候选与验收结果统一维护于 [VALIDATION.md](../VALIDATION.md#当前验收2026-10-18)，
本页保存机制、复用命令及限制，不维护另一套阶段状态。

## 20 / 10 / 1 kHz 时序设计

本节定义下一轮实现和验收的目标。下面的「既有实现」仍描述当前源码与历史证据；
本节不是新配置已经构建、烧录或实测通过的声明。

### 参考依据与频率

[ST 电机控制生态页](https://www.st.com/content/st_com/en/ecosystems/stm32-motor-control-ecosystem.html)
的 Test configuration 1 使用 20 kHz PWM、10 kHz FOC、1 kHz 速度调节，并包含 G431。
该测试使用不同控制算法、编译器和采样配置，其负载百分比不能当作本工程的执行预算。

本机官方 `MCSDK_v5.Y.4-Full/MotorControl/` 源码提供了进一步依据：

- `templates/parameters_conversion.h.ftl`：`TF_REGULATION_RATE = PWM_FREQUENCY / REGULATION_EXECUTION_RATE`，
  `REP_COUNTER = 2 * REGULATION_EXECUTION_RATE - 1`；执行率 2 对应 RCR=3。
- `MCSDK/MCLib/G4xx/Src/r3_2_g4xx_pwm_curr_fdbk.c`：`R3_2_TIMxInit()` 使用
  RCR=1 → UG → RCR=3 预置启动相位；`R3_2_GetPhaseCurrents()` 关闭 TRGO，
  `R3_2_TIMx_UP_IRQHandler()` 配置采集后重新开放 OC4REF；`R3_2_WriteTIMRegisters()` 检查迟到。

参考这些时序原则，继续使用本工程自己的 Platform 合同、固定 B/C 采样和 Motor 状态机，
不整体移植 MCSDK。速度环仍在同一采样时基上整数分频，不照搬独立 SysTick 慢环。

| 项目 | 目标 |
| --- | --- |
| TIM1 PWM | 170 MHz、PSC=0、ARR=4250、中心对齐，20 kHz 不变 |
| 有效 update event（UEV） | RCR=3，每 100 µs 一个指定谷底，三相 preload 同时装载 |
| 同步电流采样 / 电流环 | 每个有效 UEV 后只采第一个峰值附近，10 kHz |
| 速度环 | 每 10 个电流样本更新一次，1 kHz；其余拍保持输出 |
| SSI / Bus 采集和 Runtime 观测读取 | 各按 10 个电流样本分频，保持 1 kHz |
| ControlTime | 保持 20000 tick/s；相邻电流样本间隔改为 2 tick |

### 确定的 sample → compute → actuation

以稳态某次有效谷底 UEV 为相对零点。表中峰值使用近似值；CCR4=4240 的触发比
25 µs 峰值早 10 个 timer count，约 59 ns，最终角度参考还需计入真实采样保持时刻。

| 相对时间 | 事件与数据归属 |
| --- | --- |
| 0 µs，谷底 UEV | 硬件装载上一 slot 的完整 CCR1/2/3；短 UP IRQ 核对 slot 后开放采样触发 |
| 约 25 µs，首个峰值前 | OC4REF 上升沿同步触发 ADC1/2，取得本 slot 的 B/C 电流 |
| ADC 完成后 | 在进入控制计算前关闭触发门；接收 sample、消费命令、计算当前输出 |
| 50 µs，谷底 | 重复计数器抑制 UEV，保持原 active CCR；不是本次请求的生效边界 |
| 约 75 µs，第二个峰值 | 触发门关闭，不产生第二次同步采样 |
| 90 µs 以前 | 本 slot 的三相 preload 写入、UDIS 释放和复核必须全部完成 |
| 100 µs，下一有效谷底 UEV | 本次计算的 CCR1/2/3 一起生效，并进入下一个 slot |
| 约 125 µs | 下一个电流样本，间隔 100 µs |

一次输出保持两个完整 PWM 周期。名义 sample→CCR 生效延迟约 75 µs；保留 10 µs
提交预留后，trigger→完整 stage 截止约 65 µs，必须再扣 ADC 转换、异常入口及提交前的
Platform 工作。现有实物入口 CNT≈3829 对应的回调入口→截止约为
`3829 + 2 * 4250 - 1700 = 10629 cycles`，约 62.5 µs；这是新相位的预算推导，
不是新固件的计时结果或 WCET。

角度计算必须区分测量时刻和电压作用参考时刻。测量 Park 使用 sample 时刻；若逆 Park
采用随后 100 µs 电压保持区间的中心，名义预测时间为 75+50=125 µs。
CCR 生效延迟与保持区间中点不能混称，角度外推也不消除电流闭环的实际延迟。
电流 PI 的 dt 改为 100 µs、速度 PI 的 dt 为 1 ms，增益需按这两个周期重新检查。

### slot、截止与失败语义

- Platform 独占有效 UEV、触发门、CCR preload 和 slot 身份。UP IRQ 拥有 UIF 的清除，
  stage 不再清 UIF；接受的请求携带当前 sample slot 和 actuation epoch，只能进入下一指定 UEV。
  sample 的 ControlTime 按硬件 slot 导出，正常间隔为 2 tick；漏掉 slot 必须保留时间缺口，
  不能仍只推进 2 tick，从而把旧 Position/Bus 或漏采样伪装成新鲜数据。
- UP IRQ 仅在该 UEV 后首个上数半周的允许窗口内开放 OC4REF，初始 arming 窗口取 10 µs。
  必须证明 OC4REF 仍为低、当前 slot 正确且距离首触发有裕量；迟到不能改采第二个峰值。
  CNT/DIR 会每 50 µs 重复，必须同时使用硬件 UIF、单调 cycle 年龄和 slot 身份防别名。
- ADC 入口首先关闭后续触发，再做电流换算和控制计算。stage 校验的目标是下一有效 UEV，
  允许跨越中间的谷底/峰值，不再要求整个计算留在原下数半周。
- 三相写入仍使用短临界区与 UDIS；UDIS 不得跨过目标 UEV。检查整个事务的完成期限，
  不把“开始写入时尚未迟到”视为成功，不将迟到结果顺延或自动重放。
- 原 25 µs consumer guard 不能直接保留，也不能只改为 100 µs。新 guard 必须检查指定
  UEV 的绝对截止；完整 ADC IRQ 还须包含观测尾部、HAL 收尾和异常返回的余量。
  实现前先在模型中确定各检查点，实测分别记录 stage、consumer 与完整 IRQ 的范围。
- 拟采用 Break/UP 优先级 0、ADC 优先级 1、SPI 优先级 2，让 UP 可以打断超时的 ADC。
  UP 检查上一 slot 是否完整结束、fresh stage 是否具备；超时立即关 MOE、失效 epoch，
  旧 ADC 服务尚未退出时不得开放新采样。仍须直接检查 UIF/cycle；优先级不能替代迟到判断。
  ADC busy 必须覆盖整个 IRQ 和 HAL 收尾；fresh stage 交付要求按已使能/预热交付状态判定，
  首次 UEV、IDLE/FAULT 观察和 Start 的 quiet slot 不得因本就无需提交而误报漏交付。
  PRIMASK 临界区、同级中断和 CPU 故障仍可能延迟软件关断，不宣称此机制替代独立硬件 Trip。
- Start 先保持 MOE=0，提交本次 Start 的安全预热输出，并经真实目标 UEV 确认生效；
  后续 sample 再提交 fresh 请求并核对使能条件。不能把首次 sample 或旧 pending 当作预热完成。
  Stop/Trip 立即关 MOE、写 neutral 并取消 pending/committed/enable 资格，Drive Timing 持续。
- neutral、启动重复计数相位与所有 UG 必须在注入 ADC 接受外部触发之前完成。
  `TIM_TRGO2_RESET` 在 UG 时也可能输出触发，不能把它当作对 UG 永久免疫的关闭状态。
  清除启动 UIF/待处理中断后，才开放运行期 UIE 与计数；运行期禁止额外 UG。

### 实现范围与最小验收

静态配置先通过 `.ioc` / CubeMX 改 RCR 和 UP IRQ，再调整 Platform 的触发门、slot、
stage/commit/enable。同步修改 ControlTime 推进、Runtime 装配、1 kHz 分频和心跳门限，
检查零偏等待/窗口样本数对应的实际时长。长期架构不增加线程、队列或第二个控制状态所有者。
HCC 对「下一 PWM 周期」的表述需明确为 Target 声明的下一有效更新边界；本配置中它
间隔两个载波周期。Runtime 的预测参数必须与该具体时序核对，不沿用旧值，也不把
定时器重复计数、UIF 或 slot 实现泄漏给可移植控制算法。
三个 Keil Target 共享静态 MCU 配置，必须检查全部构建与受影响诊断；旧工具不得按新镜像
继续套用 50 µs 采样、原截止或原二进制布局。历史证据仍归原镜像，不改写为新版本通过。

离线模型重点覆盖首次相位、100 µs 采样、第二峰抑制、同一 UEV 三相装载、slot/epoch
失配、UP 迟到、ADC 超时、跨周期别名、UDIS 截止及 Start/Stop/Trip 交错。
候选稳定后只做受影响的无功率时序验收：频率、相位、完整最慢控制路径和截止失败关断；
不重跑无关的全部历史台架流程，也不以构建或模型成功替代实物时序通过。

SSI 接收及 OVR 后收尾仍是独立未解决项：在高优先级 ADC 尾部启动 RX-only IT，可能在
SPI 获得服务前溢出。降低采样频率不能消除单次接收的这个风险；正式 RUN 许可冻结前必须
完成有界接收和错误清理的针对性验证。当前未因此增加带电操作或宣称闭环预算通过。

## 既有实现：固定时序与截止

TIM1 与 CPU 同为 170 MHz，PSC=0、ARR=4250、RCR=0，中心对齐模式 1。
完整 PWM 周期 50 us，峰值和谷底各一次 update；CH4 为 PWM2、CCR4=4240，
OC4REF 上升沿触发 ADC1/2 injected conversion。正常回调在峰值后下数半周期，目标是随后谷底。

| 相位 | 事件 |
| --- | --- |
| 上数 CNT=4240 | ADC 触发 |
| 峰值 CNT=4250 后下数 | 转换完成，回调提交 |
| 下数距谷底不足 1700 counts | 截止，拒绝提交；1700 counts 对应 10 us |
| 谷底 CNT=0 | 三相 preload 成组进入 active |

只有当前回调内完整、合法的三相请求可接受：

- 必须下数，CNT 不低于 1700，入口还须严格高于该值。
- 入口先读 DWT cycle，再读 CNT；经过 cycle 必须小于 `入口 CNT - 1700`，
  防止旧回调经过整周期后再次遇到相似 DIR/CNT，被误当作当前请求。
- duty 浮点转整数后，在短临界区复核窗口与 actuation epoch。
- 置 UDIS 后写三路 CCR preload，写前/后检查截止，恢复 update 后再次检查时间和 Break。
  失去资格时先关 MOE、写 neutral、作废旧 epoch，最后恢复 UDIS 并失败返回。
- 成功 stage 在 UDIS 内清旧 UIF；后续回调须看到同 epoch、真实 UIF 和足够经过时间，
  才确认 preload 已跨过对应谷底，不能仅凭“已经调用过 stage”认定可使能。

UDIS 阻止 preload → active 更新，不停止计数器；PRIMASK 防软件交错，不能替代硬件 UDIS。
依据 [ST RM0440：Advanced-control timers、Center-aligned mode 与 TIMx_CR1 UDIS](https://www.st.com/resource/en/reference_manual/rm0440-stm32g4-series-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)。

1700 counts 是当前 Platform 的保守软件策略，不是定时器强制的最小预留。实现要求整个
三相事务在该线以前完成，包含 UDIS 恢复后的复核；并非只要求事务开始时还剩 10 µs。
现有证据尚未量化改变该值所需的总线/临界区最小余量，不能据注释认定 10 µs 已通过
最坏情况论证。低侧采样窗口是另一项物理约束，不能用它直接替换该预留。

## 资源与失败语义

Platform 启动时开启 DWT CYCCNT 而不清零，其他运行期模块不得停用或重置它。
32 位差值允许短窗口跨回绕，不证明任意长挂起后的请求仍新鲜。
Platform 拥有 TIM1 CCR1/2/3、UDIS、UIF；UIE 保持关闭，不增加 update IRQ。
其他代码不得运行期生成 UG、清 UIF 或改变该时序配置。

`power_enable()` 在临界区复核状态、epoch、截止和 Break，拒绝时 safe-off 并取消 pending/committed。
恢复须重新提交、跨边界、再提交本拍请求，不能利用被拒绝前的预热请求。
safe-off 先关 MOE、再失效旧请求；Drive Timing 和采样继续。
输出开启时本拍未成功 stage，或 consumer 自入口起耗尽 25 us，均 safe-off。
25 us 不含之后的位置/母线辅助函数及 HAL 收尾，不是完整 ADC ISR WCET。

TIM6 独立动作截止属于验证入口，本修复不覆盖 CPU 挂死、调试 halt 或任意时钟修改，
也不替代独立 Hardware Trip。更换正式消费者时须明确这些保障的所有者。

## 本地检查

在仓库根目录按修改影响选择检查：

```text
python firmware/targets/BLDC_G431/validation/tests/verify_platform.py
python firmware/targets/BLDC_G431/validation/tests/verify_model.py
python firmware/targets/BLDC_G431/validation/tests/verify_timing.py
python firmware/targets/BLDC_G431/validation/tests/verify_trip_snapshot.py
python firmware/targets/BLDC_G431/validation/tests/verify_timing_tool.py
python firmware/targets/BLDC_G431/validation/tests/verify_candidate_tool.py
```

依赖 ARM Compiler 5、Python `unicorn` / `pyelftools`，编译器可由 `KEIL_ARMCC_BIN` 指定。
临时检查构建不覆盖 Target 正式产物。`verify_platform.py` 编译真实 Platform；
模型显式推进 CNT/DIR、DWT、update 和 CCR shadow，注入截止、Break、关断交错及固定 ADC rank。
`verify_timing.py` 同时执行真实 timing probe 与 Platform；台架 `verify_model.py` 的 HCC 为替身。
各组结果分开，不把模型指令数当 CPU cycle，不据此给出 WCET 或电气结论。

正式候选需 Keil 全量 Rebuild 0 Error / 0 Warning，核对反汇编：浮点转换在关中断前，
三相写入处于 UDIS 内，失败先关 MOE，所有退出恢复 PRIMASK。
模型不模拟真实 Flash 时序；调用图补计函数指针后仍不含异常压栈、被抢占上下文和嵌套，
不代替 1024-byte 启动栈的实物高水位验收。各轮构建与测试原值保留在对应本地候选目录。

## 候选身份准备

全量构建后，将同次 HEX、AXF、MAP 保存到新的本地候选目录；冻结本轮固件与工具，
再离线生成清单。输出文件必须尚不存在，命令从仓库根目录运行：

```text
python firmware/targets/BLDC_G431/validation/tools/candidate_manifest.py --build-dir "<候选产物目录>" --output "<新证据目录>/candidate-manifest.json" --gdb "<arm-none-eabi-gdb.exe>"
```

生成器需要 `pyelftools`，不连接 ST-Link；检查 AXF 加载内容与 HEX 一致、MAP 关键地址/尺寸，
保存完整字段布局及源码/工具哈希。清单只证明离线准备，不证明构建过程或实物通过。
生成后修改源码、头文件、工程或工具会使清单失效；另建候选，不覆盖历史清单。
后续实测带同一 `--candidate-manifest`，工具位置可用 `--cli` / `--gdb` 指定。
旧镜像前序摘要不能进入新候选事务链，必须从实际新启动建立证据；身份选择不等于功率授权。

## 功率关闭时序注入

`timing_probe.c` 从真实 ADC consumer 调用正式 Platform 的 stage/safe-off。
受理时清理前态，下一采样才执行；保留旧 mailbox/snapshot 布局，结果另存
`g_target_validation_timing`，读前后须为同一偶数 sequence。

| 命令 | 注入方式 | 预期结果 |
| --- | --- | --- |
| 10：正常 | 新回调直接提交 30% / 40% / 70% | 接受 |
| 11：截止前 | 选择下数 CNT 2200～3000，由真实 stage 成功证明可接受 | 接受 |
| 12：截止后 | 等待下数 CNT 不高于 1600 | 拒绝，CCR 不变 |
| 13：跨周期 | 同回调经过至少 8500 cycle，再进入表观合法下数相位 | 拒绝，CCR 不变 |
| 14：取消 | 合法提交后 safe-off，同拍再次提交 | 首次接受、再次拒绝，保持 neutral |

仅接受 key/duration/duty 全零，三相请求由用例固定；要求有效新鲜母线低于 4 V、
输出关闭、保护健康、采样恢复。轮询 DWT/TIM1 有 cycle 与固定循环次数双上限，
不写 CNT/DIR/DWT/UDIS/UIF，不调用 `power_enable()`；所有结局 safe-off，
分别保存 stage 后和 cleanup 后的读数，不用清理后的 neutral 冒充提交结果。

TIM6 优先级低于 ADC，不能打断 ADC 内等待，时钟停止时靠有限循环退出。
跨周期注入可能丢一次 ADC 通知、出现约 100 us 间隔；保留累计统计，随后至少两个有效且
间隔为 25～75 us 的采样才置 recovery_ticks=2，不宣称注入全过程连续。

主机先离线解析全部字段，再允许 SWD；要求新启动尚未消费命令。
套件顺序为 10、11、12、14、13，跨周期最后。`--inspect-only` 只观察；
失败不重试，仅在已尝试写请求后追加 SAFE_OFF。以下命令需要已核对的逻辑供电状态：

```text
python firmware/targets/BLDC_G431/validation/tools/timing_check.py --candidate-manifest "<新版清单>" --output "<新证据目录>" --inspect-only
python firmware/targets/BLDC_G431/validation/tools/timing_check.py --candidate-manifest "<新版清单>" --output "<另一个新证据目录>"
```

consumer 入口、stage 前后、取消第二次调用和 cleanup 的 CNT/DWT 是顺序读数，非原子硬件快照。
CCR 仅是 preload 证据，本入口不能证明 active 三相同时更新、引脚波形、精确截止裕量或完整 ISR WCET。

## 路径修订的原因与边界

前序候选曾在首个正常用例等待超时或 stage 被拒绝；分段候选显示置 UDIS 后的
第二次时间检查已过截止，尚未写新 CCR。离线模型在注入点推进时间，不能预测实板 Flash 耗时。
多轮记录保留在本地，不将不同候选串成同一事务，也不把失败改写为通过。

优化保留 1700-count 截止、四处时间复核、epoch、Break、UDIS 成组写入和失败关断：

- 内联小型时间检查；已取消请求在浮点转换前拒绝，有效请求仍在临界区复核。
- 保护快照直接读比较器 VALUE 位，保留锁存原因与 PRIMASK 语义。
- 固定单 injected rank 直接读 JDR1，保留完成/配对有效性检查。
- 探测先于诊断复制，新 mailbox/SAFE_OFF 优先；忙请求推迟到下一早期入口，不跳过 fresh stage。
- 时钟初始化后开启 Flash 预取；实测 FLASH_ACR=0x00040704、4 WS，I/D cache 开启。
  依据 [RM0440 Instruction prefetch](https://www.st.com/resource/en/reference_manual/rm0440-stm32g4-series-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)，
  核对 [ES0431 Rev 9](https://www.st.com/resource/en/errata_sheet/es0431-stm32g431xx441xx-device-errata-stmicroelectronics.pdf)；当前 Run 模式不涉及其调试低功耗唤醒场景。

`e8fbccb4…` 首先通过五种功率关闭用例和普通 STAGE_ONLY，但普通带母线 PWM 首次 stage 拒绝，
使能尝试为零；该次没有拒绝时 CNT，不能确定具体拒绝点。专用用例不能替代普通路径验收。
随后将普通请求接收与首次 stage 分到相邻回调：下一拍重新检查 current/trip/bus，
执行先于 Watch 复制和统计；SAFE_OFF 仍优先，BUSY 不替换事务，总截止仍从接收起算。
Platform 公共合同、保护条件及截止未因此放宽。

## 最终候选与结果入口

`8b7fc787…` 的五种功率关闭时序、普通 STAGE_ONLY 及 30% / 70% 单次 PWM 均已完成
针对性验证；完整身份、计数、读数与限制只在 [当前验收表](../VALIDATION.md#当前验收2026-10-18)维护。
当前取消用例第二次返回 CNT=1727，接近截止；顺序读数不是严格余量，不能外推未来算法预算。
当前候选没有重做三来源带功率 Break；旧保护注入与恢复仍归属原 `29AB2861…`。

精选照片入口：

- [30% 稳态](evidence/20261018-pwm-dispatch/la-duty30-stable.jpg)与[包末尾](evidence/20261018-pwm-dispatch/la-duty30-packet-end.jpg)，来源与哈希见 [index.json](evidence/20261018-pwm-dispatch/index.json)。
- [70% 稳态](evidence/20261018-pwm-dispatch/la-duty70-stable.jpg)及[元数据](evidence/20261018-pwm-dispatch/la-duty70-stable.json)；包尾未取得合格波形，不以曲线终点后的空白判低电平。

当前只覆盖 LA 的两档稳态和限定结束观察；总限时包含排队/staging，计数不代替实际 Gate 包长。
三相同边界电气更新、正式 Runtime 的计算/中断预算、精细 VGS/死区、真实门限、电流增益与带载仍未验收。
后续按修改影响选择检查，不把这些缺口变成未受影响已完成项目的重复人工流程。

## 本地原始记录

以下均为忽略目录，不随仓库分发，公开结论以验收页和精选照片为入口：

| 本地目录 | 保存内容 |
| --- | --- |
| [20261018-pwm-deadline](local-evidence/20261018-pwm-deadline/) | 最初离线修复与候选准备 |
| [20261018-timing-probe](local-evidence/20261018-timing-probe/) | 最初用例与正常提交失败候选；含 revision-07 收尾 |
| [20261018-timing-path](local-evidence/20261018-timing-path/) | 分段定位与优化；06-fixed-rank 为 e8 候选 |
| [20261018-stage-path](local-evidence/20261018-stage-path/) | 普通 STAGE_ONLY 的独立路径记录 |
| [20261018-powered-pwm](local-evidence/20261018-powered-pwm/) | 普通请求修订与最终 03-early-dispatch 候选、实测 |
| [docs-before-cleanup](local-evidence/20261018-powered-pwm/docs-before-cleanup/) | 两份收口前原文，保留逐轮历史数值与限制 |

每候选保存独立 HEX/AXF/MAP、构建、源码/工具身份和实测，不转移证据归属。
早期硬件与保护历史继续见 [COMMISSIONING.md](../COMMISSIONING.md)，原证据不删除、不覆盖。
