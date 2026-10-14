# F405 M0 台架验证

本流程用于本 Target 的硬件能力验收。默认镜像进入 OBSERVE，保持功率关闭；
PWM_BURST 仅用于电机三相线已经断开的短时波形测量。
台架入口位于 validation/，通过既有 HCC 操作硬件，不是正式 Motor / ControlRuntime。

## 无功率连续电流统计

CAPTURE_CURRENT 只收集 Platform 已发布的 B/C 名义电流，不做零点修正或 A 相重构。
测零点时电机三相断开，沿用第三轮的 12 V / 0.100 A 静态供电，确认 MOE=0；
ST-Link 3V3 供电线断开。等电源稳定后提交，不在上电时自动采集。

调试器先写 command=4、power_key=0，最后写新的非零 sequence。
duration_us 与 common_duty 在本命令中不使用。捕获期间 state=3；
不调用 stage 或 power_enable，不接受其他 stage/功率/采集请求，SAFE_OFF 可以取消。

每次接收 4096 个连续、有效且完整的 B/C 事务，约 204.8 ms。
invalid、缺相、非有限值或时间间隔不符会以 BAD_CURRENT 结束，不把部分窗口当作成功。
ADC 停止时，独立 TIM1 update 截止仍记录 BAD_CURRENT；若 callback 尚未恢复，
快照状态可能暂未更新，应同时读 g_target_validation_stop_reason。

成功后 state=0、result=COMPLETE，current_statistics 保存：

- request_sequence：本窗口所属请求；sample_count 必须等于 4096。
- first_sample_time / last_sample_time：差值应为 4095 × 50 us。
- phase_b / phase_c 的 mean_a、min_a、max_a，单位 A。
- variance_a2：Welford 累积得到的无偏样本方差，单位 A^2；PC 端开平方得到噪声标准差。

完成结果保持到下一次 CAPTURE_CURRENT。运行中整份快照仍遵守序列号契约；
若 SWD 速度不足以取得整个快照，可在确认采集完成后独立读取 current_statistics，
核对请求号、计数并连续读取两次一致结果。另行检查 MOE=0、六路输入 LOW、
stage/enable 计数无增量，以及观测失败计数无增量。

重复几个独立窗口评估均值稳定性。可用第一窗口均值估计 offset，再比较后续窗口
的均值残差；不能用同一窗口减去自己的均值来宣称校准通过。
窗口均值稳定不代表单样本噪声小，也不证明增益、真实负载电流或长时温漂已校准。
callback_max_cycles 只统计本 consumer，不是整个 ADC ISR 的 WCET。

## 已有仪器与电机

用户已接好 MT6701。电机标称参数来自用户提供的
[2804 资料](../../../hardware/BLDC_G431/actuator/image%284%29.png)：
12N14P、7 对极、12 V、额定电流 0.5 A、KV 220、线间电阻 5.1 Ω，
标称相电阻 2.55 Ω、相电感 0.86 mH。最大电流 2 A 只作为资料数值，
不作为当前测试设定；电阻、电感、相序和电角度偏置尚未实测。

当前前端名义灵敏度 20 mV/A，12-bit ADC、3.3 V 名义参考，
约为 40 mA/LSB。0.5 A 对应约 10 mV / 12.4 counts。
进入电流闭环前需要先测零点、噪声和信号幅度；这个量化计算不能替代实际精度验证。
校准修正与缺相重构仍属于后续 portable Measurement。

示波器为正点原子 DS100：双通道、标称 50 MHz，基础套餐配普通 MCX 探头。
最大标称采样率不代表所有时间档位和双通道下都达到该速率。
参考制造商 [DS100 手册](https://supereyes.ru/img/instructions/Manual_Punctual_Atom_DS100_Mini.pdf)；
菜单随固件版本可能不同。

- 两支探头接 CHA / CHB；探头开关与示波器菜单都设为 X10，使用 DC 耦合。
- 两个地夹统一接板子 GND。电池供电不作为通道隔离的证明。
- 独立 MCX 鳄鱼夹线用于 OUT 波形发生器，本轮不用它接模拟测量通道。
- 不把地夹接相线或上桥臂 Source。高侧 VGS 的差分/双通道测量方案另行核对，
  不能用 Gate 对 GND 的单一读数代替 Gate 对 Source。
- 日常测量入口采用下面的预留接口；内部 PWM/Gate 网络不作为首轮必需接线点。
  高侧 VGS 测量原则参考 [Tektronix 探头说明](https://www.tek.com/en/documents/whitepaper/abcs-probes-primer)。

## 预留接口与测量范围

用户没有 PCB 布线源文件，探头接内部器件脚困难。本轮依据
[板子正面照片](../../../hardware/ODRIVE_V36_405/PCB/ODrive照片_单排MOS.png)
和 HARDWARE_BASELINE.md 采用接口测量；照片正面未见标注的 PWM/Gate/SOx
专用测试接口，不能据此断言背面也没有测试点。

| 项目 | 测量入口 | 可以形成的证据 |
| --- | --- | --- |
| 母线电压 | 左侧 DC 的正、负端子；存在压差时改测已确认的 DCBUS/PGND 母线电容 | 输入电压与内部母线分别记录，bus.voltage_v 对照实际 DCBUS |
| 逻辑供电 | 顶部丝印明确的 3V3、5V、GND | 对应电源轨的直流电压 |
| MT6701 通信 | 已接编码器的 SCK、MISO、GPIO1/CS 排针 | 时钟、数据和片选波形；CRC/有效性由固件统计 |
| M0 功率输出 | 照片右下 M0 三相螺丝端子，按实物相位丝印识别 | 各相对 GND 的开关波形、频率与测试结束后的变化 |
| 采样与命令处理 | ST-Link | 采样间隔/失败计数、stage/enable/result、TIM1 与 GPIO 读回 |

端子测量先断电固定探头或短绝缘引线，避免通电后移动夹子。示波器探头尖端
接被测相端，两路地夹都接板上 GND；地夹不能接另一相或 DC 正端。
地参考优先选顶部标注 GND 的排针或已确认的 DC 负端；中间 AUX 属于制动半桥，
不能因为其端子标有负号就把它当作 GND。
当前三相 duty 相同，优先分别测各相对 GND，不能用近零的相间电压判断没有 PWM。

相端电压是桥臂输出，不是 Gate 对 Source 电压。断开电机、safe-off 后相端可能
悬空并保留电压，因此不要求它立即归零；停止周期切换也不能单独证明六颗 MOS
都已关断。精确死区、逐管 VGS 和最终 Gate 关断仍属于独立未验证项，不能由
MOE/GPIO 读回或相端波形代替。

若以后需要可见的采样/控制事件，可在确认空闲的 GPIO 排针上增加诊断标记；
软件标记只表示相应执行事件，不视为内部六路 PWM 的复制或真实 Gate 证据。

## 镜像与软件证据

正式工程：MDK-ARM/ODRIVE_V36_405_M0.uvprojx。
普通 Rebuild / Download 沿用 DEVELOPMENT.md 的用户操作分工。
复杂命令、内存读取和结果分析由 Codex 处理；同一时刻只有一个工具占用 ST-Link。

CubeCLT 可以读取 Keil AXF 的符号与类型。接入前先核对板上 Flash 与对应 HEX，
地址和结构布局从这份 AXF 取得，不沿用旧镜像的地址。
GDB 的 attach 可能暂停 CPU；只在功率关闭、允许破坏采集连续性时用于断点、
寄存器和执行位置诊断。连续采集使用 STM32_Programmer_CLI 的 SWD HOTPLUG
内存读写，不能混入 halt、reset、download；用 DHCSR 的 S_HALT、采样计数增量
和失败计数核对读取期间仍在运行。烧录后还需确认已复位并 Run，不能把 SRAM
中烧录程序的执行状态当成应用已经启动。

main 使用 WFI 时，运行中 SWD 调试需按 RM0090 配置 DBGMCU_CR 的 DBG_SLEEP，
保持 Sleep 下调试所需的 HCLK。先保存原值，仅置 DBG_SLEEP，不置定时器冻结位，
核对 S_HALT=0、采样仍连续并重复核对 Flash。结束调试后恢复原值；这个设置可能
改变待机功耗，电源电流记录需区分配置前后。未配置时的大块读回不一致，不能
直接判为 Flash 被改写。

编译/链接检查必须确认：

- 全部正式工程成员参与构建，0 Error / 0 Warning。
- MAP 中 stage_phase_duty / power_enable / safe_off / target_validation_update_irq
  具有最终代码地址，不能只在被移除的 section 列表中出现。
- g_target_validation、mailbox 和 DMA 缓冲区位于可用 SRAM；不放入 CCM。
- IRQ 向量进入本 Target 的真实 handler，三项公共 HCC 保持不变。
- main / Core 生成区只在 USER CODE 内接入，CubeMX 重生成保留验证入口与工程成员。

静态检查入口（从仓库根目录运行）：

    python firmware/targets/ODRIVE_V36_405_M0/verify_target.py

命令防护的 CPU 模型回归使用实际 ARMCC 编译的 validation 源码与 fake HCC/time。
该检查只验证 mailbox、边界检查、单次使能和截止行为，不模拟 ADC/TIM/SPI 电气行为，
不等价于实物 L1 / L2。
回归入口为 validation/tests/verify_model.py，依赖 ARM Compiler 5、
Python 的 unicorn / pyelftools。默认查找 D:/keil5/ARM/ARMCC/bin，
其他安装位置可设置 KEIL_ARMCC_BIN。测试构建只使用临时目录，不改正式 Objects：

    python firmware/targets/ODRIVE_V36_405_M0/validation/tests/verify_model.py

## 第一轮：只观测

1. 断开电机的三相功率线，保留 MT6701。先完成正式 Rebuild。
2. 按已经核对的板级接法供电和烧录，当前不提交任何台架动作。
   若只有逻辑域供电，不能据此验证未供电模拟前端和 Gate Driver。
3. 保持 MCU 连续运行，查看 g_target_validation：
   started=1、state=0，tim1_cr1 的 CEN=1，tim1_bdtr 的 MOE=0。
4. fast_tick_count 应持续增长；正常连续采集间隔目标为 50 us。
   min 在至少两次有效观测前为 UINT32_MAX，不把它当成已测结果。
5. 慢转磁铁，观察 position_count、position 和 invalid_position_count。
   位置范围为 [0,1) turn，跨零不能被误当成速度或累计圈数。
6. 比较 bus.voltage_v 和万用表母线读数；记录电压、零电流读数与失败计数。
   功率关闭时应形成 B/C 独立观测，平台不重构 A，也不做零点校准。

先保存两次间隔明确的观测读数，再判断增量、间隔和失败原因。
调试器连续读取时，快照前后的 sequence 必须相同且为偶数。
整份结构的 SWD 读取可能跨过多个更新周期；若无法满足这个条件，只把独立
32-bit 计数、单个 float 或寄存器当成分别取得的读数，不组合成同一时刻的观测。
需要比较多个字段的同一采集时刻时，应使用经序列号核对的快照或另设板上锁存。
采集期间暂停 MCU 会破坏时序证据，应单独识别该段数据。
首次数值异常先定位，不直接进入 PWM_BURST。

## 第二轮：stage-only

mailbox 只由操作者提交，先写 payload、最后写 sequence；等 command_sequence
确认消费后，才提交下一条。写同一个 sequence 不会重复执行。

stage-only 不执行 power_enable。仅逻辑域供电、MOE=0 时可以先检查本轮；
若功率域已经供电，先断开电机三相线。后续接入母线电源和 PWM_BURST 前
仍必须确认三相线已经断开。

第一条非功率动作：

    g_target_validation_request.command = 2
    g_target_validation_request.power_key = 0
    g_target_validation_request.duration_us = 2000
    g_target_validation_request.common_duty = 0.5
    g_target_validation_request.sequence = 1     // 最后写

预期：stage_successes 增加 2，enable_attempts 不增加，result=1，state 回到 0，
MOE 保持 0，CEN 与 fast_tick_count 继续运行。
stage-only 两个采样回调后结束；duration 是故障情况下的总截止上限。
重复读同一 mailbox 不增加新的动作。失败时记录 result 和最小关键快照。

## 第三轮：12 V 限流供电与静态观测

这一轮先不上 PWM，沿用已有台架镜像。0.10 A 是输入电流的保守测试起点，
不是该板已经测得的待机需求，也不是电机相电流限额。

1. 电源 OUTPUT 关闭，暂时拔下 ST-Link USB，板子断电。
   断开电机三相线，保留 MT6701。
2. 拔掉之前由 ST-Link 给板子供电的 3V3 线，保留 SWDIO、SWCLK、GND
   及已经接好的 NRST。由板上电源树提供逻辑电源。
3. UTP1306-II 设置 12.00 V、CURRENT 限流 0.100 A；可将 OVP 设为 13.0 V。
   电源红色 + 接板子左侧 DC 正端，黑色 - 接 DC 负端。
   电源绿色 GND 是接地端，不替代黑色输出负端，本轮无需增加接线。
4. 核对极性和固定接线后插回 ST-Link USB，再开启 OUTPUT，先观察 2～3 s。短暂 CC 可能来自
   母线电容充电；超过 2 s 仍持续 CC、输出达不到 12 V 或反复启动时关闭 OUTPUT，
   回报实际电压/电流及状态，先分析再决定是否调整限流。
   OVP/OCP 提示、异味、冒烟或明显快速升温同样立即关闭 OUTPUT。
5. 稳定后用万用表直流电压档测 DC 正负端、顶部 3V3/GND 和 5V/GND。
   回传电源显示的电压、电流、CV/CC 状态及这三项电压。
6. Codex 用 HOTPLUG 核对 MCU 连续运行、MOE=0、GPIO 读回、命令初始值、
   采样间隔及失败计数，再读取母线和 B/C 零电流读数。

本轮采用以下初步排错门槛，不作为最终校准精度承诺：

- 电源稳定在 CV，DC 端约 12 V；3V3 为 3.20～3.40 V，5V 为 4.75～5.25 V。
- 12 V 附近 bus.voltage_v 与万用表测得的实际 DCBUS 差值不超过 0.30 V；
  DC 输入端与 DCBUS 存在压差时，先确认母线测点和输入路径，不能直接按输入端校准。
- 有效采样间隔继续为 50 us，失败计数不持续增加，未发生功率使能。
- B/C 原始读数处于中间量程附近且稳定；零偏先记录，不要求未经校准的电流
  数值精确为 0 A。贴近 ADC 量程端点、明显漂移或观测异常时先定位。

CURRENT 工作限流与 OCP 超限关闭功能分别设置，见
[优利德 UTP1000-II 系列说明](https://instruments.uni-trend.com.cn/cate/116.html)。
慢速 SWD 读取只提供所读时刻的电流/原始值，完整 fast-tick 噪声统计另行采集；
不能把电源待机输入电流当作电机 B/C 相电流。

母线粗检不通过时，先核对 rank 顺序、采样时间、DMA 原始 pair 和 VREFINT。
本 F405 可读取出厂 VREFINT_CAL，地址与换算见 ST 的 stm32f4xx_ll_adc.h；
当前实现使用 1.21 V 名义值，出厂标定能解释多少偏差应先单独计算。
再在无电机、MOE=0、限流不变的条件下，把电源从 12.00 V 调低到 11.00 V，
稳定后记录电源、万用表 DC/3V3 与 ADC pair，区分比例误差和固定偏移。
实验有效比例不能直接冒充已确认的分压电阻标称值。

进一步定位时，先用直流电压档测 DC 负端与顶部 GND 的压差，
区分原理图中 PGND 与 GND 的参考差异。若需要测分压节点，商家单排 MOS
原理图中的 R5 为 39 kΩ 上臂，R12 为 2.2 kΩ 下臂，C3 为并联滤波电容。
参考照片和商家 3D 布局中，R5 是 AUX Gate Driver 左侧竖放电阻中的左边一颗，
右边竖放的是 R82；R5 上方横排左边为 R81、右边为 R12。
R81、R82 是 AUX 输入下拉，不可按器件是否同排来推断分压电阻编号。
VBUS_S 没有引出到现有排针。
定位器件时先关闭电源 OUTPUT（面板 ON/OFF 控制输出），确认母线低于 1 V；
固定黑表笔到顶部 GND，保持无电机、MOE=0 和原有限流后重新供电，
红表笔分别测 R12 两端，并在同一轮复测 DC 正负端。只在表笔能稳定接触时测量。
R12 不方便接触时，可改测 R5 两端；其中低电压端同为 VBUS_S，
高电压端按原理图应接 DCBUS，需要与 DC 正端在同一轮对照。
R12 的其中一端应为 GND；另一端按标称值应约为实际 DCBUS 电压 / 18.73。
同时与 ADC 原始值和参考读数换算出的输入电压比较，区分前端分压偏差与 ADC
侧偏差。实际元件、节点或参考未确认前，不用固定加数或乘数掩盖母线误差。

R5 高端与 DC 正端电压疑似不一致时，先用直流电压档直接核对压差：
红表笔固定在 DC 正端，黑表笔分别接 R5 两端，记录端点方位及带符号的电压。
原理图中与 DCBUS 直连的一端应接近 0 V；这比两次对 GND 读数相减更直接，
也避免在后续断电测量中凭记忆选择高电压端。
若确认输入到分压取电端存在压差，需要再核对真正的功率母线。
可选择背面 DC 输入附近的 C38：原理图中它跨 DCBUS/PGND，
属于母线滤波电容，不要任取其他电源轨的电容替代。
也可测背面沿电机端子排列的八颗大电容之一（原理图 C26～C33）；
靠近 DC 输入的一颗便于在背面直接接触两个焊盘。
电源滤波大电容 C5 属于 XL7005A 降压后的驱动电源，C6 属于 5 V，
不能仅因都是大电容就将其读数视为 DCBUS。商家图中 XL7005A 输入接 DCBUS，
其输出经 L1 形成 12 V 级驱动电源；该支路的输入输出压差不直接解释 DCBUS 压降。
关闭 OUTPUT 并确认 DC 母线低于 1 V 后再翻面、定位；沿用无电机和原有限流
重新供电，两表笔直接接选定母线电容的两个金属焊盘，同面测直流电压，记录符号及数值。
测完关闭 OUTPUT。用该电压与 DC 输入、R5 取电端对照，区分输入到功率母线
的压差与只存在于采样支路的压差；母线观测应对应功率级实际母线。
整组母线电容确认存在同样压差后，优先检查供电入口：核对电源线的接法，
以及输入电压测的是板上端子螺丝、线头还是电源输出端。
可在关闭 OUTPUT 并放电后翻到背面定位 DC 接口的两根大金属脚，
沿用原电压和限流，直流电压档直接测这两脚之间电压，测完关闭输出。
两表笔均在背面，不要求跨正反面探测；探头接金属脚，记录其与端子螺丝、
母线电容之间的读数差异，以区分端子内部接触与焊接/PCB 侧路径。
确认不一致后，先关闭 OUTPUT，并拔掉 ST-Link USB；
测阻前确认 DC 母线和 R5 高端对 GND 均接近 0 V，避免残留电压干扰。
用最低电阻档检查这两个点是否直接连通；交换表笔重复测量，并与表笔互碰
的阻值比较。必要时可追加同一正面的 C51 两端作为电源轨对照：C51 在 M1
Gate Driver 旁，原理图中一端接 12 V 级驱动电源，另一端接 GND；其焊盘可
用来核对 R5 高端是否与驱动电源直接连接。蜂鸣或充电过程中的瞬时低阻
不能单独证明两点属于同一条铜线。测完保持断电，待连接关系确认后再继续。
若怀疑输入到母线之间经过半导体，可在确认输入及母线均放电至接近 0 V 后，
用二极管档测 DC 正端与 R5 取电端，并交换表笔记录稳定读数。
单向结压降、反向 OL 只支持存在半导体路径，不能据此认定是正常防反接器件；
仍需核对实物输入连接和具体器件，排除经其他支路供电的情况。
二极管档出现负读数时，先停止该档测量，断开外部供电并改用直流电压档，
核对输入端、R5 取电端对 GND 及这两点之间的残余电压；电容充电或蜂鸣
不能单独作为短路证据。

若需区分 R5 取电端是否实际接在 XL7005A 输出侧，可追加一次 16 V 静态观测，
无需直接升到 24 V：先关闭 OUTPUT，设置 16.00 V、限流 0.100 A、OVP 17.0 V，
继续保持三相线和 ST-Link 3V3 供电线断开，不提交任何 PWM 命令。
开启输出后观察是否稳定 CV；持续 CC、异味或明显快速升温时关闭输出，不增加限流。
对照 DC 输入、已定位的母线大电容和 R5 取电端；若 DC 与母线升到约 16 V，
而 R5 取电端保持约 12～13 V，则支持采样取电点位于驱动电源侧，需要继续核对实物连接。
该实验不证明输入到母线存在串联器件，也不代替逻辑电源或真实 Gate 关断验收。
测完关闭 OUTPUT；第四轮 PWM 前恢复 12.00 V / OVP 13.0 V，并重新核对供电状态。

## 第四轮：短时 PWM 波形与关断测试

在观测、stage-only 和第三轮静态供电检查通过，确认电机三相线断开并核对
探头及软件关闭状态后，
才安排 PWM_BURST。软件关闭状态检查 MOE=0、六路 GPIO 读回为低和
enable_attempts 的预期值；实际 Gate 关断是否已测量另外记录。
三相 duty 强制相同，既不执行 FOC，也不用于连接电机的转动实验。

输入检查：10.0～14.0 V 的有效最新母线观测（age <= 5 ms）、
本次有效 B/C 电流观测、duty 0.10～0.90、总时间 1000～10000 us。
PWM_BURST 必须提交 TARGET_VALIDATION_POWER_KEY；每次消费后清除，
错误、结束、复位或恢复观测均不自动发起下一次使能。
确认值只是操作门槛，不是安全认证，也不能阻止直接修改硬件寄存器。

流程：先 stage，跨过确定 update boundary，再于下一有效回调再次 stage 后 enable；
期间每拍重新 stage。失效、拒绝、Platform 自行关断、操作者 Stop 或截止均 safe-off。
TIM1 update IRQ 独立检查截止；ADC callback 也检查截止。
截止关断受中断响应时间影响，不承诺精确到请求的单个微秒。
如果 ADC 停止，g_target_validation 可能停留在旧状态，独立 stop_reason 和 MOE
用于判断关断；不能仅凭快照 state 判断正在输出。

先断开 OUTPUT，固定探头：尖端接 M0 任一相端，地夹接顶部 GND。
若端子的 A/B/C 丝印被遮挡，先用照片方向的左/中/右记录，不猜相序。
示波器先用电池供电，两路地夹同地，探头与菜单都设 X10、DC 耦合。

第一条命令使用三相同 duty=0.50、duration_us=2000。操作者先将 DS100 置于
单次触发等待状态，再由 Codex 提交一条 mailbox 请求；板上自动结束，不连续重复。
供电限流沿用已经通过静态检查的设置；出现持续 CC 或保护提示时先停查。

分别采两条独立短脉冲，观察两个尺度：

- 周期细节：约 10 us/div、5 V/div，上升沿触发约 6 V，检查约 50 us 的周期。
- 整段包络：约 0.5 ms/div、5 V/div，同样单次触发，观察短脉冲列按时结束。

2000 us 包含 stage 准备时间，实际输出脉冲列会稍短，不能要求它恰好等于 2 ms。
每次结束后由 Codex 检查 result=COMPLETE、MOE=0、enable_successes 的单次增量、
中性 compare 恢复和持续采样。没有触发、波形异常或结束原因不符合预期时先分析，
不靠延长至连续 PWM 寻找波形。

第一相通过后，再按相同方式测另外两端；最后用三相同 duty=0.30 / 0.70 的
独立 2 ms 请求检查高电平宽度随 duty 改变。每次换探头先关 OUTPUT，
用万用表确认 DC 母线已放电至 1 V 以下再移动接线。
测完先关闭 OUTPUT 再连接示波器 USB 传输波形。

三相端子波形、关闭后的持续采样和旧 duty 失效分别验收；相端波形不承担
精确死区或最终 Gate 关断验收。以后若确需内部诊断，另外确认可靠探测点及接线，
MCU 输入映射为 A: PA8/PB13、B: PA9/PB14、C: PA10/PB15；500 ns 死区
测量约需 100～200 ns/div 和适合的接地方式，VGS 还需按 Source 参考测量。

本板没有已确认 Hardware Trip。功率开启期间不使用断点或暂停调试：
CPU 停止时两条软件截止路径都不能执行。操作要求中的电源输出关闭与物理接线确认
由用户完成，采集与回归结论分别保存。
