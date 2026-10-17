# BLDC_G431 首板修复与实物调试记录

记录范围：首板 Bring-up、DRV8300 修复、截至 2026-10-15 的第 14 次独立短脉冲请求，
以及 2026-10-17 的保护候选逻辑、12 V 静态、第 15/16/17 次 OVP、第 18/19 次 OCP，
及 2026-10-17～18 的第 20/21 次 30%/70% PWM、第 22/23 次 70% 包末尾补采、第 24 次 AUTO 观察，
随后功率关闭时的五窗电流统计与编码器手动正反转观测。
本文件保存过程与证据边界；历史段落中的“待测”按当时状态保留，
当前汇总以 [台架验证状态](VALIDATION.md) 和 [硬件基线](../../../hardware/BLDC_G431/HARDWARE_BASELINE.md) 为准。

少量原始波形已另存为[公开证据](validation/evidence/20261018-baseline/README.md)。下文 `local-evidence` 链接为本机原始记录索引，不随仓库分发；历史操作指令不是当前待执行任务。

## 本轮证据定位

- 主动验证镜像 HEX SHA256：B53572898B0CD019698746BA5FA57B78B7174DB11DDE5DCC3A529E882BDF1362。
- 各次主动操作前均从同次 AXF/MAP 获取布局，并核对当前 Flash 与 HEX；本轮镜像覆盖 28588 字节。
- 共 14 次独立请求，均为 duration_us=2000、三相共同 duty=0.5；包含前置 staging，不将请求时长当作实测 Gate 包宽。
- 最后完成证据：20261015-150321-first-burst-repeat2-execute-ee32f007/summary.json。
- 该轮高侧 C 照片：scope-highside-c-hc-mc.jpg；原图哈希、读图数据及操作记录与该次 summary 同目录。
- 原始 SWD/CLI 日志、冻结的诊断脚本、快照和照片已复制到本机
  [本轮证据目录](validation/local-evidence/20261014-15-repair-check/)，包含逐文件 SHA256 清单 ARCHIVE_MANIFEST.json。
  原临时目录仍保留：C:/Users/wjh2/AppData/Local/Temp/codex-g431-repair-check-20261014-0f885ee4a17d42eb985ac8e768d8162e。
  local-evidence 的原始数据由 Git 忽略，不会随仓库克隆分发；仓库文档保存结论和过程。
  冻结脚本用于追溯本轮，不作为脱离实时前置检查的自动执行入口。
- 最新操作偏好为用户回复一次“开始”，只读检查通过后明确宣布 8 秒可中断倒计时，
  结束后再次核对并仅提交一次原参数请求。此前 15 秒为历史操作记录。

## 阶段记录

### 2026-10-15：保护注入候选，仅离线

在上述 14 次实物请求结束后，增加 Target 私有 DAC 注入与软件 safe-off 前的 Break 证据采集，
以及独立 CLEAR_INJECTION / REARM 命令；启动仍不自动输出。
106 个 ARM 执行模型用例通过，全量构建 0 Error / 0 Warning，候选 HEX 为
29AB28611691DD220624708ECF826BE9EA31F4C82753418DCC6BE64D8F5FACFF。
尚未烧录，没有新增实物验收。详见 [注入方案与验收步骤](validation/PROTECTION_PROBE.md)。

### 2026-10-17：保护候选身份核对及 ST-Link 供电逻辑检查

用户确认 OUTPUT 关闭、ST-Link 3V3 已接，无电池/电机。
备份完整 128 KiB Flash 后发现已匹配保护候选 29AB2861… 的 29720 个镜像字节，
本次未执行烧录；不能由此确定此前何时加载。首次连接未取得有效运行状态，
无母线软件复位后运行恢复，初始状态原因未确定，仍需下一供电阶段核对自然上电启动。

读取工具最初将 ARMCC 的 1 字节 PhaseMask 按 4 字节解码，已修正为从 AXF 同时取得
字段偏移和宽度；修正前的该字段不作为硬件证据。27 项主机安全检查通过。
修正后两次只读检查通过：status/started=1、OBSERVE、current invalid=0、
Hardware Trip 健康、六路 LOW、MOE=0；采样与 SysTick 持续前进。
随后六个明确的逻辑请求为 stage-only、4096 点采集、以及 key=0 的命令 3/5/6/7。
前两项 COMPLETE，后四项 LOCKED；stage successes=2、enable attempts/successes=0、
probe outcome/held=0、DAC 仍为 2065/345/345、TIM6 结束后关闭，无自动重放。
观察约 19.991 samples/ms SysTick，consumer 最大 14.32 us（不是整个 ADC ISR WCET）。
母线 ADC 约 2.65 V，与先前 ST-Link 3V3 记录相近；无万用表对照，不资格化真实电压/反灌。

本次没有带功率请求，历史功率脉冲仍为 14 次。原始备份、复位前后读回、工具版本、
命令与捕获结果见 [本次会话](validation/local-evidence/20261017-151739-probe-flash-logic/session-summary.json)。
下一项为移除 ST-Link 3V3 后，12 V / 50 mA 的自然上电与静态检查，再准备受控 OVP。

### 2026-10-17：12 V 静态及单次注入工具准备

用户回报“12V供电无异常”。本次未执行 reset/download/halt，直接 HOTPLUG 读取到健康
新启动状态：命令/stage/enable/capture 均为 0，采样和 SysTick 持续前进，current invalid=0，
六路 LOW、MOE=0、Trip condition/cause=0，DAC 阈值 2065/345/345 未变。
三次初读与随后严格预检的母线观测约 11.96～12.04 V；用户未另报精确电流与 CV/CC，
不把此前 16 mA/CV 当作本次新读数。此次上电正常，首次连接异常的成因仍未确定。

新增独立主机工具 probe_once.py，59 项无硬件状态机检查通过；logic_check.py 的读取层
补充了 AXF 派生的 64 位观测时间及母线浮点值，原 27 项主机检查继续通过。
工具从前次 summary 锚定静止计数和命令结果，核对 Flash/布局、路由、比较器和 DAC 模式，
--preflight 只读通过；尚未执行 --execute-once，没有提供任何功率 key。
功率脉冲历史仍为 14 次。波形人工确认后恢复分为四次独立命令：故障保持时 rearm 拒绝、
清除注入但保留锁存、无功率 STAGE_ONLY 被锁存拒绝、显式 rearm 后保持输出关闭。

工具版本、只读快照和检查记录见
[本次静态证据](validation/local-evidence/20261017-153714-powered-static/qualified/summary.json)。
下一项为 LA 单通道等待触发，用户“开始”后 8 秒可中断倒计时，再单次执行 OVP。

### 2026-10-17：OVP 单次注入，寄存器证据通过，波形待确认

用户“开始”后，先以 HOTPLUG 只读核对 Flash、布局、两次健康状态和前次静态证据。
明确宣布 8 秒倒计时；等待完成后再次检查并仅提交一次 BREAK_OVP，sequence=1。
工具返回 PROBE_OBSERVED_OFF_SCOPE_PENDING；本镜像本次启动的 enable attempts/successes=1/1，
stage attempts/successes=5/5，guard IRQ=4。历史带功率请求累计 15 次，不将本次故障提前
结束的 Gate 包宽当作 2 ms 截止时长。

独立 capture 两次读取相同且 sequence=4：MOE before=1、COMP before=0、无 BIF/B2IF；
随后 COMP3=高、BIF=1、MOE after=0。捕获期间可屏蔽 IRQ 已屏蔽，采集之前没有软件
MOE 清零，采集后才软件 safe-off。elapsed=70 cycles / polls=1 仅为轮询观察区间，
不是实际 Gate 传播延迟。consumer 最大 3135 cycles（18.44 us），不代表完整 ISR WCET。

结束后 OBSERVE / result=stop=HARDWARE_TRIP，MOE=0、六路 LOW、TIM6 CEN/UIE 关闭，
key 已清、ADC/SysTick 持续、invalid=0、无额外 enable。DAC1_CH1 仍为 0，其他两路
仍为 345；COMP3/BIF 和 OVP sticky cause 保持，BIE 暂停软件通知，BKE/BK2E 不变。
已告知用户可以放下探头，并请求 LA 波形及电源/DRV 状态。尚未清注入或 rearm；
实际 Gate 和恢复流程均待确认，不把本轮寄存器观察直接作为完整硬件保护验收。
证据、冻结工具和原始 CLI 读回见
[本次 OVP 现场](validation/local-evidence/20261017-153714-powered-static/ovp-execute/observation-record.json)。

### 2026-10-17：人工要求重测 OVP，第 16 次功率请求

尚未收到首次照片时，用户明确要求“再来一次”。先只读检查，原本预期保持故障，
实际发现命令/stage/enable/probe 计数均为 0、名义阈值已恢复、Trip 健康，是新的启动状态。
这是状态变化，不是持续故障恢复通过；本主控没有发送 reset/clear/rearm，也不推定状态变化原因。
首次寄存器证据仍在磁盘，首次 scope/recovery 均保持未验收。

对新启动的两次只读结果执行完整健康检查、保存独立锚点后，宣布并完成 8 秒倒计时，
仅提交一次新的 OVP。回读本次启动 sequence=1，stage=5、enable=1、guard IRQ=4；
捕获再次为 COMP3/BIF 成立、软件 safe-off 前 MOE 1→0，elapsed=70 cycles / polls=1，
不作为 Gate 延迟。结束时六路 LOW/MOE=0、TIM6 已关、invalid=0、阈值 0/345/345、
OVP condition/cause 保持、BIE 暂停、CPU/采样运行，没有自动重放。历史功率请求累计 16。
已立即通知用户可以放下探头；当前等待本次 LA 照片及电源/DRV 状态，尚未执行恢复。
最新证据见 [本次人工重测](validation/local-evidence/20261017-153714-powered-static/ovp-repeat1-execute/observation-record.json)。

### 2026-10-17：OVP 的 LA 包结束照片确认，恢复前连接未通过

先收到 20 us/div 的周期局部照片，右端仍有高脉冲延伸出可见窗口，未据此验收包结束。
随后收到同一 STOP 记录的 50 us/div 视图：CHA 为 X10 / DC / 5 V/div、上升沿约 4.97 V，
水平偏移 281 us；显示频率 20.0 kHz、Vmax 12.2 V、正脉宽 24.8 us、Vmin -204 mV。
可见四个完整高脉冲，末尾回到低电平并保持至窗口结束，起始另有很窄的边沿残迹。
本图通过有限 Gate 包结束检查；不将自动极值、显示缩放或边沿残迹用于精确尖峰、
死区、关断延迟或真实 17 V 阈值验收，也没有补发功率请求。
原图及 SHA256/判读保存在
[LA 包结束证据](validation/local-evidence/20261017-153714-powered-static/ovp-repeat1-execute/scope-la-packet-end.json)。

恢复前的只读连接识别到 ST-Link，但未取得 MCU core ID。一次连接尚未结束时误启动的
另一只读检查报 DEV_CONNECT_ERR，不把该次竞争当作独立 MCU 诊断；随后串行 1000 kHz
HOTPLUG 仍未取得 core ID。没有 reset/halt、clear/rearm、阈值写入或新增 PWM。
最后成功回读仍为第 16 次结束时的故障保持状态，不能称为照片后的实时状态。
恢复四步均未执行；等待确认 OUTPUT/连接状态后重新核对本启动与原捕获的连续性。

### 2026-10-17：确认曾断电，第 17 次补做 OVP 恢复

用户说明此前已关闭电源、现在刚打开并要求继续。只读 HOTPLUG 恢复，Flash 仍逐字节匹配
29AB2861…；新启动命令/stage/enable/probe 均为 0、保护健康、母线约 11.96 V、采样正常，
MOE=0、六路 LOW。本次没有复位。前次连接失败发生在用户关电期间，不据此判断 MCU 故障，
也不能认定所有历史连接失败均有同一原因。第 16 次照片继续有效，恢复未完成。

为减少重复人工抓图，主机工具新增显式恢复用波形引用：核对先前同镜像/source、已通过的
寄存器证据、包结束照片及哈希。只允许用于不使能输出的四步恢复；本轮 scope_qualified
保持 false。新增引用来源错误、图片变化、功率命令误用、恢复中更换引用等反例后，
74 项主机工具检查通过；原逻辑工具 27 项继续通过。固件与构建产物未改动，不重复编译。

实时预检通过，明确宣布并完成 8 秒可中断等待，只提交第 17 次独立 OVP 注入。
本次 sequence=1、stage=5 / enable=1 / guard=4；COMP3/BIF 成立且软件 safe-off 前 MOE 1→0，
elapsed=70 cycles / polls=1，不能当作实际关断延迟。输出关闭、故障保持后，逐条检查并发送：

1. sequence=2 REARM：REARM_REJECTED，held=1、COMP3/BIF/cause=OVP 均保持。
2. sequence=3 CLEAR_INJECTION：恢复 2065/345/345、COMP 回低、held=0；OVP sticky 保留、BIE 仍暂停。
3. sequence=4 STAGE_ONLY：HARDWARE_TRIP，stage/enable 计数未增加，验证锁存继续拒绝动作。
4. sequence=5 REARM：成功，cause/COMP/Break=0、BIE 恢复，保护健康但不自动输出。

四步来自同一连续启动和原 capture；active_request 始终为 1，stage/enable/guard 仍为 5/1/4，
六路 LOW、MOE=0、TIM6 已关、采样和 SysTick 推进且 invalid=0。未自动重试、复位或烧录。
本次没有新波形照片；关断波形证据来自第 16 次，恢复证据单独来自第 17 次。
冻结工具、74/27 项主机检查结果、每步原始读回及结论见
[本次恢复记录](validation/local-evidence/20261017-153714-powered-static/ovp-recovery-rearm/observation-record.json)。
OVP 注入关断及恢复阶段通过，实际阈值、精确 Gate 延迟及 OCP/BRK2 仍待验收。

### 2026-10-17：第 18 次 C 相 OCP，寄存器关断证据通过

用户在说明交流后明确要求继续 C 相验证并“直接开始”。只读 HOTPLUG 核对同次 AXF/MAP、
29720 字节 Flash 身份及健康状态，发现已是新的零事务启动；没有沿用第 17 次的运行期计数，
本主控也没有复位。两次回读采样正常、保护健康、六路 LOW/MOE=0，母线约 11.94 V。
当前主机工具 SHA256 与 74/27 项通过检查的冻结版本一致，无固件或工具修改。

明确宣布并完成 8 秒可中断倒计时后，重新完整核对，只提交一次 BREAK_OCP_C（命令 6，
2000 us / 共同 50%，使能后四个 ADC 回调再注入）。本启动 sequence=1，stage=5、
enable 尝试/成功各 1、guard IRQ=4；历史功率请求累计 18 次，没有自动重试。

capture sequence=4、source=C、outcome=OBSERVED：注入前 MOE=1、比较器全低、无 Break 标志；
注入后 COMP1=高、B2IF=1、MOE=0，均在软件 safe-off 前采集。elapsed=70 cycles / polls=1，
不能作为 Gate 关断延迟。consumer 最大 3144 cycles（约 18.49 us），不是整个 ADC ISR WCET。
结束后两次回读均为六路 LOW/MOE=0、TIM6 关闭、key=0、invalid=0，CPU/采样继续；
阈值为 2065/0/345、held=1、OCP condition/cause 保持，BIE 暂停且 BKE/BK2E 保留。

已立即告知可以放下探头；当前等待本次 LA 包结束照片及电源/DRV 人工反馈，尚未 clear/rearm。
C 相波形及四步恢复未验收，共享 AB 未执行，实际 4 A 门限和精确 Gate 延迟未资格化。
证据、冻结工具与原始读回见
[本次 C 相 OCP](validation/local-evidence/20261017-153714-powered-static/ocp-c-ready-230805-execute/observation-record.json)。

### 2026-10-17：C 相 OCP 波形及同轮四步恢复通过

用户提供第 18 次的 LA STOP 照片：100 us/div、水平偏移 0、X10 / DC / 5 V/div，
CHA 上升沿 4.97 V；显示频率 20.0 kHz、正脉宽 24.8 us、Vmax 12.2 V、Vmin -204 mV。
可见四个完整高脉冲及其后持续低电平，起始另有很窄的边沿残迹，有限 Gate 包结束检查通过。
不由自动极值推定真实瞬态、死区或保护传播延迟。本次照片未附电源/DRV 人工状态报告，
不代填精确输入电流、CV 或温升。原图及 SHA256/判读保存在该请求的 scope-la-packet-end.jpg/.json。

恢复前重新核对当前 Flash、两次运行状态、原请求及 capture，确认同一连续启动内仍 held=1、
COMP1/B2IF/OCP cause 保持、MOE=0、六路 LOW，母线约 12.00 V。随后四条无功率命令逐次验证：

1. sequence=2 REARM 返回 REARM_REJECTED，故障持续时拒绝恢复。
2. sequence=3 CLEAR_INJECTION 恢复阈值、COMP 回低、held=0，OCP sticky 仍保留且 BIE 暂停。
3. sequence=4 STAGE_ONLY 返回 HARDWARE_TRIP，stage/enable 未增加，锁存继续阻止动作。
4. sequence=5 REARM 成功，cause/COMP/Break 清零、BIE 恢复，但功率输出保持关闭。

四步都锚定第 18 次原捕获，active_request=1，stage/enable/guard 始终为 5/1/4，
六路 LOW、MOE=0、TIM6 已关、采样与 SysTick 继续且 invalid=0，没有自动重启。
本次波形和恢复来自同一连续运行，未借用 OVP 照片，未新增 PWM、复位、烧录或修改固件。
第 18 次 C 相注入关断及恢复阶段通过，共享 AB、实际 4 A 阈值和精确 Gate 延迟仍未验收。
汇总与每步原始证据见
[本次 C 相恢复](validation/local-evidence/20261017-153714-powered-static/ocp-c-recovery-rearm/observation-record.json)。

### 2026-10-17：第 19 次共享 A/B 注入与恢复通过，本轮保护检查结束

用户按“电源/DRV 无异常且单次触发就绪后回复开始”的准备说明回复“开始”，没有另报新的
精确电流/CV 或温升数值。只读预检确认仍是 C 相恢复后的同一启动、镜像匹配、母线约 12.01 V、
保护健康、采样正常、输出关闭；主机工具与冻结的 74/27 项检查版本一致。
明确宣布并完成 8 秒可中断等待后重新检查，仅提交一次命令 7（2000 us / 共同 50%）。
本启动 sequence=6，stage/enable/guard 从 5/1/4 增至 10/2/8，历史功率请求累计 19。

capture sequence=10、request_sequence=6、source=AB、outcome=OBSERVED：注入前 MOE=1、
比较器全低、无 Break 标志；随后 COMP2/4 同时高、B2IF=1、MOE=0，全部在软件 safe-off 前采集。
elapsed=70 cycles / polls=1 为轮询观察区间；结束时六路 LOW/MOE=0、TIM6 关、key=0、
阈值 2065/345/0、held=1、OCP condition/cause 保持，CPU/采样继续且 invalid=0。

用户随即人工报告“和前面的波形一样”，接受为本请求的有限 LA 包结束及后续低电平功能确认；
没有本次照片或逐项原始数值，不复制 C 相精确读数。重新只读核对同一请求及 held capture 后：

1. sequence=7 REARM：REARM_REJECTED，故障存在时拒绝恢复。
2. sequence=8 CLEAR_INJECTION：恢复阈值、COMP 回低、held=0，OCP sticky 保留。
3. sequence=9 STAGE_ONLY：HARDWARE_TRIP，stage/enable 未增加。
4. sequence=10 REARM：成功，cause/COMP/Break 清零、BIE 恢复，功率输出仍关闭。

恢复全过程 active_request=6，stage/enable/guard 保持 10/2/8，未新增功率动作、复位或烧录。
最后独立只读检查再次核对健康、镜像、关断、计数、原 capture 及采样推进，invalid=0、
母线约 12.02 V，无自动重启。共享 A/B 同时激活不能单独资格化每条 OR 路由。
共享 A/B 的寄存器、人工波形和同轮四步恢复见
[本次 A/B 恢复](validation/local-evidence/20261017-153714-powered-static/ocp-ab-recovery-rearm/observation-record.json)。

本轮 OVP、C 相及共享 A/B 受控注入的关断和恢复检查完成，汇总见
[保护阶段证据](validation/local-evidence/20261017-153714-powered-static/protection-stage-summary.json)。
OVP 采用第 16 次照片与第 17 次同轮恢复，C 相采用第 18 次照片与同轮恢复，共享 A/B 采用
第 19 次人工波形复核与同轮恢复；不混合不同启动的故障持续状态。
实际 4 A/17 V 门限、精确 Gate 延迟/动态 VGS/死区、不同 duty、带负载和电机运行仍未验收。
下一阶段按 VALIDATION.md 补不同 duty 的无电机短脉冲；当前没有新的功率动作排队。

### 2026-10-17：不同占空比验证离线准备，实物尚未执行

用户要求继续下一步。确认当前固件命令 3 已支持可变共同 duty，生产 C 源码及 HEX/AXF/MAP
仍匹配既有构建清单，本次没有改固件、重新构建目标镜像或烧录。
新增独立主机工具 pwm_once.py，仅允许 30%/70%、2000 us，前后检查硬件配置、采样/保护、
锚定计数、实际 mailbox 参数、一次使能与结束关断；失败不重发，写后失败只补 SAFE_OFF。
62 项主机检查通过；原保护 74 项、逻辑 27 项继续通过。

离线模型 fixture 记录实际收到的 PhaseDutyRequest，runner 加入 30%/70% 并核对三相实际传参，
扩展后的 108 项 ARM 执行模型通过。仅模型文件变化，不修改固件 ABI 或生产代码；
原 106 项构建清单作为历史冻结记录保留，不覆盖其源码哈希。
还用上一阶段保存的 TIM1 原始数据静态核对了新工具的 PWM 配置解码；未取得新实时硬件状态。

TIM1 PWM1 与互补低侧输出、DRV8300DPWR 非反相输入共同决定：程序 duty=30%/70% 时，
LA 的名义高脉宽分别约 35/15 us，实际读数受死区与边沿影响，不能把低侧互补关系当作参数错误。
先用 500 us/div 捕获整包，再保持 STOP 放大到 20 us/div 看稳定周期；不为放大重发。
步骤见 [不同占空比检查](validation/DUTY_CHECK.md)，冻结文件与日志见
[本次离线清单](validation/local-evidence/20261017-duty-check/source-freeze.json)。
本步骤没有 SWD 访问、没有新增实物请求或后台功率动作，累计仍为 19；等待第一档现场准备。

### 2026-10-17：第 20 次请求，30% PWM 命令及结束关断检查通过

用户按既定 LA/GND、X10/DC、5 V/div、500 us/div、单次触发及 12 V/50 mA 准备说明回复“开始”。
当前工具和源文件哈希与离线冻结清单一致；只读检查核对当前 Flash、TIM1 配置、采样/保护及
结束关断状态，发现为健康的新启动、事务计数均为 0、母线约 11.94 V，本主控没有复位。
预检通过后明确宣布并完成 8 秒可中断等待，执行工具再检查，仅发一次命令 3，
共同 duty=0.3（mailbox 浮点位模式 0x3E99999A）、duration_us=2000，sequence 最后提交。

本启动 sequence=1，返回 COMPLETE，stage attempts/successes=40/40、enable=1/1、
guard IRQ=39；结束后两次回读六路 LOW/MOE=0、TIM6 停止、key=0、保护健康，
采样与 SysTick 推进且 invalid=0。mailbox 参数与提交值一致，注入 capture 未变，无自动重放。
历史功率请求累计 20；没有改固件、重新烧录或自动清故障/rearm。

已通知可以放下探头，保留 STOP 全包后放大到 20 us/div 读取稳定周期；LA 名义高脉宽约 35 us
只是预期值，尚未收到本次照片/读数，不填为实测结果。70% 未执行。
本次证据见 [30% 首次现场](validation/local-evidence/20261017-duty-check/duty30-ready-234955-execute/observation-record.json)。

### 2026-10-17：30% 的 LA 稳定脉宽初筛通过，包末尾待确认

用户提供第 20 次的稳定周期 STOP 图：20 us/div、水平偏移 -10.5 us、CHA X10 / DC / 5 V/div，
触发 CHA 上升沿 4.97 V。显示 20.0 kHz、正脉宽 34.8 us、Vmax 12.2 V、Vmin -204 mV；
LA 占空比由显示值推导约 69.6%，符合程序共同 duty=30% 时低侧互补脉宽约 35 us 的预期。
稳定频率、基本电平与脉宽响应初筛通过；不将 0.2 us 的名义差值直接当作实测死区或传播延迟。
图中还有绿色迹线，按 CHA 数据判读，不从 STOP 菜单推断采集阶段 CHB 一定关闭。

该图只显示连续周期的局部，未确认整包最终下降沿后的持续低电平，真实包宽仍未验收。
已要求先保持 STOP 返回 500 us/div 确认末尾，再重新准备单次触发进行 70%；
没有补发 PWM、连接 MCU 或修改固件，实物请求仍为 20 次。
原图和哈希/读数见 [30% 稳定周期](validation/local-evidence/20261017-duty-check/duty30-ready-234955-execute/scope-la-stable.json)。

### 2026-10-18：30% 包结束确认，第 21 次 70% 请求与稳定周期通过

用户按旧 STOP 记录检查末尾及重新准备单次触发的说明回复“已结束，开始70%”。
第 20 次的最后脉冲后持续低电平按人工确认记录，不新增整包照片，不推定精确包宽。
只读预检重新核对冻结文件与实时镜像、TIM1 配置、采样/保护和关断，发现健康的零事务
新启动、母线约 12.05 V；未由主控复位，也不沿用前次启动计数。

明确宣布并完成 8.0066 秒可中断等待，执行前再检查，仅提交一次命令 3、共同 duty=0.7、
duration_us=2000，mailbox 位模式 0x3F333333。本启动 sequence=1，返回 COMPLETE，
stage attempts/successes=40/40、enable=1/1、guard IRQ=39。结束后两次回读均六路 LOW/MOE=0、
TIM6 已关、key 清零，保护健康、invalid=0、采样继续，注入 capture 未变。累计 21 次，
没有改固件、重新烧录、清故障/rearm 或自动重发。

用户随即提供 LA 的 STOP 稳定周期照片：X10、5 V/div、50 us/div，显示 20.0 kHz、
正脉宽 14.8 us、Vmax 12.2 V、Vmin -12.3 mV。低侧占空比约 29.6%，符合程序 70% 的
互补预期；与前档 34.8 us 对照，稳定脉宽变化、频率和基本电平初筛通过。
这张局部图不证明整包末尾，仍待在旧 STOP 记录中确认最后脉冲后持续低电平，未安排新请求。
不从自动极值或 0.2 us 名义差值推定真实瞬态、死区或延迟。电源电流/CV 与 DRV 温度未另报。
本次原图、参数及结束状态见
[70% 现场](validation/local-evidence/20261017-duty-check/duty70-ready-235953-execute/observation-record.json)。

### 2026-10-18：70% 包末尾未捕获，电源和 DRV 正常

用户反馈第 21 次“其他都正常 包末尾不知道 没抓到”，
补齐本轮电源/DRV 正常的人工报告，未另报电流/CV 或温度数值。新 STOP 图为 20 us/div、
水平偏移 -357 us，黄、绿两迹线在同一位置停止，后面为空白网格；不能据此确认包末尾持续低电平。
70% 的稳定周期通过保持，包末尾记为未捕获。后续补采须在 SINGLE 前设 500 us/div、偏移 0，
重新确认现场就绪后才单独发一次请求；本步未连接 MCU、未发脉冲，累计仍为 21。
本次照片与反馈见
[70% 记录边界](validation/local-evidence/20261017-duty-check/duty70-ready-235953-execute/scope-record-boundary.json)。

### 2026-10-18：电源重启后独立补采 70% 整包，第 22 次功率请求

用户请求“70%补采开始”，中断后要求“继续”。无后台测试进程、新预检为健康零事务启动；
第一轮 8.0189 秒倒计时后，用户说明重启了电源并要求重新来。当时尚未调用 execute，
未发功率命令，旧预检及准备参数已作废，该倒计时不计为独立功率请求。

重新检查冻结源文件/工具/镜像、实时 Flash、TIM1 配置、采样/保护和关断，确认新上电健康、
事务计数为 0、母线约 12.02 V，主控未复位。重新宣布并完成 8.0056 秒准备时间，执行前再检查，
仅提交一次命令 3、共同 duty=0.7、2000 us，返回 COMPLETE。本启动 sequence=1，
stage attempts/successes=40/40、enable=1/1、guard IRQ=39；结束两次回读六路 LOW/MOE=0、
TIM6 关闭、key 清零、保护健康、invalid=0、采样推进，注入 capture 未变。累计 22 次，无自动重发。

本次仅补整包结束证据，保持 500 us/div；整包照片/人工末尾确认及本轮结束电源/DRV 反馈待回报。
第 21 次 14.8 us 稳定周期照片仍为独立来源，不改写为本次同轮照片。没有固件修改、烧录或 rearm。
记录见 [70% 包末尾补采](validation/local-evidence/20261017-duty-check/duty70-repeat1-restart-ready-002806-execute/observation-record.json)。

### 2026-10-18：拿错探头后按用户要求重测，第 23 次功率请求

用户报告“再来一次 我拿错探头了”，第 22 次没有有效波形，误拿探头所接测点未说明。
重新核对冻结文件/实时镜像、同次完成计数、配置及健康关断，母线约 11.96 V、与第 22 次同一启动。
提醒 CHA 探头接 LA/GND、X10、500 us/div、单次等待，宣布并完成 8.0081 秒准备时间；
执行前再检查，仅新增一次命令 3、70% / 2000 us，请求 sequence=2、COMPLETE。
累计 stage=80、enable=2/2、guard=78，本次分别增加 40、1、39；结束回读六路 LOW/MOE=0、
TIM6 关闭、key 清零、保护健康、invalid=0，采样继续且注入 capture 未变。累计 23 次，
没有自动重发、复位、烧录或 rearm。本次整包末尾波形和结束电源/DRV 状态待回报，
第 21 次稳定周期照片保持独立来源，第 22 次错误探头报告另存。
记录见 [70% 再次补采](validation/local-evidence/20261017-duty-check/duty70-repeat2-ready-003050-execute/observation-record.json)。

### 2026-10-18：第 23 次稳定周期确认，暂停补发并核对 DS100 采集窗口

用户反馈“还是抓不到啊”，新 STOP 图当前为 50 us/div、水平偏移 -496 us、CHA X10 / DC / 5 V/div，
显示 20.0 kHz、正脉宽 14.8 us、Vmax 12.2 V、Vmin -249 mV。本次稳定周期通过；
黄、绿迹线在同一位置停止，后面为空白网格，包末尾持续低电平仍不确认。当前 STOP 显示不能反推
采集前的时基，也不据自动极值判断真实瞬态；本轮结束电源/DRV 状态未另报。

核对 DS100 V1.5 制造商手册的波形存储、水平系统、采样率/存储深度及单次触发说明，
先保留旧记录，查看 STOP / 500 us/div、水平偏移 0 的总览，并确认是否在 STOP 后改为 50 us/div。
不直接认定操作员设错，也不再盲目重发；若需新采集，先检查单次等待时的设置照片。
本步仅保存图片与诊断，没有连接 MCU 或新增功率请求，累计仍为 23。
证据见 [第 23 次记录边界](validation/local-evidence/20261017-duty-check/duty70-repeat2-ready-003050-execute/scope-la-record-end.json)。

### 2026-10-18：核对 STOP 设置，确认设置后再采集

用户又提供旧 STOP 图，当前 100 us/div、水平/垂直偏移均为 0，
CHA 上升沿的当前阈值为 -441 mV，稳定周期仍为 20.0 kHz / 14.8 us。
两路记录一起结束后的空白不能确认包末尾；采集当时的时基/阈值仍未知。
流程改为先关 OUTPUT，核对 STOP 下 500 us/div、水平偏移 0、CHA 上升沿约 +5 V 的设置照片，
确认后再安排单次等待；本步未连接 MCU 或发命令，累计仍为 23。
证据见 [STOP 设置核对](validation/local-evidence/20261017-duty-check/duty70-repeat2-ready-003050-execute/scope-stop-overview-100us.json)。

### 2026-10-18：按用户选择 AUTO，第 24 次请求完成，短包未保留

用户要求正常重新发并改用 AUTO，取消先提供设置照片的流程。预检核对冻结文件/实时镜像及
健康零事务启动，母线约 12.03 V，未由主控复位。宣布并完成 8.0049 秒等待，执行前再检查，
仅提交一次命令 3、70% / 2000 us。本启动 sequence=1、COMPLETE，stage=40、enable=1/1、guard=39；
结束两次回读六路 LOW/MOE=0、TIM6 关闭、key 清零、保护健康、invalid=0、采样继续，注入 capture 未变。
累计 24 次，没有固件修改、烧录、自动重放或 rearm。

用户随即反馈“没抓到”。AUTO 可能被后续画面覆盖短包，实际仪器采集过程未独立核对；
先不增加请求。供电 OUTPUT 是否一直开启、当前 LA 是否持续约 0 V 且无周期脉冲待人工确认，
稳态后态与包末尾边沿/精确时间分别记录；供电中断后不当作本次同轮关断观察。
记录见 [70% AUTO 观察](validation/local-evidence/20261017-duty-check/duty70-repeat3-auto-ready-004637-execute/observation-record.json)。

### 2026-10-18：供电连续、LA 后态低电平确认，占空比基础检查收尾

用户明确回复“一直开着 始终0V”，确认第 24 次之后电源 OUTPUT 持续开启，AUTO 下 LA
保持约 0 V。接受为请求结束后低电平的人工复核；不声称用户看到了该次短包或最终下降沿，
不推定精确包宽或 Gate 关断延迟。70% 稳定脉宽由第 23 次 14.8 us 照片支持，后态由第 24 次支持，
来源分开记录，避免冒充同一次连续采集。

随后独立只读复核冻结文件、实时 Flash、同一第 24 次事务锚点、PWM 配置、健康保护和采样推进：
sequence=1、stage=40、enable=1/1、guard=39 保持，六路 LOW/MOE=0、TIM6 已关、key=0、
cause/COMP/Break=0、invalid=0，母线约 12.06 V。没有新增功率命令、复位、烧录或 rearm，
历史功率请求累计仍为 24。证据见
[最后只读核对](validation/local-evidence/20261017-duty-check/post-low-final-inspect-010453/summary.json)。

本轮 LA 的 30%/70% 基础脉宽响应和结束关断/后态检查收尾通过，仍保留 70% 最终边沿/精确包宽、
真实瞬态、动态 VGS/死区、其他相/相端不同 duty、相间时序与带负载能力的测量边界。
汇总见 [占空比阶段结论](validation/local-evidence/20261017-duty-check/duty-stage-summary.json)。
下一步转入电流采样零点/噪声复核及已知电流对照准备，不自动安排新的功率动作。

### 2026-10-18：新镜像下的静态电流五窗复核

用户“继续”后首次只读连接未取得 core ID，没有写入；用户确认此前关闭 OUTPUT、现在已打开。
重新核对候选 Flash 后，在健康新启动中仅提交五次 CAPTURE_CURRENT，key=0，序号 1～5。
五窗各 4096 点、共 20480 点，锁存重复读取一致、首末 ControlTime 差各 4095 tick，
consumer 间隔约 49.66～50.36 us。B 均值 −12.64～−12.07 mA、标准差 3.30～3.44 mA；
C 均值 +0.23～+0.80 mA、标准差 3.33～3.47 mA。第一窗作离线候选后，其余窗均值
残差绝对值最大为 B 0.413 mA、C 0.466 mA；未写入校正，不作为增益或温漂资格化。
stage/enable 从始至终为 0，结束六路 LOW/MOE=0、TIM6 停止、保护健康、invalid=0，
采样继续，没有重置、烧录或新增功率请求，历史功率请求仍为 24 次。
主机新增采集工具通过 5 个合成测试方法及其子用例，目标固件未改变。
数据和源码冻结见 [本轮统计](validation/local-evidence/20261018-current-review-011611/analysis.json)，
条件、范围和下一步已知电流对照准备见 [电流复核](validation/CURRENT_CHECK.md)。

### 2026-10-18：MT6701 手动一圈观测，待确认实际操作方向

用户确认磁铁固定在电机轴上，并按“正对出轴端看向本体、顺时针约 10 秒一圈”的说明回复开始。
实机预检通过：约 12.02 V、保护健康、MOE=0、enable=0，候选 Flash 匹配。
首个主机采集进程在等 stdin 时遇 EOF，只有静止基线、没有运动采集或目标写入；
改用有 45 秒超时的本地主机 START 文件后重新预检，再提示用户转动并开始 25 秒记录。
这是主机同步方式调整，固件未改动，所有 SWD 操作仅上传内存，无复位、halt 或命令写入。

取得 3 个静止基线及 101 个运动/停止抽样，共 104 点，抽到的 position_valid 均为真。
基线约 307.002°，运动主趋势递减，观察到一次约 0.0241 圈到 0.9946 圈的回绕；
按相邻最短角差展开，净变化约 −0.998108 圈，最后十点中位数约 307.683°。
首尾模一圈差约 +0.681°，包含手动回标误差，不作为编码器绝对精度或重复定位精度。
起始整理位置和回标有小幅反向移动，不能称每点严格单调。当前主机抽样间隔约 0.24 秒，
不等于全部 1 kHz SSI 帧连续记录；valid 字段与角度为独立诊断读数，不声称原子帧配对。
全程事务计数未变，结束 enable=0、MOE=0、保护健康。实际是否按约定方向转满一圈仍待用户确认，
确认前只记录递减及一次回绕的观测事实，不固化方向映射，也未做电角度对齐。
原始内存、逐点数据、工具快照及统计见
[手动转动观测](validation/local-evidence/20261018-position-cw-observation-01/analysis.json)。

### 2026-10-18：目测一圈条件确认及逆方向复核

用户补充上一轮为“顺时针大概转了一圈，纯目测”，确认方向而未提供精确对标。
因此只认定顺时针角度递减及回绕的粗查结果，不以 −0.998108 圈或首尾差评价精度。
补充说明保存为上一轮目录中的 operator-confirmation.json，原始分析按历史状态保留。

随后用户按逆时针约一圈的说明回复开始。工具增加 direction 参数，仅改变记录标签，
重新做只读预检，基线约 318.076°、母线约 12.03 V、保护健康且 enable=0。
提示逆时针转动后记录 25 秒，共 108 点（3 个基线、105 个运动/停止抽样），
抽到的有效标志均为真。角度主趋势递增，观察到 0.996094→0.008789 圈的一次正向回绕，
相邻最短角差展开的净变化约 +1.001282 圈；末尾十点中位数约 318.538°。
数据与逆时针约一圈的操作指令相符；用户未另行确认精确行程，且本轮不要求精确行程。
各次读取的事务计数不变，最终 MOE=0、enable=0、保护无故障，无目标写入。
当前方向关系按正对出轴端看向本体的约定记录为：顺时针递减，逆时针递增；
尚未将该约定写为应用层方向配置，电角度零偏/极对数对齐、绝对精度与全部 SSI 帧连续性仍未验收。
原始数据与脚本快照见 [逆方向观测](validation/local-evidence/20261018-position-ccw-observation-01/analysis.json)。

### 以下为原硬件基线阶段记录

以下正文从硬件基线原第 7 节迁入，正文原样保留。


当前已经验证：

```text
SWD connection                 PASS
Option Bytes readback          PASS
nSWBOOT0=0, nBOOT0=1          PASS
Keil full Rebuild ×2           0 Error / 0 Warning
Flash program                  PASS
Flash verify                   PASS
```

同日将 Hardware Trip 接入普通 Bring-up：bind → arm protection → start drive timing；callback 仅请求 rescan，故障事实来自 authoritative snapshot。正式 4 A OCP / 17 V OVP 配置下，全量构建为 0 Error / 0 Warning，烧录校验通过；连续 5 次重启均 arm 成功且无虚假 trip。约 22 秒无断点回归中，Current=20.00 kHz、invalid=0，Position/Bus=1 kHz，SSI CRC 抽样 25/25 通过；snapshot 有效、condition_active=false、causes=NONE，459333 次软件 MOE 检查未发现使能。供电条件仍为 ST-Link 3V3、无电池、无电机，未调用 Power Enable。

持续故障验证发现 active Break flag 无法清除会导致重复中断、阻塞后台 rescan。Target 已改为首次 Trip 后暂停 BIE 软件通知，保持 comparator 与 BKE/BK2E 硬件保护；显式 rearm 成功才恢复通知。约 2.3 秒持续 OVP 实测中通知次数不再增长，Current 保持约 20 kHz，SysTick 与 rescan 持续前进；BIE 暂停期间叠加 OCP，snapshot 仍保留两个 cause。6 次 active rearm 拒绝、5 次条件消失后恢复、新 episode boundary 竞争测试均通过，28 次观测 current invalid=0、SSI CRC 有效、MOE=0。一次性 rearm 验证入口已移除；普通固件重新全量构建、烧录校验通过，5 次重启与约 22 秒采集回归通过，456573 次软件 MOE 检查未发现使能。

此前进行 3S 电池供电、无电机、MOE=0 的静态检查：SWD 回读六路 MCU 驱动输入均低、保护 snapshot 无故障，Current/Position/Bus 采集正常；用户报告 VBUS 实测 12.04 V，3V3/VREF+ 和六路静态 VGS 符合预期。母线 ADC 约 11.9 V，该单点对照不构成测量校准。U5（DRV8300DPWR）随后被确认烫手；用户补充首次焊接后直接接电池时，该区域曾疑似短暂冒烟，来源未确认。故功率域静态验收未通过，停止带功率验证。

断电排查发现原 B/C 自举支路为几 Ω、A 为 MΩ 级；C26 离板高阻，原焊盘仍低阻。移除 U5 和 C25–C27 后三路空焊盘均恢复高阻（用户报告至少 100 MΩ）；旧 U5 相关引脚粗测为 MΩ 级，密脚接触可靠性未确认。U5 移除状态下，六路 Gate–Source 均约 10 kΩ；三相高侧/低侧 Drain–Source 二极管档正向约 0.45 V、反向超量程，未见明显硬短路，该筛查不构成高压漏电或开关性能验收。已更换备用 DRV8300DPWR 并复装三颗自举电容，用户报告三路 BST–SH 在板阻值均约 40 MΩ，原低阻未复现；用户更正复装后的断电读数：VBUS 对 GND 约 46 kΩ 后上升变慢，47 kΩ / 5.1 kΩ 分压电阻在板分别约 42 kΩ / 4 kΩ；这与分压及其他并联支路的量级相符，未见母线硬短路，不据此判定电阻精度或功率域验收通过。尚不能确定原异常来自芯片内部损伤还是拆焊消除的连锡；2026-10-14 更换后以 12 V / 50 mA 限流、无电机重新静态上电；用户报告输入 16 mA、CV，短时无明显温升，30 s 内电压、电流和状态稳定，万用表 VBUS=12.1 V、3V3=3.33 V。该轮 HOTPLUG 观测中 MOE=0、六路 MCU 输入 LOW、current invalid=0、保护无故障；母线 ADC 约 11.96～12.06 V，该对照不构成测量校准。随后用户回报 HA–MA、LA–GND、HB–MB、LB–GND、HC–MC、LC–GND 六路静态读数均为 0 V，DRV 无异常。更换后的低阻与异常发热未复现，本轮限流静态检查通过；低侧使用 GND 近似 Source 的结果仅适用于本轮无负载状态。未测 DRV 独立支路电流。未执行主动 PWM 或带功率 Break 测试。

将 Bring-up 从 main USER CODE 区整理到独立 validation 模块后，正式 Keil 全量构建为 0 Error / 0 Warning；新版烧录校验及 26080 字节 Flash 回读与 HEX 一致。ST-Link 3V3、母线 OUTPUT 关闭、无电机条件下，约 21.1 s 的 HOTPLUG 回归通过。随后移除 ST-Link 3V3，以原 12 V / 50 mA 限流、无电池、无电机条件复核新版；用户报告电源 12.1 V / 16 mA / CV、DRV 无异常，约 21.3 s 的 HOTPLUG 回归通过，母线 ADC 约 12.04 V。两轮平均采样均约 20.00 kHz、invalid=0，位置与母线采集有效、后台 rescan 持续推进，保护 snapshot 无故障，六路 MCU 驱动输入 LOW；各约 42 万次软件 MOE 检查未报告使能，各 32 次读回均 S_HALT=0。计数只支持平均速率判断，调试结构不提供整份一致性快照；未执行主动 PWM，母线 ADC 的单点对照不构成测量校准。

上述入口整理回归对应只拆分 Bring-up 的镜像。2026-10-15 新增受控命令、TIM6/DWT 截止与连续电流统计后，正式构建 0 Error / 0 Warning，63 个 ARM 模型用例通过；新版烧录及 28588 字节 Flash 回读核对通过。ST-Link 3V3、母线 OUTPUT 关闭、无电机条件下，0.1 / 0.5 / 0.9 三档 STAGE_ONLY 均完成，每次成功 stage 两次、enable 次数始终为 0，同一请求无重复执行。三轮 CAPTURE_CURRENT 均收齐 4096 个连续有效 B/C 事务，ControlTime 跨度均 4095 tick，锁存结果重复读回一致；consumer 入口间隔约 49.66～50.36 us，观察到的 consumer 最大执行时间 17.79 us，不作为整个 ADC ISR 的 WCET 资格化。三轮名义电流 B 均值约 -12.69～-12.56 mA、C 均值约 0.29～0.41 mA，两相单样本标准差约 1.22～1.29 mA；该结果只对应本轮逻辑供电，不作为 12 V 工作供电下的零点或增益校准。

同轮独立 ADC IRQ 丢失诊断中，ADC 计数与 ControlTime 停止、CPU 未 halt、SysTick 持续前进；TIM6 guard 独立记录 BAD_CURRENT 并停止测试计时，MOE=0、六路 MCU 输入 LOW、enable=0。该次判定时距最后 ADC consumer 入口约 107.46 us，不是实际功率 Gate 关断延迟或最坏响应界限；ADC-owned 快照按设计暂留在旧 CAPTURING 状态，独立结束原因已可读。恢复 ADC IRQ 后将 126 点部分窗口发布为 BAD_CURRENT，没有误报 COMPLETE；随后单独复位，默认观测与采样回归通过。

随后以实际 12 V / 50 mA 限流供电采集无功率电流基线，ST-Link 3V3 移除，无电池、无电机；用户报告电源 12.1 V / 16 mA / CV、DRV 无异常。五个独立窗口均完成 4096 点（共 20480 点）、ControlTime 跨度各 4095 tick，consumer 入口间隔约 49.66～50.36 us，锁存结果重复读回一致。各窗口 B 均值 -12.60～-12.19 mA、单样本标准差 3.21～3.41 mA；C 均值 -0.60～-0.28 mA、标准差 3.25～3.45 mA。仅用第一窗口均值作偏置候选，后四个窗口的均值残差绝对值最大约 0.42 mA，未向固件应用校正；这些短时结果不构成增益或温漂校准。相对 ST-Link 逻辑供电，标准差增加，来源未确认。MOE=0、六路 MCU 输入 LOW、stage/enable 次数为 0、current invalid=0，Hardware Trip snapshot 有效且无故障；母线 ADC 约 12.04 V，未据此校准母线。未执行主动 PWM，真实带功率截止/Break 关断仍待验收。

2026-10-15 继续按上述静态供电流程做 DS100 供电波形初筛。用户确认实体与菜单均为 X1、先前测点顺序正确；CHA 使用 AC / 20 mV/div。照片中 3V3 的 Vp-p 在 1 ms/div 为 47.9 mV、10 us/div 为 23.6 mV；尖端与地夹接同一 GND 的同档本底分别约 8.4 mV、11.4 mV。随后 VREF+ 的用户直流读数为 3.36 V，两档 Vp-p 分别约 48.7 mV、24.4 mV，幅度与形状接近 3V3；两点非同步采集，不据此判断相干性。测量本底不可直接用峰峰值相减扣除，X1 带宽、接地与采集/显示条件限制了结论；未见 VREF+ 比 3V3 明显更大的波动，尚不能确认电流噪声增大的来源，也不据此判定稳压器损坏或完整纹波规格合格。本轮没有执行固件命令或主动 PWM。

随后用户报告 LA 对 GND、X10、SINGLE 短脉冲测量已就绪。只读 HOTPLUG 再次核对镜像、关断和保护通过；一次性控制脚本在 PC 检查 12 组不满足前置条件的输入均拒绝，正式执行前重新从 AXF/MAP 获取布局并将 28588 字节 Flash 与 HEX 核对。仅提交一条 PWM_BURST（sequence=1、duration_us=2000、三相共同 duty=0.5，时间包含前置 staging），没有 halt/reset/download 或自动重试。命令返回 COMPLETE，成功 stage 40 次、Power Enable 尝试/成功各 1 次，TIM6 guard 计数增加 39；单次 key 已清零。两次结束后回读均为 OBSERVE、MOE=0、六路 MCU 输入 LOW，TIM6 CEN/UIE 已关闭，采样与 SysTick 持续推进，current invalid=0、保护有效且无故障；后次回读 stage/enable 计数未继续增加。以上为稳定结果字段与结束后寄存器证据，整个运行快照不具备同一时刻一致性；没有据此推定实际 Gate 脉冲宽度、死区或硬件关断延迟。已要求用户关闭 OUTPUT 并保留 SINGLE 波形，LA 真实波形照片待核对。

用户随后说明第一次示波器触发阈值未设妥，明确要求再执行一次。再次只读核对供电、镜像、前次事务及保护后，仅补发一条相同参数的 PWM_BURST（sequence=2、2000 us、三相共同 duty=0.5）。本次 enable 尝试/成功各增加 1（累计各 2）、stage 各增加 40（累计各 80）、guard IRQ 增加 39（累计 78），返回 COMPLETE；结束后两次回读均为 MOE=0、六路输入 LOW、TIM6 停止、key 清零，采样继续且 invalid=0、保护无故障，未再次重放。此为用户显式要求的第二次独立单次命令，未开启自动重试；示波器照片仍待核对。

收到第二次命令后的 LA 对 GND 整体波形照片：DS100 为 STOP、X10、5 V/div、500 us/div，显示 20.0 kHz、Vp-p=12.5 V，可见有明确开始和结束的有限脉冲包。照片中实际耦合仍为 AC，因此只作为交流幅度、重复频率及有限脉冲包的初步证据；未用光标量出包宽，未据此确认 Gate 绝对高低电平、占空比、精确关断延迟或死区。没有把改变暂停状态的耦合菜单当作恢复原始直流数据；后续直流电平检查需要 DC 重新采集。本次仅保存和分析照片，未新增 PWM 命令，电源及 DRV 的本次人工反馈待补。

随后收到 STOP 局部图，时基为 10 us/div、X10 / 5 V/div，频率仍为 20.0 kHz、Vp-p=12.5 V；可见周期约 50 us，高电平宽度视觉约 25 us，与共同 50% duty 相符，尚未用光标或 Duty 测量值定量验收。用户确认 DC 是保持 STOP 放大旧记录时才改，故仍按原 AC 采集记录处理，不能据此确认 Gate 绝对高低电平；并报告电源始终无异常，未单独明确 DRV 温升。此轮未新增 PWM，已准备相同 2 ms / 50% 的一次性工具，待 DC 实际采集就绪后再提交；绝对电平、精确时长和死区检查仍待完成。

用户随后确认 DC 单次采集已就绪、DRV 无异常发热。OUTPUT 正常关/开后，先重新核对镜像与健康默认关断；本次启动的动作/命令计数及 result/stop 均为 0，按显式允许的正常断电重启基线处理，没有执行软件 reset 或 rearm。第三次独立单次请求仍为 2000 us / 三相共同 duty=0.5，新启动内 sequence=1；返回 COMPLETE，stage 成功 40、enable 尝试/成功各 1、guard IRQ 39。结束后两次回读 MOE=0、六路输入 LOW、TIM6 停止、key 清零，采样继续、invalid=0、保护无故障且无重放；这些计数属于本次新启动，不能与之前启动的 sequence=1 混用。DC 波形及绝对电平待照片核对。

收到第三次独立请求后的新 DC 照片：STOP、X10、5 V/div、20 us/div，显示 20.0 kHz、Vmax=12.2 V、Vmin=-12.3 mV。LA 对 GND 的直流摆幅符合本轮约 12 V、无电机条件下的预期，但不作为带负载 VGS、精细过冲或死区验收。该窗口包含较长启动前平线、首个窄脉冲和后续脉冲，+Duty 显示约 33.0%；不能忽略该读数，也不先判定固件实际占空比为 33%。计划仅在 STOP 记录中移动窗口至连续周期，比较 Duty 与正脉宽/周期；首个瞬态与占空比的定量结论仍待核对，本轮未新增 PWM。

保持 STOP 将窗口移到连续周期后，用户回报正脉宽 24.8 us；新照片明确为 -Duty=50.4%（并非负脉宽 50.4 us），同时仍显示 20.0 kHz、Vmax=12.2 V、Vmin=-12.3 mV。由周期 50 us 和正脉宽计算的正占空比为 49.6%，与负占空比互补。此前混有启动前平线和首个窄脉冲窗口的 +Duty≈33% 不作为稳定 PWM 占空比结论。LA 对 GND 在本轮无电机、约 12 V 条件下的基本电平、频率、稳定占空比初筛通过；尚未据此量出实际死区、精确截止延迟、首个瞬态或带负载 VGS，LB/LC 实测及其他能力继续分步核对。本轮仅观察旧记录，没有新增 PWM。

用户确认 LB 测量就绪后，按同一流程重新核对正常 OUTPUT 关/开后的健康启动、镜像、采样、母线和保护，仅提交第四次独立 2000 us / 三相共同 50% 请求。本次新启动内 sequence=1、stage 成功 40、enable 尝试/成功各 1、guard IRQ 39，返回 COMPLETE；结束后两次回读 MOE=0、六路 LOW、TIM6 停止、key 清零，采样继续、invalid=0、保护无故障、计数未重复增长。没有软件 reset/rearm 或自动重发，LB 的 DC 照片待核对。

LB 捕获后，用户表示已会判读并人工复核与 LA 结果一致。按用户报告将 LB 无电机基本电平、频率、稳定占空比初筛记录为通过；没有新增 LB 照片或逐项原始数值，不将 LA 的精确数值复制成 LB 实测值。证据类型为本次命令及关断的 SWD 回读，加上用户示波器人工复核；本轮未再新增 PWM，下一测点为 LC。

用户确认 LC 就绪后，重新核对正常关/开后的健康启动和镜像，只提交第五次独立 2000 us / 三相共同 50% 请求。本启动内 sequence=1，COMPLETE，stage 成功 40、enable 尝试/成功各 1、guard IRQ 39；结束后两次回读 MOE=0、六路 LOW、TIM6 停止、key 清零，采样继续、invalid=0、保护无故障、无重放。用户随后人工复核 LC 也与 LA/LB 一致，按用户报告将 LC 无电机基本电平、频率、稳定占空比初筛记为通过；没有 LC 原始照片或逐项数值，不复制 LA 精确数值。至此 LA/LB/LC 三路低侧基本初筛完成，证据分别为 LA 照片及 LB/LC 人工复核；相端输出、高侧 VGS、死区与带功率 Break 仍需分开验证。

用户确认 MA 相端测量就绪后，重新核对正常关/开后的健康启动、镜像、母线、采样和保护，仅提交第六次独立 2000 us / 三相共同 50% 请求。本启动内 sequence=1，COMPLETE，stage 成功 40、enable 尝试/成功各 1、guard IRQ 39；结束后两次回读 MOE=0、六路 LOW、TIM6 停止、key 清零，采样继续、invalid=0、保护无故障且无重放。该命令已执行完毕；用户中断后询问 MA/MB/MC 为什么均为 12 V，尚未说明是万用表/静态值还是 SINGLE 捕获窗口内全程平线，因此不据此判定相端测试通过或硬件故障。静态悬空电位与测试窗内开关波形必须区分。随后追加只读复核时 ST-Link 可见，但报告 Unable to get core ID / No STM32 target found，未获得新的运行状态；可能的供电/连接状态尚未确认，不用这次失败推断门极或 MCU 故障，也不把此前成功的关断回读当作当前实时确认。未新增脉冲或执行 reset/rearm。

用户进一步明确：只要打开电源，尚未发 PWM，万用表测 MA/MB/MC 对 GND 均约 12 V。因此这是上电静态相端读数，不是第六次请求的 PWM 波形证据，也不能仅用脉冲后的残余电荷解释。上下管均关断时相端没有主动拉低，微小漏电与寄生耦合可能影响高阻万用表读数，但当前尚未确认具体路径，不能直接判定正常或高侧导通。后续先用万用表直流电压档复测 HA–MA、HB–MB、HC–MC 的静态 VGS；此测量不使用示波器地夹连接相端。最新只读连接报告 No debug probe detected，仍未获得新的 MCU 运行状态；未新增 PWM、reset 或 rearm。

用户随后回报 HA–MA、HB–MB、HC–MC 三组万用表静态读数均为 0 V。该报告说明本次静态检查没有高侧栅源驱动电压，结合此前低侧静态为 0 V、限流输入稳定和 DRV 无异常发热，三相对 GND 约 12 V 更符合关断高阻状态下的相端电位；具体偏置路径尚未确认，也不能只用 VGS=0 排除 Drain–Source 故障或代替开关波形验收。本次只读连接仍报告 No debug probe detected，未获得新 MCU 运行状态、未新增 PWM。继续 MA 波形前须恢复 ST-Link，并由用户重新准备 MA 对 GND 的 DC 单次采集；由于初始相端为高电位，优先选择约 5 V 的下降沿触发捕获首次拉低。

用户确认触发阈值变化来自误切换到 CHB，已改回正确通道，并重新报告 MA 已就绪。ST-Link 连接恢复后，重新只读核对同次 AXF/MAP、28588 字节 Flash 与 HEX、正常 OUTPUT 关/开后的健康零计数启动、母线约 12.03 V、采样和保护；前置检查通过。仅提交第七次独立 2000 us / 三相共同 50% 请求，本启动内 sequence=1。返回 COMPLETE，stage 成功 40、enable 尝试/成功各 1、guard IRQ 39；结束后两次回读 MOE=0、六路 LOW、TIM6 停止、key 清零，采样与 SysTick 继续、invalid=0、保护无故障且动作计数未重复增长。未执行 reset/rearm、halt/download 或自动重发。本轮证据目录为 20261015-140421-first-burst-repeat2-execute-c3a75554；已要求关闭 OUTPUT、保留 STOP，MA 相端波形及本轮电源/DRV 人工反馈待核对，未将命令完成等同于相端或死区验收通过。

收到第七次命令后的 MA 照片：STOP、X10、DC、5 V/div、50 us/div，水平偏移 -420 us、CHA 下降沿阈值 5.35 V；显示 Vmax=5.54 V、Vmin=179 mV、频率未测出。可见缓慢下降后接近平线，当前窗口没有可识别的 20 kHz 周期切换，不记为相端通过；曲线形状可能来自电源关闭或相端放电，但触发时间和事件尚未确认，不能据此确定硬件故障。先核对是在命令后还是 OUTPUT 关闭时进入 STOP，并在旧记录中把水平偏移回到 0 us 查看触发附近；不自动发新脉冲。补充采集顺序：确认本次触发已 STOP 后才关闭 OUTPUT；若仍等待触发，手动 STOP 后关闭 OUTPUT，并将该轮记为未捕获。本轮仅保存原图及哈希/读数，电源与 DRV 人工反馈仍待补。

用户明确要求重测，随后确认“MA 等待触发”。先前只读前置检查通过，执行前再次核对同次 AXF/MAP、Flash/HEX、正常断电后的健康零计数启动、约 12.04 V 母线、关断、采样与保护后，仅提交第八次独立 2000 us / 三相共同 50% 请求。本启动内 sequence=1，返回 COMPLETE，stage 成功 40、enable 尝试/成功各 1、guard IRQ 39；结束后两次回读均 MOE=0、六路 LOW、TIM6 停止、key 清零，采样与 SysTick 继续、invalid=0、保护无故障且无重放。证据目录为 20261015-141105-first-burst-repeat2-execute-0c4617da。已明确要求先观察 SINGLE 是否由本次命令触发并 STOP，再关闭 OUTPUT；若仍等待，先手动 STOP 再关闭 OUTPUT，报告未触发。新波形及本轮电源/DRV 人工反馈待核对，未自动重发或执行 reset/rearm。

收到第八次命令后的 MA 新照片：STOP、X10、DC、5 V/div、10 us/div，水平偏移 -36 us，CHA 下降沿阈值 8.92 V。显示频率 20.0 kHz、正脉宽 25.0 us，对应正占空比约 50%；Vmax=12.6 V、Vmin=-1.35 V，低平台视觉接近 GND，下降沿可见窄负向突出。MA 的周期切换、频率和稳定占空比初筛通过，证明本轮相端能够被拉低，不把上电静态约 12 V 直接等同于卡高故障。但 -1.35 V 仅是当前采集窗口的自动极值，尚未确认尖峰宽度、实际电路波形与探头接地造成的分量，也不能当作整个低平台电压或直接忽略。下一步保持 STOP 放大旧记录的下降沿至约 500 ns/div，并确认地夹接法；如需比较接地影响，另待准备就绪后才补单次采集。原图及哈希/读数保存在本次目录的 scope-ma-dc-pwm.jpg/.json；未新增 PWM，电源/DRV 人工反馈仍待补，MB/MC 相端与高侧 VGS/死区仍未验收。

随后收到 MA 下降沿的 STOP 放大图，实际时基为 50 ns/div（不是此前建议的 500 ns/div）、X10、DC、5 V/div，水平偏移 480 ns；显示 Vmax=12.9 V、Vmin=-1.16 V。负向凹陷集中在下降沿附近，随后平台视觉回到 0 V 附近，未见持续负平台；不据照片精确给出尖峰持续时间。该图按旧记录放大处理，当前窗口的自动极值与上一窗口不同，不能当作重新采集后尖峰减小的证据，也不因缩小时基提高原始带宽或采样分辨率。用户明确地夹使用鳄鱼夹，并报告电源和 DRV 无异常；该人工反馈补齐本轮观测。MA 周期功能初筛保持通过，实际负向瞬态与接地测量分量仍未分离，下一步先确认是否有配套短弹簧地，以安排同一 MA 测点的短接地对照；未新增 PWM，MB/MC 相端仍待测。

用户确认没有配套短弹簧地。先沿用现有普通探头完成 MB/MC 同类基本周期波形检查，MA 瞬态峰值保留为待短接地复核项；没有把缺少配件转化为峰值合格结论，也未自动改为自制接地或新增脉冲。下一测点为 MB，对 GND，恢复 X10/DC、5 V/div、10 us/div、水平偏移 0 us、CHA 下降沿约 5 V、SINGLE；换点先关 OUTPUT，上电稳定后重新等待触发，收到就绪再做本次实时前置核对。

用户确认“MB 等待触发”后，重新只读核对镜像、正常断电后的健康零计数启动、约 11.96 V 母线、采样、保护与关断，随后仅提交第九次独立 2000 us / 三相共同 50% 请求。本启动内 sequence=1，COMPLETE，stage 成功 41、enable 尝试/成功各 1、guard IRQ 40；结束后两次回读均 MOE=0、六路 LOW、TIM6 停止、key 清零、invalid=0、保护无故障、采样继续且无重放。stage/guard 按实际计数记录，不将此前各轮的 40/39 固定套用，也不据计数精确推定实际 Gate 脉冲包宽。证据目录为 20261015-142109-first-burst-repeat2-execute-c89d658c。

用户随后报告未捕获并明确要求再来一次。基于第九次完成证据重新核对同一启动中的计数、镜像、约 12.02 V 母线和健康关断，仅补发第十次独立同参数请求，本启动内 sequence=2；COMPLETE，stage 成功增加 40、enable 尝试/成功各增加 1、guard IRQ 增加 39（累计 stage=81、enable=2、guard=79）。结束后两次回读关断、保护、采样与无重放检查通过。证据目录为 20261015-142240-first-burst-repeat2-execute-8cdf424f；已提醒先确认 SINGLE 已 STOP 再关 OUTPUT，未捕获则先手动 STOP 再关。MB 波形及本轮电源/DRV 人工反馈待核对；本次为用户明确要求的独立单次补发，没有自动重试、reset 或 rearm。

第十次请求后用户明确人工复核 MB 波形“与 MA 一致”。据此将 MB 相端基本周期切换、频率与稳定占空比初筛记为通过，证据为本轮命令/关断回读及用户报告；没有 MB 照片或逐项原始值，不复制 MA 的精确 Vmax/Vmin、正脉宽或尖峰持续时间。该报告不作为实际瞬态峰值、高侧 VGS 或死区验收，也没有单独补充本轮电源/DRV 反馈。本轮仅记录结果，没有新增 PWM；下一测点为 MC，对 GND，沿用原供电与单次采集设置，收到就绪后再重新核对实时状态。

用户确认“MC 等待触发”后，重新只读核对同次 AXF/MAP 与 Flash/HEX、前次完成计数、约 11.95 V 母线、关断、有效采样和健康保护；执行前再次核对，通过后仅提交第十一次独立 2000 us / 三相共同 50% 请求。同一启动内 sequence=3，COMPLETE，stage 成功增加 40、enable 尝试/成功各增加 1、guard IRQ 增加 39（累计 stage=121、enable=3、guard=118）。结束后两次回读均 MOE=0、六路 LOW、TIM6 停止、key 清零，采样与 SysTick 继续、invalid=0、保护无故障且无重放。证据目录为 20261015-142506-first-burst-repeat3-execute-978652dc；已提醒先确认 SINGLE 已 STOP 再关 OUTPUT，未触发则先手动 STOP 再关。本轮没有 reset/rearm 或自动重发，MC 波形和电源/DRV 人工反馈待核对。

第十一次请求后用户明确回报 MC 波形“一致 无异常”。据此将 MC 相端基本周期切换、频率与稳定占空比初筛记为通过，并记录本轮电源/DRV 无异常；没有 MC 照片或逐项读数，不复制 MA 的精确极值和脉宽。至此，12 V / 50 mA 限流、无电机条件下，LA/LB/LC 低侧基本波形及 MA/MB/MC 三相基本输出检查完成，原低阻和异常发热未复现，最初故障的具体原因仍未确定。MA 的负向瞬态峰值保留待短接地复核；三相同为 50% 的单独采集也不证明不同 duty、相间时序或带负载行为。本轮仅记录结果，未新增 PWM。下一步使用两支普通探头分别采 HA/GND 与 MA/GND（两地夹都接 GND），在同一采集中比较稳定平台的 HA−MA，先做 A 相高侧驱动电平粗查；不将双通道平台比较当作精细动态 VGS/死区验收。

准备高侧双通道时，用户反馈两手持探头无法同时按键/回复，下降沿低阈值又会因接触抖动提前触发。操作流程因此改为 HA 所在 CHA 上升沿约 18 V（本轮静态约 12 V、预期导通为 20 多伏），并在用户拿起探头前的一次“开始”之后安排明确的 15 s 可中断倒计时。倒计时结束仍重新做原实时前置核对，仅发一条固定请求；不在后台预先排队，不以倒计时替代固件截止。地夹先固定，捕获后再放下探头查看与回报；后续长期重复采集优先固定短引线。此轮只调整流程和本地操作计划，没有新增硬件动作、PWM 请求或固件改动，高侧平台检查仍未执行。

用户回复“开始”后，最初只读前置检查识别到 ST-Link，但读取 MCU 报 Unable to get core ID / No STM32 target found；该次在 Flash 身份核对前失败，power_submission_attempted=false，未启动倒计时或发脉冲。提示确认 OUTPUT/SWD 后，仅做 HOTPLUG 只读检查即恢复连接；未使用 reset、halt 或 rearm，不据此确定先前掉线原因。重新完整核对镜像、健康零计数启动、约 12.03 V 母线、采样、保护及关断通过后，明确宣布并完成 15.0104 s 可中断等待；等待期间没有收到停止或接线变更。

倒计时结束重新做原实时前置核对，仅提交第十二次独立 2000 us / 三相共同 50% 请求，供 CHA=HA/GND、CHB=MA/GND 的高侧 A 平台采集。本启动内 sequence=1，COMPLETE，stage 成功 40、enable 尝试/成功各 1、guard IRQ 39；结束后两次回读均 MOE=0、六路 LOW、TIM6 停止、key 清零，采样与 SysTick 继续、invalid=0、保护无故障且无重放。证据目录为 20261015-145416-first-burst-repeat4-execute-3cb09f5c。已立即告知放下探头，随后确认 STOP 再关 OUTPUT；若未捕获则手动 STOP 后关。高侧 A 双通道图及本轮电源/DRV 人工反馈待核对；未自动重发，不把命令完成当作实际 VGS 电平验收通过。

收到第十二次请求后的 HA/MA 双通道照片：STOP、20 us/div、水平偏移约 -52 us，CHA 为 X10 / DC / 10 V/div、垂直偏移 0 V；CHB 顶部实际为 X10 / 5 V/div，并非先前建议的 10 V/div。触发为 CHA 上升沿 18.0 V。CHA 显示 20.0 kHz、Vmax=22.9 V、Vmin=-339 mV，正脉宽照片约 25 us；这些极值属于 HA/GND，不作为 MA 峰值。两路低平台视觉接近同一零参考，同一时刻高平台按各自刻度读图约为 HA/GND 22 V、MA/GND 12 V，对应高侧导通 VGS 粗估约 10～11 V，关断平台差接近 0 V。不是将两个通道在不同时刻取得的自动 Vmax 相减。不同刻度使两条高平台视觉接近，不代表电压相等；当前图足以支持 A 相高侧稳定平台电平粗查通过，不要求仅为刻度不同重测。实际瞬态峰值、通道匹配误差和精确死区仍未资格化，本轮电源/DRV 未单独补充人工反馈。原图及哈希/读图元数据保存为 scope-highside-a-ha-ma.jpg/.json；本轮没有新增 PWM。下一步保留当前可用显示设置，改测 CHA=HB/GND、CHB=MB/GND，仍由一次“开始”后的可中断 15 s 倒计时协调。

用户将后续准备时间调整为 8 s，并回复“开始”进行高侧 B 检查。首次只读前置检查再次报 MCU core ID 不可用，power_submission_attempted=false，未倒计时或发脉冲；HOTPLUG 只读重查即恢复，原因未确定。重新核对镜像、健康零计数启动、约 11.99 V 母线、采样、关断与保护后，明确宣布并完成 8.0146 s 可中断等待，随后再次核对并仅提交第十三次独立 2000 us / 三相共同 50% 请求，供 CHA=HB/GND、CHB=MB/GND 采集。本启动内 sequence=1，COMPLETE，stage 成功 40、enable 尝试/成功各 1、guard IRQ 39；结束后两次回读 MOE=0、六路 LOW、TIM6 停止、key 清零、invalid=0、保护无故障，采样继续且无重放。证据目录为 20261015-150040-first-burst-repeat2-execute-a624e5b1；未 reset/rearm 或自动重发。

用户随后报告高侧 B“结果和 A 一致”，并要求直接准备 C。按人工复核记录 B 相稳定平台粗查通过，不复制 A 相的精确读数、照片或瞬态峰值；本轮电源/DRV 未单独补充报告。下一步只将 CHA 尖端由 HB 改到 HC、CHB 由 MB 改到 MC，两地夹仍接 GND；保留当前可判读的显示设置与 8 s 倒计时，收到一次“开始”后才进入只读检查和后续单次采集流程。本次仅记录结果、准备操作计划，没有新增脉冲。

用户回复“开始”后进行高侧 C 检查。先通过 HOTPLUG 只读连接确认，再完整核对镜像、前次完成计数、约 12.05 V 母线、关断、有效采样和保护；明确宣布并完成 8.0149 s 可中断等待，等待期间无停止或就绪条件变更。随后重新核对，只提交第十四次独立 2000 us / 三相共同 50% 请求，供 CHA=HC/GND、CHB=MC/GND 采集。同一启动内 sequence=2，COMPLETE，stage 成功增加 40、enable 尝试/成功各增加 1、guard IRQ 增加 39（累计 80/2/78）。结束后两次回读均 MOE=0、六路 LOW、TIM6 停止、key 清零，采样与 SysTick 继续、invalid=0、保护无故障且无重放。证据目录为 20261015-150321-first-burst-repeat2-execute-ee32f007；已立即告知放下探头，确认 STOP 后关 OUTPUT。高侧 C 波形/人工复核及本轮电源/DRV 反馈待补；没有 reset/rearm、halt/download 或自动重发。

收到第十四次请求后的 HC/MC 双通道图，用户报告电源/DRV“无异常”。STOP、20 us/div、水平偏移 -52.5 us，本图 CHA/CHB 顶部均为 X10 / 10 V/div（CHB 实际已由先前的 5 V/div 改为 10 V/div），CHA 为 DC、垂直偏移 0 V，触发 CHA 上升沿 18.0 V。CHA 显示 20.0 kHz、正脉宽 25.0 us、Vmax=22.9 V、Vmin=-339 mV；极值属于 HC/GND，不作 MC 的精确测量。两路稳定高平台相差约一大格，按两路各 10 V/div 粗估高侧导通 VGS 约 10 V，关断平台差接近 0 V，C 相高侧稳定平台粗查通过。原图及哈希/元数据存为 scope-highside-c-hc-mc.jpg/.json；本次仅记录图片和结论，没有新增 PWM。

本轮 12 V / 50 mA 限流、无电机条件下，修复后的供电、六路静态 VGS、三路低侧基本波形、三相输出基本波形及三路高侧稳定平台粗查均完成；原 B/C 低阻与 DRV 异常发热未复现。最初故障可能的芯片损伤与拆焊消除连锡等原因仍无法区分，实际瞬态峰值、精确死区及真实带功率保护/负载能力保持未验收。本轮基础故障初筛结束，后续属于功率级能力验收；OUTPUT 保持关闭，先整理长期结论与过程记录，没有新的带电动作排队。

以上包括 MCU 启动、无功率观测、修复后的限流静态关断、十四次独立短脉冲命令回读、LA/MA/高侧 A/C 平台照片、LB/LC、MB/MC 与高侧 B 的人工复核初筛证据，不代表完整功率级验收；以下能力仍未完成实机验证：

```text
Nonidentical-duty / interphase timing / loaded PWM waveform
Dynamic Gate VGS / dead-time
Current-sense offset / gain
Powered OCP / OVP thresholds and TIM1 Break shutdown
MT6701 physical timing / position accuracy qualification
UART / USB / FDCAN
Motor operation
FOC
```

后续验证必须按实际修改影响边界推进，不因为 Target 已能下载就提前宣称功率级或控制链路通过。

