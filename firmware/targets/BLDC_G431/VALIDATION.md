# G431 台架验证

`validation/` 通过 HCC 使用 Platform，提供默认 OBSERVE、功率关闭的台架入口；
它不是正式 Motor / ControlRuntime。旧功率包与保护注入命令仍要求断开电机三相；
新增有限电压开环命令按[专用流程](validation/OPEN_LOOP.md)连接电机。
本页汇总当前有限验收、历史证据归属和复用入口，不表示有带电操作排队。

本页汇总 Keil 台架、默认关断观察与专用无功率计时入口；后两者的构建及执行边界见
[runtime/README.md](runtime/README.md)。各入口的镜像与验收证据不互相替代。

## 静止坐标电流环：低幅值两轴正反向跟踪通过

B/C 名义电流先扣冻结零偏再同时取反，统一为桥→电机正方向；依据为原始电路及
已修正物理相映射的正α/正β静态对照，不再将独立增益精度待测表述为方向未知。
新增命令17复用已有PI，在恒等坐标系中控制αβ电流；正式转子FOC标定及运行许可保持关闭。
流程及验收边界见 [CURRENT_HOLD.md](validation/CURRENT_HOLD.md)，精选数值见
[电流环证据](validation/evidence/current-hold/summary.json)。

台架、Observe、Timing三个目标Rebuild均零错误警告；223项台架ARM模型、46组实际Observe
入口模型及7项主机工具检查通过。台架Control组采用与正式入口相同的速度优化设置。
操作者完成逻辑供电切换后，安装HEX `d3b968ea78804e71ecc7e0dd52f450497fff0b1a1c96b1836b65322626ba7efb`，
独立核对47688个Flash加载字节及默认关断，随后恢复12 V／0.10 A原接线并确认正常。

本启动请求序号1执行4096点关断采零；首次试验尝试在只读身份上传阶段USB超时，未写请求，
操作者重插USB后保留镜像及同启动零点，只发送请求序号2的一次3秒电流环命令17。
目标α=60 mA、β=0，保持窗口5000点均值为α=59.560 mA、β=0.016 mA，
均值矢量误差0.440 mA、单拍误差峰值26.798 mA，均为名义增益换算。
电压请求峰值0.190 V，未触发电压限幅；名义相电流峰值69.868 mA。
stage30001/30001、enable1/1，正常完成后MOE=0、六路LOW、guard停止、Trip/invalid均为0。
consumer最大观测34.412 µs，不能作为完整ADC IRQ或WCET证明。

操作者报告无异常，但未留意轴运动；编码器前后净差约−0.066°，不是完整运动轨迹。
首轮证明有限静止电流环的负反馈和跟踪。随后沿用同一固件、原12 V／0.10 A接线，
补β正向、α反向、β反向三个独立目标，各3秒、每次前留5秒准备；未改代码、重刷或重跑旧套件。
本次连接后的新启动分别以请求1/3/5取得4096点关断零点，请求2/4/6各执行一次功率试验。
每次末尾保持窗口均为5000点，结果如下；首行保留前轮同镜像的独立证据。

| 名义目标 | α反馈均值 [mA] | β反馈均值 [mA] | 均值矢量误差 [mA] |
| --- | ---: | ---: | ---: |
| α=+60、β=0（前轮） | 59.560 | 0.016 | 0.440 |
| α=0、β=+60 | 0.125 | 59.869 | 0.181 |
| α=−60、β=0 | −59.693 | 0.006 | 0.307 |
| α=0、β=−60 | 0.032 | −59.981 | 0.037 |

新增三次均通过原跟踪筛选，电压请求峰值最高0.204 V，无电压限幅或Trip；
每次stage30001/30001、enable1/1，均完成后MOE=0、六路LOW、guard停、invalid=0。
consumer累计最大观测34.5 µs，仍不是完整IRQ或WCET。操作者报告三次均无异常。
四个独立恒定轴向点不能替代动态带宽、二维联动、全工作区、绝对电流精度、转子电角映射、
旋转FOC或速度闭环验收。旧证据保持原镜像归属，下一步转向编码器电角映射。

## 有限电压开环：正反向运动与自动关断通过

本轮将外部电流精度对照后移，先在既有台架 Target 增加单次低幅值电压开环，
推进电机运动与编码器跟随验证。Observe 入口及正式 FOC 的三项可信标志保持关闭，
不把开环运动或名义电流监视当成标定合格。

新增命令 15，旧邮箱 20 字节、旧快照 184 字节、时序命令 10～14 保持兼容。
一次完整 4096 点关断零点窗口合格后冻结 B/C 偏置；两拍中性 duty 跨边界后使能。
电压缓升、固定保持、缓升电频、恒频旋转和电压缓降共约 3 秒，另有从接收计时的
3.5 秒独立关断期限。电压矢量硬上限 0.5 V、电频绝对值不超过 5 Hz；首轮候选
请求为 0.3 V／+2 Hz。位置、母线、采样、名义电流异常或 Stop／Trip 撤销本次输出，
不自动重试或清故障。

纯模块实际 ARM 代码 11 组通过；台架实际 ARM 模型 188 组通过，含原 135 组和新增
53 组。主机单次工具的 16 项替身检查通过，包含 payload 读回后才提交 sequence、
零点同启动锚、失败只补一次 SAFE_OFF；回调检查按实际 10 kHz，ControlTime 仍为 20 kHz。
Keil 台架全量构建及最终增量构建均为零错误、零警告。

镜像核对工具已支持 ARMCC 的合法 RW 压缩，继续严格比较 HEX／AXF 加载字节及 MAP
入口、地址和符号；19 项镜像工具测试通过。最终候选 HEX 为
`5021fa45bf4e6a459e77aa45644b790eebe067d682e613ce6aef6f6f3374e8d8`。

操作者确认 OUTPUT 关闭、无外部注入、三相脱桥、ST-Link 单独逻辑 3V3、编码器保留后，
先读回此前 Observe 加载字节及关断状态，再以 NORMAL 下载并校验本候选。
安装后独立 Flash 上传与候选的 41120 个加载字节一致；两次只读检查确认启动正常、
10 kHz 回调与时基持续推进、SSI 有效、保护健康、六路 LOW／MOE=0，所有命令、
stage／enable 计数及新开环结果均为零。软件 VBUS 约 2.719 V，未新增万用表读数。
旧 Observe 的启动零点不沿用；本轮没有发送采零、功率或试转请求。

操作者恢复原三相接线、撤去注入源、断开 ST-Link 3V3 并固定电机空载后，报告静态
约 0.02 A／CV、万用表 VBUS 12.12 V、无异常发热。一次命令 4 完成 4096 点零点，
B／C 均值约 −12.968／−0.458 mA，峰峰值均约 19.435 mA。
结束后 Host 恰逢正常 TIM1 IRQ 临时清 UIE 的窗口，误拒绝并仅发送 SAFE_OFF 2；
修正 Host 按 10 kHz callback／20 kHz ControlTime 持续推进检查后，只读核对原始零点和
唯一关断链并恢复锚，未重采、未修改或重刷固件。修正后的开环工具 19 项及旧 PWM 工具
67 项用例通过。

同一启动随后仅发送一次命令 15（request 3），0.3 V／+2 Hz 五段轨迹正常完成：

| 本次有限实测 | 结果 |
| --- | --- |
| 采样／使能期间采样 | 30002／30001 |
| stage 接受／尝试 | 30001／30001 |
| enable 成功／尝试 | 1／1 |
| 编码器净位移 | −0.384155 圈，约 −138.30°，沿原编码器坐标 |
| 名义相电流峰值 | 约 0.114605 A，未触发 0.25 A 辅助门限 |
| consumer 最大观测 | 5695 cycle，约 33.5 µs；不是完整 ADC IRQ 或 WCET |
| 终态 | 模块 COMPLETE、result／stop=1、六路 LOW／MOE=0、guard 停止、Trip／invalid=0 |
| 人工观察 | 已转动、平稳、无异常、保持 CV；目测方向未注意到 |

本轮证明有限低电压开环运动与自动关断，不能据净位移确认极对数、严格同步跟随或
电角度零位。位移包含初始定向与降压阶段运动，不包括关断后滑行；电流增益／极性和
FOC 闭环仍未验收。实际完整 ADC IRQ 的最坏预算也不能由单次 consumer 最大值代替。
配套工具、参数和现场步骤见[有限电压开环](validation/OPEN_LOOP.md)，
身份和已验证边界见[本轮证据摘要](validation/evidence/voltage-open-loop/summary.json)。

随后使用同一 HEX 完成 −2 Hz 对照，未改固件或重新烧录。反向前只读检查发现事务和
捕获计数已清零、运行时间回退，说明两轮之间发生过重新启动，原因未确定。因此在本次
启动重新采集一次 4096 点零点，B／C 均值约 −13.416／−1.797 mA，不沿用上一启动偏置。

| 同固件方向对照（均为 0.3 V、约 3 秒） | +2 Hz | −2 Hz |
| --- | --- | --- |
| 原编码器坐标净位移 | −0.384155 圈 | +0.386047 圈 |
| 名义相电流峰值 | 约 0.114605 A | 约 0.112819 A |
| enable 成功／尝试 | 1／1 | 1／1 |
| 终态 | COMPLETE、六路 LOW、MOE=0、Trip=0 | COMPLETE、六路 LOW、MOE=0、Trip=0 |
| 人工方向观察 | 未确认 | 从出轴端看逆时针 |

两轮支持“正电角方向与原编码器角增量反号”，`encoder_direction=-1` 仅作为候选，
尚未安装配置。逆时针的目测描述与配置符号不是同一约定。反向本轮只补充方向观察，
未把上一轮的平稳、温升和 CV 报告移用于本轮。极对数、电角度零位及电流精度仍待验证；
下一步以有限静态电角位置观测检查零位候选与映射关系。
数值及原始摘要身份见[正反向对照证据](validation/evidence/voltage-open-loop/reverse-comparison.json)。

### 静态电角往返候选：离线检查和安装后默认关断通过

新增命令 16，在同一有限功率事务中完成 0 → 指定电角 → 0 的三段静态保持及缓升缓降。
正常轨迹 5 s、独立期限 5.5 s；初始仍为 0.3 V、往返幅度 0.25 电角圈，复用原低幅值
限制、同启动零点及故障关断。三个保持窗口保存独立新 SSI 的圆均值、跨度和首尾时间，
用于筛选方向、零位和标称极对数映射的一致性候选；不安装标定，不修改正式控制许可。

真实 ARM 轨迹模块 14 组、台架模型 203 项、新主机替身 10 项及原开环主机 19 项通过。
台架 Keil 全量及最终增量构建均为零错误、零警告。候选 HEX 为
`703ef33cf47d63bbc858c5afd6309a3377fac9c2d702a5fba8043ba0ef127464`。
操作者确认无外部母线、ST-Link 逻辑 3V3 后完成下载、校验及复位；独立 Flash 读回与
候选 43016 个加载字节一致。两次只读核对确认 10 kHz 采样、SSI 有效、保护健康、
六路 LOW／MOE=0、guard 停止、命令和 stage／enable 计数为零；未采零或发功率请求。
Observe／Timing 产物和用户 Keil 设置保持。
新增路径的模型不能替代实际转子收敛、已知电流增益/极性或正式 FOC 验收。
参数、筛选门限和复用命令见[静态往返流程](validation/OPEN_LOOP.md#有限静态电角往返)，
身份和已验证边界见[静态往返候选摘要](validation/evidence/angle-survey/summary.json)。

随后操作者确认恢复 12 V／0.10 A 条件正常，工具沿用前次 DMM 12.12 V 参考，未新增人工
电流或电压数值。本启动 request 1 完成 4096 点零点，B/C 均值约 −12.805/−0.722 mA。
唯一功率 request 2 完成五秒轨迹，stage 50001/50001、enable 1/1、采样 50002 点，
最终 COMPLETE、六路 LOW／MOE=0、guard 停止、Trip/invalid=0，名义峰值约 0.114504 A。
三个窗口各 250 份独立 SSI，位置跨度均为零；圆均值依次 0.379395、0.351440、0.379395 圈。
往返位移为 −/+0.027954 圈（约 10.06°），返回误差为零；7 极对／方向 −1 假设下最大
offset 残差 0.036214 电角圈，超过预设 0.035 门限，保持标定未确认。

只读追查发现原始网表 INHA/INLA 接 U4.32/29，即 PA10/PB15、TIM1_CH3/CH3N；
INHC/INLC 接 U4.30/27，即 PA8/PB13、TIM1_CH1/CH1N。Platform 原按 CCR1=A、CCR3=C
写入，与物理 A/C 相反，旧硬件说明也有相同错误。该事实解释了静态电流的相位对应，
不单独解释全部位移偏差；修正后必须重建方向候选，不能直接沿用 −1。
来源与修正范围见[输出相位核对](validation/evidence/angle-survey/pwm-phase-mapping-audit.json)。

## PWM 物理相映射修正：实机静态复核取得角度候选

修正 Platform 输出适配：物理 A/B/C duty 分别写 CCR3/2/1，B/C 电流通道不互换，
PWM 频率和保护配置不变。硬件基线和设计说明同步纠正。真实 Platform ARM 模型新增
从原始网表独立推导物理相的三例，当前 101 项通过；旧时序兼容 15 项、新静态 Host
11 项通过。三个 Keil Target 全量构建均为零错误、零警告；旧模型与实机证据不重写为新镜像结果。

台架 HEX 为 `2b882a4832cfc39f21182d9c81b9f9c7952914f1704f08bb2846aeb03b91b040`。
首次安装前读取因 ST-Link 探针本体 DEV_CONNECT_ERR 失败，未发生目标写入；操作者明确
重插 USB 后，完成逻辑供电核对、下载校验和复位，独立读回 43016 加载字节一致。
两次只读确认默认关断、采样和 SSI 正常、零请求，随后才按操作者确认恢复 12 V／0.10 A。

| 同一修正镜像的有限静态往返 | 0.3 V | 明确授权的 0.4 V 对照 |
| --- | --- | --- |
| 功率请求／此前完整零点请求 | 2／1 | 4／3 |
| 三个窗口独立位置数 | 每窗 250 | 每窗 250 |
| 方向候选 | +1 | +1 |
| 返回同一电角的机械位置差 | 约 +4.109° | 约 −0.110° |
| 最大 offset 残差（电角圈） | 0.045654，未过 0.035 门限 | 0.028097，通过初步筛选 |
| 名义相电流峰值 | 约 0.112241 A | 约 0.143374 A |
| stage／enable 增量 | 50001 全接受／一次成功 | 50001 全接受／一次成功 |
| 终态 | COMPLETE、六路 LOW／MOE=0、Trip/invalid=0 | 同左 |

两次均正常关断，未因不合格结果自动重复。0.4 V 由操作者单独确认，未改固件、接线或
放宽判定门限；按新要求，操作前准备时间为 5 秒。每轮均在功率关闭、转轴静止条件下
重新采本次完整 4096 点零偏，未要求再次烧录或重跑示波器流程。

当前可保存 7 标称极对、方向 +1、offset 约 0.025594 电角圈的候选，不安装为可信配置。
增加励磁后回程差显著减小，支持后续检查机械/励磁因素，但不能唯一归因。
电流增益、FOC 相电流正方向、电角度精度和正式电流/速度闭环仍待验收。
名义电流及 consumer 最大观测不代替精度、完整 ADC IRQ 或 WCET。
原始摘要身份、三窗口电流及候选边界见[相映射修正证据](validation/evidence/pwm-phase-mapping/summary.json)。

## 电源显示参考与现有器材：已准备采集，零偏只读核对通过

已知电流工具增加 `--reference-source supply-display`，明确保存电源实际显示电流的
来源和粗对照限制；默认 DMM 调用兼容。两种来源均保持原有镜像、逻辑供电、功率关闭、
冻结零偏及一次请求前检，不自动判定增益或授予运行许可。32 项纯 Host 替身测试通过。

操作者用已完全脱离功率桥的电机两线绕组建立独立支路，退出万用表后报告电源显示
0.10／0.15／0.20 V 对应 0.013／0.021／0.029 A，并报告约 0.29 V 时进入限流。
该独立支路尚未接板上分流，不能记为板上电流或校准结果；过程与限制见
[电流复核](validation/CURRENT_CHECK.md#后续已知电流对照准备)。

操作者随后确认 OUTPUT 关闭、无注入、VBUS 无外部供电、三相脱桥、仅 ST-Link 逻辑
3V3 和原编码器接线，万用表 VBUS 为 2.76 V。安装既有默认关断 Observe 镜像
`b2ca00eea00a72477dfbaaaf21a1179525468b9bcb5ccc593a921797fe6040ef`，未改固件源码。
第一次 HOTPLUG 下载在 45 秒超时；只读证明原 Timing 加载字节完整、CPU 运行且 MOE 关闭。
改用 NORMAL 完成下载、校验和软件复位，随后全 Flash 读回核对候选加载字节一致。

本次启动两次只读健康检查通过：IDLE、faults／guard／MOE 均为零，时基推进、请求为零；
2048 点零偏 READY，B 约 −12.512 mA、C 约 −1.377 mA，软件母线约 2.720 V。
零偏保持同一冻结身份；未写采集 request、未发功率指令、未做非零板上电流对照。
该次记录结束时板保留默认关断 Observe 镜像；原定直接分流注入后移，当前先准备上文的有限电压开环。
来源、安装经过及只读范围见 [准备证据摘要](validation/evidence/current-reference-preparation/summary.json)。

## 当前 10 kHz 控制节拍：完整 Runtime 软件闭环模型通过

在电流对照器材未就绪期间，先补齐当前时序的软件验证，不修改正式固件或标定许可。
真实 ARM C Runtime 与独立合成 PMSM 对象采用 100 µs 电流拍、1 ms 速度更新；
每拍先用旧输出积分 75 µs，在显式有效边界装载后再用新输出积分 25 µs。
新输出保持 100 µs，施加角预测到 125 µs 的作用中点，未把预测时间当输出延迟。

最终默认双节拍执行共 30 组通过：保留旧 50 µs 的 13 组，当前 100 µs 的 17 组。
覆盖完整启停与许可、真实 PI 输入周期、反馈／计算／提交失败、双编码器方向下的合成
正反目标跟踪，以及生效前撤销和漏边界拒绝。过期 pending 在下一 sample 进入 PI／stage
前先失效，不能由新提交覆盖后顺延。生效前 Stop／故障用异常早采样检验真实 Runtime
的撤销优先级；该注入不是正常 100 µs 采样的实现。

MATLAB R2021b 使用独立 `ode45`，对两种轨迹各取 4 段累计复算，均通过既定容差。
当前分段对象的最大 Id／Iq 差分别约 5.39e-9／2.72e-8 A；这是数值对象一致性，
不是传感器精度。故意将边界后的电压列清零时，复算按预期拒绝，避免忽略后半段仍误通过。
来源身份、模型配置及曲线见 [软件模型摘要](validation/evidence/slot-foc-model/summary.json)。

本次未连接或操作设备。三个正式 Target 的 HEX／AXF 与用户 Keil 设置逐字节保持；
没有新烧录或使能，也没有重跑既有示波器流程。合成参数、理想母线和边界替身不能替代
电流增益／方向实物对照、电角度标定或带电 FOC 验收。

## 同源控制计算与真实提交：无功率集成窗口通过

新增 Timing `compute-stage`（mode=3），在同一次 ADC 回调内，由同源 Runtime 的原提交点
直接进入真实 HCC。真实 stage 包含在直接调用计时中，不在 Runtime 返回后补交；合成
电流、12 V 母线、位置和标定仍留在隔离试具，正式 Observe 的运行许可保持关闭。

最终 HEX 为 `46b4bafd9237ed352641d1c0103f69cd578b5f509acbcbc98bf778aecd6ad526`。
仅 ST-Link 逻辑供电、OUTPUT 关闭，电池/三相/外部注入断开，编码器接线保持，操作者
本轮重新确认条件；DMM 2.7 V 继续沿用旧参考，没有新增人工测量。

| 一次 request7 的结果 | 记录 |
| --- | --- |
| 控制与提交 | 80/80 计算，真实 HCC 调用/接受均为 80，sink=enable=0 |
| 分支覆盖 | 72 普通拍、8 速度更新与观测刷新拍；31 拍限幅 |
| 直接调用（包含真实 stage） | 普通拍 5254–5314 cycles，30.906–31.259 µs；慢拍 6716–6721 cycles，39.506–39.535 µs |
| Platform 锚点至调用返回 | 最长 8347 cycles / 49.100 µs |
| IRQ 首末 C 标记 | 最长 10015 cycles / 58.912 µs；末拍对应绝对预算 10702 cycles / 62.953 µs |
| 两次后健康 | CPU running、采样推进，快照间隔 17000 cycles / 100 µs；faults=guard=0、电流/编码器有效、MOE=0 |

初次候选 `f89866d2…` 虽已 80 次真实提交全接受，末拍之后仍出现 Platform 时序锁存，
下一空拍冻结 NOT_READY，整窗判失败。双 ADC 同步仍成立、SSI 无传输故障；现有锁存未保存
首错指令位置，不能将推断写成精确定位。修订把末拍的统计聚合及覆盖核对一起移到下一
配对空拍，末拍仅保存固定 5word 时长和分类并立即关断。许可检查通过后才将确认点从
79 聚合为 80；提前失去许可或取消则保留 79 点和末拍原始读数，不追加第 81 次控制。
未改变 Platform 截止、静态时序或正式算法，未清故障重发旧窗口；最终候选一次请求通过。

Timing 全量及修订构建均为 0 错误、0 警告；Runtime ARM 35 组、入口 ARM 15 组及 Host
29 项通过，并用真实旧 AXF 核对新增枚举缺失时在设备连接前拒绝。Observe 和旧台架的
HEX/AXF/MAP 逐字节保持，未重跑其台架。身份、失败与成功窗口见
[集成证据摘要](validation/evidence/compute-stage/summary.json)。

当前板保留上述默认关断 Timing 镜像和冻结 COMPLETE。80 次 accepted 表示提交受理；
末拍关断取消其 pending，不能说全部 80 组已跨 UEV 生效。IRQ 标记未覆盖末标记后的
工作，有限窗口不是 WCET，也不是正式启停、真实反馈或带电 FOC 验收。
下一步继续电流增益/方向实物对照，再推进受控电角度标定和正式运行接入。

## 前次 20 / 10 / 1 kHz：分别完成无功率提交与计算窗口

已实现 20 kHz PWM、10 kHz 同步电流采样与计算、1 kHz 速度更新的装配。
RCR=3、首峰一次采样触发门及下一有效谷底的三相提交，共同定义 sample → compute →
actuation；完整 ADC 服务须在目标谷底前 10 µs 退出。机制及 ST 参考依据见
[PWM_TIMING.md](validation/PWM_TIMING.md#20--10--1-khz-时序)。正式 Observe 的控制许可仍关闭。

操作者本轮确认 ST-Link 逻辑供电、OUTPUT 关闭、电池/三相/外部注入断开，编码器接线
保持。DMM 2.7 V 沿用既有参考，本轮没有新增万用表测量。没有要求重跑历史示波器流程。

| 独立窗口 | 镜像与实际结果 |
| --- | --- |
| 真实 stage | `45f60bec…`：一次 request5，80 次调用全部接受，enable=0，前后健康通过 |
| 同源 Runtime compute | `817b52d3…`：一次 request6，80/80 完成；72 普通拍、8 速度更新与观测刷新拍，sink=80，真实 stage/enable=0 |
| compute 直接调用 | 普通拍 4696–4761 cycles，27.624–28.006 µs；慢拍 6219–6224 cycles，36.582–36.612 µs |
| compute IRQ 首末 C 标记 | 7998–9506 cycles，47.047–55.918 µs；最后一拍对应的绝对预算为 10711 cycles，63.006 µs |
| compute 后检查 | CPU running、采样持续推进，快照间隔 17000 cycles / 100 µs，faults=guard=0，电流/位置有效，MOE=0 |

本轮同时修复两处外围交错：SSI 改为私有有限 4 字节事务，最多产生 32 个 SCK，不再
在高优先级 ADC 尾部启动持续 RX-only 时钟；纯 regular ADC IRQ 不占电流 sample slot，
可跨 UEV，同一 HAL 分发中晚到的 JEOS 则按原首峰窗口重新认领。真实 ARM 模型已复现
并覆盖该 regular/injected 占用问题，真实旧 injected 未退出、迟到与过期请求继续拒绝。

第一个完整 compute 窗口虽发布过 80 点 COMPLETE，后健康仍出现 Platform 时序故障，
因此保留为失败。终态关断保持立即执行，将复核和 256 B 发布放到下一采样空拍，并先核
真实许可，不再追加第 81 次控制。随后修正 regular/injected 占用，最终一次 compute 的
前后健康均通过。没有清故障后重发同一窗口，也不把旧镜像失败记录改成通过。

Platform ARM 98 组、SSI ARM 41 组、真实 Core IRQ 9 组、Runtime 计时 25 组、入口 14 组
及主机计时工具 22 项通过；三个 Keil Target 均构建为 0 错误、0 警告。生成区与 CubeMX
输出核对一致，用户本地 Keil 设置保持。完整身份与原始统计见
[时序证据摘要](validation/evidence/slot-timing/summary.json)。

该轮结束时板保留 `817b52d3…` 的默认关断 Timing 镜像及冻结 COMPLETE，未发送任何使能请求。
两个窗口来自分别标识的镜像；compute 使用合成输入与数值 sink，不能据此宣称同一镜像
的完整真实 RUN 已验收。IRQ 读数仍排除末标记后的统计和异常入出栈，不是 WCET。
电流增益/方向、电角度标定、真实计算与提交的集成及带电 FOC 仍待针对性验收。

以下各节保留历史镜像及当时结论，文中的“当前”仅指对应那次验证，不代表板上最新状态。

## 独立调用计时：部分窗口，仍不具备带电实时预算

后续只做过一次 RAM 放置对比：算法与诊断对象原样重链接，将 8 个数学热段
（3888 B，含 veneer）复制到片内 SRAM 执行，未修改正式源码或 Keil 配置。
普通拍 9 个有效样本平均 27.565 µs，相比下述 Flash 基线的 28.074 µs 仅减少约 1.8%；
第 10 次计算的 IRQ 标记区间 8929 cycles（52.524 µs）触发 IRQ_BUDGET，SSI 位置也失效。
SRAM 代码已与 AXF 逐字节核对，但收益不足，这条试验已停止，没有继续调整放置方案。
该次试验结束时，板保留一次性对比镜像 `4fefda7b…` 及冻结失败状态，MOE=0、guard=0、采样推进；
该试验没有修改当时正式提交的三个 Target 配置及两个旧入口生成物。

专用 `BLDC_G431_Timing` HEX 为
`4bb96c712cf295df24c5316d247c525226ebdf22f3ba514019438449e0feb91d`。
每拍仅调用一次私有同源 Runtime，真实提交与纯计算分开测量，没有重复执行 Observe
的 OFF Runtime。PWM、ADC、SSI 配置、1700 counts 截止及原 25 µs 保护均未改变。
操作者重新确认纯 ST-Link 逻辑供电、OUTPUT 关闭，电池/三相/外部注入断开，SSI 接线
保留；DMM 2.7 V 沿用旧参考，本轮没有新增万用表测量。

最初镜像下载后位置许可无效，因此没有写测试请求。只读数据出现 SPI OVR；计时入口
空闲时仍在 IRQ 尾部查询保护，去掉这段重复查询后，新启动的位置前检恢复有效。
该修订仅影响 Timing 条件分支。末端实时保护检查在活动窗口中保留，失败拍也先保存
本拍 IRQ 差值再冻结，防止与上一成功拍读数混用。

最终镜像只发送一次 stage 请求和一次独立 compute 请求，两者均未重发：

| 测量 | 实际结果 |
| --- | --- |
| stage-only | 首拍真实调用 1、接受 0、enable 0；原截止正确拒绝 |
| 该次被拒绝的 HCC 调用 | 603 cycles / 3.547 µs，不能代表成功事务成本 |
| stage 前诊断段 | 1694 cycles / 9.965 µs；原锚点至返回共 2297 / 13.512 µs |
| stage 原窗口 | 入口 CNT=3829，距截止 2129 cycles / 12.524 µs；返回 CNT=1413 |
| compute-only | 完成 13/80 拍：12 普通快拍、1 速度拍；sink 13、真实 stage/enable 均 0 |
| 普通快拍直接 Runtime 调用 | 4713–4778 cycles / 27.724–28.106 µs |
| 速度更新拍直接 Runtime 调用 | 5394 cycles / 31.729 µs，仅 1 拍 |
| compute IRQ 首末 USER 标记 | 7816–8451 cycles / 45.976–49.712 µs，仅已记录的 13 拍 |

compute 在下一次执行前因真实位置许可失效而冻结 NOT_READY，未完成观测刷新或限幅
覆盖。后检查仍为 CPU running、采样时基推进、入口 faults=0、独立 guard=0、MOE=0，
但位置与诊断许可无效。只读 SPI handle 的 ErrorCode=4，即 HAL_SPI_ERROR_OVR。
该附加测量负载下 SSI 服务尚不稳定；没有把 13 拍写成完整 80 拍通过，也没有重启掩盖后态。

直接调用区间不含模板复制、合成输入和额外 stamp，但仍有调用及 DWT 开销，compute
使用数值 sink，不包含真实 HCC 成功提交。IRQ 区间包含 HAL、健康检查及 Platform 尾部，
排除末标记后的统计、终态发布和异常返回，不能据 49.712 µs 宣称整个 ISR 小于 50 µs。
诊断前段约 9.7–10 µs 也不能冒充正式 RUN 的固定开销。即便移除这段诊断，已测纯计算
自身仍超过 25 µs 半周，不能靠删掉 10 µs 预留证明当前整链能及时提交。

新增模块 ARM 模型 20 组、入口集成 13 组、Host 替身 21 项通过；已有 Observe 65 组与
Platform 87 组通过。三个 Target 原生构建均无错误警告，最终 Timing 修订增量亦无错误
警告。Observe HEX 仍为 `6073e8c7…`，旧台架仍为 `efdc97e3…`，两者与原基线逐字节一致。
Timing 最终加载 45728 B，RW+ZI=8984 B（含栈）。没有要求重跑旧波形。

[窗口原值、镜像与源码身份](validation/evidence/runtime-timing/summary.json)保留完整归属。
上述测量结束时尚未改变采样/提交相位或进入带电 FOC；后续采用的时序设计见本页顶部链接。
SSI 的有界接收与错误收尾仍须独立验证，不能由新的名义计算窗口代替。

## 热路径速度优化后的提交预算：仍未通过

当前 Observe HEX 为 `6073e8c75c0c71815946fcfdaca97be317c4e3aea10bbb38903a977f22972a86`。
只调整 Observe 的 Control、Motor、Runtime 三个组为 ARMCC `-O3 -Otime`；生成区、HAL、
Platform、旧台架配置、浮点模式、算法公式和拒绝条件均未改。实际链接对象确认速度优化
生效，六个算法对象不再保留独立的 `__ARM_isfinitef` 函数；正式/私有 Runtime 均为 3092 B。
源码 pragma 不能完整替代本机翻译单元内联策略，因此没有保留该试验或自定义浮点分类。

优化前曾仅在私有同源 Runtime 加六个临时 DWT wrapper，单次普通拍读得：双角更新共
1558 cycles（9.165 µs）、电流处理 555（3.265 µs）、电流控制器 2755（16.206 µs），
stage 诊断端口 715（4.206 µs）；速度与位置估计均未调用。stage 行包括边界读取和真实
HCC 拒绝；这些是插桩包围读数，不是函数净耗时，不能相加推算无探针整链或 WCET。
临时源和产物只保留在本地证据；当前镜像已撤掉全部六个 wrapper。

操作者重新确认仅 ST-Link 逻辑供电、OUTPUT 关闭、电池/三相/外部注入断开、SSI 接线
保留。下载前核对旧 Flash、健康 IDLE、MOE=0 和推进的采样时基；DMM 2.7 V 沿用旧参考，
没有新增万用表测量。新候选下载校验后只执行一次 request1，无功率命令。

| 同类首拍读数 | 原无探针基线 | 速度优化无探针候选 |
| --- | --- | --- |
| 原锚点→stage 返回 | 8260 cycles / 48.588 µs | 6457 / 37.982 µs |
| step 入口→stage 返回 | 7103 / 41.782 µs | 5342 / 31.424 µs |
| 原入口 CNT / 实际可用窗口 | 3771 / 2071 cycles | 3770 / 2070 cycles（12.176 µs） |
| stage 调用 / 接受 / enable 尝试 | 1 / 0 / 0 | 1 / 0 / 0 |

原锚点读数本次减少约 21.8%，只对应这两次有限首拍，不代表稳定加速比或最坏时间。
新返回点 CNT=2661、CR1=161，已处于随后上数半周；CNT 高于截止值不表示仍在原窗口。
原 HCC 继续拒绝迟到提交，80 拍尚未完成，速度/观测刷新分支没有新增实机覆盖。
真实后态仍为 CADENCE(4)、guard=0、MOE=0、CPU running、采样推进，软件 VBUS 约
2.634 V；故障清理与真实 OFF 观察在上述返回点之后，读数不能据此冒充完整正式 RUN ISR。

全量 Observe 构建 0 错误、0 警告，实际加载 77052 B（未压缩 77392 B），RW+ZI=25864 B
含 4 KB 栈；静态最大调用链 760 B 仍含未知回调，不是最坏栈证明。旧台架配置逐项保持，
未要求重跑未受影响的波形项目。速度策略下算法 103 组、Target 65 组、FOC Runtime 13 组、
提交诊断 15 组通过；另有 5554 组 Ospace/Otime 控制器输出/状态/积分逐位一致，以及
SVPWM 9942 对既有参考比较。有限对照不等于全浮点输入等价证明。

[当前读数、编译策略和源码身份](validation/evidence/hot-path-budget/summary.json)保留证据归属。
主要成本确属当前控制链，优化后仍无法在原窗口完成；下一步应评估关键路径的计算与提交
组织，明确延迟和采样假设，再选择有针对性的改法。没有放宽原截止、改变 PWM/ADC 时序，
也没有进入带电 FOC。

## 正式同源控制链提交预算：初次基线未通过

本节基线 Observe HEX 为 `2a3ad80ad0363904cf93245e8aed4e29b6c7386d0a4e5e64cbc2c2f1e0a57510`。
本轮收敛 Runtime 的重复 Park，并将两个角统一接到实际 CORDIC；控制器仅对本拍已验证、
归一化的局部相量省去重复检查，公共 API、向量有限性、逐拍配置、限幅与积分检查保留。
5554 对新旧控制器输出/历史逐位一致，代表性 ARM 执行轨迹 1305→855 条指令；指令数
不换算成实机周期或整链加速比。真实电机标定、控制和功率许可继续关闭。

独立诊断试具通过符号重命名编译同一 Runtime，真实执行 ADC 上下文、Platform 原锚点、
CORDIC 与 HCC stage；合成电流、12 V 母线、位置及 RUN 状态不进入真实业务快照。
无控制器内部探针。操作者本轮重新确认仅逻辑 3V3、OUTPUT 关闭、电池/三相/外部注入
断开，保留编码器；DMM 2.7 V 沿用旧参考，本轮软件 VBUS 约 2.659 V。

首轮 `aaadc166…` 的准备开销导致真实 ADC cadence 故障，未执行 stage；试具构造随后
移到启动采样前，ADC 准备拍只复制固定模板。只读确认该旧故障、原镜像、持续采样和
MOE=0 后，下载校验修订候选并软件复位；没有清故障命令或重发旧窗口。

修订候选仅一次 request1 即在首个计算拍触发真实提交拒绝，未完成计划的 80 拍：

| 证据 | 读数或结论 |
| --- | --- |
| 原 callback 入口 CNT | 3771；截止 CNT 1700 |
| 本拍原锚点允许的前段窗口 | 2071 cycles，约 12.182 µs |
| 原锚点→stage 返回 | 8260 cycles，约 48.588 µs |
| 诊断 step 入口→stage 返回 | 7103 cycles，约 41.782 µs |
| 真实 stage 调用/接受/enable 尝试 | 1 / 0 / 0 |
| 返回点 CNT / CR1 | 4039 / 177；已跨过原窗口，不能因当前仍为下数半周而接受旧请求 |
| 诊断首因 / shadow Fault | STAGE / ACTUATION(1024) |
| 真实后态 | CPU running、持续采样、Motor FAULT_CADENCE(4)、独立 guard=0、MOE=0 |

原截止条件正确拒绝了迟到请求，未放宽 deadline、改变 PWM 周期或开放使能。失败结果
重复读取不变，后健康检查保留真实故障；不将“安全拒绝正确”写成“实时预算通过”。
本拍尚无速度更新/观测刷新，80 拍的其他分支只有模型覆盖，没有实机完整窗口证据。
上述读数包含诊断分派和边界读取，属于这次失败的观测，不是无探针 WCET。

Observe 全量及修订构建均无错误警告，MAP 未压缩加载大小 68032 B、实际加载 67692 B，
压缩 RW 的物理字节与 HEX/AXF 一致；RW+ZI=25864 B，含 4 KB 栈。旧台架独立重建
无错误警告，HEX 仍为 `efdc97e3…`，未烧录。离线模型通过：数学13、电流控制23、
SVPWM比较7、FOC Runtime13、Target集成64、提交诊断15、Platform87组；主机stage20、
计算32、电流27项测试。它们不替代未通过的实际截止。

[失败读数、候选及源码身份](validation/evidence/stage-budget/summary.json)与
[执行边界](runtime/README.md#无功率提交预算诊断)保留测量归属。下一步应先定位整链
41.8 µs 计算段的主要成本，再决定计算组织与采样/提交安排；当前没有进入带电 FOC 的
时序依据。电流增益/极性对照可独立准备，既有未受影响台架证据继续有效。

## 控制器分段与被动 IRQ 计时（历史基线）

本节历史 Observe HEX 为 `ffa15851a36623230401a6688cc85572970a606cd41080219534feaa17e474cd`。
本轮在操作者确认的纯逻辑供电条件下，只做后台诊断；每个候选均先核对旧镜像与实时健康，
NORMAL SWD 下载校验后，各提交一次请求。基线 `faa25d02…` 与优化版均完成 192 次算例、
257 点全周对照、控制器 128 次有效/32 次拒绝及后续 256 点 ADC IRQ 窗口，冻结复读一致。
前后 CPU running、无故障 IDLE、MOE=0，时基持续推进；没有功率命令或示波器复测。
沿用前轮 DMM 参考 2.7 V，本轮没有新增万用表测量；软件 VBUS 约 2.646～2.654 V。

控制器七个边界区分校验、角度/Park、PI、调制、回算与提交。按 170 MHz 配置换算，
以下 clean 仅表示已覆盖的异常进入计数未变化，仍含插桩开销：

| 分段 | 基线 clean 均值 [µs] | 优化后 clean 均值 [µs] | 样本数，基线 / 优化 |
| --- | --- | --- | --- |
| 输入校验 | 4.639 | 4.634 | 68 / 98 |
| 相量准备与测量 Park | 6.277 | 5.994 | 73 / 65 |
| PI 与逆 Park | 3.686 | 3.667 | 102 / 79 |
| SVPWM | 7.976 | 5.709 | 60 / 76 |
| 施加 Park 与积分 | 5.260 | 5.232 | 63 / 86 |
| 输出及历史提交 | 2.259 | 2.245 | 99 / 102 |

SVPWM 的五次 `fmaxf/fminf` 库调用改为有限值直接比较，本轮该段 clean 均值约减少
28.4%；前后 clean 范围为 7.935～8.112 / 5.671～6.024 µs。原算术、限幅和失败语义
保持，9942 对新旧实现输出逐位一致。其他行的 clean 输入集合和代码布局会变化，不将其
均值变化归因于算法优化；整段控制器没有 clean 样本，不能相加各行或给出整链净加速比。

IRQ 窗口在后台算法冻结后才开启，避免统计开销叠加。优化版 IRQ USER CODE 区间为
256 点，均值 19.727 µs、最大 30.771 µs；其中 226 次包含 injected 回调，既有锚点到
consumer 均为 2.329 µs，consumer 与 Observe 嵌套均值分别 13.695 / 12.624 µs。
这些是控制关闭且带插桩的观察路径，包含抢占，排除异常入栈/出栈和末尾统计；不是完整
ISR、RUN 路径或 WCET，不证明约 15 µs stage 前段预算，实际控制继续关闭并使用软件后端。

前期未完成候选没有计作通过。并发采集造成的观测开销叠加通过顺序窗口消除；另一次
CORDIC 拒绝与普通异常清除 exclusive reservation 的证据相符，独占改为最多四次有限
尝试，遇 busy 仍立即拒绝，外设失败隔离不变。真实 ARM 指令测试覆盖 reservation 丢失
和持续失败上界，修订候选完整通过；现场记录不足以唯一确认前次拒绝的根因。

Observe 全量构建 0 Error / 0 Warning，加载 61908 字节，RW+ZI=23984 字节（含 4 KB 栈）。
静态主链 864 字节且含未知回调，不构成最坏栈证明。旧台架重建同样无错误警告，HEX
仍为 `efdc97e3…`，没有烧录。完整身份与验证见本轮
[统计、源码身份和限制](validation/evidence/controller-profile/summary.json)。执行接口见
[runtime 说明](runtime/README.md#后台无功率计算计时)。下一步应以无探针正式流水线核对
stage 前段预算，优先处理已量出的校验、相量准备及回算成本；保留电流增益/极性与电角度
实物确认门槛，不因这次局部提速开放功率。

## 软件与 CORDIC 无功率对照（历史基线）

本节历史 Observe HEX 为 `67fcc9d8636093603a2118f65d06838f33a5f4af70ecb09c4a246971e6e42a72`。
同次 HEX/AXF/MAP 的 60296 个加载字节和真实 DWARF 布局核对通过；Observe 与旧台架
均为 0 Error / 0 Warning。新增后台调用链静态深度 856 字节且存在函数指针边，原 1 KB
栈余量不足，因此由 `.ioc` 改为 4 KB 并用本地 CubeMX 重新生成 startup。
两 Target 共享这项栈配置，旧台架 HEX 因而变为 `efdc97e3…`，未烧录旧台架。
Observe 的 RW+ZI 共 22864 字节，包含 4 KB 栈；静态调用图不构成最坏栈证明。

操作者本轮重新确认仅 ST-Link 逻辑 3V3，OUTPUT 关闭，电池、三相和外部电流源断开，
保留 SSI。万用表参考沿用前轮的 2.7 V，本轮软件 VBUS 约 2.653～2.654 V。
只读前检核对板上旧 `b171488b…` 镜像和持续健康状态，NORMAL SWD 下载校验后软件复位。
对新镜像仅写一次 request：软件与硬件各六例、各 16 次，合计 192 次计时，随后完成
257 点全周扫描。冻结结果重复读取不变；前后 CPU running、IDLE、Motor/guard faults=0、
MOE=0，时基持续前进，未发送功率命令。

两后端使用相同输入与各自独立的积分历史。完整电角度路径包含预测、校验、三角后端及
边界测量开销，以下仅列已覆盖异常进入计数未变化的 clean 样本，按 170 MHz 配置换算：

| 路径 | 软件 clean 均值 [µs] | CORDIC clean 均值 [µs] | clean 样本数，软件 / 硬件 |
| --- | --- | --- | --- |
| 普通算例的测量电角度 | 7.135 | 6.224 | 9 / 9 |
| 双角算例的测量电角度 | 7.131 | 6.230 | 8 / 8 |
| 双角算例的施加电角度 | 6.920 | 6.144 | 7 / 7 |

全周最大 sin/cos 分量差分别约 `1.01e-6 / 1.19e-6`，硬件单位圆平方范数误差
`2.15e-6`；正式角度路径配对误差 `1.13e-6`，三相 duty 最大差 `6.56e-7`，均通过
本轮 `2e-5` 诊断门限。这是软件与硬件数值对照，不是实物角度或电流精度标定。

有效控制算例的 total 和电流控制段均没有 clean 样本；不能据此给出纯算法整链耗时或
整体净加速比。普通算例电流控制段的含抢占均值为软件 40.300 µs、硬件 42.930 µs，
这两个读数不能用于判定后端退化。新分段测量的额外开销也使本轮 total 不能直接与下节
旧仪器化结果作性能回归比较。现有证据说明 CORDIC 可减少部分角度计算耗时，尚不足以
证明约 15 µs stage 前段预算；下一步应细分电流控制和实际 ISR 路径，再决定优化。

实际控制仍使用默认软件后端、`control.enabled=false`，Start 仍拒绝；本轮不开放 FOC。
[对照摘要、完整统计与源码身份](validation/evidence/cordic-comparison/summary.json)保留双后端
各段总样本、clean 样本、IRQ 进入数及误差。执行接口和限制见
[runtime 说明](runtime/README.md#后台无功率计算计时)。离线检查通过：电角度 15、计时
19、硬件适配 9、Target 集成 56、直接 FOC Runtime 模型 8 组；主机对照工具 24、
共享电流工具 26 项替身测试。外设替身不代替上述实体精度与 cycle 读数。

## 后台计算计时与有限定向核心（历史基线）

本节历史 Observe HEX 为 `b171488b619911d963ba1f2a80bdf52a1baabd1b1ccbdd11e2c12123beb2b302`，
全量 Rebuild 及最终构建均为 0 Error / 0 Warning，同次 HEX/AXF/MAP 的 48580 个加载字节
与真实类型布局已核对。旧台架独立 Rebuild 同样无错误警告，HEX 仍为 `8b7fc787…`。
下文各节保留各自历史镜像、源码身份与验证范围，不转写为本节的新实测。

操作者确认 OUTPUT 关闭，电池、电机三相和外部注入源断开，仅 ST-Link 逻辑 3V3，
编码器保留原接线；VBUS 万用表读数 2.7 V。只读前检确认旧 `c151e560…` 镜像、IDLE、
无故障和 MOE=0，随后下载并校验新镜像、软件复位。没有连接电机或发送功率命令。

对新镜像只提交一次后台计时请求。独立合成算法状态完成六例各 16 次，96 次统计冻结且
重复读取不变；前后 CPU running、IDLE、Motor/guard faults=0、MOE=0，控制时基持续前进。
软件 VBUS 约 2.663 V，与本轮万用表读数相容；逻辑供电回供到母线的来源仍未确定。

以下按 170 MHz 配置换算，均包含中断抢占和计时调用开销：

| 后台算例 | 最小 / 平均 / 最大观测耗时 [µs] |
| --- | --- |
| 正常计算 | 55.924 / 66.627 / 101.647 |
| 速度输出保持 | 51.488 / 55.961 / 97.806 |
| 独立施加角 | 60.312 / 73.950 / 126.306 |
| 电流与电压限幅 | 56.012 / 63.881 / 101.976 |
| 坏电流预期拒绝 | 2.765 / 5.658 / 21.041 |
| 坏母线预期拒绝 | 17.012 / 30.564 / 86.535 |

[精选摘要与源码身份](validation/evidence/compute-probe/summary.json)保留计数和原始 cycle 统计。
这些观测不能区分纯算法与抢占耗时，不构成完整 ISR、WCET 或约 15 µs stage 前段预算
通过的证据。实际控制许可继续关闭，后续先分段定位计算与抢占开销，再验收完整实时路径。

离线验证：计时模块 14、Target runtime 50 组 ARMCC / Unicorn 行为检查与主机工具
18 个替身测试通过，包括 Stop 被 ADC 抢占消费、母线越界立即撤销许可、旧快照/心跳拒绝，
以及最后一步计算后撤销完成结果。

另外新增 Motor 内部有限静态定向核心，固定 αβ 电压缓升/保持，独立机械位置窗口收敛后
仅给出双方向 offset 候选。15 组正式 ARM C 检查通过，含独立理想转子、跨零、重复位置、
堵转及零序公共模反例。它尚未装配到实体执行路径，没有实际定向；方向、极对数、相序及
电流极性仍须独立确认。完整语义见 [Motor 说明](../../app/motor/README.md)。

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
