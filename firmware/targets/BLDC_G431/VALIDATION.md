# G431 台架验证

`validation/` 通过 HCC 使用 Platform，提供默认 OBSERVE、功率关闭的台架入口；
它不是正式 Motor / ControlRuntime，主动命令仅用于电机三相已断开的台架。
本页汇总当前有限验收、历史证据归属和复用入口，不表示有带电操作排队。

本页对应 Keil 的 `BLDC_control V1` 台架 Target；另一个默认关断的
`BLDC_G431_Observe` 构建与离线验证见 [runtime/README.md](runtime/README.md)，
两个入口的镜像与验收证据不互相替代。

## 控制链集成与合成对象验证

公共 Runtime 已接通增益/方向修正、测量电角度、Park、电流 PI、独立电压施加角和 SVPWM，
速度 PI 按同一 fast tick 整数分频。Start 先关断旧 epoch、跨拍 neutral stage 和 enable，
成功后才提交 RUN；Stop 保留冻结零偏、清积分和待启动请求。活动反馈或控制失败关断锁存。
当前 G431 的控制配置仍关闭，电流、电角度和时序许可未确认，实际装配继续拒绝启动。

实际验证：Motor 8、电流预处理 19、双角电流 PI 21、原 Observe runtime 43、新直接
FOC Runtime 8 组 ARMCC / Unicorn 检查通过。独立合成 PMSM 在两个编码器方向下完成
90 → 40 → −70 rpm 变目标，各段末尾误差断言小于 3 rpm；非单位有符号电流系数同样通过链路。
另覆盖 15 类启动/运行失败关断，及旧 duty、待启动请求和积分不会自动复活。

MATLAB R2021b 使用基础 `ode45`，对真实 ARM C 控制链导出的 27304 拍轨迹选取
四段各 750 个连续区间累计复算，全部通过。这里的微小数值误差只说明独立积分器一致，
不是实物精度或闭环性能指标。固定理想 VBUS、关断后的理想零电压，以及合成对象/PI 参数
均已在[摘要](validation/evidence/foc-runtime-model/summary.json)和[曲线](validation/evidence/foc-runtime-model/response.png)注明；
不模拟实际回灌、死区、噪声或执行周期，不能据此授权实物反转。

Observe 全量 Rebuild 及最终增量构建均为 0 Error / 0 Warning。最终离线 HEX 为
`f05766bf945849c8dcab5192eeb0ee88b8e4e473714e2566e57dd6458b8bbea0`，
已通过同次 HEX/AXF/MAP 与真实 DWARF 布局离线核对；旧台架 HEX 仍为 `8b7fc787…`。
本轮没有连接 ST-Link、烧录、复位或执行任何实物功率指令；板上仍沿用 `c151e560…`。
完整控制代码的 stage 截止、ISR 执行预算、实物采样窗口和反馈标定仍须在开放功率前验收。

## Observe 已知电流采集准备

新增 Target 私有的无功率诊断入口：零偏冻结后，显式请求一次 4096 点原始 B/C 窗口，
锁存均值、极值、无偏样本方差、首末时间和当时冻结的 offset。Stop 或条件失效会中止，
不包含 stage、enable、重启或校准值应用；Motor 与十通道 UART 语义保持原边界。
实现和复现入口见 [runtime](runtime/README.md#已知电流诊断采集)。

实际执行 ARMCC / Unicorn 统计模块 11 组、runtime 43 组；主机 fake 测试 22 个方法通过。
检查包括独立双精度统计参考、4096 点完整性、冻结零偏、BUSY/Stop/异常中止、
单地址一次写、前检拒绝零写入，以及采后数据和实时健康状态的区分。
Observe 全量 Rebuild 与最终增量构建均为 0 Error / 0 Warning。

本轮离线 HEX SHA256 为
`8b918ddf2ff3570ce9b5c2ee77cb466939b9c1587fa80384d6f944285fa4894f`。
默认离线准备已用真实 HEX/AXF/MAP 绑定 38176 个加载字节，并从同次 AXF 成功派生布局；
捕获结果为 72 字节，相关枚举实际为 1 字节，request 为 4 字节。
本轮未连接设备、烧录、复位或注入电流；板上仍为下述已实测的 `c151e560…` 镜像。
上述软件检查不表示已知电流增益/方向、实机采集入口或 ISR 执行预算已经验收。

## FOC 算法离线准备（2026-10-19）

Park / 逆 Park、圆限幅和居中 SVPWM 通过 12 组 ARMCC / Unicorn 检查；
d/q 电流 PI 与限幅回算通过 16 组，包含近单位相量修正、独立数值参考与简化 RL 对象阶跃。
电角度映射与预测通过 10 组，速度 PI 通过 15 组；均执行正式 ARM C，并用独立数值参考对照。
Runtime 保留原 30 组，增加标定门控、逐拍预测、延迟消费与低通、失效恢复等检查，共 38 组通过。
这些结果不证明实际电机闭环或实时预算，算法和复现入口见 [control](../../../control/README.md#foc-变换与电流-pi)。

Observe 已加入源码并构建为 0 Error / 0 Warning。Runtime 已接入电角度计算，但实际 Target
尚未确认极对数、方向和零位，角度保持无效；电流/速度 PI 尚未调用，Start 仍拒绝、Id/Iq 无效。
该轮离线构建 HEX 为 `4f41457fd3ec2190bd4d4e61a6285ae9f3faa1927a85f02418e92740a8885ee0`，
未下载到板；板上沿用下节已实测的 `c151e560…`。没有重新烧录、复位或复采旧波形。
已知电流对照的器材与节点准备见 [电流复核](validation/CURRENT_CHECK.md#后续已知电流对照准备)。

## Observe 电流预处理增量（2026-10-19）

当前已烧录并校验的 Observe HEX SHA256 为
`c151e5604f8d93610a049650e34f199d007e3ad9077a427cf8472d6bd60af969`。
新增可移植相电流零偏、缺相重构与 Clarke，并在 ControlRuntime 的同步边界接入；
所有 Start 仍拒绝进入 RUN，没有功率使能或 Park / Id / Iq 实现。

本轮实际执行 ARMCC / Unicorn 电流测量 11 组与 runtime 30 组，覆盖四种相集合、
独立数值参考、许可/静止/连续采样门控、失败锁定和冻结偏置。
Keil 两个 Target 全量 Rebuild 均为 0 Error / 0 Warning，最终 Observe 增量构建同样无错误警告；
AXF 加载字节与 HEX 一致、未含旧 validation 入口；原台架 HEX 身份保持不变。

现场由操作者确认 OUTPUT 关闭、电池与电机三相断开、ST-Link 逻辑 3V3、编码器连接且转轴静止。
烧录前 HOTPLUG 校验板上仍为下节反馈镜像，MOE=0、IDLE、无故障，软件母线约 2.65 V；
下载后校验成功并复位。4096 点窗口完成，校零状态 READY、首因 NONE。

| 实测相 | 本次冻结 offset | 窗口峰峰值 |
| --- | --- | --- |
| B | −12.424 mA | 9.146 mA |
| C | −0.338 mA | 10.289 mA |

两次分离 SWD 读回的窗口均值、极值、offset 和样本数一致，期间控制时间继续推进；
CPU running、guard/Motor faults=0、IDLE、MOE=0，相电流处理输出有效。
[本次实测摘要](validation/evidence/20261019-observe-feedback/current-zero-summary.json)
仅记录冻结统计及有限结论；实时 SWD 电流字段不视为同一拍原子快照。
本次没有重新发送 UART 请求或复做旧 PWM 抓图，既有证据保留各自镜像归属。
零偏就绪不证明已知电流增益/方向、实际开关采样窗口、温漂、电角度标定、完整 ISR WCET 或 FOC。

## Observe 反馈增量（2026-10-19）

默认关断入口接入 Motor 内部 ControlRuntime、周期编码器机械速度估计和协议无关快照。
Start 仍返回 NOT_READY，没有 stage、power enable 或故障 re-arm 调用。
该轮 Observe HEX SHA256 为
`236b6dae08d3f9e701076e3d4fd5eea0825de972f7e6a4849e3e39662648bc16`。

ARMCC / Unicorn 检查通过：Motor 8、VOFA 编解码 12、快照适配 6、速度估计 10、
runtime 21 组。包含真实采集间隔、正反跨圈、重复/过期/无效观测、恢复和无需命令触发的
有效性发布；已在途旧帧保持完整。Keil 两个 Target 全量 Rebuild 均为 0 Error / 0 Warning，
Observe 的 AXF 加载字节与 HEX 一致，不含旧 validation 入口；原台架 HEX 保持下述身份。

纯逻辑供电下已烧录并校验上述 Observe 镜像：OUTPUT 关闭，电池和电机三相断开，
ST-Link 提供逻辑 3V3，CH340 使用 COM7、115200、8N1，转换器 VCC 不接。
端子 VBUS 的用户万用表读数为 2.68 V，软件约 2.63–2.65 V，确有端子低压，
来源路径尚未确定，不记为仅有 ADC 偏置，也不视为 12 V 母线验收。

串口静态 3 s 捕获 301 帧，控制时间前进 3.00 s，状态 IDLE、faults=0、
目标为零、静止速度有效且为零、Id/Iq 为 NaN、valid_flags=487。
随后逐条发送 Stop / Clear / Start Velocity 120 rpm / Stop，分别收到
ACCEPTED / ACCEPTED / NOT_READY / ACCEPTED，各窗 39–41 帧，均保持 IDLE、零目标、无故障。
烧录后及命令检查后 HOTPLUG 读回 MOE=0、CPU running；没有执行功率使能。

VOFA+ 已使用 JustFloat 接收十通道。[手动转轴截图](validation/evidence/20261019-observe-feedback/manual-velocity-vofa.png)
显示 I2 机械速度曲线具有正、负响应及回到零附近的平台；仅按此范围完成手动反馈验收，
不推断实际转动方向定义或峰值精度。图中 I0 的混合名称是显示改名错误，I0 仍为时间。
本轮不证明速度精度、完整 ISR WCET 或 FOC。新增实测步骤见
[VOFA 操作说明](../../../host/vofa/README.md#首次无功率反馈验收)，不因此重复旧 PWM 抓图。

## 当前验收（2026-10-18）

当前时序候选 HEX SHA256：
`8b7fc787ff174fda0ddda3f206a0578941b767bb97af823bc090551c3f491c33`。
Keil 全量 Rebuild 为 0 Error / 0 Warning，台架 ARM 模型 176 项通过。
逻辑时序检查保持功率关闭；普通 PWM 限定于 12 V / 50 mA、无电池、无电机。

| 项目 | 当前候选结论 | 证据限制 |
| --- | --- | --- |
| 五种提交时序 | 正常/截止前接受，截止后/跨周期拒绝，取消后同拍再次提交拒绝；使能尝试为零 | CCR 读回是 preload 证据；不证明实际三相同边界更新或完整 ISR 预算 |
| 普通 STAGE_ONLY | 两次 stage 成功、COMPLETE；使能次数为零，结束关断 | 独立于专用时序入口，不证明带母线使能 |
| 普通 30% / 2000 us | 39 次 stage 均成功，使能一次，COMPLETE；末态 MOE=0、六路 LOW、保护健康且采样继续 | LA 稳态 20.0 kHz、正脉宽 34.7 us、Vmax 12.4 V；另有包末尾低电平照片，不测精确包长 |
| 普通 70% / 2000 us | 新启动上 39 次 stage 均成功，使能一次，COMPLETE；结束健康关断 | LA 稳态 20.0 kHz、正脉宽 14.8 us、Vmax 12.2 V；未取得合格包尾波形，按操作者选择不再补采 |
| 软件修订范围 | 普通请求改为下一采样早期执行；截止仍为 1700 counts | 本轮针对性验证完成；旧保护注入记录保留原镜像归属，不宣称当前候选已实测三来源带功率 Break |

当前照片见 [30% 稳态](validation/evidence/20261018-pwm-dispatch/la-duty30-stable.jpg)、
[30% 包末尾](validation/evidence/20261018-pwm-dispatch/la-duty30-packet-end.jpg)、
[70% 稳态](validation/evidence/20261018-pwm-dispatch/la-duty70-stable.jpg)。
来源与哈希见 [30% 索引](validation/evidence/20261018-pwm-dispatch/index.json)和
[70% 元数据](validation/evidence/20261018-pwm-dispatch/la-duty70-stable.json)。
70% 图中两路数据共同截止，之后的空白不是测得的低电平；局部视图与命令计数不证明包长。
这些结果可保留为基础基线，**不代表 HCC 全部兑现，也不是可直接接电机的控制应用**。

## 历史证据归属

硬件修复后的静态与基本波形由旧基础镜像 `B5357289…` 取得；
2026-10-17～18 的保护注入、旧两档 duty、电流与位置观测属于 `29AB2861…`。
它们不转写成当前 `8b7fc787…` 的实测。完整逐次记录见 [COMMISSIONING.md](COMMISSIONING.md)。

| 历史范围 | 已证明内容 | 未证明或来源限制 |
| --- | --- | --- |
| 修复后静态与基本 PWM；`B5357289…` | 原低阻/异常发热未复现，静态六路 VGS 约 0 V；LA/LB/LC 与 MA/MB/MC 基本切换通过，高侧稳定平台粗查通过 | LA、MA、高侧 A/C 有照片；其他测点为人工复核，不复制精确读数；最初故障成因未确定 |
| OVP / COMP3 / BRK1；`29AB2861…` | 软件 safe-off 前 MOE 1→0 的寄存器证据；LA 有限包结束照片；独立请求中四步恢复通过 | 照片与恢复来自不同请求，不测实际 17 V 门限或精确关断延迟 |
| C 相 OCP / COMP1 / BRK2；`29AB2861…` | 寄存器证据、LA 包结束照片及同轮四步恢复通过 | 不测实际 4 A 门限、电气延迟或完整功率级能力 |
| 共享 A/B OCP / COMP2/4 / BRK2；`29AB2861…` | 两比较器同时为高、BRK2 与 MOE 1→0；同轮四步恢复通过，无自动重启 | LA 由人工复核；不能分别证明两条 OR 输入路由 |
| 旧 30% / 70% LA 响应；`29AB2861…` | 稳态 20.0 kHz，正脉宽分别 34.8 / 14.8 us；命令与结束关断通过 | 70% 稳态照片和后态约 0 V 人工确认来自不同请求；未捕获最终边沿或精确包宽 |
| 静态电流五窗；`29AB2861…` | 每窗 4096 点，B 均值 −12.64～−12.07 mA、C +0.23～+0.80 mA，标准差约 3.30～3.47 mA；stage/enable 为零 | 偏置候选未应用，不等于已知电流增益、方向、负载或温漂校准 |
| MT6701 手动正反转；`29AB2861…` | 按出轴端看向本体，顺时针递减、逆时针递增，并取得两种跨零回绕 | 目测约一圈不作精度标定；抽样有效不证明所有 SSI 帧连续，未做电角度对齐 |

保护候选完整 HEX SHA256：
`29AB28611691DD220624708ECF826BE9EA31F4C82753418DCC6BE64D8F5FACFF`，覆盖 29720 字节。
其历史构建为 0 Error / 0 Warning、当时 106 项模型通过；后续工具扩展不改写该构建事实。
旧基础波形镜像覆盖 28588 字节，完整身份见 [公开基础照片索引](validation/evidence/20261018-baseline/index.json)。
[精选基础照片](validation/evidence/20261018-baseline/README.md)保留原来源、读数与哈希。

## 进入正式控制前的门槛

| 边界 | 仍需完成 |
| --- | --- |
| 提交与实时预算 | 三相不同合法请求在同一硬件边界的实际电气更新；正式消费者、计算量和中断负载下的预算 |
| 默认关断与截止 | 正式入口接入后明确 DWT / TIM6 guard、缺拍检测和故障所有者；不能用台架保护替代应用保障 |
| 测量与位置 | 已知电流增益/方向、必要零点复核、可测窗口、观测新鲜度、极对数与电角度对齐 |
| 动态功率级 | 精细 VGS/死区、真实 OCP/OVP 门限、其他相/相端不同 duty、相间时序与受控带载能力 |

MA 负向尖峰尚未用短接地分离测量影响；稳定平台粗查和内部配置值不代替动态测量。
条件满足后先建立有界低能量电机入口，从小电流定位/短时开环进入电流环、速度环；
本文无电机 `PWM_BURST` 不直接用于接电机运行。复测按实际修改影响选择，未受影响证据保留原归属。

## 命令与结果

调试器先写 payload，最后写新的非零 sequence；等待 command_sequence 确认消费后再提交。
相同 sequence 不重复执行；复位丢弃旧请求，不自动采集、使能或 Restart。

| command | 动作 | 限制 |
| --- | --- | --- |
| 1 SAFE_OFF | 关闭并取消当前动作 | 不需要 power_key |
| 2 STAGE_ONLY | 两个采样回调内 stage 相同 duty，再 safe-off | 不调用 Power Enable；可无母线测试 |
| 3 PWM_BURST | 跨边界后单次使能，逐拍 fresh stage，到期 safe-off | 每次显式 power_key=0x4310A11E；无电机 |
| 4 CAPTURE_CURRENT | 功率关闭时捕获连续 B/C 统计 | 不 stage、不 enable；忽略 duration/duty |
| 5 BREAK_OVP | PWM 后降低 DAC1_CH1，触发 COMP3 / BRK1 | 固定 2000 us / 50%；逐次 key；无电机 |
| 6 BREAK_OCP_C | PWM 后降低 DAC3_CH1，触发 COMP1 / BRK2 | 同上；保持注入直到显式清除 |
| 7 BREAK_OCP_AB | 降低共享 DAC3_CH2，触发 COMP2/4 / BRK2 | 同上；不分别验收两条 OR 输入 |
| 8 CLEAR_INJECTION | 关闭后恢复注入前 DAC 阈值 | 不清锁存、不 rearm、不 enable；动作中拒绝 |
| 9 REARM | 关闭后显式恢复 Hardware Trip | 物理故障存在时拒绝；不 enable；动作中拒绝 |
| 10～14 TIMING_* | 正常、截止前后、跨周期、取消时序注入 | 零 payload、母线低于 4 V、不 enable；见 [PWM_TIMING.md](validation/PWM_TIMING.md) |

普通 stage/burst：common_duty=0.10～0.90，duration_us=1000～10000，包含排队及 staging。
PWM_BURST 还要求有效有限的 B/C、健康 Trip 和 10～14 V 的有效母线；
母线年龄不超过 100 个 ControlTime tick（5 ms）。每次消费请求清零 key，它不是安全认证。
动作中拒绝替换，SAFE_OFF 可取消；失败、Trip、Clear 与 Rearm 均不自动 Restart。

result：0 NONE、1 COMPLETE、2 OPERATOR_STOP、3 LOCKED、4 BAD_REQUEST、5 BUSY、
6 BAD_CURRENT、7 BAD_BUS、8 STAGE_REJECTED、9 ENABLE_REJECTED、10 PLATFORM_OFF、
11 HARDWARE_TRIP、12 BAD_CLOCK、13 PROBE_REJECTED、14 BREAK_NOT_OBSERVED、
15 REARM_REJECTED、16 TIMING_REJECTED、17 TIMING_TIMEOUT。
BREAK_* 的 result=11 必须结合匹配请求的 probe.outcome=OBSERVED 和原始证据，不能单独判通过。
active_request_sequence 对应实际动作；command_sequence 也包含被拒绝的后续请求。

## 截止、连续采集与快照

ControlTime 由 ADC 回调推进，20 kHz、1 tick=50 us；ADC 停止时它也停止，不作独立截止时钟。
台架以 170 MHz DWT 模 32 位差值计时，最长动作约 210 ms；TIM6 使用 1 MHz、ARR=49，
只在普通 stage、功率及电流采集期间开启 nominal 50 us update，结束关闭 CEN/UIE，保留共享 DAC handler。
ADC 与 TIM6 均检查截止，采样间隔超过 75 us 关断；TIM6 发现 DWT 不前进则 BAD_CLOCK。
TIM6 优先级 15，ADC/Break 为 0，延迟受中断响应影响；它不提供 CPU-independent 截止。
主动 PWM 期间禁止 halt、断点或长时间屏蔽中断；Hardware Trip 是需独立验证的硬件能力。
TIMING_* 的 ADC 内等待另有 DWT / 固定轮数双上限，不靠低优先级 TIM6 打断。
Trip 通知仅请求 rescan，事实来自 authoritative snapshot；动作中立即 software safe-off，锁存未退休则拒绝新动作。

CAPTURE_CURRENT 从消费请求起收集 4096 个完整、有效、有限的 B/C 事务，约 204.8 ms。
相邻 observation_time 差为 1，DWT consumer 入口间隔须为 25～75 us；这些不是采样孔径或死区测量。
Welford 保存 mean/min/max/无偏 variance；溢出以 BAD_CURRENT 结束，不应用偏置、不重构 A。
接受和结束时发布 `g_target_validation_capture`；读前后须为同一偶数 sequence，且 request 匹配、
COMPLETE、sample_count=4096、首末差=4095 tick。失败部分窗口不能当作完整统计。
另核对 MOE=0、六路 LOW、stage/enable 无增量；ADC 丢失时先看独立 stop_reason，不能把旧快照当实时状态。

`g_target_validation` 由 ADC consumer 单写，奇数 sequence 表示更新中，整份使用须读前后同一偶数。
`g_platform_bringup` 保留 76-byte Watch ABI，没有整份一致性保证；慢字段属于不同上下文。
`callback_max_cycles` 只测 consumer，不是完整 ADC ISR WCET。SWD 不足时读独立计数或锁存 capture。

## 本地软件验证

在仓库根目录运行受影响检查，不因文档或无关提交重复整套测试：

```text
python firmware/targets/BLDC_G431/validation/tests/verify_platform.py
python firmware/targets/BLDC_G431/validation/tests/verify_model.py
python firmware/targets/BLDC_G431/validation/tests/verify_timing.py
python firmware/targets/BLDC_G431/validation/tests/verify_trip_snapshot.py
python firmware/targets/BLDC_G431/validation/tests/verify_logic_tool.py
python firmware/targets/BLDC_G431/validation/tests/verify_probe_tool.py
python firmware/targets/BLDC_G431/validation/tests/verify_pwm_tool.py
python firmware/targets/BLDC_G431/validation/tests/verify_current_tool.py
python firmware/targets/BLDC_G431/validation/tests/verify_candidate_tool.py
python firmware/targets/BLDC_G431/validation/tests/verify_timing_tool.py
```

ARM 模型依赖 ARM Compiler 5、Python `unicorn` / `pyelftools`；默认 `D:/keil5/ARM/ARMCC/bin`，
可用 `KEIL_ARMCC_BIN` 覆盖，检查构建只写临时目录。纯主机测试不连接 ST-Link。
真实 Platform 87 项、时序 42 项、保护 snapshot 644 项的寄存器模型分别覆盖提交交错、
固定 rank、用例等待与清理、比较器/Break/锁存/PRIMASK；不模拟真实 Flash 耗时或电气行为。
台架模型的 HCC/time 为替身，覆盖命令、fresh staging、一次使能、截止、Trip、统计和恢复。
主机历史收尾为逻辑 27、保护 113、PWM 62 项，电流 5、候选 16、时序 20 个 unittest 方法（含子用例）；
统计口径不同，不合计成“硬件测试数量”。历史现场使用旧工具，不追认为修正后的工具已经上板。

正式工程为 `MDK-ARM/BLDC_control V1.uvprojx`，新增源码同步工程成员并全量 Rebuild。
核对 guard、stage/enable/safe-off、Trip 最终地址；使用同次 AXF/MAP 获取布局，先核对芯片与 Flash/HEX。
烧录与带电测试分开；连续采集使用 HOTPLUG 并核对 S_HALT=0，halt/reset/download 诊断不混入连续证据。

## 主机工具与候选身份

`validation/tools/` 不自动信任当前构建。新版使用显式 `--candidate-manifest`，
生成方法、逻辑时序命令见 [PWM_TIMING.md](validation/PWM_TIMING.md)；工具路径可由 `--cli` / `--gdb` 指定。
清单离线检查同次 HEX/AXF/MAP、编译输入/头文件/配置、工具哈希和字段布局，再允许首次 SWD；
实时 Flash 仍须逐字节一致。源、配置或工具变化需新候选，不覆盖旧清单。
未指定清单的旧入口仍使用 `20261015-protection-probe/BUILD_MANIFEST.json` 及电流/位置的旧冻结；
不匹配会拒绝，不能拿旧前序摘要进入新候选事务链。清单生成不代表实物验收或功率授权。

| 工具 | 复用边界 |
| --- | --- |
| `logic_check.py` | key=0；`--inspect-only` 只观察；完整逻辑套件要求新启动未消费命令 |
| `timing_check.py` | 必须显式候选，固定用例顺序；失败不重试，仅在已尝试写请求后 SAFE_OFF 清理 |
| `current_check.py` / `position_watch.py` | 功率关闭的统计或只读观测；不把独立诊断字段当原子帧 |
| `pwm_once.py` | `--inspect-startup` 的只读新启动结果可交给保护预检；单次请求另行执行 |
| `probe_once.py` | `--preflight` 不写；`--execute-once` 仅一次固定 BREAK_*，保持故障，不自动恢复 |

保护恢复按 held-rearm → clear → verify-sticky → rearm 分步执行；先保存波形确认。
verify-sticky 仅 STAGE_ONLY，检查锁存拒绝，不再使能；异常只 SAFE_OFF，不自动重试或清故障。
前序文件、实时健康、计数和 capture 每步均重新核对，文件接口不是自动带电操作队列。

## 测量与证据入口

供电/换点由现场操作者完成，关 OUTPUT 后接线；普通探头地夹只接板上 GND，不接相端。
高侧 VGS 以同一时刻 Gate/GND 与 Source/GND 平台相减，不能将不同时间的 Vmax 相减；
稳定平台粗查不代替精确动态 VGS/死区。相端 safe-off 后可悬空，静态约 VBUS 不等于导通。
默认起点为 12 V / 50 mA、无电池/电机、母线供电时断开 ST-Link 3V3；50 mA 不是相电流限额。
换线需确认母线放电至 1 V 以下；异常升温、持续 CC 或异味先关断，不靠提高限流继续。
SINGLE 确认 STOP 后再关 OUTPUT；未触发先手动 STOP，避免把关电边沿当 PWM。旧记录缩放不补回未采样数据。

- [COMMISSIONING.md](COMMISSIONING.md)：完整现场历史、测量接法与人工确认，历史“下一步”不是当前指令。
- [硬件基线](../../../hardware/BLDC_G431/HARDWARE_BASELINE.md)：修复与物理事实。
- [保护流程](validation/PROTECTION_PROBE.md)、[旧 duty 检查](validation/DUTY_CHECK.md)、[电流复核](validation/CURRENT_CHECK.md)：分项复用条件与限制。
- 本机 [保护汇总](validation/local-evidence/20261017-153714-powered-static/protection-stage-summary.json)、[旧 duty 汇总](validation/local-evidence/20261017-duty-check/duty-stage-summary.json)：原事务链。
- 本机 [当前 PWM 记录](validation/local-evidence/20261018-powered-pwm/)：当前候选 `03-early-dispatch/` 及原始构建/实测；[收口前原文](validation/local-evidence/20261018-powered-pwm/docs-before-cleanup/)按原样保留。

`local-evidence` 仅供本机追溯，不随仓库分发；公开读者使用本页照片入口与精选索引。
原图、测量条件与限制共同支持结果摘要，未公开全日志，不声称可据此独立重算全部寄存器结论。
