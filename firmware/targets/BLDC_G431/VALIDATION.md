# G431 台架验证

入口位于 validation/，通过现有 HCC 使用 Platform；不是正式 Motor / ControlRuntime。
默认启动 OBSERVE，保持功率关闭。主动命令仅用于电机三相已断开的台架。

## 阶段收尾边界

本阶段可作为硬件适配与基础验证基线保留，**不代表底层 HCC 全部兑现，也不是可直接接电机的控制应用**。
2026-10-18 收尾审查仅核对源码、文档与离线测试，没有新增上板动作。

| 项目 | 当前结论 | 进入正式控制前的要求 |
| --- | --- | --- |
| G431 占空比提交 deadline | 存在明确实现缺口 | `platform/realtime_motor_io.c` 仅以 consumer callback 范围开放 stage，未核对 TIM1 剩余时间；须拒绝迟到提交，并保证三相 CCR 在同一更新边界生效 |
| 默认关断、单次动作、故障锁存与显式恢复 | 已有本页限定条件下的基础验证 | 正式 Runtime 接入后重新验证；不能用测试入口的保护代替应用保障 |
| 缺拍检测与独立截止 | 当前由验证入口的 DWT / TIM6 补充；ControlTime 本身随 ADC 回调推进 | 更换 `main()` 入口时明确这些保障的所有者，保留独立于 ADC 回调的截止机制 |
| 电流、位置、母线观测 | 已有基本采集与有限实测 | 电流增益/方向、采样窗口、观测新鲜度策略及电角度对齐仍须完成 |
| 动态功率级与带载能力 | 未验收 | 精细 VGS / 死区、真实保护阈值及受控带载测试分项验收 |

deadline 缺口由源码与公共 `realtime_motor_io_stage_phase_duty()` 合同对照确认，
不是此前示波器已经观察到的故障。当前 108 项 ARM 模型执行的是验证模块与替身 HCC，
不会覆盖真实 Platform 的三次 CCR 写入时序。本轮保留原目标镜像与实测归属，登记缺口，
修复应单独关联真实 Platform 的边界测试和后续回归。

后续工作顺序为：冻结这份有限基线，明确新阶段功能范围，再补齐进入控制闭环所需的接口实现与验收。
以下流程说明如何验证，并不表示有带电操作排队。

## 当前实机结论（2026-10-18）

功率检查限定于 12 V / 50 mA、无电池、无电机条件。
2026-10-15 完成基础波形初筛；2026-10-17 完成保护候选逻辑及三种来源的注入关断与恢复检查。

| 项目 | 结论与证据 |
| --- | --- |
| 更换 DRV 和自举电容后的静态检查 | 原低阻与异常发热未复现；供电稳定，静态六路 VGS 约 0 V |
| LA/LB/LC 低侧基本波形 | 通过；LA 照片、LB/LC 用户人工复核 |
| MA/MB/MC 相端基本周期切换、频率、占空比 | 通过；MA 照片、MB/MC 用户人工复核；三相共同 50% |
| 高侧通电时的栅源驱动电平 | A/C 相照片、B 相用户人工复核的稳定平台粗查通过；精细动态 VGS 未验收 |
| 瞬态峰值与精确死区 | 未验收；MA 负向尖峰待短接地复核，目前只有鳄鱼夹地线 |
| 其他相/相端的不同 duty、相间时序及带负载 PWM | 未验收；本轮不同 duty 实际波形范围为 LA |
| 30%/70% 无电机基础响应 | 通过；第 20 次 30% 的 LA 照片显示 34.8 us，第 21/23 次 70% 显示 14.8 us，均 20.0 kHz、Vmax 12.2 V，符合低侧互补预期；各次 COMPLETE、一次使能及结束关断检查通过。30% 包结束人工确认，70% 后态由第 24 次人工复核。主机 62 项与 ARM 模型 108 项通过 |
| 70% AUTO 后态观察 | 第 24 次后用户确认 OUTPUT 一直开启、LA 始终约 0 V，人工后态检查通过；最后只读复核计数未变、六路 LOW/MOE=0、保护健康且采样继续。70% 最终边沿及精确包宽未捕获，不与第 23 次稳定照片混为同一记录 |
| 保护候选镜像与 ST-Link 供电逻辑检查 | 2026-10-17 回读已是 29AB2861…；软件复位后采样、stage-only、4096 点采集、无 key 拒绝通过；本次未烧录、未使能功率 |
| 保护候选 12 V 静态与只读预检 | 用户报告 12 V 供电无异常；直接读回启动健康，未再复位，母线观测约 11.96～12.04 V；MOE=0、六路 LOW、Trip 健康，使能尝试为 0；精确电流/CV 未另报 |
| OVP 单次注入与恢复 | 第 15/16/17 次均取得 COMP3/BRK1 与软件 safe-off 前 MOE 1→0 的寄存器证据；第 16 次 LA 照片确认有限包结束并保持低电平。第 17 次独立运行中四步恢复通过，全程无额外 enable，最终保护恢复健康且输出关闭；未重复拍摄本次波形 |
| C 相 OCP 注入与恢复 | 第 18 次 COMP1/BRK2 与软件 safe-off 前 MOE 1→0 的寄存器证据、LA 有限包结束照片及同一连续运行中的四步恢复均通过；最终保护健康、六路 LOW/MOE=0，使能次数保持为 1，无自动重启 |
| 共享 A/B OCP 注入与恢复 | 第 19 次 COMP2/4 同时为高、BRK2 成立及软件 safe-off 前 MOE 1→0 的寄存器证据通过；LA 波形由用户人工复核与前次一致，同轮四步恢复通过。不能分别证明每条 OR 输入路由 |
| 实际 OCP/OVP 阈值及完整功率级验收 | OVP、C 相及共享 A/B 注入关断与恢复、LA 的 30%/70% 基础响应和后态检查通过；实际门限、精确 Gate 延迟、动态 VGS/死区、其他相/相端的不同 duty、相间时序和带负载能力仍未资格化 |

截至本轮共 24 次独立带功率请求：前 14 次为旧镜像基本波形；第 15/16/17 次为新镜像 OVP，
第 18 次为 C 相、第 19 次为共享 A/B OCP。第 17/18/19 次分别完成同轮四步清除/恢复；
第 20/21 次为 30%/70% 普通 PWM，均返回 COMPLETE，结束时保护健康、六路 LOW/MOE=0，无自动重启。
第 22 次独立补采 70% 整包，命令与结束关断通过；此前被用户重启电源打断的倒计时未发命令，不计入次数。
第 22 次因拿错探头未取得有效波形，用户明确要求后第 23 次独立补发，命令/结束关断及稳定周期通过，包末尾仍未确认。
第 24 次按用户要求用 AUTO 观察，命令/结束关断通过；用户确认供电连续、LA 始终约 0 V，后态检查通过。
最后只读检查确认同一完成计数和健康关断，无新增使能。本轮 LA 不同 duty 基础检查已收尾，
70% 的稳定照片与后态复核来自不同请求，未测其最终边沿时刻或精确包宽，见
[占空比阶段汇总](validation/local-evidence/20261017-duty-check/duty-stage-summary.json)。
保护阶段证据来源与边界见
[本轮保护检查汇总](validation/local-evidence/20261017-153714-powered-static/protection-stage-summary.json)。
最初异常的具体成因尚未确定。实测事实见
[硬件基线](../../../hardware/BLDC_G431/HARDWARE_BASELINE.md)，完整阶段记录及本机原始证据位置见
[首板实物调试记录](COMMISSIONING.md)。

上述 `local-evidence` 链接仅供本机追溯，不随仓库分发。GitHub 可查看的少量原始波形、
来源与 SHA256 见[公开证据索引](validation/evidence/20261018-baseline/README.md)。
原图与本页测量条件共同构成公开结果摘要；未公开全部日志，不宣称可据此独立重算所有寄存器结论。

## 已验证范围与后续门槛

1. **保护关断链路（本轮注入检查完成）。** 12 V / 50 mA、无电机条件下，
   OVP/BRK1、C 相与共享 A/B 输入/BRK2 的寄存器硬件关断、实际 LA 包结束和四步恢复通过。
   寄存器证据在软件 safe-off 前取得；波形为 OVP/C 照片及 A/B 用户人工复核。
   共享 A/B 同时触发不能单独证明每条 OR 输入路由。阈值注入不作为实际 4 A / 17 V
   触发精度标定，也不测得精确 comparator→Gate 延迟；回归仍须保留这些边界。
2. **LA 两档基础响应已完成，动态测量仍待补。** 程序 30%/70% 对应低侧实测高脉宽 34.8/14.8 us，
   结束关断及后态检查通过，见 [不同占空比检查](validation/DUTY_CHECK.md)。
   其他相/相端的不同 duty、相间时序仍须分别验证；改善探头固定与接地后复核 MA 负向尖峰。
   精确动态 VGS/死区及 Gate 关断时刻先确定测量方法，不用平台粗查、后态读数或内部配置值替代。
3. **电流测量和位置准备。** 保留已有零点候选、噪声窗口和 SSI 结果，
   补已知电流对照、增益及必要的零点复核；随后确认位置方向和电角度对齐方案。
   零点稳定、CRC 有效分别不等于增益校准或位置/电角度已标定。
   当前执行顺序见 [电流采样复核](validation/CURRENT_CHECK.md)；2026-10-18 五个 4096 点静态窗口完整，
   B 均值 −12.64～−12.07 mA、C +0.23～+0.80 mA，标准差约 3.30～3.47 mA；
   stage/enable 均为 0、结束保护健康。短时复核完成，偏置未应用，已知电流增益对照待准备。
   MT6701 手动正反转粗查已取得方向相反及两种跨零回绕记录：按出轴端看向本体的约定，
   顺时针读数递减，逆时针读数递增。净变化约 −0.998/+1.001 圈只与目测约一圈相符，
   不作为精度标定；详细记录见 COMMISSIONING.md，电角度对齐仍待完成。
4. **低能量电机测试。** 在保护、采样和位置条件满足后，建立独立且有界的电机测试入口，
   使用限流电源从小电流定位/短时开环开始，再进入 FOC 电流环与速度环。
   本文现有无电机 PWM_BURST 入口不直接用于接电机运行。

候选镜像在 2026-10-15 构建，HEX SHA256 为
`29AB28611691DD220624708ECF826BE9EA31F4C82753418DCC6BE64D8F5FACFF`，覆盖 29720 字节。
当时 106 项模型检查通过、Keil 全量 Rebuild 为 0 Error / 0 Warning。
2026-10-17 已逐字节核对板上 Flash；后来只扩展模型和主机工具，没有修改这份目标镜像。
原 106 项构建记录按历史事实保留，当前离线检查见下文。

各次请求、断电造成的证据中断、波形确认及恢复顺序统一保存在
[实物调试记录](COMMISSIONING.md)，此处不再重复现场进度。
注入方法见[保护验证流程](validation/PROTECTION_PROBE.md)，
LA 两档结果见[不同占空比检查](validation/DUTY_CHECK.md)。
复测需重新核对接线、镜像与健康状态；历史“下一步”不作为当前执行指令。

## 命令与结果

调试器先写 payload，最后写新的非零 sequence；等待 command_sequence 确认消费后
再提交下一条。相同 sequence 不重复执行。复位丢弃旧请求，不自动开始采集或 PWM。

| command | 动作 | 限制 |
| --- | --- | --- |
| 1 SAFE_OFF | 关闭并取消当前动作 | 不需要 power_key |
| 2 STAGE_ONLY | 在两个采样回调内 stage 三相相同 duty，然后 safe-off | 不调用 Power Enable；可无母线测试 |
| 3 PWM_BURST | 跨确定边界后单次使能，逐拍提交 fresh duty，到期 safe-off | 每次显式 power_key=0x4310A11E；无电机 |
| 4 CAPTURE_CURRENT | 功率关闭时捕获连续 B/C 电流统计 | 不 stage、不 enable；忽略 duration_us/common_duty |
| 5 BREAK_OVP | 单次 PWM 后降低 DAC1_CH1，触发 COMP3 / BRK1 | 固定 2000 us / 50%；逐次 power_key；无电机 |
| 6 BREAK_OCP_C | 单次 PWM 后降低 DAC3_CH1，触发 COMP1 / BRK2 | 同上；保持注入直到显式清除 |
| 7 BREAK_OCP_AB | 单次 PWM 后降低共享 DAC3_CH2，触发 COMP2/4 / BRK2 | 同上；不能单独验收 A/B 两条路由 |
| 8 CLEAR_INJECTION | 关闭功率后恢复注入前保存的 DAC 阈值 | 不清锁存、不 rearm、不 enable；动作中拒绝 |
| 9 REARM | 关闭功率后调用 Hardware Trip 显式恢复 | 物理故障存在时拒绝；不 enable；动作中拒绝 |

STAGE_ONLY / PWM_BURST 的 common_duty 范围为 0.10～0.90，
duration_us 为 1000～10000 us，包含使能前 staging。
PWM_BURST 另要求完整有效且有限的 B/C 采集、正常的 Hardware Trip snapshot，
以及 10～14 V、年龄不超过 100 个 ControlTime tick（5 ms）的有效母线观测。
每次消费请求都会清零 power_key；它是单次操作确认，不是安全认证。
动作进行中拒绝替换，SAFE_OFF 可以取消；失败、Trip、恢复均不自动重启。

result：0 NONE、1 COMPLETE、2 OPERATOR_STOP、3 LOCKED、4 BAD_REQUEST、
5 BUSY、6 BAD_CURRENT、7 BAD_BUS、8 STAGE_REJECTED、9 ENABLE_REJECTED、
10 PLATFORM_OFF、11 HARDWARE_TRIP、12 BAD_CLOCK、13 PROBE_REJECTED、
14 BREAK_NOT_OBSERVED、15 REARM_REJECTED。
BREAK_* 的 result=11 只有结合匹配请求的 probe.outcome=OBSERVED 及原始证据
才表示观察到所选链路；其他真实故障也会返回 11，不能只看 result。
active_request_sequence 标识实际动作，command_sequence 包括被拒绝的后续请求。

## 截止与保护

G431 ControlTime 是由 ADC 回调推进的 20 kHz 计数，1 tick=50 us。
ADC 停止时 ControlTime 也停止，不能用它充当独立截止时钟。

验证入口使用 170 MHz DWT CYCCNT 的模 32 位差值计时，动作最长约 210 ms，
覆盖其回绕。复用 CubeMX 已配置的 1 MHz TIM6 / TIM6_DAC IRQ，
只在动作期间将运行期 ARR 设为 49，开启 nominal 50 us update；
结束后关闭 TIM6 的 CEN/UIE，保留共享 DAC handler。
TIM6 guard 和 ADC consumer 均检查动作截止；采样间隔超过 75 us 时关断。
TIM6 update 中发现 DWT 未前进时以 BAD_CLOCK 关断。
TIM1 PWM、采样配置、Hardware Trip 的 comparator / BKE / BK2E 保持原配置。

TIM6 IRQ 当前优先级 15，ADC 与 Break IRQ 为 0；关断延迟取决于中断响应，
不保证请求截止的单个微秒。它解决 ADC 回调停止且 CPU/中断仍可运行的情况，
不提供 CPU-independent 截止。主动 PWM 期间禁止 halt、断点和长时间屏蔽中断；
Hardware Trip 的硬件关断仍是单独能力，必须另做实物验收。

Trip 通知只请求 rescan；故障事实来自 authoritative snapshot。
动作期间通知会立即 software safe-off，sticky cause 未退休时拒绝新动作；
本入口不提供自动 rearm 或自动 Restart。

## 连续电流统计

CAPTURE_CURRENT 默认请求示例：

    g_target_validation_request.command = 4
    g_target_validation_request.power_key = 0
    g_target_validation_request.sequence = 1  // 最后写，使用新的非零序号

从消费请求的回调起收集 4096 个连续、完整、有效且有限的 B/C 事务，
约 204.8 ms。相邻 observation_time 差必须为 1 tick；
DWT 回调入口间隔还须处于 25～75 us，以拒绝 ADC 缺拍被软件计数掩盖的情况。
这些时间是 consumer 入口间隔，不是 ADC 采样孔径或精确 PWM 死区测量。

统计使用 Welford 算法，保存各相 mean_a / min_a / max_a / variance_a2；
variance_a2 是无偏样本方差，PC 开平方得到标准差。累积溢出也以 BAD_CURRENT 结束。
不应用零点修正，不重构 A，不据此修改 Platform 名义传递关系。

接受请求时和结束时发布 g_target_validation_capture，中途不改该结果对象。
整份读取前后 sequence 必须相同且为偶数；还须核对 statistics.request_sequence
对应本次请求、result=COMPLETE、sample_count=4096、
last_sample_time-first_sample_time=4095（等价于 204750 us）。
失败的部分窗口可以留作诊断，不能当作完整统计。
完成结果保持到下一次接受 CAPTURE_CURRENT，可连续读取两次一致结果。

另核对 MOE=0、六路 MCU 输入 LOW、stage/enable 计数无增量。
ADC 丢失时，快照和采集结果可能停在旧状态；
独立 g_target_validation_stop_reason 先记录结束原因，
ADC 恢复后才将部分结果及原因同步到快照。不能只读旧 state/result 判断仍在运行。

用多个独立窗口比较均值与噪声。第一窗口可以给出 offset 候选值，
后续窗口检查其稳定性；完整 offset/gain 校准、负载电流与温漂另行验收。

## 快照与镜像

g_target_validation 的运行期唯一写者为 ADC consumer，sequence 奇数表示更新中；
读前后同一偶数才使用整份结构。SWD 速度不足时只观察独立 32-bit 计数，
或读取已经锁存的 capture 结果。
g_platform_bringup 保留原 76 字节 Watch ABI，慢字段仍由不同上下文更新，
整份没有一致性序号，不把它当作同一时刻快照。
callback_max_cycles 只统计本 consumer，不是整个 ADC ISR WCET。

正式工程为 MDK-ARM/BLDC_control V1.uvprojx。
要求全量 Rebuild 0 Error / 0 Warning，并核对 MAP 中 guard IRQ、
stage / enable / safe-off、Hardware Trip 路径的最终地址。
读回地址和布局来自同次构建 AXF/MAP，先核对芯片与 Flash/HEX，
不能把旧镜像的验证结果或地址用于新版。

烧录前关闭母线、断开电机；烧录后复位并 Run，确认 started=1、
g_platform_bringup.status=1、计数递增、MOE=0。
连续采集用 SWD HOTPLUG，核对 DHCSR S_HALT=0；
halt/reset/download 诊断与连续采集证据分开。

## 本地软件验证

实际 ARMCC 编译的 validation 源码与 fake HCC/time 在 Unicorn 中执行。
覆盖命令防护、fresh staging、单次使能、独立截止、Trip、连续性与统计计算；
本轮另执行真实 protection_probe.c，用显式 MMIO 效果模拟 DAC/COMP/TIM，
覆盖硬件证据缺失、配置变动、时钟停滞、注入保持、清除与显式恢复。
当前共 108 个用例，包括后来加入的 30%/70% 请求与实际传给 HCC 的三相 duty 断言。
2026-10-18 收尾时重新编译并执行模型，108 项全部通过；这不是 Target 全量 Rebuild。
HCC / 时间仍是替身，不模拟电气行为，不证明真实 Platform 的 staging deadline、
实际中断延迟、恢复边界或硬件 L1/L2；真实 Platform 的逻辑还需独立测试，电气行为需上板。

依赖 ARM Compiler 5、Python unicorn / pyelftools；默认编译器目录
D:/keil5/ARM/ARMCC/bin，可用 KEIL_ARMCC_BIN 覆盖。检查构建只写临时目录。

    python firmware/targets/BLDC_G431/validation/tests/verify_model.py

主机工具 validation/tools/logic_check.py 只允许 key=0 的命令，先核对完整 Flash，
再从同次 AXF 获取字段偏移和宽度（ARMCC 的 PhaseMask 为 1 字节），以 HOTPLUG 读取。
--inspect-only 仅观察；完整逻辑套件要求本次启动还没有消费过请求，不自动重放。
只读块不宣称整份 ADC 快照一致，使用独立计数、稳定状态、锁存采集和实际硬件寄存器交叉核对。
27 项主机安全检查覆盖零 key、允许命令、序号、字段宽度及拒绝条件，不访问硬件：

    python firmware/targets/BLDC_G431/validation/tests/verify_logic_tool.py

validation/tools/probe_once.py 独立拥有一次功率密钥提交和分步恢复入口。
2026-10-18 收尾修正预检结果不能直接串联执行的问题后，113 项主机状态机检查通过，
覆盖命令、证据缺失、旧请求、阈值/路由、恢复顺序及新启动 → 预检 → 单次执行的文件输入输出链。
测试本身使用替身，不接触硬件；历史现场使用的是修正前版本，不追认为新工具已经上板。
上板每次操作先核对 Flash、AXF/MAP 和前次证据，重复只读检查通过才允许写请求；
--preflight 不写，--execute-once 仅写一次固定 2 ms / 50% 的 BREAK_*。
执行后保持故障，不自动 clear/rearm；异常清理仅 SAFE_OFF，不自动重试功率请求。
波形经用户确认并单独记录后，才允许按 held-rearm → clear → verify-sticky → rearm 执行恢复。
verify-sticky 使用无功率的 STAGE_ONLY，检查锁存拒绝，不尝试再次 Power Enable。
这些主机检查仍不证明硬件保护或精确关断延迟。

    python firmware/targets/BLDC_G431/validation/tests/verify_probe_tool.py

另外两组纯主机检查可分别运行，不需要 ST-Link、构建产物或本机原始证据：

    python firmware/targets/BLDC_G431/validation/tests/verify_pwm_tool.py
    python firmware/targets/BLDC_G431/validation/tests/verify_current_tool.py

2026-10-18 收尾复核为逻辑 27 项、保护 113 项、PWM 62 项、电流 5 个 unittest 方法
（含子用例）通过。各组统计方式不同，不合计成一个“硬件测试数量”。

## 主机实测工具的交付边界

`validation/tools/` 保存本轮固定候选镜像的台架工具，尚未形成从全新克隆开始的跨机器实测入口。
纯主机测试可独立运行，实测程序还有以下本地前置条件：

- `logic_check.py` 固定检查上述候选 HEX，并依赖同次 AXF/MAP；从离线 AXF 取得布局，再读回完整 Flash 核对。
- CubeProgrammer 和 GDB 使用脚本内的本机 CubeCLT 路径，尚未提供外部路径配置。
- 构建身份依赖被 Git 忽略的 `local-evidence/20261015-protection-probe/BUILD_MANIFEST.json`。
- `current_check.py` 和 `position_watch.py` 还核对本机 `20261017-duty-check/source-freeze.json`。
  该清单冻结的是当时版本；本次保护工具修复后相关源码哈希已变化，继续引用旧清单会拒绝运行。
  原始清单和当时的工具副本保留不改，不能通过更新旧清单冒充本次工具已经上板。

后续复用前需建立新的、显式审查的构建/源码清单与工具路径配置，并重新验证工具入口；
不能删除身份检查或仅替换硬编码哈希来绕过约束。已保存的历史实物结论仍属于各自的冻结版本。

当前模拟测试已验证以下数据衔接：`pwm_once.py --inspect-startup` 的只读新启动结果，
可作为 `probe_once.py --preflight` 的前序文件；预检保存的 `summary.json` 再交给独立的
`--execute-once` 调用。每一步仍检查实时健康状态、事务计数和 probe capture，没有自动执行或重放。
这里描述接口关系，不是当前接电指令。供电、接线、仪器就绪和一次功率请求须单独确认。

## 供电纹波初筛

DS100 先用一个 CHA 通道测 3V3，再按相同接法测 VREF+；地夹只接 GND，
接线或换点时先关闭 OUTPUT。使用靠近测点的地参考与尽量短的地线。
这一轮只测约 3.3 V 的小信号：实体探头和菜单均为 X1，先 DC / 1 V/div
核对直流约 3.3 V，再 AC / 20 mV/div / AUTO / Vp-p 查看纹波。
先记录 1 ms/div，再记录 10 us/div；幅度超出屏幕时适当增大电压档位。
这些是本轮起始设置，不是 DS100 的纹波精度保证。

在相同设置、同样供电下，将尖端与地夹都接同一个 GND，记录测量本底；
与 3V3 的同档波形比较，避免把拾取/仪器噪声直接当作电源纹波。
照片保留探头倍数、耦合、Vp-p、时间/电压档位和采样率。
AC 显示围绕 0 V 的交流起伏；其峰峰值不是电流样本的标准差。
波形初筛不能单独证明电流噪声来自电源；后续按需比较 capture 负载状态。

菜单依据 [DS100 厂家手册](https://supereyes.ru/img/instructions/Manual_Punctual_Atom_DS100_Mini.pdf)
第 4.12～4.17 节，固件版本可能改变菜单位置。后续 PWM/Gate 使用 X10，
高侧测法和带宽/接地限制另行核对。

## 低侧 Gate 基本波形（无电机）

沿用 12 V / 50 mA 限流供电，无电池、无电机，ST-Link 3V3 断开。
OUTPUT 关闭时接线：普通探头地夹只接 GND，尖端依次测 LA、LB、LC。
实体和菜单均为 X10，实际采集前设 DC，先用 5 V/div、10～20 us/div，
CHA 上升沿约 5 V、SINGLE 等待触发；包络检查另用 500 us/div。
暂停后改变耦合菜单不构成按新耦合重新采集的证据。

收到本测点就绪后，再从同次 AXF/MAP 获取布局并核对实时 Flash/HEX、
Core running、默认关断、有效采样、健康保护和 10～14 V 母线。
只提交一条 2000 us / 三相共同 duty=0.5 请求，sequence 最后写；
不自动重发、rearm、halt/reset 或下载。正常 OUTPUT 关/开后的新启动
必须重新验证，不沿用上一次启动的动作计数或请求序号。
操作人先确认 SINGLE 已由本次命令触发并 STOP，再关闭 OUTPUT 并保留记录。
若命令结束后仍等待触发，先手动 STOP、再关闭 OUTPUT，并记录未捕获；
避免把关闭电源造成的下降沿误当成本次 PWM 波形。
结束后核对 key 消费、enable 仅增加 1、COMPLETE、MOE=0、六路输入 LOW、
TIM6 停止、无故障、采样继续；关闭 OUTPUT 后保留 STOP 照片与读数。

核对频率约 20 kHz、周期约 50 us、稳定正脉宽接近 25 us，
高电平接近驱动供电、低电平接近 0 V。记录 Vmax / Vmin 和单位；
自动 Duty、正脉宽 / 周期交叉检查。包含启动前平线或首个不完整脉冲的
窗口不能直接代表稳定占空比，应保持 STOP 移到完整连续周期再核对。
基本检查只覆盖相应测点与本轮无负载条件，不替代精确包宽、
起始瞬态、过冲、死区、高侧 VGS 或带功率 Break 验收。

## 相端输出基本波形（无电机）

三路低侧基本检查后，沿用上述单次请求和镜像/保护/关断核对流程，
普通探头尖端依次接 MA、MB、MC，地夹始终只接 GND。
换点先关 OUTPUT。沿用 X10 / DC / 5 V/div / 10～20 us/div，
SINGLE、触发阈值约 5 V；若上电静态相端接近 VBUS，优先使用 CHA 下降沿，
捕获首次拉低。包络需要时另取 500 us/div。

检查各相对 GND 是否在接近 0 V 与 VBUS 之间周期切换，
记录频率、稳定电平和占空比。三相共同 duty=0.5，不能用接近 0 的相间电压
判定没有 PWM。断开电机、上下管均关断时相端可能悬空，
上电静态或 safe-off 后均可能测得较高电位，不要求它立即归零；
单独的静态相端电压不能证明 MOS 导通，停止周期切换也不能证明六颗 MOS 均已关断。
相端波形不等同于高侧 Gate 对 Source 电压或精确死区，相关项目单独验收。
自动 Vmin / Vmax 包括切换瞬态，不直接当作稳态平台电压。
出现明显负向尖峰时，先保持 STOP 放大边沿并记录接地方式；
必要时用更短的 GND 回路重新采集比较，再判断电路与测量引入的分量。
改变探头接法须关闭 OUTPUT；旧记录放大不能增加原始采样带宽或时间分辨率。

重复的同类基本检查可接受用户明确人工复核一致的报告，有差异再补照片；
按证据来源记录，不把其他测点的精确数值复制为本测点读数。

## 高侧稳定平台电平粗查（双通道，无电机）

先检查 A 相：CHA 尖端接 HA，CHB 尖端接 MA，两只地夹均接同一板上 GND。
普通探头地夹不能接 MA；换线时关闭 OUTPUT 并确认母线已放电至 1 V 以下。
两支普通探头均使用 X10，两个通道菜单分别设 X10、DC、10 V/div，
两路垂直偏移均为 0 V，使零位标记对齐。时基 10 us/div、水平偏移 0 us，
触发改为 CHA 上升沿约 18 V，SINGLE；上电稳定后重新进入等待。
本轮静态 HA/GND 约为母线电位 12 V，预期高侧导通时 HA/GND 达 20 多伏；
18 V 位于二者之间，用于减少接触抖动造成的 0～12 V 跳变提前触发。
这只是本轮 12 V 供电下的触发选择，不是高侧 VGS 合格阈值。

双手持探头时，先固定两只地夹、完成示波器设置并等待，
操作人拿起探头前只需回复一次“开始”。执行方先做只读前置检查，
通过后明确宣布 8 s 倒计时（用户调整后的操作等待时间）；操作人利用该窗口将两支尖端稳定接到 HA、MA。
倒计时由当前交互回合的可中断等待执行，不预先在后台排队上电脉冲；
收到停止、修改接线或其他影响就绪条件的输入应取消本轮。
倒计时结束后仍重新核对实时状态，只发一条原参数请求，不自动重发。
捕获后可放下探头再查看 STOP 并回报结果，避免要求持探头时操作按钮或打字。
长期重复测量优先断电焊短绝缘引线并固定探头；手持采集只作稳定平台粗查。

仍使用同一镜像、实时前置检查及一次 2000 us / 三相共同 50% 请求。
记录同时显示的两路稳定周期，按同一时刻的平台电压比较
VGS = V(HA/GND) − V(MA/GND)；导通平台预期约 10～12 V，
关断平台预期接近 0 V，该量级仅作为本轮粗查起点。
不要将两个通道在不同时间取得的 Vmax 直接相减，也不把 HA/GND 单独当作 VGS。
若照片中两路采用不同电压档位，只要零位可辨、平台未削顶，可按各自刻度
读取同一时刻的平台电压；两条线视觉重合不代表实际电压相同。
本轮 A 相实际采用 CHA 10 V/div、CHB 5 V/div、20 us/div，
可保留这些已能判读的设置继续 B/C，相应尖端改为 HB/MB、HC/MC，两地夹仍只接 GND。

DS100 当前已核对手册未列出 A−B 数学运算，不假定存在该功能；
可先用同一采集中的稳定平台与光标作粗略比较。
长地线、通道增益/偏移差异和时间偏差限制了相减精度，
不能用本步骤资格化动态尖峰或精确死区。
参考 [Tektronix 差分测量说明](https://www.tek.com/en/documents/whitepaper/abcs-probes-primer)。
确认本次 SINGLE 已 STOP 后再关闭 OUTPUT；若未触发先手动 STOP，再关闭 OUTPUT。

## 上板顺序

1. 无母线、ST-Link 3V3：默认观测、非法/重复命令、STAGE_ONLY、CAPTURE_CURRENT。
   不提交 PWM_BURST；独立 guard 的实测与 ADC 丢失诊断单独记录。
2. 电源 OUTPUT 关闭时移除 ST-Link 3V3，保留 SWDIO/SWCLK/GND/NRST，
   无电池、无电机，恢复已验证的 12 V / 50 mA 静态供电条件。
3. 功率保持关闭，核对供电、保护与输入状态，收集多个电流统计窗口。
4. PWM_BURST 和带功率 Break 属于后续步骤，先准备探头接法与短脉冲验收条件。

电源红色正端接 VIN、黑色负端接 GND；先短时观察 CV/CC、输入电流和 DRV 温升。
持续 CC、异味或快速升温时关闭 OUTPUT，先定位，不靠提高限流继续。
50 mA 是静态输入限流起点，不是相电流限额或器件不损坏的保证；
主动 PWM 的供电条件需另行核对。

六路静态网络对为 HA/MA、LA/GND、HB/MB、LB/GND、HC/MC、LC/GND。
低侧以 GND 近似 Source 仅适用于无负载静态状态。
高侧 VGS 必须 Gate 对 Source；普通示波器地夹统一接 GND，不能接相线。
不便接触时关闭 OUTPUT、确认母线低于 1 V 后固定绝缘引线。

硬件修复事实保存在 hardware/BLDC_G431/HARDWARE_BASELINE.md；
完整临时日志留在本地，不追加到本流程。静态正常与模型 PASS
均不证明 MOS 开关性能、动态 VGS、死区或真实带功率 Break 关断。
