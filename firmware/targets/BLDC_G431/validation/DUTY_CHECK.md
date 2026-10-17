# G431 不同占空比的无电机短脉冲检查

本文保留本轮方法与历史操作记录。过程段落中的“下一步”“待补”表示当时状态，不是当前执行指令。
`local-evidence` 链接仅供本机追溯；可随仓库查看的原图见[公开证据](evidence/20261018-baseline/README.md)，
当前接口缺口与验收门槛见[台架验证](../VALIDATION.md)。

当前状态（2026-10-18）：本轮 LA 的 30%/70% 基础脉宽响应和请求结束后的关断检查通过，历史功率请求累计 24 次。
30% 的第 20 次照片显示 34.8 us，包结束由人工确认；70% 的第 23 次照片显示 14.8 us，
第 24 次后用户确认电源 OUTPUT 一直开启、AUTO 下 LA 始终约 0 V。两档均为 20.0 kHz、峰值约 12.2 V。
最后只读检查确认仍是第 24 次的完成计数、六路 LOW/MOE=0、保护健康、采样继续，没有新增使能。
70% 稳定周期照片和结束后低电平来自不同请求，不声称捕获了同一记录的最终边沿或精确包宽。
本轮基础检查已收尾，不再为补整包照片重复发请求。汇总见
[本轮结论与证据](local-evidence/20261017-duty-check/duty-stage-summary.json)。
沿用 HEX 29AB28611691DD220624708ECF826BE9EA31F4C82753418DCC6BE64D8F5FACFF，
固件已有命令 3 的可变占空比入口，本步骤没有修改生产 C 源码、重新构建目标镜像或烧录。

## 本轮范围

先测 LA/GND 的 30% 和 70% 两个独立请求，每次三相共同 duty，duration_us 固定 2000。
每个请求各一次使能、各自有限时长结束；第二档需要单独准备和明确请求，不连续自动执行。
2000 us 包含使能前 staging，不将其直接当作实际 Gate 包宽。
暂不改变相间占空比，不接电机，不调用故障注入、clear 或 rearm。

当前 TIM1 为 20 kHz、中心对齐 PWM1，高侧使用 CH1/2/3，低侧使用互补输出。
common_duty 表示高侧/相端的名义占空比，LA 为低侧 Gate，其名义高电平约为互补部分：

| 请求的三相共同 duty | 名义周期 | LA 名义高电平宽度 | LA 名义占空比 |
| --- | --- | --- | --- |
| 30% | 50 us | 约 35 us | 约 70% |
| 70% | 50 us | 约 15 us | 约 30% |

上述为当前配置推导，不是已测结果或精确合格限。DRV8300DPWR 为低侧输入非反相型号，
内部死区、传播与 Gate 边沿会使读数偏离理想值。器件极性依据
[TI DRV8300 数据手册的器件对照表](https://www.ti.com/lit/ds/symlink/drv8300.pdf)。
本轮先确认两档的稳定脉宽变化、基本电平和包结束；不能据 LA 一点推定三相或相端全部验收。

## 工具与离线结果

- `tools/pwm_once.py`：只接受 30% 或 70%，固定 2000 us，发送命令 3。
- `--inspect-startup`：只读，要求健康的零事务新启动，生成独立锚点。
- `--preflight --previous-summary ...`：只读，要求当前计数、结果及 probe capture 与先前证据一致。
- `--execute-once --previous-summary ...`：重新检查后只提交一次请求，先写参数，最后提交 sequence。
- 每次取得同次 AXF 的地址/字段布局，校验冻结 HEX/AXF/MAP 和实时 Flash；不使用旧镜像脚本。
- 前后核对采样、保护、供电观测、关断状态，以及 TIM1 周期、PWM 模式/极性、preload、采样触发配置。
- 结束要求 COMPLETE、key 清零、恰好新增一次 enable、六路 LOW/MOE=0、TIM6 停止、保护健康、采样继续。
- mailbox 的 command/duration/duty 位模式必须与本次请求相符；故障注入 capture 必须不变。
- stage/guard 计数只作合理性检查，不作为真实包宽或 Gate 关断延迟测量。
- 任一检查失败不重试；已尝试写命令时只补 SAFE_OFF，不清故障、不 rearm、不重复使能。

新增主机工具的 62 项检查通过，覆盖两档浮点位模式、唯一提交、旧证据拒绝、异常状态/
配置/结束结果、恢复后的非零计数，以及写前失败零动作、写后失败仅关断。
ARM 执行模型扩展至 108 项：在原范围加入 30%/70%，并核对实际测试模块传给 HCC 的
三相 duty、限时停止和无重放。模型的 HCC/MMIO 是替身，不等于电气验收。
既有保护主机 74 项、逻辑工具 27 项继续通过。

本次还用上一阶段已保存的 TIM1 原始数据检查了解码与配置要求，属于历史记录静态核对，
不是当前板子的实时预检。本步骤没有 SWD 访问，没有新功率请求。
冻结源码、逐项哈希和检查日志位于
[离线清单](local-evidence/20261017-duty-check/source-freeze.json)，操作计划见
[本轮计划](local-evidence/20261017-duty-check/plan.json)。原 106 项构建清单按历史记录保留。

## 第一档的现场操作

1. 电源 OUTPUT 关闭，无电池、无电机；ST-Link 3V3 保持断开，保留 SWD/GND。
2. 沿用 CHA 尖端 LA、地夹 GND。实体和菜单均 X10，DC，5 V/div；CHB 关闭。
3. 先设 500 us/div、水平偏移 0、CHA 上升沿约 5 V、SINGLE，以保留整个短脉冲包。
4. 电源仍为 12 V / 50 mA；开启 OUTPUT，确认供电/DRV 正常，再使示波器进入单次等待。
5. 操作员回复一次“开始”。执行方实时预检通过后宣布 8 秒可中断等待，再检查并只发一次 30% 请求。
6. 结束后可放下探头；确认示波器 STOP，先保留整包及末尾平线，再在同份记录中放大到
   20 us/div 查看稳定连续周期，读取频率、正脉宽和电平。首尾不完整周期不用于稳定占空比。
7. 本轮没有保持故障要求；确认采集 STOP 后可关闭 OUTPUT，保留记录。未触发则先手动 STOP 并报告，
   不把随后关电形成的边沿当作 PWM 捕获，也不自动重发。

第一档核对后再安排 70%；改时基/移动旧 STOP 记录不会增加原始采样细节，不需要为放大而重发。
实际结果分别记录“寄存器/命令结果”和“照片或人工读数”，未采集前不填入名义推导值。

## 实物进度

用户回复“开始”后，重新核对冻结工具、源文件和候选镜像，当前是健康的新启动，
母线约 11.94 V、采样/保护正常、六路 LOW/MOE=0，事务计数全为 0；本主控没有复位。
明确宣布并完成 8 秒可中断倒计时，重新检查后仅提交一次命令 3，common_duty=0.3、
duration_us=2000。本启动 sequence=1，mailbox duty 位模式为 0x3E99999A，返回 COMPLETE；
stage=40、enable 尝试/成功各 1、guard IRQ=39，key 清零，六路 LOW/MOE=0、TIM6 已关，
采样继续且 invalid=0、保护无故障，注入 capture 未改变，没有自动重放。
随后用户提供 LA 的 STOP 稳定周期图：X10 / DC / 5 V/div、20 us/div、水平偏移 -10.5 us，
CHA 上升沿 4.97 V；显示 20.0 kHz、正脉宽 34.8 us、Vmax 12.2 V、Vmin -204 mV。
由显示周期与脉宽得到 LA 占空比约 69.6%，与共同 duty=30% 的低侧互补预期相符，
频率、基本电平及稳定脉宽响应初筛通过。自动极值不作为真实瞬态峰值资格化。
当前图是连续周期局部，尚未显示整包的最后一次下降沿及持续低电平；不能由这张图
推定包已结束或精确包宽。先保持 STOP 返回 500 us/div 确认末尾，再准备独立 70% 请求，
无需为了移动/放大同一旧记录重发。原图、哈希和读数见
[30% 稳定周期](local-evidence/20261017-duty-check/duty30-ready-234955-execute/scope-la-stable.json)。
记录见 [30% 首次现场](local-evidence/20261017-duty-check/duty30-ready-234955-execute/observation-record.json)。

随后用户回复“已结束，开始70%”，确认上述旧 STOP 记录的最后脉冲后持续为低电平，
30% 的包结束功能检查通过。这是人工确认，没有新增整包照片或精确包宽读数，见
[30% 包结束确认](local-evidence/20261017-duty-check/duty30-ready-234955-execute/scope-packet-end-manual.json)。

第二档预检仍校验冻结源码、工具、HEX/AXF/MAP、实时 Flash 与 PWM 配置；发现健康的零事务
新启动，母线约 12.05 V、六路 LOW/MOE=0、采样和保护正常，本主控未复位。
明确宣布并完成 8.0066 秒可中断等待，再检查并仅提交一次 common_duty=0.7、2000 us 请求。
本启动 sequence=1，mailbox 位模式 0x3F333333，返回 COMPLETE；stage=40、enable=1/1、
guard IRQ=39。结束后两次回读六路 LOW/MOE=0、TIM6 已关、key=0、保护健康、invalid=0，
采样继续且注入 capture 未变，无自动重放。计数不作为实际 Gate 包宽测量。

70% 的 STOP 照片为 X10 / 5 V/div、50 us/div，显示 20.0 kHz、正脉宽 14.8 us、
Vmax 12.2 V、Vmin -12.3 mV。LA 占空比由显示值推导约 29.6%，符合低侧互补预期。
两档稳定脉宽由 34.8 us 变为 14.8 us，频率与基本电平初筛通过；不据自动极值或名义差值
资格化真实瞬态、死区或传播延迟。本图未覆盖整个有限包的末尾，仍须保持 STOP 返回
500 us/div 确认最后脉冲后持续低电平，无需重发。
原图与判读见 [70% 稳定周期](local-evidence/20261017-duty-check/duty70-ready-235953-execute/scope-la-stable.json)，
命令与结束状态见 [70% 现场](local-evidence/20261017-duty-check/duty70-ready-235953-execute/observation-record.json)。

用户随后报告“其他都正常 包末尾不知道 没抓到”。新 STOP 图为 20 us/div、水平偏移 -357 us，
稳定周期仍显示 20.0 kHz / 14.8 us / 12.2 V；黄、绿两条迹线在同一横坐标终止，其后只有空白网格。
该显示边界不能当作脉冲包结束，更不能将空白网格当作持续低电平。保留稳定周期通过，
将包末尾明确记为未捕获；电源和 DRV 的人工正常反馈已补齐，未另报电流/CV 或温度数值。
照片与反馈见 [70% 记录边界](local-evidence/20261017-duty-check/duty70-ready-235953-execute/scope-record-boundary.json)。

补采时先关 OUTPUT，保持原 LA/GND 接法，将时基设为 500 us/div、水平偏移 0，
必须在重新进入 SINGLE 等待前完成设置。CHA 仍为实体/菜单 X10、DC、5 V/div、上升沿约 5 V；
保持 12 V / 50 mA、无电池/电机、ST-Link 3V3 断开。开启 OUTPUT 并确认正常后，操作员重新回复“开始”。
执行方须重新只读预检、宣布 8 秒等待，再单独发一次 70% 请求，不自动重试。
这次先保持 500 us/div 查看完整有限包及末尾平线，稳定脉宽已有证据，无需再次放大。
本次仅保存旧记录及人工反馈，没有连接 MCU 或增加功率请求，累计仍为 21。

用户回复“70%补采开始”，中断后要求“继续”。只读核对无后台测试进程及健康零事务启动，
第一次 8.0189 秒倒计时结束后，用户立即说明已重启电源并要求重新来；该次尚未调用执行工具，
没有写功率命令。旧预检及已准备参数作废，单独保存中断记录，不计为功率请求。

按新上电状态重新核对冻结文件、实时 Flash、PWM 配置、采样/保护与关断，母线约 12.02 V、
事务计数为 0，未由主控复位。重新宣布并完成 8.0056 秒等待，执行前再次核对后仅发一次
共同 duty=0.7、2000 us 请求。本启动 sequence=1、COMPLETE，stage=40、enable=1/1、guard=39，
结束两次回读均六路 LOW/MOE=0、TIM6 停、key=0、保护健康、invalid=0，采样继续，注入 capture 未变。
累计第 22 次；本次整包波形及本轮电源/DRV 的结束反馈待回报。第 21 次稳定脉宽照片按独立证据保留，
不冒充本次同轮照片。保持 STOP / 500 us/div 查看整包末尾，不自动重发。
记录见 [70% 包末尾补采](local-evidence/20261017-duty-check/duty70-repeat1-restart-ready-002806-execute/observation-record.json)。

用户随后说明拿错探头并明确要求再来一次，第 22 次不接受为有效波形，实际误拿探头所接测点未说明。
先重新核对冻结文件、实时镜像、同次完成计数、配置与健康关断，母线约 11.96 V；与第 22 次为同一启动。
提醒 CHA 对应探头接 LA/GND、X10、500 us/div、单次等待，宣布并完成 8.0081 秒准备时间，
再检查后仅提交一次同参数 70% 请求。本启动 sequence=2、COMPLETE，累计 stage=80、enable=2/2、
guard=78；本次分别新增 stage=40、enable=1、guard=39。结束回读六路 LOW/MOE=0、TIM6 停、key=0、
保护健康、invalid=0，采样继续且注入 capture 未变。没有自动重发、复位或重新烧录。
历史累计第 23 次；本次整包末尾照片及结束电源/DRV 状态待回报，保持 STOP / 500 us/div。
证据见 [70% 再次补采](local-evidence/20261017-duty-check/duty70-repeat2-ready-003050-execute/observation-record.json)。

用户随后反馈“还是抓不到啊”，提供本次 STOP 图：当前显示 50 us/div、水平偏移 -496 us、
CHA X10 / DC / 5 V/div、垂直偏移 429 mV、上升沿阈值 4.92 V；显示 20.0 kHz、正脉宽 14.8 us、
Vmax 12.2 V、Vmin -249 mV。本次稳定周期响应通过；黄、绿迹线在同一位置终止，后面是空白网格，
仍不确认包末尾。STOP 当前为 50 us/div 不能证明采集前未用 500 us/div，也不能证明原记录覆盖全包。
实际采集时基及是否在 STOP 后缩放，须由操作员说明。自动极值不资格化真实瞬态。
照片与判读见 [第 23 次记录边界](local-evidence/20261017-duty-check/duty70-repeat2-ready-003050-execute/scope-la-record-end.json)。

核对 [制造商 DS100 V1.5 手册副本](https://fabacademy.org/2024/labs/litchee/students/dajiang-shi/files/DS100_Mini_Digital_Oscilloscope_User_Manual_V1.5.pdf)
第 7、14、16～19 页：顶部波浪条对应采集数据范围，采样率/存储深度限制一次记录时长，
SINGLE 取得一次记录后停止。实际示波器固件版本尚未读取，不将参考表直接等同本次采集参数。
先不增加请求：保持 STOP，将旧记录设为 500 us/div、水平偏移 0，提供总览并说明 50 us/div
是否在 STOP 后才修改；不能通过缩放补回未采样时段。若确需新采集，先核对单次等待设置照片，
再按独立授权、实时预检和 8 秒准备流程执行。本步骤没有连接 MCU 或修改固件，累计仍为 23。

用户继续提供旧 STOP 记录照片：当前时间档位 100 us/div、水平偏移 0、垂直偏移 0，
CHA X10 / DC / 5 V/div、CHA 上升沿，但当前触发阈值为 -441 mV；仍显示 20.0 kHz / 14.8 us。
约十余个周期后黄、绿两迹线一起终止，后面是空白网格，包末尾仍不确认。
采集前是否采用相同档位/阈值仍未知，不将 STOP 菜单当作采集参数的历史证明。
先关 OUTPUT，只检查设置：时间档位 500 us/div（0.5 ms/div）、水平偏移 0、CHA 上升沿约 +5 V，
其余仍为 X10 / DC / 5 V/div；保持 STOP 提供设置照片，确认后再安排单次等待与独立启动。
不因未看到末尾直接增加请求。照片见
[STOP 设置核对](local-evidence/20261017-duty-check/duty70-repeat2-ready-003050-execute/scope-stop-overview-100us.json)。

用户明确选择 AUTO 观察并要求正常重新发，沿用原 70% / 2000 us，取消先提供设置照片的要求。
只读预检确认冻结文件/实时镜像和健康零事务启动，母线约 12.03 V；宣布并完成 8.0049 秒等待，
执行前再核对，仅新增一次功率请求。本启动 sequence=1、COMPLETE，stage=40、enable=1/1、guard=39，
结束两次回读六路 LOW/MOE=0、TIM6 停、key=0、保护健康、invalid=0，采样继续，注入 capture 未变。
历史累计第 24 次，没有固件修改、烧录、复位或自动重放。

用户随后报告“没抓到”。AUTO 的后续强制触发可能覆盖短包，但本机的实际捕获过程未取得照片，
不能把缺失照片当作输出为低的证明。先不补发，确认本次之后电源 OUTPUT 是否一直开启，
再确认 AUTO 下 LA 当前是否持续约 0 V 且不再出现周期脉冲。人工后态复核与包末尾边沿/
精确时间是不同证据；若供电中断，不当作本次同轮关断观察。
记录见 [70% AUTO 观察](local-evidence/20261017-duty-check/duty70-repeat3-auto-ready-004637-execute/observation-record.json)。

用户随后明确回复“一直开着 始终0V”，确认第 24 次之后电源 OUTPUT 持续开启，AUTO 下 LA
保持约 0 V，无周期脉冲的后态检查通过。该回复没有声称看到了本次短包或最终边沿，不升级为
整包连续采集、精确 Gate 关断时刻或包宽测量。第 23 次的 14.8 us 稳定周期照片与第 24 次的
结束后电平人工复核分开引用。人工记录见
[70% 后态低电平](local-evidence/20261017-duty-check/duty70-repeat3-auto-ready-004637-execute/operator-post-burst-low-report.json)。

随后独立只读检查再次校验冻结文件和实时 Flash、同一第 24 次事务锚点、PWM 配置、健康保护及采样推进：
sequence=1、stage=40、enable=1/1、guard=39 均未改变，六路 LOW/MOE=0、TIM6 停、key=0、
cause/COMP/Break=0、invalid=0，母线约 12.06 V。没有写命令、清故障、rearm、复位或烧录。
本轮 LA 两档基础响应与结束关断检查收尾通过；其他相的不同 duty、相间时序、负载能力和精确动态测量仍未验收。
最后只读证据见 [收尾检查](local-evidence/20261017-duty-check/post-low-final-inspect-010453/summary.json)。
下一步转入电流采样零点/噪声复核及已知电流对照准备，不自动安排功率动作。
