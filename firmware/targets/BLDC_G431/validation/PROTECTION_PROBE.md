# G431 受控保护注入

以下保留旧镜像的方法与实物结果。当前 20 / 10 / 1 kHz 候选保留四个采样回调的
注入等待计数，其名义等待改为 400 µs；总事务截止仍为 2000 µs。
新共享时序的构建或模型通过不替代本文旧波形之外的新实测。

当前阶段（2026-10-17）：候选 Flash 身份已核对，仅 ST-Link 3V3 阶段逻辑检查通过，
12 V 静态与只读预检通过，OVP 寄存器证据、第 16 次 LA 包结束照片及第 17 次四步恢复通过。
第 18 次 C 相 OCP 的 COMP1/BRK2 寄存器证据、LA 包结束照片及同一连续运行中的四步恢复通过。
第 19 次共享 A/B 的 COMP2/4/BRK2 寄存器证据、用户人工波形复核及同轮四步恢复通过。
三种来源本轮注入检查完成；最终保护健康、输出关闭，未自动重启。
仅用于 12 V / 50 mA、无电池、无电机的首板台架。
原有 14 次实物请求属于旧镜像；第 15/16/17 次为三次独立 OVP，恢复证据仅来自第 17 次连续运行；
第 18 次为 C 相、第 19 次为共享 A/B OCP，后两次波形与恢复均在各自连续运行中确认。

## 实现与边界

target_validation.c 仍拥有单次动作、fresh staging、一次使能和 DWT/TIM6 截止。
protection_probe.c 是 Target 私有测试钩子，仅改变一个现有 DAC 阈值，不修改
Platform 的硬件保护实现、比较器路由或 BKE/BK2E。正常启动没有注入和自动 PWM。

| 命令 | 阈值 | 预期比较器输出位图 | 预期标志 |
| --- | --- | --- | --- |
| 5 BREAK_OVP | DAC1_CH1 → 0 | COMP3，0x4 | BIF，0x80，BRK1 |
| 6 BREAK_OCP_C | DAC3_CH1 → 0 | COMP1，0x1 | B2IF，0x100，BRK2 |
| 7 BREAK_OCP_AB | DAC3_CH2 → 0 | COMP2 与 COMP4，0xA | B2IF，0x100，BRK2 |

三个命令均固定 common_duty=0.5、duration_us=2000，逐次提供 power_key。
沿用健康保护快照、有效 B/C 采集、10～14 V 新鲜母线及两次 fresh staging 的前置条件。
使能后再经过四个 ADC 回调（nominal 200 us）才进行一次注入；总截止仍从接收请求计算。
如果在此之前电流/母线/保护条件失效、stage 失败或收到 SAFE_OFF，就取消动作而不写 DAC。
AB 阈值是共享的，观察到两个比较器翻转加 BRK2 不能证明每条 OR 输入单独连通。

注入前再次检查比较器输入配置、DAC 工作模式、Break 路由、BIE、BKE/BK2E、AOE 关闭、
无旧 Break 标志、全部比较器低、MOE=1，以及阈值未被外部改动。
短暂屏蔽可屏蔽中断后写 DAC=0；在**任何软件 safe-off 之前**记录比较器、TIM1 SR 和 BDTR。
仅当预期比较器全部为高、没有其他比较器为高、预期 Break 标志成立且 MOE=0，
才记为 OBSERVED。随后在恢复中断前无条件 safe-off，硬件不响应也不能继续输出。

轮询有 1700 个 DWT 周期（10 us）和 128 次两道上限；10 us 只限制等待段，
不代表整个临界区或 Gate 关断延迟。实际 consumer/ISR 用时需新镜像上板复核。
DAC 电气时间参考 [STM32G431 数据手册](https://www.st.com/resource/en/datasheet/stm32g431cb.pdf)，
此窗口是台架观察预算，不是保护性能承诺。

## 证据与恢复

g_target_validation_probe 为独立 68 字节序号快照；读取前后 sequence 必须是同一偶数。
请求接收后发布 PREPARED，执行后发布 OBSERVED / REJECTED / NOT_OBSERVED。
必须核对 request_sequence 与本次实际动作相同，不能使用上次残留的成功记录。

核对 bdtr_before 的 MOE=1、comp_before=0、sr_before 无 BIF/B2IF；
再核对 bdtr_after 的 MOE=0、comp_after 与 expected_comp_mask、sr_after 与 expected_break_flag。
这是屏蔽软件关断期间的寄存器证据；还要在示波器上确认实际 Gate 包停止。
elapsed_cycles 是从阈值写入前到本次轮询取样的观察区间，包含 DAC 建立、读取和指令时间，
不能当作 comparator→Gate 传播延迟，也不能资格化 4 A / 17 V 的实际触发精度。

若关断波形已验收、但断电导致恢复证据中断，可以另开一次同参数请求专门补恢复。
主机工具的 --recovery-scope-reference 只接受同 HEX、同注入来源、寄存器证据通过且原图
哈希匹配的完整包结束记录；仅可用于四条不使能输出的恢复命令，不能给功率请求使用。
新请求仍必须取得自己软件 safe-off 前的硬件关断证据，每一步恢复仍核对当前请求与 capture。
此模式单列旧波形来源，新请求的 scope_qualified 保持 false，不合并两个启动的恢复状态。
没有引用时仍要求当前请求自身的波形确认；引用在四步之间也不得改变。

注入后（包括未观察到预期 Break 的情况）保持阈值为 0，held=1。
顺序为：

1. 先确认 MOE=0、六路 MCU 输入 LOW、TIM6 已关、运行计数继续。
2. 保持注入，单独发送 REARM：预期返回 REARM_REJECTED，物理条件和锁存仍在。
3. 发送 CLEAR_INJECTION：只恢复保存的原阈值；不清 Break/sticky cause，不解锁。
4. 条件回到正常后，用无功率 STAGE_ONLY 请求核对锁存仍拒绝新动作；单独 REARM 成功才退休旧 episode。
5. rearm 后仍保持输出关闭，不自动重启。下一次 PWM 必须是新的明确请求和一次性 key。

CLEAR/REARM 在动作中返回 BUSY；SAFE_OFF 随时可以取消且不会暗中清除注入。
CLEAR 发现阈值被外部改为非零时拒绝覆盖。复位会按正常启动重新配置名义阈值，
丢弃旧命令且保持功率关闭；不能拿复位代替本轮“故障持续时拒绝恢复”的验收。

## 单次实物验证流程

ST-Link 供电阶段已完成：Flash 本来已匹配候选，本次仅备份和回读，未重复烧录。
首次运行状态未通过检查，OUTPUT 关闭时软件复位后正常；初始原因未确定。
复位后 OBSERVE / MOE=0 / 计数正常；stage-only 和 4096 点采集通过，命令 3/5/6/7
在 key=0 时全部返回 LOCKED，enable_attempts/successes=0，probe 未准备/未注入、阈值未改变。
母线 ADC 在 ST-Link 3V3 阶段约 2.65 V，与此前同供电记录一致；没有万用表对照，
不据此确定真实 VBUS 电压或反灌电流。原始证据及本次限制见
[会话记录](local-evidence/20261017-151739-probe-flash-logic/session-summary.json)。
新版读取布局和字段宽度必须来自同次 AXF/MAP，不能沿用旧地址或旧控制脚本。

12 V 阶段用户报告供电无异常。本次未再复位，直接观察到健康的新启动状态，
三次初读及后续严格只读检查通过：母线观测约 11.96～12.04 V、采样正常、Trip 健康，
六路 LOW / MOE=0、stage/enable/命令序号均为 0。精确输入电流和 CV/CC 未另报，不代填。
首次连接的初始状态原因仍未查明，本轮独立上电正常不等于已确定其原因。
冻结的主机工具、59 项主机检查和只读记录见
[12 V 静态证据](local-evidence/20261017-153714-powered-static/qualified/summary.json)。

下一步接探头时先关 OUTPUT，保持 ST-Link 3V3 断开，保留 SWD/GND；电源维持 12 V / 50 mA。
先只接 CHA：探头尖端 LA、地夹 GND，实体与菜单 X10，DC，5 V/div，100 us/div，
CHA 上升沿约 5 V，单次触发。Gate 波形应为少量 20 kHz 周期后结束；
这组设置用于观察整个包，不用于测尖峰、死区或精确关断延迟。
CHB 关闭。接线和设置完成后再打开 OUTPUT，示波器进入单次等待触发。

操作员准备好后回复“开始”；主控先完成只读预检，再宣布 8 秒可中断倒计时，
到时重新检查并只发送一次 BREAK_OVP。结束后立即告知可以放下探头，
再核对故障证据、持续故障拒绝恢复、清除/解锁不自动重启。
本次已完成上述单次请求：sequence=1，stage=5 / enable=1 / guard IRQ=4，
采集前 MOE=1、COMP 全低、无 Break 标志；采集后 COMP3 高、BIF=1、MOE=0，
证据发生在软件 safe-off 前，capture outcome=OBSERVED。当前 DAC1_CH1=0、held=1、
cause=OVP，六路 LOW、TIM6 已关、CPU/采样仍运行，尚未执行恢复。
LA 照片和用户电源/DRV 状态待确认；不能仅凭寄存器结果宣布实际 Gate 波形已验收。
记录见 [OVP 现场](local-evidence/20261017-153714-powered-static/ovp-execute/observation-record.json)。
随后用户要求重测；检查时已观察到新启动和清空的 probe，首次故障的恢复验收未完成，
本主控没有执行复位/清除命令。新启动健康检查后，8 秒倒计时再执行一次 OVP。
两次均为各自启动中的 sequence=1；当前应使用
[人工重测现场](local-evidence/20261017-153714-powered-static/ovp-repeat1-execute/observation-record.json)。
重测结束回读寄存器证据通过、故障保持，不合并两次启动的恢复证据。
随后 LA 的 STOP 照片显示四个完整 20 kHz 高脉冲后回到低电平，末尾可见平线；
显示正脉宽 24.8 us、Vmax 12.2 V，50 us/div、X10 / DC / 5 V/div。
起始处有一条窄边沿残迹；仅确认有限脉冲包结束，不资格化尖峰、包精确时长或 Gate 传播延迟。
[波形判读](local-evidence/20261017-153714-powered-static/ovp-repeat1-execute/scope-la-packet-end.json)
与原始请求分开保存，原始 summary 的 scope_pending 状态不覆盖。
准备恢复时连续只读 HOTPLUG 未能取得 MCU core ID；无 clear/rearm/reset/halt 或新增 PWM。
当前先核对 OUTPUT 与连接状态；恢复前必须重新确认原请求/capture/保持故障未丢失。
用户确认此前关过 OUTPUT、现在重新打开；新启动健康。以本次“继续”指令，8 秒等待后只补
第 17 次 OVP，用先前同镜像/source 的波形记录作显式参考，不要求重新抓图。
本次寄存器证据通过；四步恢复全部通过，命令序号 1～5、active_request=1，stage/enable/guard
始终为 5/1/4。最终阈值 2065/345/345，cause/COMP/Break=0、BIE=1，MOE=0、六路 LOW，
CPU/采样继续，invalid=0。主机工具 74 项检查通过，固件未改动、未复位/重新烧录。
记录见 [恢复现场](local-evidence/20261017-153714-powered-static/ovp-recovery-rearm/observation-record.json)。
用户随后要求 C 相直接开始；新的健康启动经完整只读核对，8 秒倒计时后单发命令 6。
本启动 sequence=1、stage/enable/guard 为 5/1/4；COMP1/B2IF 成立，软件 safe-off 前 MOE 1→0，
elapsed=70 cycles / polls=1，仅为轮询观察值。结束后阈值 2065/0/345、held=1、cause=OCP，
BIE 暂停，六路 LOW/MOE=0、TIM6 已关、采样正常且 invalid=0，没有额外 enable。
当前等待 LA 完整包结束照片和电源/DRV 人工反馈，保持供电不清注入；不得引用 OVP 来源的
波形作为 C 相本轮波形。记录见
[C 相现场](local-evidence/20261017-153714-powered-static/ocp-c-ready-230805-execute/observation-record.json)。
随后收到本次 LA 的完整 STOP 照片：X10 / DC / 5 V/div、100 us/div、水平偏移 0，
显示 20.0 kHz、Vmax 12.2 V、正脉宽 24.8 us、Vmin -204 mV；四个完整高脉冲后保持低电平。
起始有很窄的边沿残迹，图中自动极值不作为实际瞬态峰值资格化。
新鲜只读核对同一请求和 held capture 后，四步恢复全部通过；最终阈值恢复 2065/345/345，
cause/COMP/Break=0、BIE=1，MOE=0、六路 LOW，stage/enable/guard 仍为 5/1/4、invalid=0。
没有新增 PWM、复位或固件改动。本次照片未附电源/DRV 人工状态报告，不代填测量值。
同轮照片与恢复证据见
[C 相恢复现场](local-evidence/20261017-153714-powered-static/ocp-c-recovery-rearm/observation-record.json)。
随后共享 A/B 请求在同一启动预检和 8 秒倒计时后单发，sequence=6、累计 stage/enable/guard
为 10/2/8；COMP2/4 同时高、B2IF 成立，软件 safe-off 前 MOE 1→0，阈值为 2065/345/0。
用户人工复核“和前面的波形一样”，记录为本轮有限 LA 包结束功能确认，不复制 C 相精确数值，
也不假称有本次照片。原请求/capture 保持核对通过后，四步恢复按 sequence 7～10 全部通过。
最终阈值 2065/345/345、cause/COMP/Break=0、BIE=1，MOE=0、六路 LOW，累计 enable=2，
恢复期间未增加 stage/enable/guard。独立结束只读检查通过。
[本轮汇总](local-evidence/20261017-153714-powered-static/protection-stage-summary.json)
保留三个来源的照片/人工报告、跨启动 OVP 波形引用与同轮恢复边界；实际 4 A / 17 V 门限、
独立 A/B OR 路由和精确 Gate 关断延迟仍未资格化。
OVP 通过后才安排 C 和 AB；任一步证据不完整即停在输出关闭，不自动重试。
注入结束后暂时保持供电，先读取保持故障及恢复证据；复位/断电会清除本次运行期
注入状态，不能代替持续故障拒绝恢复的验收。如出现 CC 或异常发热则立即关 OUTPUT。

## 离线结果

2026-10-15 候选构建时，106 个 ARMCC / Unicorn 模型用例通过，包含实际注入模块及缺失 MOE 清除、
缺失标志、错误比较器、AB 少一路、时钟停滞、阈值改动、取消、保持/恢复等反例。
模型 HCC 与 MMIO 效果是替身，不能代替上述实物验收。
Keil 全量 Rebuild：0 Error / 0 Warning。最终 AXF 反汇编核对了
DAC 写入到证据采集之间无软件 MOE 写入，以及调用者在恢复中断前 safe-off。
本机冻结 HEX/AXF/MAP、源码哈希、布局和日志位于
[构建清单](local-evidence/20261015-protection-probe/BUILD_MANIFEST.json)。

以上清单是本机历史记录，不随仓库分发。后续模型新增 30%/70% 两项，当前验证范围及命令见
[台架验证](../VALIDATION.md)，不能把后来的用例数改写到原始构建记录中。
公开原图见[证据索引](evidence/20261018-baseline/README.md)。
