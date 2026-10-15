# BLDC_G431 首板修复与实物调试记录

记录范围：首板 Bring-up、DRV8300 修复，以及截至 2026-10-15 的第 14 次独立短脉冲请求。
本文件保存过程与证据边界；历史段落中的“待测”按当时状态保留，
当前汇总以 [台架验证状态](VALIDATION.md) 和 [硬件基线](../../../hardware/BLDC_G431/HARDWARE_BASELINE.md) 为准。

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

