# G431 实现与验证边界

当前交付是固定 G431 板、2804 电机及 MT6701 原接线下的有限空载 FOC 控制基线。
正式工程 `MDK-ARM/BLDC_control V1.uvprojx` 只保留 `BLDC_G431_Run`；观察、计时和旧台架
位于 `MDK-ARM/BLDC_G431_Validation.uvprojx`，共享核心源码。构建、模块职责和资源归属见
[runtime/README.md](../runtime/README.md)，历史控制基线的UART / VOFA操作见
[Host 说明](../../../../communication/host/vofa/README.md#有限-run-操作)。

当前Run已接入原生FreeRTOS、460800/8N1 UART DMA及Core Protocol事务Server，关闭旧VOFA装配；
对象、请求应答及50/250Hz遥测已有逻辑供电证据；本阶段按250Hz完成通信验收。
500Hz仍未达原门槛，作为容量边界保留。修正前的协议＋FreeRTOS镜像已有一次正向有限启停
调速及两条状态变化Event证据；当前Stop/栈修正版完成无功率UART联调，不重做带电P7。
故障Event及复杂竞态仍限于模型。旧实物记录继续绑定各自HEX。

`validation/` 台架经 HCC 使用 Platform，不是正式 Motor / ControlRuntime。
旧功率包与保护注入要求电机三相断开；有限开环仅按[专用流程](OPEN_LOOP.md)接电机。
本页是证据索引，不是待执行的带电操作队列。下方逐次记录中的“当前”“下一步”只指当时的镜像和阶段。

本次合并准备收敛为协议分层、同一 Run 的 UART/CAN 静态选择、CAN 内部回环、控制配置归位
及已完成的有限 FOC 调优与诊断证据。原任务 P6 的最终速度性能和重复性验收仍未通过，
留待后续处理，不将本次收尾记为原任务全部 PASS。外部 CAN 物理总线、电流绝对精度、
持续运行和完整 WCET 不在本次已证明范围；默认仍为 UART，全部原有安全边界保持。
[合并前离线检查](evidence/protocol-can/merge-preparation.json)复核 UART 黄金字节、
UART/CAN 事务及 Host 模型，并以干净源码副本补全最新 CAN Normal 构建；未新增硬件动作。

## Showcase Stage 2B：Telemetry 所有权离线收敛

以 `0079cef` 为基线，SystemRuntime拥有协议无关的单消费者订阅及原单槽Mailbox。
Telemetry只读Runtime配置、MotorTelemetrySample和内部订阅，不再查询Communication epoch。
Server经Device Port及保留的Target测试缝begin/replace/end订阅，独占Session和线上序号。
复制期间发生替换或end时，Producer在发布前丢弃旧副本，不推进产生计数、不计覆盖。
同Session幂等OPEN保留订阅与序号；失效/内部Fail-closed/CAN Bus-off结束订阅并清旧槽。

UART保留首次发送前的覆盖缺口、同值BUSY只编码一次、TX接受才退休、终态/Event优先级、
generation切换后的序号单调及耗尽规则。overwrite仅计同订阅同generation内的覆盖。
CAN Motion仍取实时sample_sequence低16位并自然回绕；恢复后无Session且不重发旧Motion。
正式名称收敛为`motor_telemetry.h / MotorTelemetrySample / target_runtime_read_motor_telemetry()`；
`target_runtime_integrity_ok()`只表示内部首因为NONE，不是Session、Start、Motor故障或订阅许可。

修改前及纯改名后各8个入口通过，最终60个离线入口全通过；其中SystemRuntime 17组、
UART Server/Client 38组、CAN 22组，覆盖订阅切换窗口、首发缺口、BUSY、配置修订、
Bus-off/恢复、内部身份耗尽及不一致Fail-closed，并断言CAN先进入原关断端口再清理订阅。G431四Target与独立CAN Normal副本
均真实Rebuild，0 error / 0 warning。资源按同一基线源码及链接配置比较：

| Run构建 | Flash 基线→本次 | 静态RAM 基线→本次 |
| --- | --- | --- |
| UART | 66896→67016 B（+120） | 21088→21104 B（+16） |
| CAN Normal | 60736→60872 B（+136） | 17792→17800 B（+8） |

已知任务调用深度保持：Bootstrap304 B、Telemetry408 B、Idle40 B，Communication为
UART1224 B／CAN504 B。静态报告仍有汇编/间接调用未知量，不代替真实任务栈余量验收。

[本轮离线证据](evidence/showcase-v1/stage2b.json)与[UART/CAN源码身份](evidence/showcase-v1/stage2b-identity.json)
记录最终构建、资源和已知静态调用深度；最终HEAD通过包含证据的Git检查点定位。
未连接硬件、未烧录、未运动，不产生新实物证据。原P6未通过、500Hz损失1.92%的原失败、
外部CAN物理总线及实机调度/WCET未证明范围保持；旧HIL仍绑定原镜像。

## Showcase Stage 2A：Runtime 单路径离线收敛通过

以 `a0537f0` 为基线，正式 `target_runtime.c/.h` 已删除旧 pending command/reject/stop 状态、
`take_command()` 和旧 `submit_command()` API；命令、参数与 Stop 只经过 RuntimeExchange。
TIM6 时限仍先 safe-off，再直接请求 internal STOP，由 ADC 消费、取消旧请求和生产 Completion。
Exchange 公共实现及语义未改，内部 Stop 不生成伪外部完成，已排队外部 Stop 保留自己的 ticket。

旧同名 API 仅保留在 Observe 和[无状态模型适配器](tests/runtime/legacy_command_adapter.c)，
其声明位于 [Validation 头文件](communication/legacy_command.h)，不进入正式 Run。
有限 Run 模型的旧输入改为构造 RuntimeRequest，拒绝/BUSY 不再伪装成一条生产 Motor 命令。
Observe 的旧拒绝事件行为保持。两个本地辅助文件已退跟踪并精确忽略，恢复与验证不要求它们存在。

修改前 8 个受影响入口通过；修改后 12 个入口通过，其中 Exchange 为 22 组、有限 Run 为 12 组、
Observe 为 46 组。新用例检查真实 ARM 写入的 safe-off/PRIMASK 顺序、两种内外 Stop 到达次序、
取消参数写、准确 ticket、无伪 Completion 和旧运动意图不重放。UART/CAN 事务、黄金向量及控制模型通过。
G431 Run、Observe、Timing 和旧台架均全量 Rebuild，0 error / 0 warning。

| Run 资源 | 基线 | 本次 | 变化 |
| --- | --- | --- | --- |
| Flash 实际加载 | 67080 B | 66896 B | −184 B |
| 静态 RAM | 21104 B | 21088 B | −16 B |

最终构建身份 `0x26bd6622c925ee7e`，见[离线证据](evidence/showcase-v1/stage2a.json)及
[源闭包](evidence/showcase-v1/stage2a-identity.json)。本次未连接硬件、未烧录、未进行电机运动，
不产生新实物证据；旧镜像归属、原 P6 未通过及 500 Hz 1.92% 失败记录保持。

## Showcase v1 目录归位：离线验证通过

从合并基线 `4a9d20528b777ba97b65286cc66f9fcb34f85175` 在 `stage/showcase-v1` 完成结构迁移。
公共 Control / Motor / Platform、共享通信以及 G431 Runtime / Communication / Validation 已归位；
Run / Observe 按原有编译条件拆开入口，共享原 Motor / ControlRuntime 和算法。
正式 Run 的旧裸机 VOFA 模型适配已移到测试夹具，控制与保护执行语句保持。

60 个现有离线入口全通过，随后复核 3 个 Runtime 入口和 16 个工具入口；后两组是受修改影响的重复检查，
不另算独立模型。G431 四个 Target 与 F405 M0 全量 Rebuild 均为 0 error / 0 warning，另以独立源码副本完成 CAN Normal 构建。
源标记对照、工程成员/include、Python syntax/import、文档链接与锚点核对通过。
316 份供应源码、IOC、历史 JSON/CSV/SVG 及黄金向量逐字节保持。

最终 UART 构建身份为 `0xcb04339e37c61b1c`，CAN Normal 为 `0x310470ba9c1ff193`；
两者均仅离线构建，未安装或新增实物验证。旧实测继续绑定原镜像，500 Hz 的 1.92% 失败记录及原 P6 未通过结论保持。
见[结构迁移证据](evidence/showcase-v1/closure.json)及[最终 UART 源闭包](evidence/showcase-v1/uart-identity.json)。
新的代码导航从 [Target README](../README.md)进入；原有诊断解释见[历史说明](history/runtime-diagnostics.md)。

## 协议拆分与CAN静态接入增量

[P0基线](evidence/protocol-can/p0.json)核对已合并UART源闭包、冻结镜像与旧模型来源。
[P1迁移](evidence/protocol-can/p1.json)将UART实现及字节流合同移入顶层`communication/protocol/`；
原七份生产源码/头内容不变，Codec 12组、Server/Client 34组、DMA 11组通过，四个Target
全量构建均零错误零警告。三个Validation HEX与基线完全相同；Run的Flash 66648 B、静态RAM
21184 B不变，可装载内容仅build ID的8字节常量改变，其余逐字节相同。
本增量未连接设备或烧录，无新增UART实物、CAN或调优证据；旧实物记录仍绑定原镜像。

[P2共享绑定](evidence/protocol-can/p2.json)将业务编号/结果/设备值与Device Port置于
`communication/protocol/device/`，Firmware负责RuntimeRequest/Completion规范化，Target只接本机端口与
UART统计。Codec 12组、Server/Client及真实绑定36组、实时交接19组、静态RTOS 15组通过；
纯协议正式源只使用`communication/protocol/`头目录编译。Run全量构建零错误零警告，Flash 67320 B
（增加672 B转换代码）、静态RAM仍21184 B；Communication已知静态调用深度仍1224 B，
不代表新镜像栈实测。UART黄金字节/Host Codec不变，控制/保护/RTOS/DMA源码未修改。
本检查点无硬件操作，CAN与调优尚未验证。

[P3 CAN模型](evidence/protocol-can/p3.json)按确认后的Node 1～127映射，验证763个ID
互不重叠、28份人工黄金帧与19组ARM Codec/Server/真实绑定模型；覆盖Stop重入、Session替换、
优先队列、Bus-off撤会话与恢复前提。UART 12组/47924断言和Server 36组回归通过。
Run全量构建零错误零警告，Flash/RAM与P2相同；可装载内容仅build ID的8字节改变。
此时CAN尚未链接正式Run，控制器、内部回环和实物均未验证；旧实物证据不据此改写。

[P4双静态构建](evidence/protocol-can/p4.json)在同一Run选择UART或CAN；两个Profile
均全量构建零错误零警告，最终恢复UART。链接检查证明未选硬件句柄/Server/缓冲区及拒绝占位
均移除。真实HAL帧端口9组、UART DMA 11组、UART Server 36组及RTOS模型通过。
UART Flash/RAM为66664/21088 B，CAN为60504/17792 B；任务栈分配未改。
500 kbit/s、过滤器及恢复流程已有离线证据，尚无FDCAN控制器内部回环、实物调度或外部总线证据。

[P5内部回环](evidence/protocol-can/p5.json)在冻结专项镜像上通过26组无功率实测：
实际TX Message RAM、控制器回环、FIFO/IRQ、唯一Communication、CanServer及Runtime ticket/
Completion贯通。24个注入帧中6个被硬件过滤，18个进入RX；16个应答匹配，DLC/reserved非法帧
未获业务身份。Start忠实返回NOT_READY，普通重复不重交接；无会话Stop完成后相同帧再次执行，
会话和普通水位不变。共4317个硬件TX全部完成，RX lost为0；通过轮次末态IDLE/目标0/MOE0/六LOW，
Motor/Guard/Internal均0。通信/遥测栈最低635/272 word，满足各自25%余量要求。

首轮诊断整块读取FDCAN寄存器触及保留地址，触发ARA（0x00800000）并关断通信，尚未发测试帧。
保留原故障记录；脚本改为只读有效寄存器，经操作者RST后同一固件完整通过，不放宽错误路径。
正式IOC保持Normal，专项代码在默认构建完全移除；默认UART镜像已恢复到板上并独立全Flash核对。
不证明TCAN3414DR、CANH/CANL、终端、真实ACK/仲裁/外部Error Passive/Bus-off、双节点或CAN运动。

## 当前能力与证据矩阵

[原始双 ADC 供电对照](evidence/foc-tuning/raw-adc-analysis.json)保存同一诊断固件
在逻辑 3V3 和 12 V 默认关断下各 512 对连续整数码，未发送运动命令。B/C 标准差从
1.15/1.11 码升至 3.19/3.20 码，相关系数从 0.026 升至 0.879；共同分量方差约增 14.66 倍，
差分底噪基本不变。12 V 本启动 UART 收发均 0、无会话，因此运动和活动遥测均不是该扰动
出现的必要条件。1 kHz 调度分相均值仅解释少量方差；具体供电/参考/地/模拟前端来源及
其对速度纹波的贡献仍未确定，不作为调优通过或硬件故障结论。
[原码曲线](evidence/foc-tuning/raw-adc-comparison.png)由保存 CSV 经 MATLAB 绘制，
统计、分相和 DFT 与 Python 独立复算一致。请求工具的两次写后核验报错也保留在证据中；
每轮仅写一次，随后双份完整冻结记录一致，未重发或自动复位补采。诊断只在编译宏启用时
占用 2108 B（含对齐的 RAM 增量 2112 B），不成为正式控制路径的长期负担。
采集完成后已撤去正式 Run 的诊断宏，离线重建零错误零警告；Flash 加载 66728 B、RAM
21088 B，除 8 字节构建身份外与 Ki600 正式镜像逐字节相同。未再次烧录；板上最后安装的
仍为采集用诊断镜像。当前源码保留请求字的诊断小修正只做 ARM 模型验证，不追认为旧镜像
已经实测；诊断构建来源及正常入口恢复身份分别记录在上述摘要中。

[前端与噪声投影分析](evidence/foc-tuning/frontend-noise-analysis.json)进一步按网表
核对同源 VREF+ 偏置与 ADC 参考的低频抵消关系；优先调查高频路径差异、Buck 轻载开关及
局部参考地/OPAMP 动态，而不是把任意 3V3 波动直接判作根因。TP10/TP12 名义约 3.3 V，
TP2/TP3 是放大前输入、零电流约 0.1014 V，内部放大后才约 1.623 V。
保存的 12 V 静态误差经当前两相重构/Clarke 等效为 α 约 7.09、β 约 1.04 名义 mA 标准差；
若其协方差在运动时不变，q 噪声强度有机械 14 阶起伏。这是条件模型，不证明速度均值的
14 阶因果关系。51.2 ms 短窗也不足以从频谱验证 40/60 rpm 的该阶速度纹波。
[模型曲线](evidence/foc-tuning/frontend-noise-model.png)及 Python/MATLAB 复算
均为离线分析，未修改固件或新增实测；具体模拟来源仍待有区分能力的定点测量。

[静态电源波形](evidence/foc-tuning/scope-observations.json)记录操作者在原 12 V
静态供电下的 DS100 测量：短地底噪报告约 1.52 mVpp，VREF+ 直流报告 3.31～3.32 V；
VREF+ 快窗约 24.4 mVpp，200 µs/格下 VREF+ 与 3V3 均约 30.4 mVpp，保留三张原始照片。
两个测点先后测量，不证明瞬时相关；自动测频不用于认定 Buck 工作频率，纹波不等于布局
缺陷、ADC 误差或速度误差根因。本轮只读镜像检查因未识别到探针而未完成，没有烧录、
复位或发送采集/运动命令；板上身份仍按最后安装记录，不冒充新鲜 MOE/镜像读回。

[电流低通与第三相路由评估](evidence/foc-tuning/current-filter-evaluation.json)
用两份保存原码各剔除相同前 128 点后比较 384 点。12 V 条件下，α 误差标准差从 7.229
名义 mA 经 1 kHz 离散一阶低通降至 4.059 mA，低频群延迟约 119.35 µs，200 Hz 附加
相位约 −8.43°；群延迟不是 CPU 执行时间。200/500/1000/2000 Hz 共八组序列与 32 个
频响点经 Python/MATLAB 独立复算一致，[比较图](evidence/foc-tuning/current-filter-evaluation.png)
为离线回放，不证明闭环裕度或速度性能。G431 只有两个 ADC；A/OPAMP3 到 ADC2 CH18，
与 B/OPAMP2 的 ADC2 CH16 共用 ADC，增加 rank 不等于三相同一采样孔径。A 的现有增益
占位值也未作为方向确认。该分析检查点的正式实现仍为 B/C 同步采样、无数字电流低通、5 ms 速度
低通；本轮未修改固件、PI、限流或采样时序。下一增量拟单独评估可旁路的 1 kHz 电流
控制反馈低通，保护继续使用原始校正相电流，第三相与增流另行对照；P6 性能尚未通过。

[1 kHz 控制反馈低通候选](evidence/foc-tuning/current-filter-candidate.json)已完成
离线实现、冻结、逻辑供电安装和一次有限运行。算法位于 `firmware/control/current_feedback_filter.h`，Runtime
只在 RUN 控制输入支路更新 alpha/beta；0 旁路、1 直通，首有效样本初始化，Stop/故障
清历史。原始校正相电流与快速过流、采样/提交时序、PI、限幅、保护及协议对象布局保持。
RUN 发布 PI 实际采用的滤后 DQ，IDLE/Stop 观测仍用未滤波电流。
FOC ARM 模型 47 组覆盖旧/新节拍、原码回放、旁路逐位一致、首样本/重启、原始过流、
坏采样/坏历史关断与合成正反跟踪；Run/Observe/交接模型另行通过，均不作为实物或 WCET
证明。四 Target Rebuild 全部零错误零警告；Run 实际 Flash 加载/RAM 为 67080/21104 B，较原正常
构建增加 352/16 B，构建身份 `0x137624c44b3f866e`。228 项保守输入闭包仅算法新头、
Runtime 源/头及 Profile 四项变化；独立离线 GDB 核对加载镜像系数、PI 和边界值一致。
[安装与默认关断](evidence/foc-tuning/current-filter-install.json)独立读回全部 67080 B
匹配，4096 点校零 READY，UART 身份与推进时间正确，运动请求为零，前后 MOE0/六路 LOW。
安装前探针连接失败和主机快照检查误判均保留原日志；未因此重复烧录或自动清故障。

[同参数有限对照](evidence/foc-tuning/current-filter-comparison.json)只执行一次
+40 rpm、约 1.2 秒 Stop；电流 PI 0.5/600、速度 PI 0.002/0.008 及全部原边界不变。
250 Hz 同一域连续 480 帧无丢失，通信错误增量为零；停机时无故障、首因为空、六路关闭，
前后完整 Flash 一致，操作者报告无异常。首次主机串口打开阻塞时尚未提交任何命令，
只读证明 Start/电机命令均为零；操作者重接串口并确认曾复位/重开电源后，重新前检才执行。

与原 Ki600/相同速度 PI 的历史单次记录相比，尾部 20% 速度峰峰值 21.75→17.64 rpm、
标准差 4.844→4.711 rpm，仍未观察到持续进入 ±5% 误差带。RUN 的电流反馈口径由原始
DQ 改为滤后 DQ，所见电流误差变小不能当成独立物理精度提升；±5% 仅沿用描述约定，
不替换任务书验收门槛。单点电流跟踪误差峰值反而
增大。不同启动及初始角度、仅一次有限窗口，不足以声明显著因果收益。对照曲线与导出
样本见[图](evidence/foc-tuning/current-filter-comparison.png)及
[CSV](evidence/foc-tuning/current-filter-comparison.csv)。相同 0.2～1.196 秒窗口按整数
控制时间筛选，14 阶角度分量幅度约 4.167→4.179 rpm，未被低通消除；仍不能据此区分
真实机械纹波与编码器周期误差。本次没有实际时序故障，
但不代替完整 IRQ/WCET、长期稳定性和独立转速验证；P6 速度性能仍为 FAIL。

| 能力 | 已有证据 | 当前限制 |
| --- | --- | --- |
| 默认关断与启动门控 | Run 逻辑供电校验；真实 ARM 装配模型覆盖反馈、校零及初始化失败 | 上电不自动启转；未就绪拒绝，校零失败不自动重采 |
| FOC 电流内环 / 速度外环 | 20 kHz PWM / 10 kHz 电流 / 1 kHz 速度；正式 Run 正反有限实测 | 固定标定和低速空载范围，电流单位为名义 A |
| UART 正向启动、设速、Stop | [140 帧及命令记录](evidence/runtime/uart-run.json)、[曲线](evidence/runtime/uart-run.svg)；检查点 `afc931b` | `+60→+40 rpm→Stop`；Stop 撤销功率，不保证主动减速 |
| UART 反向及 MCU 独立时限 | [203 帧及停机首因](evidence/runtime/uart-negative-limit.json)、[曲线](evidence/runtime/uart-negative-limit.svg)；检查点 `30e4a9c` | `−60→−40 rpm`，首 RUN 至 IDLE 约 1.4999 s；调速不续期，无主机 Stop |
| 命令交接与一致性观测 | Run / Observe 模型，正式 UART 实物记录 | 单普通请求槽、Stop 优先；最近结果不是逐请求可靠应答 |
| 保护与时序 | 原 Platform 保护实测、故障模型、独立守卫和正式 Run 停机读回 | 正式 Run 故障注入、2 圈触界及完整 ISR WCET 尚无实物验收 |
| 性能与长期能力 | 既有台架速度曲线和电阻量级交叉检查 | 原 15 rpm 峰值初筛失败保留；持续运行、带载、独立电流精度、持久标定和故障恢复未完成 |
| 自研协议编解码 | [协议合同](../../../../communication/spec/CORE_PROTOCOL_V1.md)、[P1 离线证据](evidence/protocol/p1.json)；真实 ARM C 与 Python、30 个黄金帧对照，已接入Run | 离线库验证与后续实物证据分开归属 |
| 内部命令与参数交接 | [P2构建和ARM证据](evidence/protocol/p2.json)、[P7有限实测](evidence/protocol/p7.json)；ticket、Stop取消、Completion4、PI Staged/Active | Stop取消/内部破坏等竞态主要由模型覆盖；P7只证明本轮有限请求生命周期 |
| FreeRTOS启动与时间域 | [P3启动证据](evidence/protocol/p3.json)、[P7有限实测](evidence/protocol/p7.json)；TIM7 HAL、SysTick Kernel、静态任务/Mailbox、默认关断 | 已覆盖名义通信与一次有限运动，不能替代WCET或最坏调度证明 |
| UART DMA字节流 | [P4构建、模型和实物证据](evidence/protocol/p4.json)；512 B RX、130 B单帧TX、460800回环、覆盖/帧错误恢复、冻结TX关断 | 字节流证据不代替Server/事务、50/250/500 Hz协议压力或运动负载 |
| Session/事务/线上对象 | [P5构建、模型和实物证据](evidence/protocol/p5.json)；七对象、参数去重/原结果重放、Stop/Clear、Python客户端/JSONL | 纯逻辑供电；本轮未发送Start/Update，无Telemetry/Event或协议压力验收 |
| Telemetry/Event/通信压力 | [P6初测](evidence/protocol/p6.json)、[TX优化及验收调整](evidence/protocol/p6-tx.json)；50Hz×10s及250Hz×30s零缺帧，按250Hz完成本阶段通信验收 | 500Hz×10s缺96/5001帧，与96次覆盖一致，1.92%仍未通过原<1%门槛；本轮无运动，Event变化仅模型验证 |
| 自研协议＋FreeRTOS有限闭环 | [P7实物证据](evidence/protocol/p7.json)、[457帧观测](evidence/protocol/p7-samples.csv)；同身份重复Start只启动一次，+60→+40 rpm→无会话Stop，225帧RUN全有效、零缺帧 | 固定空载正向单次；两条状态Event通过，故障注入、反向和独立时限触发未在本镜像复测，不作性能验收 |
| 最终工程与资源核对 | [P8本地收尾证据](evidence/protocol/p8.json)、[Stop/栈修正](evidence/protocol/stop-stack.json)；四个Target原生Rebuild零错误零警告，Run/Validation隔离，新镜像无功率重复Stop通过 | 新源码身份与旧P7分开；按修正范围不重做带电P7；推送、PR和合并未执行，原500Hz失败保持 |

表中旧UART正向及反向两次正式 Run 实物记录属于 HEX
`0e73f54e03ceaa9b21f3ae7cfc305c1ca50ecc9577613cc47f974d54fd2eee76`。
源码整理或新构建不会把旧证据改写成新镜像已实测。未受影响的功能证据继续保留；
受影响的命令交接、后台通信与实时路径分别用针对性检查确认，不重跑无关底层波形。
本页后续保留各轮测量条件、失败首因与镜像，读者不必依赖未分发的本地日志判断上述边界。

## Sessionless Stop及栈未测量值修正

sessionless Stop仅在途去重，不查写完成缓存，终态后完全相同的帧可以再次提交；BUSY拒绝
也不缓存。非0 Session的普通请求及Stop继续按原Session/Sequence/完整内容去重与重放。
Session、水位、单Stop槽及全部COBS/CRC/Header/Payload检查保持，未扩大Header或对象布局。
四个栈字段初始化为0xFFFFFFFF，测量后原样更新；通信状态映射保留真实0，不把它改为未知值。

P1/P2/P3/P5真实ARM模型分别12/19/15/34组通过，覆盖无会话Stop在途一次提交、完成后
相同帧再次完成、不改变当前Session，以及尚无Session、BUSY后重试和普通会话Stop缓存。
栈模型证明初始未知值、测量返回0及实际Target对象映射；没有在实物上故意耗尽任务栈。
四个Keil Target再次Rebuild均零错误零警告，三个验证Target的HEX与前次一致。

正式Run新build ID为`0xac7c5cfe4d3d6ed3`，HEX为
`9491e86beef0770a1c93d91ca83b9d0954e12d03b8bb360c41234439fe15f4a7`，
Flash装载66648 B、静态RAM21184 B；最终205项输入仅Server代码/头、SystemRuntime初始化和
Target通信状态映射4项变化，Motor/ControlRuntime/HCC/Platform/DMA及RTOS配置均未改变。

操作者确认OUTPUT关闭、退压后ST-Link独立逻辑3V3及串口条件。烧录与前后独立Flash读回
66648 B匹配；两次相同session0/Sequence/完整字节Stop均返回ACCEPTED，Motor调用0→2，
当前Session不变，后续普通请求有效，无自动重试。接受启动0，末态IDLE、目标0、六路LOW/
MOE0，guard/内部/时序首因0。初次关断快照记录后台三项栈尚未测量为0xFFFFFFFF；后续最小
剩余栈690/440/271/109 word已测量且超过25%。本轮没有Start/Update/Clear或带电P7。

[修正证据](evidence/protocol/stop-stack.json)与[P8](evidence/protocol/p8.json)
保存新源码身份、模型及UART读回；旧50/250/500Hz压力和P7继续绑定修正前HEX。
500Hz的1.92%失败记录未改写，也未将旧运动记录当作新镜像重新实测。

## 修正前的构建与隔离核对

正式Run及独立验证工程的三个Target均完成原生Rebuild，全部零错误、零警告。
本轮只核对构建、链接、资源及说明，没有修改固件源码、连接设备、烧录、复位或发送运动命令。

| Target | Flash装载字节 | 静态RAM字节 | 入口 |
| --- | ---: | ---: | --- |
| BLDC_G431_Run | 66628 | 21192 | Core Protocol＋静态FreeRTOS＋UART DMA |
| BLDC_G431_Observe | 79260 | 26480 | 裸机观察／VOFA |
| BLDC_G431_Timing | 48080 | 9616 | 裸机专项计时 |
| BLDC_control V1 | 57256 | 19488 | 裸机旧台架 |

链接产物核对三个系统异常向量与唯一强符号：Run来自原始Port，Validation来自裸机Core；
Run未链接旧VOFA、CMSIS包装、动态分配或临时诊断符号，Validation未链接Kernel/Port/新Server。
供应源码24文件逐一匹配来源哈希。直接调用图及显式绑定的sample/trip回调没有可达内核API；
未知间接调用、异常压栈和完整WCET仍不由静态调用图证明。

Run的HEX与P7一致，build ID仍为`0xfd763adb40fa8964`，可装载AXF区及MAP也相同；
AXF整体哈希变化仅在非装载`.comment`区，旧AXF与新AXF分别保留，不覆盖P7身份。
已通过的Codec 12组、Server/Client 28组、任务14组、交接19组及DMA 11组按记录的来源哈希
匹配后复用，没有将其写成P8重新执行；物理证据继续按P4/P5/P6/P7各自镜像归属。
本镜像压力窗口最小剩余栈690/438/254/109 word，P7运动为690/464/271/109，均超过25%。

[P8证据](evidence/protocol/p8.json)汇总P0–P7、四份最终产物、资源与边界。
P6按250Hz完成已确认的阶段范围，500Hz原门槛仍FAIL；本次只完成本地收尾，PR和合并尚未执行。
两份用户Keil本地设置保持未提交；Validation工程仅有调试器DLL差异，构建配置及成员与HEAD一致。

## 自研协议＋FreeRTOS：单次有限闭环通过

沿用P6 TX优化后的已安装镜像，未改固件、未重新烧录。操作者确认12.0 V/0.20 A、ST-Link
3V3断开且SWD/GND保留，原三相与编码器、固定空载、无外部注入，RST后静止、VOFA断开。
前检4096点校零READY、IDLE/OFF、六路LOW/MOE0且无故障，软件母线约12.009 V；
独立Flash在前后均读回66628 B匹配，build ID为`0xfd763adb40fa8964`，HEX为
`ec3a17d9c2ebc45b4c6c0e8d7f446c00318cf460e0063b51ec1e853a08b54890`。

新Session读回设备及状态，整组PI写0.001/0.004使Staged generation从1增至2，遥测250Hz。
仅发一次新身份Start +60 rpm，并立即背靠背重复完全相同的帧与sequence；约0.400秒改
+40 rpm，约0.900秒发送session_id=0的Stop。四条原始命令终态均按身份/subject核对，
两条Start终态内容一致；无自动重发、Clear、第二次新身份Start或反向。
MCU accepted_starts仅0→1，Motor调用计数0→3，恰为Start/Update/Stop。

完整记录457帧遥测，线上序号1..457连续，含配置切换前5帧及新generation的452帧；
225帧RUN全部valid_flags=0x3ff，目标60/40 rpm分别100/125帧，均引用Active generation 2。
两条STATE_CHANGED为IDLE→RUN、RUN→IDLE，独立事件序号1/2连续；无FAULT_LATCHED。
通信错误/覆盖/事件丢弃与Host解析错误均0，终态无重发。这里只验收通信、参数及有限运行
的集成；速度峰值约70.57 rpm，不作为性能调参或跟踪指标通过的依据。

Stop后IDLE、目标0、Runtime OFF、六路LOW/MOE0、guard/内部/Motor/时序首因均0，
TIM6原时基保持；Active generation为2且不在使用，电角标定配置及冻结零偏前后逐字节/值
一致。最小剩余栈690/464/271/109 word均超过25%，操作者反馈“无异常”。
[P7证据](evidence/protocol/p7.json)保存身份、请求/终态、前后读回和范围，
[观测CSV](evidence/protocol/p7-samples.csv)使用MCU控制时间，不以Host到达时间代替。
本轮没有验证持续带载、故障恢复、故障Event、2圈触界、独立时限触发或完整ISR WCET。

## TX优化：250Hz通信验收通过，500Hz保留容量边界

在原队列、波特率及控制参数下，仅调整Communication发送路径：复用既有130 B编码工作区，
BUSY且Mailbox产生身份未变时不重复编码；样本覆盖、响应或Event改写工作区后立即失效。
没有第二个待发样本槽。请求与Completion仍先处理，发送尝试之后再观察最佳努力Event；
已排队终态和Event的发送优先级不变，新观察的Event在下次service参与选择。

临时无功率诊断镜像的64项有界DWT记录区分了首个瓶颈：剔除前两项后，原逐帧编码耗时
中位数92.29 µs，USART TC软件处理标记到下一DMA启动中位数132.52 µs；加入编码复用后，
62项中61项命中，后者降为34.42 µs。记录包含抢占，TC软件标记不是引脚精确发送终点；
它只说明该负载下的服务成本，不证明完整ISR WCET。诊断记录不进入正式镜像。

最终Run全量构建零错误、零警告，Code65558/RO1054/RW444/ZI20748，静态RAM21192 B。
28组真实ARM Server/Client模型通过，新增验证BUSY只编码一次、最新样本覆盖、工作区被
响应/Event/IN_PROGRESS复用后的失效及损坏缓存长度。控制、DMA、Kernel/Port和保护未改，
复用其未受影响证据。正式build ID为`0xfd763adb40fa8964`，HEX为
`ec3a17d9c2ebc45b4c6c0e8d7f446c00318cf460e0063b51ec1e853a08b54890`，
烧录与独立Flash读回66628 B匹配。完整输入摘要见[P6 TX证据](evidence/protocol/p6-tx.json)。

最终镜像按原逻辑供电条件复测，均穿插1 Hz状态请求：50Hz/10秒500帧、250Hz/30秒7501帧，
零缺口、零覆盖、零重发、零解析/链路/内部错误。500Hz/10秒收到4905帧，序号1..5001缺96帧，
与96次覆盖一致，1.92%仍超过原<1%门槛；9次状态请求无重发，最大响应6.15ms。
单帧92 B在实际460704.6 baud下约占1.997ms，500Hz样本已占46000 B/s，几乎用尽线路。

本阶段验收边界经确认调整为250Hz名义通信闭环；500Hz明确保留FAIL，不宣称原P6压力指标
全部通过，也不放宽设备截止或功率保护。结束时IDLE、目标/命令计数/接受启动均0、MOE0、
六路LOW、guard/内部/Motor/时序故障0；最小剩余栈690/438/254/109 word均超过25%。
遥测恢复enabled/250Hz，PI仍Default，尚未发送运动命令；下一步仅补新协议下有限启停调速。

## P6初测：遥测名义负载通过，500Hz发送吞吐未达标

正式Run已接入Telemetry及Event，能力位0x3F。Telemetry任务只推进自己的产生/覆盖计数，
值携会话epoch、该epoch首个产生计数和配置generation；Communication独占Session及线上
序号映射。Mailbox深度1，BUSY时peek保留，TX接受才退休；旧epoch、旧配置或迟到样本丢弃。
有效快照变化形成Event，深度8满丢最旧；终态优先于Event，Event优先于遥测，不抢写在途DMA。

Run全量构建零错误、零警告，Code65374/RO1054/RW444/ZI20732，静态RAM21176/32768 B。
25组Server/Client、14组真实Kernel/任务模型、12组Codec（47922断言、30黄金帧）及19组
有限Runtime交接通过；测试替代Transport、实时步进或异常切换，不等价于实际调度/WCET。
构建ID为`0xf2ae4e3aed717c5b`，HEX为
`b0b5673da847b578597a3f94d121d9ebae92cda86dc4f5b82bd8028da769994c`，
独立Flash读回66444 B匹配。未改控制算法、功率时序、Kernel/Port、静态队列容量或保护门限。

在操作者确认OUTPUT关闭、ST-Link逻辑3V3、原三相及编码器保留、USB串口VCC不接且VOFA
断开后，三个速率均穿插每秒一次COMMUNICATION_STATUS读取。50Hz/10秒收到501帧，
250Hz/30秒收到7500帧，均无缺口、覆盖、响应重发或两端解析/链路错误。初次50Hz试验曾
被Host将读线程延迟当成半帧超时而丢1帧；已修正Host空闲判据并保留失败记录，设备端
20ms门限不变，复测通过。

500Hz/10秒收到4676帧，序号1..5000缺324帧，恰好对应Mailbox覆盖324次，损失6.48%，
未达到小于1%的门槛。9次周期状态读取均无重发，最大响应约6.32ms；两端解析、RX断续、
DMA覆盖、UART错误、TX错误及内部故障均0。单帧92 B在实际460704.6 baud下约占1.997ms，
500Hz几乎占满线路；当前吞吐约43.2kB/s，覆盖已把首个瓶颈限定在单帧TX服务吞吐，
编码与任务调度各占多少尚未分别计时。没有扩容、降速或放宽门槛，P6整体尚未通过。

结束读回Motor IDLE、目标0、命令计数/接受启动均0、六路LOW/MOE0，guard、内部、Motor及
时序锁存故障为0，TIM6原时基保持。任务最小剩余栈Bootstrap690、Communication440、
Telemetry254、Idle109 word，均超过25%。遥测配置恢复enabled/250Hz，PI仍Default且Active
不在使用。本轮未发送Start/Update/Stop/Clear，未制造实物Event或运行运动负载；下一步先
测量TX帧间空档并区分编码/调度成本，解决500Hz瓶颈后再进入协议下有限试转。

## 会话事务与Python客户端：逻辑供电联调通过

正式Run由唯一Communication任务持有Parser、Session、Sequence、普通/Stop在途及固定
完成缓存4/终态FIFO4；通过准确ticket交给既有实时所有者。Stop取消未消费请求，已完成
历史不改写；新拒绝也退休序号，重复处理中只提示IN_PROGRESS，完成后重放原终态。
参数写仍由ADC权威应用，后台不改PI实例。20ms未完成、未知ticket/kind、损坏的索引/计数、
响应FIFO溢出及不可恢复Transport错误锁内部故障，不作为普通BAD_REQUEST继续处理。
这些故障/竞态由ARM模型验证，本轮没有另作实物故障注入。

Run原生Rebuild零错误、零警告，Code63666/RO1058/RW444/ZI20468，静态RAM20912/32768 B；
Server静态状态及编码工作区2576 B，不增加任务、动态分配或ISR协议计算。17组Server/Client
模型、12组Codec（47922断言、30黄金帧）、19组Runtime交接通过。模型运行实际ARM Server、
Codec、参数、交接和Motor，替代Transport、实时推进及C库复制；Python客户端对接同一模型，
包括丢响应后原字节重发、总超时结果未知、新会话不重放旧Start及旧响应过滤。
P4未受影响的DMA/Core/Kernel及独立Validation证据复用，不声称本轮重跑旧波形或全部工程。

构建身份由204项正式源/配置及保守头文件闭包的规范化内容产生，短ID为
`0x3df214f45eb49571`，完整摘要与输入清单保存在[P5证据](evidence/protocol/p5.json)。
HEX `18eefc3d5c8f97c2e7a6a5cd974f32dc984dd45742f98be71e1fe7f6f1092b54`
已安装，原生校验及独立Flash读回64740 B匹配，DEVICE_INFO读到同一build ID。

操作者确认OUTPUT关闭、仅ST-Link逻辑3V3、编码器与三相原接线保留，无电池或外部注入；
USB串口共地、VCC不接、VOFA断开。25项实物请求/状态检查通过：七对象可读；重复PI写仅
增加一次generation；内容冲突不替换原结果；后续写不改变缓存中的旧权威响应；长度、只读、
版本/flags/未知消息及会话权限按合同拒绝。CRC坏帧、过期Stop片段和line break只恢复通信，
片段未进入Motor。新会话保留参数并只读回状态，旧Session拒绝且不污染新序号。

无会话Stop实际执行一次，重复应答没有再次进入Motor；健康IDLE的Clear返回真实ACCEPTED。
Motor计数从0增至2，恰为Stop/Clear，接受Start仍为0；没有发送Start/Update。三次新的合法
PI写使Staged generation从1增至4，最终值恢复Default 0.001/0.004，Active仍generation0且
不在使用。停机读回IDLE、目标0、六路LOW/MOE0，guard/内部/Motor故障均0；FE、CRC、
半帧超时和消息错误均留在通信统计。任务最小剩余栈Bootstrap690/768、Communication418/768、
Telemetry281/384、Idle109/128 word，均超过25%，仅证明本轮请求负载。

[Host操作说明](../../../../communication/host/protocol/README.md)提供显式连接、读写和JSONL记录器。
本轮未启用线上Telemetry/Event，DEVICE_INFO暂不声明对应能力；默认遥测配置不代表采集
链已验收。50/250/500 Hz压力、运动下调度与栈、WCET及新协议有限试转仍需单独验证。

## UART DMA：字节流、覆盖恢复及发送超时通过

CubeMX生成USART1 460800/8N1、RX DMA1_Channel2 Circular、TX DMA1_Channel3 Normal及
三个优先级5的IRQ；RX/TX硬件仲裁保持LOW。Target Adapter独占寄存器，IRQ只核对必要
进度、处理错误并通知Communication；不解析协议或调用Motor。Bootstrap最后开放RX。
Validation仍为裸机115200轮询，不链接Adapter或Kernel；没有新增正式Target。

四个原生Target均零错误、零警告；Run静态RAM为18336/32768 B。11组DMA ARM模型、
12组SystemRuntime、19组交接、12组有限Run、46组Observe及Core IRQ 14/15/15项通过。
DMA模型覆盖合并/重复通知、回绕、覆盖与复制后复核、PE/FE/NE/ORE、TX内部复制、
DMA TC至USART TC的BUSY、10ms截止和迟到TC、内部故障后关闭事件源。模型采用模拟MMIO
和字节复制替身；真实C库复制另外通过下面的原生串口测试核对。

操作者确认OUTPUT关闭、ST-Link独立逻辑3V3，USB串口共地且VCC不接、VOFA断开；
原三相和编码器保留，无电池或外部注入。临时回环镜像使用正式Adapter和Kernel，仅替代
后台service，不处理Motor命令。COM5按460800连接，20种长度共7835 B逐字节回环一致，
无丢失或重复，包含27 B复制及4096 B跨圈流。暂停读取40ms并注入2000 B后检测覆盖、
提升epoch，后续117 B标记完全恢复；Host line break计到一次FE，后续126 B标记恢复，
均未锁Motor故障。故意冻结已接受的TX后锁TIMEOUT首因8、guard/Motor bit7，六路LOW；
再输入1000 B无响应、无重复故障，Communication继续运行。实物读回确认故障终态，
精确10ms边界由ARM模型验证，本轮没有测量真实关断延迟。

最后恢复正式Run，原生校验及独立Flash读回56516 B匹配，HEX为
`dd0e5c4d9ee479be590fcc59a06ddd9fd9ea4cc0877ce4e79d160b1cf3d00278`。
正式镜像不存在回环/故障注入符号，排空4096 B原始输入且发送0 B；Motor IDLE、接受启动0次，
MOE=0、六路LOW、guard及内部故障0。栈最小剩余Bootstrap691/768、Communication691/768、
Telemetry300/384、Idle110/128 word，均超过25%；仅适用于本轮负载。

[P4证据](evidence/protocol/p4.json)分别绑定临时与正式镜像及统计。
本轮不含新协议Server、Session/事务、线上参数、Telemetry发送、协议压力或电机运动；
不改写旧带电证据，也不证明WCET、最坏抢占、持续带载及独立电流精度。

## 原生FreeRTOS：纯逻辑启动与静态任务通过

Run手工集成STM32Cube FW_G4 V1.6.3的Kernel V10.3.1和RVDS/ARM_CM4F Port，供应源码未改。
CubeMX只生成TIM7硬件时基及异常配置。四个原生Target均零错误、零警告；Run静态RAM为
17400 / 32768 B，Validation保持裸机，Run不链接旧VOFA、CMSIS-RTOS或动态分配实现。
11组任务ARM模型、19组交接、12组有限Run和46组Observe模型通过；Core IRQ三种入口
11/12/12项通过。任务模型替代异常切换边界，因此实物启动另行核对。

操作者确认OUTPUT关闭、先退压再仅由ST-Link提供逻辑3V3，原三相和编码器保留，无电池或
外部注入。安装后原生校验及独立HOTPLUG读回53252 B匹配，HEX为
`59b227d4995736cae7220bb73656c572b9c77fb264524a772a169b34cedc8b48`。
调度器前HAL tick为4，Bootstrap读到HAL=6、Kernel=2后完成装配、自删除；之后任务数为3，
Communication/Telemetry均在延时表，当前任务为Idle。两次读取之间HAL、Kernel和ControlTime
持续推进，实际TIM7为1kHz，TIM6保留169/49分频配置及原守卫，NVIC与集成合同一致。

| 任务 | 初始栈word | 本轮最小剩余word |
| --- | --- | --- |
| Bootstrap | 768 | 690（删除前） |
| Communication | 768 | 723 |
| Telemetry | 384 | 299 |
| Idle | 128 | 109 |

Motor保持IDLE、接受启动0次，六路LOW/MOE=0，guard和内部故障均为0，未发送运动命令。
[P3证据](evidence/protocol/p3.json)包含读回值、时基差值、任务与异常归属及镜像摘要。
本轮只证明无通信负载的启动和后台交接；尚未验证UART DMA/协议压力、运动时的调度延迟、
最坏栈或WCET，不替代旧控制镜像的带电功能证据。

## 收尾构建：正式入口隔离与有限复测通过

主工程只包含 Run；验证工程承接 Observe、Timing 和旧台架，引用相同的 Platform、Motor 和
算法源码。Run 使用独立的运行入口宏，诊断源码、请求邮箱及 IRQ 测量钩子不进入正式构建；
ADC 完整退出截止、TIM6 心跳/有限时长守卫、故障锁存、校零和 CORDIC 均保留。
后台 UART 寄存器访问与 VOFA 解析/发送分别移至 `target_uart`、`target_vofa`，通过完整
Motor 命令及有界一致快照与 `target_runtime` 交接；交接成功不等于逐请求业务应答。

四个 Keil Target 全量构建均为零错误、零警告。新 Run 的 Code48508 / RO928 / RW312 /
ZI7832，静态 RAM 合计 8144 B，包含既有主栈和堆；旧实测镜像为 24328 B，减少的诊断状态
没有改作 FreeRTOS 内存。AXF/MAP 确认正式保护仍链接、专项诊断符号不存在。
12 组 Run、46 组 Observe、Core IRQ 的验证/Run 两种构建 10/11 项、107 项 Platform、
644 项保护快照、26 项母线 DMA、19 项候选工具及16组 Timing 入口检查通过。
它们验证实际 ARM 源码或主机工具，不代表电气正确性和 WCET。

冻结 Run HEX 为 `745b74e453e61e8c4532e70f2f23591ed2fc33930921014311a95c05340345e6`，
加载 49448 B。逻辑供电安装、原生校验与独立 Flash 读回一致；4096 点零偏 READY、
80 帧均为有效 IDLE，六路关闭、启动次数0、guard与时序首因均为空，未发送运动命令。

操作者确认原12 V／0.20 A、ST-Link 3V3断开、原三相和编码器固定空载、无外部注入，
RST后静止形成本启动零偏。仅执行一次 `start:60`、约0.406秒后 `speed:40`、约0.906秒后
`stop`，无固定准备倒计时、反向、自动重试或Clear。200帧全部有效位511、故障0；
其中40帧目标60、50帧目标40、110帧IDLE。两段速度均值46.404／38.211 rpm含起步及调速
过渡，不是稳态性能验收；遥测母线11.903..12.042 V，没有新增万用表或电源电流读数。

停机后Motor为IDLE、目标0、已处理3条命令、Runtime OFF；接受启动1次、active0、
limit_reason0、净位移+0.638977圈。六路LOW/MOE=0、guard0、88 B时序首因全零，
冻结零偏未改变，独立Flash再次读回匹配；操作者报告无异常。
[收尾构建及完整200帧证据](evidence/runtime/closure.json)分别保存软件检查与实物范围。
反向及独立1.5秒关断的实物证据继续绑定下节旧镜像，不改写为本镜像补测；本轮模型覆盖守卫交接。
本轮没有扩展参数或解除有限运行策略，也未补测正式RUN故障注入、2圈实物触界、持续运行或WCET。
完整日志、冻结产物及单次现场脚本保留在本地 `validation/local-evidence/20261021-baseline-close-01/`。

## 正式 Run 补验：反向调速及独立时限通过

沿用下节的同一 Run 镜像，没有改源码、重建或烧录。操作者确认原12 V／0.20 A、
ST-Link 3V3断开、原三相与编码器固定空载、无外部注入，VOFA串口由单次采集独占。
首次前检发现本启动零偏在161点后锁定FAILED，原因是静止/位置有效条件丢失，
没有发送运动命令。该记录不能区分轴曾移动还是位置反馈短暂异常；不据此修改门限。
操作者保持静止并按一次RST后，4096点零偏READY；只读核对58688加载字节与原镜像相同，
IDLE、六LOW/MOE=0、guard0、时序首因空，随后直接执行已确认的一次试验。

UART仅发送 `start:-60` 与约0.39993秒后的 `speed:-40`，没有正常或兜底Stop、Clear、
反向重试或自动复位。收到203帧，其中目标−60为40帧、−40为110帧，全部有效位511、
故障0。两段速度均值−45.955 / −38.658 rpm、平均Iq约−23.137 / −25.397名义mA，
均包含过渡，不作为稳态性能或电流精度验收。

首个RUN至首个IDLE的控制时间差约1.4999秒；从主机Start尝试到首个IDLE约1.512秒，
后者含串口传输和观测延迟。停机记录明确为 `limit_reason=1`（TIM6时限到期）、
`active=0`、接受启动1次、净位移−1.025513圈。Motor命令数3，分别对应两条UART请求
及守卫交接的Stop；目标0、流程OFF、IDLE，冻结零偏保留。调速没有重新计时，
正常路径没有依赖主机Stop。最终六LOW/MOE=0、guard0、88 B时序首因全零，镜像再次匹配，
操作者报告无异常。

[反向与时限曲线](evidence/runtime/uart-negative-limit.svg)和
[完整203帧证据](evidence/runtime/uart-negative-limit.json)保留命令时刻、
前检失败、人工复位及停机原因。与下节正向试验合并，已覆盖正式UART正反低速启动、
目标更新、显式Stop、MCU独立时限及一致性观测；手工操作入口见
[VOFA说明](../../../../communication/host/vofa/README.md#有限-run-操作)。
本次未触发2圈边界或注入正式RUN故障，不扩展为全部保护实机验收、长期运行或WCET证明。
独立电流精度、持久标定、故障恢复及速度纹波仍未完成，自研UART协议和FreeRTOS尚未接入。

## 正式 Run：UART 正向启动、调速与 Stop 已打通

新增独立 `BLDC_G431_Run`，装配公共 Motor / ControlRuntime、原 Platform 及既有 UART / VOFA，
不通过台架运动邮箱执行控制。Observe 保持拒绝启动，Timing 和旧台架入口均不改变。
Run 上电关断、静止后采本启动 4096 点零偏；仅显式 Start 开始跨拍启动。
固定空载包络及名义电流换算依据见 [Runtime 说明](../runtime/README.md#有限运行入口)。
Target 新增独立 1.5 秒时限，调速不续期；时长到先关断再交接 Stop。
净位移达到 2 圈立即关断并锁存 bit 6，正常 Stop 不清故障或重新校零。

Run / Observe 均构建零错误、零警告；Run Code57752 / RO924 / RW376 / ZI23952。
9 项有限 Run ARM 模型和 46 项 Observe 模型通过，覆盖默认关断、反馈/校零未就绪拒绝、
CORDIC 装配失败、跨拍使能、UART 调速/Stop 优先、超时跨 DWT 回绕、净位移及故障不自动恢复。
这些测试使用 HCC / UART 替身，不证明电气正确性或 WCET。

首个 Run 候选逻辑供电时暴露消费相位问题：实际 SSI 每 1 ms 在 ADC consumer 之后发布，
Runtime 也每 1 ms 才读取缓存，旧位置可在下次读取前超过 1.5 ms 新鲜度门限，速度历史因此
反复清空、无法开始校零。该候选没有发送 Start。修正为每个 100 µs fast tick 读取完整观测
缓存，仅新时间戳更新估计，实际 SSI / 母线采集仍为 1 kHz；未放宽新鲜度或预测门限。
新增模型按“consumer 后发布、下拍可读”重现真实相位，并覆盖持续校零及 RUN 角度有效。

修正版 HEX `0e73f54e03ceaa9b21f3ae7cfc305c1ca50ecc9577613cc47f974d54fd2eee76`，
下载校验及独立读回58688加载字节匹配。逻辑供电下4096点校零 READY，速度、电角度及
十通道有效，六LOW/MOE=0、启动次数0；连续152帧均IDLE/无故障，未发送命令。

操作者确认原12 V／0.20 A、ST-Link 3V3断开、SWD/GND保留，固定空载、原三相/编码器、
无外部注入，并在母线恢复后RST重新采零。前检确认本启动4096点零偏、无故障及完整新遥测。
仅发送一次 `start:60`，约0.4秒后 `speed:40`，约0.9秒 `stop`；没有反向、重试或Clear。
本次接受一次启动，Motor命令计数3，收到140帧，其中RUN目标60为40帧、目标40为50帧，
全部有效位511、故障0、命令结果按处理顺序更新。两段均值45.397 / 38.088 rpm，分别包含
启动及调速过渡，不是稳态平均或性能达标证明。遥测母线约11.903..12.044 V，仅为板载读数。

Stop 后IDLE、目标0、流程OFF，正常冻结零偏保留；净位移记录+0.629456圈、有限守卫已退出，
限制原因0，六LOW/MOE=0、guard0、88 B时序首因全零。操作者报告无异常。
没有新增原生时间探针；本次未触发完整IRQ截止故障，不等于WCET或长期稳定性通过。
100 Hz遥测不能证明逐拍电流峰值或瞬态电流精度。Stop直接撤销功率，不声称主动减速。

[UART曲线](evidence/runtime/uart-run.svg)和
[实测证据](evidence/runtime/uart-run.json)保存完整140帧、命令时刻、镜像身份及限制。
反向及有限时长补验见本页上节；独立电流精度、持久标定、
故障恢复和速度纹波优化仍未完成。自研UART协议与FreeRTOS继续按后续增量接入。

## 历史台架：统计精简复测，速度峰值仍待改善

有限电流/速度台架的窗口统计改为逐拍累加及平方误差峰值比较，在输出关断后的既有
终态发布点一次求均值和开方。使用独立32字节累加器，保持所有窗口样本、结果ABI和
原筛选公式；空窗口不除零，Stop不重复结算，新请求清旧统计。占空比极值直接比较已
通过stage的有限值，电压保护判断、PI、控制算法、Platform及90 µs截止均保持。

324项实际ARM台架模型与41项主机检查通过；新增非恒定相电流/占空比的独立统计对照、
部分窗口Stop、关断后结算、重复Stop及新请求空窗口验证。受影响台架Target全量构建
零错误警告；Observe/Timing排除该Validation实现，未重建。机器码核对`run_current_hold`
的静态VDIV由7处降为0、VSQRT由2处降为1，剩余开方用于原电压保护；这不是动态耗时
或硬件WCET证明，浮点求和顺序变化带来的有限舍入差异已纳入模型对照。

安装HEX `34271bb7bd40fa54373ecf4066eefca1edc433cbf42c4b11554791eee9b0adc3`，
独立Flash核对56964加载字节匹配，默认关断、采样及首因记录为空。操作者确认原
12 V／0.20 A供电正常，4096点零点请求1后直接执行唯一请求2：+60 rpm／1.5秒，
Kp/Ki=0.001/0.004，全部功率和运动边界保持。12.12 V仅历史DMM参考，无新增人工读数。

本次正常完成，15002样本、enabled15001、速度更新1500次，完整4000点/400次窗口和
375点曲线。窗口平均59.835 rpm、平均误差0.165 rpm、峰值误差18.263 rpm；原15 rpm
峰值初筛仍失败，按收敛后的阶段目标保留为性能待改项。末端3.110 rpm、净位移+1.116圈，
方向和减速初筛通过。DQ均值误差0.082 mA、窗口逐拍误差峰值27.395 mA通过原10/40 mA
初筛，电流单位仍为名义A；电压请求峰值0.253 V，无电压或积分限幅。

最终六LOW/MOE=0、guard停、Trip/invalid=0，stage15001/15001、enable1/1。停机后
只读复查时序与母线首因仍空、请求仍2，未补发SAFE_OFF、自动重试或反向。操作者报告
无异常。consumer最大8350 cycles=49.118 µs，不是完整IRQ耗时或WCET；本轮完整运行
未检测到此前的IRQ退出超时，单次成功不代替长期稳定性或最坏执行时间证明。

[完整曲线](evidence/rotor-speed/statistics-lean.svg)与
[复测证据](evidence/rotor-speed/statistics-lean.json)保留镜像身份和限制。
该轮之后已接通正式 Motor 及现有 UART / VOFA，见本页顶部正式 Run 记录。
本轮台架结果保留原镜像归属，速度峰值、独立电流精度和持久标定仍分别验收。

## 首次时序故障历史：完整 ADC 中断退出越过截止

Platform 新增88字节私有首因记录，区分更新、采样入口、consumer返回和完整IRQ退出等
七个失败分支。健康路径仅保存四个时间戳，失败时先撤销输出再发布记录；Stop及后续
无效采样不覆盖首因。主机在终态分析和清理之前自动上传，不依赖停机后人工补读。
公共HCC、控制计算、PI、20k/10k/1k频率和原功率边界保持。

三个Keil入口全量构建均零错误警告；107项实际Platform ARM模型、10项Core IRQ模型和
45项主机检查通过，未重跑整套台架模型。覆盖首次记录先关断、Stop保持、DWT回绕、
迟到JEOS升级保持原IRQ入口时间，以及失败终态上传后仅一次SAFE_OFF。模型不证明WCET。

安装HEX `4572ce47ab44c694574561e78c5c7a6faae052d977f78cc8a82fb012ca702582`，
独立读回56900加载字节匹配，默认六LOW/MOE=0、请求/提交/使能0，两个首故障记录为空。
操作者确认12 V／0.20 A供电正常；4096点零点后执行唯一+60 rpm／1.5秒请求2，
Kp/Ki=0.001/0.004。12.12 V仅历史DMM参考，无新增人工读数。临时调度脚本在采零后
误判返回对象而提前退出，核对零点成功后使用同一锚继续；未重复采零，运动命令只提交一次。

约0.8098秒返回6（BAD_CURRENT）关断。首因明确为`irq_exit`，slot与callback一致、
epoch一致、时钟条件齐全、UIF=0，本拍PWM提交已成功。以170 MHz换算，consumer耗时
53.853 µs，返回点距slot起始84.282 µs；其后采集及HAL尾部至记录点共6.918 µs，
其中辅助采集段4.547 µs、剩余尾部2.371 µs。记录点距slot起始91.200 µs，超过原90 µs
截止线1.200 µs；失败判定略早于记录点。IRQ入口钩子至记录点65.059 µs，不包含异常
入栈/返回和首因复制，也不能当作旧镜像或本候选的完整IRQ WCET。

本次取得203点曲线、8100个样本，仅99/4000个窗口样本与10/400次窗口速度更新，
完整跟踪及减速未验证。主机保存原始结果6后仅提交SAFE_OFF请求3，最终六LOW/MOE=0、
guard停、Trip=0，stage8100/8100、enable1/1。操作者报告无异常。未自动重试或反向。
首因与[证据](evidence/rotor-speed/timing-first-fault.json)已保存，尚未修复超时。

代码核对显示0.8秒启用的验证窗口逐拍计算多个在线均值与误差平方根；下一步优先精简
这些非控制统计，保留原窗口全部样本、统计语义和门限，再以原参数复测。不放宽截止、
不改PI，不把本次首因未经验证地归给旧镜像，也不认定曲线记录是唯一开销来源。

## 有限速度曲线历史：取得201点，首次故障未留存

命令20新增250 Hz、最多375点的有限RAM记录，停机后才上传；同时支持有界Kp/Ki配置，
后续参数对照无需重刷相同固件。原4000点窗口、验收门限、功率边界与公共HCC保持。
配置/版本/数据所有权见[有限速度流程](CURRENT_HOLD.md#有限速度闭环)。
受影响台架Target全量构建零错误警告，321项ARM台架模型和71项主机检查通过；
Observe/Timing排除这些Validation实现，本增量未重建它们。模型不证明实际IRQ预算。

安装HEX `1c55bff9ea2beef42207e6825325d7a87ac858e9803c28f1625b8be315fbda0f`，
独立核对56388加载字节，默认关断、无请求/提交/使能通过。操作者恢复12 V／0.20 A
并确认正常，4096点零点后执行唯一+60 rpm／1.5秒请求，Kp/Ki仍为0.001/0.004；
12.12 V仅沿用历史DMM参考，没有新增人工读数。约0.8018秒返回6（BAD_CURRENT）关断，
取得201点曲线至0.8001秒，完整跟踪窗口和减速未验证。未自动重试、反向或改PI。

清理后采样入口间隔约99.735～100.118 µs、两相数据仍在更新，但采集有效性持续为false，
按实现指向平台时序故障锁存。consumer最大53.429 µs不是完整ADC IRQ；首次触发分支
尚未保存，不能据此认定新增记录开销就是根因。主机只补一次SAFE_OFF请求3，最终
六LOW/MOE=0、guard停、Trip=0，stage8020/8020、enable1/1。操作者报告无异常。
后续只读检查前ST-Link丢失，操作者重启电源后重连；原RAM锁存不可恢复，已保存的
曲线和失败终态保留。重启后只读核对默认关断、请求/提交/使能0、采样恢复，无新动作。

MATLAB分析0.35～0.8秒曲线：估速约42.76～77.18 rpm，位置区间差分也存在相近波动，
去趋势主峰约13.39 Hz（频率分辨率2.23 Hz）。两者同源于编码器，不能唯一确定机械
波动、齿槽或角度误差。[曲线](evidence/rotor-speed/trace-baseline.svg)与
[证据](evidence/rotor-speed/trace-baseline.json)保留本次失败身份。
下一步先定位完整ADC IRQ的首次时序失效点并量化尾部预算，不放宽截止或重跑旧波形；
速度调节及正式Motor/UART/VOFA验收仍未完成。

## 母线 DMA 修复记录：此前正向完整运行，速度峰值误差仍需改善

regular ADC 改由 DMA1_Channel1 以 normal 模式搬运三个 halfword，仅在整组 TC 后发布；
不再逐 rank 读 DR 和实时 EOS。ADC1_2 与 DMA IRQ 同抢占优先级，母线观测和私有诊断仍
共用一次 publication。真实采集错误锁存无效状态，不自动重采；不调用会影响 injected
采样的 HAL_ADC_Stop_DMA。CubeMX 6.18.1 实际生成配置与 Core 文件，原 USER 区保留，
三个 Keil Target 均加入 dma.c。公共 ABI、20k/10k/1k 时序、控制增益和功率边界保持。

三个 Keil Target 全量构建零错误警告；本增量执行母线26项、Core IRQ 10项和 Platform
101项实际 ARM 模型，以及69项主机检查，全部通过。覆盖原 EOC/EOS 交错、整组交付、
不完整传输、ADC/DMA 错误及无自动重试；没有重复执行此前309项整套台架模型。
模型不证明硬件 IRQ WCET、抗干扰或电流绝对精度。

安装 HEX `c2d7e3e4cb7cff7200d28b41d211750b0a97ec8fbc5de424089ea7893e64e6fc`，
独立读回56028加载字节及默认关断通过。实机核对 normal/halfword/MINC、同优先级1及
关闭 EOC/EOS 逐 rank 中断。操作者恢复12 V／0.20 A并确认正常；本启动请求1采4096点
零点，前检通过后立即执行唯一请求2：+60 rpm／1.5秒，没有准备倒计时或自动反向。
12.12 V只沿用历史DMM参考，本轮没有新增人工电压、电流数值。

本次正常完成并关断，15002样本、enabled15001、速度更新1500次，完整取得4000点电流
与400次速度窗口。净位移+1.103圈，保持窗口平均60.379 rpm、平均误差−0.379 rpm；
窗口速度误差峰值18.775 rpm超过原15 rpm门限，因此速度跟踪初筛未通过。全程最大
接受估速77.200 rpm，末端2.322 rpm，方向与减速初筛通过。窗口DQ均值误差0.187 mA、
跟踪误差峰值26.115 mA，通过原10/40 mA初筛；电流单位仍基于名义增益。

电压请求峰值0.252 V、名义相电流峰值51.465 mA，无电压或电流积分限幅；
stage15001/15001、enable1/1，最终六LOW/MOE=0、guard停、Trip/invalid=0，母线首故障
记录为空。consumer最大52.424 µs，不是完整IRQ/WCET。操作者报告无异常；没有自动
重试、补发关断或执行反向。原参数复测未再发生此前约0.8秒的采样早停，但单轮成功
不代替长期稳定性验证。下一步依据实测响应改善速度峰值误差，不重跑旧底层波形；
正式 Motor RUN、反向与独立电流精度仍未验收。详见[DMA复测证据](evidence/rotor-speed/bus-dma.json)。
以下记录保留各自历史镜像身份。

## 母线早停定位记录：regular ADC 的序列结束判断存在竞态

针对下述结果7早停，新增独立96字节首故障记录和48字节Target私有ADC采集记录；
先关断再锁存失败条件、电压、时间、raw/rank及HAL/ADC状态，SAFE_OFF和后续采样不覆盖。
详情见[有限速度流程](CURRENT_HOLD.md#有限速度闭环)。公共BusObservation、旧结果ABI、
电压/新鲜度边界、速度/电流增益和有限试转参数均保持，不把占位0 V误判为实际母线读数。

台架、Observe和Timing三个Keil入口全量构建均零错误警告；309项台架ARM模型、12项
实际母线源码ARM测试和68项主机检查通过。覆盖首次故障先关断、后续采样/SAFE_OFF保持、
BUSY请求不能改变故障归属、HAL起动失败、rank数量错误、VREFINT为零和ADC错误。
寄存器事件与HCC替身不证明实物IRQ时延、采样抗干扰或电流绝对精度。

候选HEX `6ec2aef01d7f94fb5f16e9c7b13a47ca18a7baac82e7691614807fb5408928e1`
已安装并独立读回54880加载字节；默认六LOW/MOE=0，无请求、stage或enable，回调约
9.965/ms，首故障记录为空。操作者恢复12 V／0.20 A并确认正常，4096点零点完成后直接
执行唯一请求2：+60 rpm。约0.801秒后返回7关断，净位移+0.555圈、最大接受估速73.789 rpm；
只取得9/4000个窗口样本，速度/电流完整跟踪及减速仍未验收，反向未计划或执行。

首故障属于请求2，sample/母线时间952482/952480（相差100 µs），publication已交付但采集
无效；原因rank_mismatch，已读rank=2，VBUS/VREFINT raw=1453/1498，HAL错误0、ISR=0x0F，
未记录OVR。failure_mask=10中的“欠压”来自无效publication的0 V占位，不是实测欠压。
实际采集代码先读DR，再读实时EOS；实测ISR同时保留新EOC/EOS，配套实际ARM源码模型
在“第2次DR读完后第3次转换完成”这一交错下复现相同rank=2拒绝。因此定位到结束标志
跨转换读取的竞态，下一步修复整组采集交付，不能仅放宽电压门限或调整速度PI。

固件先撤销输出，主机只补一次SAFE_OFF请求3；最终六LOW/MOE=0、guard停、Trip/invalid=0，
stage8010/8010、enable1/1，consumer最大51.924 µs（非完整IRQ/WCET）。操作者报告无异常。
此镜像只增加诊断，尚未修复竞态；未自动重试、反向或重跑旧波形。
构建、测试与候选身份见[母线诊断证据](evidence/rotor-speed/bus-diagnostic.json)。

## 有限速度闭环：首轮因母线检查早停，尚未完成验收

命令20在已有转子DQ链上复用SpeedController，以10 kHz电流环的整数10分频执行1 kHz速度环；
20 kHz PWM、采样/提交时序和旧命令保持。目标±60 rpm，单次1.5秒含0.3秒升/降速，
Id=0、Iq限幅±0.10名义A，原1.8 V电压请求上界、0.65名义A、300 rpm与2圈边界保持。
配置及初筛门限见[有限速度流程](CURRENT_HOLD.md#有限速度闭环)。
全量台架构建零错误警告，301项实际ARM模型与66项主机检查通过。新增独立耦合电气/机械
模型包含惯量、摩擦与负载变化，但这些参数为假设，不是实物辨识结果。

安装HEX `60c0a29305c0c20f64ab73e3a76c24254510f4ff5976d8e3296a102aa160093c`，
独立核对54500个Flash加载字节，默认关断通过。操作者恢复12 V／0.20 A并确认正常；
本启动请求1采4096点零点，直接执行唯一请求2：+60 rpm，无准备倒计时。
约0.827秒后返回7（BAD_BUS）并关断，没有正常完成。净位移+0.585圈，最大接受估速
79.172 rpm，电压请求峰值0.253 V、名义相电流峰值50.899 mA，无电压/电流积分限幅。

0.8～1.2秒窗口只取得269/4000个电流样本、27/400次速度更新；局部估速均值70.411 rpm，
不能据此宣称速度跟踪或电流保持通过。反向按约定跳过。工具仅补一次SAFE_OFF请求3，
原始终态结果7与清理后stop_reason=2分别保留；最终MOE=0、六LOW、guard停，Trip/invalid为0，
consumer最大51.576 µs（非完整IRQ/WCET）。操作者报告无异常。

结果7可由母线不可用、无效、非有限值、10～14 V越界、未来时间或超过5 ms年龄触发。
现有镜像未锁存触发当拍的母线值和条件位；停机后的约12 V不能证明或排除瞬态异常，
也不能直接归因电源。下一步针对该早停锁存首个失败的值、时间和采集状态，先区分电压
异常与采样交付问题，不放宽母线检查或重跑旧波形。
仅主机报告增加清理前失败终态保留，10项主机检查通过，未改固件、重烧或重复试转；
旧冻结工具身份及原始记录保留。详见[有限速度证据](evidence/rotor-speed/summary.json)。

## 有限转子 DQ：正向运动和速度边界关断已验证

命令19复用已有PositionEstimator、ElectricalAngle和CurrentController，以已审查的7极对、
正方向及零位候选计算真实转子DQ，Id固定0、Iq可带符号。测量角对齐同步采样，施加角
显式预测至sample后125 µs；配置和边界见[有限转子DQ流程](CURRENT_HOLD.md#有限转子-dq-电流环)。
仅转子模式采用Kp=0.5、Ki=300，以改善原静态Ki=10在反电动势模型中的建立不足；旧命令保持。
台架全量及最终增量构建零错误警告，275项实际ARM模型与57项主机检查通过。
模型使用独立延迟R/L及反电动势，机械速度为外部指定，不能代替实物转矩或惯量验证。

安装HEX `a43fa22ae8ac5dc00c614ccd0a389871398eee72abc13095a08c99d56001227a`，
独立核对52044个Flash加载字节及默认关断。操作者恢复12 V／0.20 A并确认正常，本启动
请求1采4096点零点，前检通过即提交唯一请求2：Id=0、Iq=+0.10名义A、最多1秒。
没有准备倒计时，12.12 V沿用旧DMM参考，本轮无新增人工数值，实时软件母线检查通过。

正向运动约0.195秒后返回23并关断；最后接受的位移约+0.251圈（90.505°），最大已接受
估速296.631 rpm。位移未达2圈，符合300 rpm速度估计边界撤销。电压请求峰值0.944 V、
名义相电流峰值74.835 mA，无电压限幅。stage1950/1950、enable1/1，最终MOE=0、
六LOW、guard停、Trip/invalid为0；consumer最大49.094 µs，不是完整ADC IRQ或WCET。
操作者报告无异常。没有正常运行满1秒，尚未进入0.6～0.8秒的2000点保持窗口，
因此恒定电流跟踪、反向和速度闭环仍未验证，不将本轮写成完整FOC验收通过。

未满足约定的正常完成与电流初筛条件，反向没有执行。原始快照中的窗口均值0为初始化值；
主机报告已修正为空窗口记null（未测），没有把它解释成100 mA误差。该修正只涉及报告及
9项主机检查，不改固件、不重烧、不重跑实测；原始记录和执行工具身份保留。
下一步复用已有速度PI控制加速，在可保持的低速下补齐电流和正反向验证，不重复定向或
自动放宽运动边界。完整身份、数值与边界见[转子DQ证据](evidence/rotor-current/summary.json)。

## 定向励磁对照：0.4 A 下通过电流与映射初筛

命令18扩大定向包络至目标上限0.5名义A、积分限值1.5 V、调制请求上限1.8 V、
名义相电流撤销阈值0.65 A；原PI增益、五秒轨迹、位置门限和硬件保护配置保持。
命令15/16/17不扩大参数范围。依据约3.1 Ω相等效电阻粗测，0.4 A的IR约1.24 V、
铜耗约0.744 W，补足积分和电压余量；流程见[有限电流磁场往返](CURRENT_HOLD.md#有限电流磁场往返)。

台架全量构建零错误警告，248项实际ARM模型和48项新旧主机检查通过。
安装HEX `e76759044f79aefcd66bf54ce59fa7431a668cfa4c5a5d8077d1ad5cf9eba660`，
独立核对49076个Flash加载字节及默认关断。操作者明确接受本轮包络并恢复12 V／0.20 A，
本启动请求1采4096点零点，自动前检通过后直接发送请求2，唯一一次0.4 A／5秒往返，
无固定准备倒计时。12.12 V仅沿用旧DMM参考，本轮未新增人工数值，实时母线检查通过。

三个保持窗口共7500点，α/β均值393.762/−0.109 mA，均值矢量误差6.239 mA、
保持误差峰值49.068 mA，通过实测前预设的20/50 mA功能初筛；峰值接近门限，
不能据此宣称动态带宽或高精度电流控制已验收。调制请求峰值1.272 V，无电压限幅，
名义相电流峰值0.421 A。

三个位置窗口各250份新SSI且稳定：移出51.412°、返回50.881°，回程差0.531°，
双向极对数估计7.002/7.075，offset最大残差2.516电角度；原映射筛选全部通过。
相较此前60 mA时7.145°回程差，提高励磁后的结果支持此前励磁不足是重要影响因素，
不能唯一确定静摩擦或齿槽等机械根因。7极对、正方向和约8.800电角度零位用于后续候选，
尚未安装正式标定，也未完成机械全圈精度验证。

stage50001/50001、enable1/1，结束后MOE=0、六路LOW、guard停、Trip/invalid为0，
操作者报告无异常。consumer最大39.453 µs，非完整IRQ/WCET。下一步转入有限转子DQ
电流环及小转矩双向验证，不重跑旧底层波形或自动开放正式RUN。完整数值与身份见
[励磁对照证据](evidence/current-survey/excitation-comparison.json)；以下历史结论保留原镜像归属。

## 电流磁场往返：保持跟踪通过，映射重复性未通过

命令18复用已有电流PI，在施加磁场坐标中完成完整一电角圈往返，不依赖或安装转子电角标定。
本轮只改台架路径；全量及最终增量构建零错误警告，244项台架ARM模型和47项新旧主机
检查通过。R/L替身包含实际延迟，SSI按预设轨迹提供，不是机械收敛模型。
流程与接口见 [CURRENT_HOLD.md](CURRENT_HOLD.md#有限电流磁场往返)。

候选HEX `c23c612dd7a5629e4f742b2b3bb861b29bb7c5addda69a5c05cf8c4914fc98aa`
已安装，独立核对48932个Flash加载字节及默认关断。操作者恢复原12 V／0.10 A并确认正常，
请求1采4096点零点，留5秒准备，请求2仅执行一次60 mA／5秒往返。12.12 V沿用旧DMM参考，
本轮未新增人工数值；实时软件母线检查通过。

三个保持窗口共7500点：α/β均值59.770/0.123 mA，均值矢量误差0.261 mA、
窗口误差峰值28.331 mA，通过原10/30 mA筛选；未记录实物移动段逐拍误差。
三个位置窗口各250份新SSI且稳定；移出53.448°、返回46.303°，回程差7.145°，
offset最大残差28.631电角度，双向极对数估计6.735/7.775，原映射筛选未通过。
三个窗口的名义电流矢量方向约0.440°/0.767°/−0.121°电角度；电流矢量接近不等于
机械位置可重复，不能将保持电流跟踪合格升级为电角标定合格。

stage50001/50001、enable1/1，调制请求峰值0.216 V、名义相电流峰值76.900 mA，
结束后MOE=0、六路LOW、guard停、Trip/invalid为0。consumer最大39.559 µs，非完整IRQ/WCET。
操作者报告无异常，关闭OUTPUT后手转只有均匀磁齿感、未报告擦碰卡滞。
60 mA低于前轮0.4 V下约0.13 A的名义励磁，不能把两轮作为仅改变控制方式的因果对照。
后续先评估定向励磁包络与映射误差方法，不重复方向验证或自动提高幅值；正式FOC许可不变。
数值、原始记录身份和边界见[电流往返证据](evidence/current-survey/summary.json)。

## 完整电角圈往返：完成关断，映射重复性未通过

命令16的静态幅度上限扩为1电角圈，整圈端点归一化到[0,1)，仍使用原5秒轨迹、
5.5秒独立期限及原保护。主机分别由往返机械位移估计极对数；只有比例、稳定窗口、
回程和offset残差全部通过，才记录本次支持标称极对数，不自动安装标定。
三个Target全量构建均零错误警告；纯轨迹ARM模型16组、台架ARM模型224项、主机14项通过。

安装台架HEX `daed5f4441696ef56ddb6746b885b97fd8407bb224738bab69f59786689d1a2c`，
独立核对47744个Flash加载字节及默认六路LOW。操作者恢复12 V／0.10 A并确认正常，
本轮没有新增人工电压/电流数值，12.12 V仅沿用旧DMM参考，实时软件母线检查通过。
请求1采4096点关断零点，留5秒准备后，请求2仅执行一次0.4 V／完整一电角圈往返。

三个保持窗口各250份新SSI观测且稳定；移出51.921°、返回48.450°，回程差3.472°，
超过原1.08°筛选门限。往返极对数估计分别6.934、7.430，回程超出标称7±0.25；
offset最大残差15.051电角度，也超过原12.6°门限。因此方向+1及约1/7机械圈量级
获得补充支持，极对数和电角零位仍未确认，不沿用前轮短行程通过作为本轮通过。

stage50001/50001、enable1/1，名义相电流峰值0.146 A；结束后MOE=0、六路LOW、
guard停止、Trip/invalid均为0。操作者报告无异常，未自动重试或增加励磁。
同角位置的方向相关差异可能涉及静摩擦、齿槽转矩或有限励磁，端点数据不足以确定根因。
下一步复用已验证的电流PI准备有限电流定向，不直接放宽门限；正式FOC运行许可保持关闭。
流程见[静态往返](OPEN_LOOP.md#有限静态电角往返)，数值和身份见
[完整电角圈证据](evidence/angle-survey/full-turn-summary.json)。旧证据保持原镜像归属。

## 静止坐标电流环：低幅值两轴正反向跟踪通过

B/C 名义电流先扣冻结零偏再同时取反，统一为桥→电机正方向；依据为原始电路及
已修正物理相映射的正α/正β静态对照，不再将独立增益精度待测表述为方向未知。
新增命令17复用已有PI，在恒等坐标系中控制αβ电流；正式转子FOC标定及运行许可保持关闭。
流程及验收边界见 [CURRENT_HOLD.md](CURRENT_HOLD.md)，精选数值见
[电流环证据](evidence/current-hold/summary.json)。

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
配套工具、参数和现场步骤见[有限电压开环](OPEN_LOOP.md)，
身份和已验证边界见[本轮证据摘要](evidence/voltage-open-loop/summary.json)。

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
数值及原始摘要身份见[正反向对照证据](evidence/voltage-open-loop/reverse-comparison.json)。

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
参数、筛选门限和复用命令见[静态往返流程](OPEN_LOOP.md#有限静态电角往返)，
身份和已验证边界见[静态往返候选摘要](evidence/angle-survey/summary.json)。

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
来源与修正范围见[输出相位核对](evidence/angle-survey/pwm-phase-mapping-audit.json)。

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
原始摘要身份、三窗口电流及候选边界见[相映射修正证据](evidence/pwm-phase-mapping/summary.json)。

## 电源显示参考与现有器材：已准备采集，零偏只读核对通过

已知电流工具增加 `--reference-source supply-display`，明确保存电源实际显示电流的
来源和粗对照限制；默认 DMM 调用兼容。两种来源均保持原有镜像、逻辑供电、功率关闭、
冻结零偏及一次请求前检，不自动判定增益或授予运行许可。32 项纯 Host 替身测试通过。

操作者用已完全脱离功率桥的电机两线绕组建立独立支路，退出万用表后报告电源显示
0.10／0.15／0.20 V 对应 0.013／0.021／0.029 A，并报告约 0.29 V 时进入限流。
该独立支路尚未接板上分流，不能记为板上电流或校准结果；过程与限制见
[电流复核](CURRENT_CHECK.md#后续已知电流对照准备)。

操作者随后确认 OUTPUT 关闭、无注入、VBUS 无外部供电、三相脱桥、仅 ST-Link 逻辑
3V3 和原编码器接线，万用表 VBUS 为 2.76 V。安装既有默认关断 Observe 镜像
`b2ca00eea00a72477dfbaaaf21a1179525468b9bcb5ccc593a921797fe6040ef`，未改固件源码。
第一次 HOTPLUG 下载在 45 秒超时；只读证明原 Timing 加载字节完整、CPU 运行且 MOE 关闭。
改用 NORMAL 完成下载、校验和软件复位，随后全 Flash 读回核对候选加载字节一致。

本次启动两次只读健康检查通过：IDLE、faults／guard／MOE 均为零，时基推进、请求为零；
2048 点零偏 READY，B 约 −12.512 mA、C 约 −1.377 mA，软件母线约 2.720 V。
零偏保持同一冻结身份；未写采集 request、未发功率指令、未做非零板上电流对照。
该次记录结束时板保留默认关断 Observe 镜像；原定直接分流注入后移，当前先准备上文的有限电压开环。
来源、安装经过及只读范围见 [准备证据摘要](evidence/current-reference-preparation/summary.json)。

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
来源身份、模型配置及曲线见 [软件模型摘要](evidence/slot-foc-model/summary.json)。

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
[集成证据摘要](evidence/compute-stage/summary.json)。

当前板保留上述默认关断 Timing 镜像和冻结 COMPLETE。80 次 accepted 表示提交受理；
末拍关断取消其 pending，不能说全部 80 组已跨 UEV 生效。IRQ 标记未覆盖末标记后的
工作，有限窗口不是 WCET，也不是正式启停、真实反馈或带电 FOC 验收。
下一步继续电流增益/方向实物对照，再推进受控电角度标定和正式运行接入。

## 前次 20 / 10 / 1 kHz：分别完成无功率提交与计算窗口

已实现 20 kHz PWM、10 kHz 同步电流采样与计算、1 kHz 速度更新的装配。
RCR=3、首峰一次采样触发门及下一有效谷底的三相提交，共同定义 sample → compute →
actuation；完整 ADC 服务须在目标谷底前 10 µs 退出。机制及 ST 参考依据见
[PWM_TIMING.md](PWM_TIMING.md#20--10--1-khz-时序)。正式 Observe 的控制许可仍关闭。

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
[时序证据摘要](evidence/slot-timing/summary.json)。

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

[窗口原值、镜像与源码身份](evidence/runtime-timing/summary.json)保留完整归属。
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

[当前读数、编译策略和源码身份](evidence/hot-path-budget/summary.json)保留证据归属。
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

[失败读数、候选及源码身份](evidence/stage-budget/summary.json)与
[执行边界](history/runtime-diagnostics.md#历史无功率提交预算诊断)保留测量归属。下一步应先定位整链
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
[统计、源码身份和限制](evidence/controller-profile/summary.json)。执行接口见
[runtime 说明](history/runtime-diagnostics.md#历史后台无功率计算计时)。下一步应以无探针正式流水线核对
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

实际控制仍使用默认软件后端、`firmware/control.enabled=false`，Start 仍拒绝；本轮不开放 FOC。
[对照摘要、完整统计与源码身份](evidence/cordic-comparison/summary.json)保留双后端
各段总样本、clean 样本、IRQ 进入数及误差。执行接口和限制见
[runtime 说明](history/runtime-diagnostics.md#历史后台无功率计算计时)。离线检查通过：电角度 15、计时
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

[精选摘要与源码身份](evidence/compute-probe/summary.json)保留计数和原始 cycle 统计。
这些观测不能区分纯算法与抢占耗时，不构成完整 ISR、WCET 或约 15 µs stage 前段预算
通过的证据。实际控制许可继续关闭，后续先分段定位计算与抢占开销，再验收完整实时路径。

离线验证：计时模块 14、Target runtime 50 组 ARMCC / Unicorn 行为检查与主机工具
18 个替身测试通过，包括 Stop 被 ADC 抢占消费、母线越界立即撤销许可、旧快照/心跳拒绝，
以及最后一步计算后撤销完成结果。

另外新增 Motor 内部有限静态定向核心，固定 αβ 电压缓升/保持，独立机械位置窗口收敛后
仅给出双方向 offset 候选。15 组正式 ARM C 检查通过，含独立理想转子、跨零、重复位置、
堵转及零序公共模反例。它尚未装配到实体执行路径，没有实际定向；方向、极对数、相序及
电流极性仍须独立确认。完整语义见 [Motor 说明](../../../motor/README.md)。

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
均已在[摘要](evidence/foc-runtime-model/summary.json)和[曲线](evidence/foc-runtime-model/response.png)注明；
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
实现和复现入口见 [runtime](history/runtime-diagnostics.md#已知电流诊断采集)。

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
这些结果不证明实际电机闭环或实时预算，算法和复现入口见 [firmware/control](../../../control/README.md#foc-变换与电流-pi)。

Observe 已加入源码并构建为 0 Error / 0 Warning。Runtime 已接入电角度计算，但实际 Target
尚未确认极对数、方向和零位，角度保持无效；电流/速度 PI 尚未调用，Start 仍拒绝、Id/Iq 无效。
该轮离线构建 HEX 为 `4f41457fd3ec2190bd4d4e61a6285ae9f3faa1927a85f02418e92740a8885ee0`，
未下载到板；板上沿用下节已实测的 `c151e560…`。没有重新烧录、复位或复采旧波形。
已知电流对照的器材与节点准备见 [电流复核](CURRENT_CHECK.md#后续已知电流对照准备)。

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
[本次实测摘要](evidence/20261019-observe-feedback/current-zero-summary.json)
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

VOFA+ 已使用 JustFloat 接收十通道。[手动转轴截图](evidence/20261019-observe-feedback/manual-velocity-vofa.png)
显示 I2 机械速度曲线具有正、负响应及回到零附近的平台；仅按此范围完成手动反馈验收，
不推断实际转动方向定义或峰值精度。图中 I0 的混合名称是显示改名错误，I0 仍为时间。
本轮不证明速度精度、完整 ISR WCET 或 FOC。新增实测步骤见
[VOFA 操作说明](../../../../communication/host/vofa/README.md#首次无功率反馈验收)，不因此重复旧 PWM 抓图。

## 历史基础台架验收（2026-10-18）

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

当前照片见 [30% 稳态](evidence/20261018-pwm-dispatch/la-duty30-stable.jpg)、
[30% 包末尾](evidence/20261018-pwm-dispatch/la-duty30-packet-end.jpg)、
[70% 稳态](evidence/20261018-pwm-dispatch/la-duty70-stable.jpg)。
来源与哈希见 [30% 索引](evidence/20261018-pwm-dispatch/index.json)和
[70% 元数据](evidence/20261018-pwm-dispatch/la-duty70-stable.json)。
70% 图中两路数据共同截止，之后的空白不是测得的低电平；局部视图与命令计数不证明包长。
这些结果可保留为基础基线，**不代表 HCC 全部兑现，也不是可直接接电机的控制应用**。

## 历史证据归属

硬件修复后的静态与基本波形由旧基础镜像 `B5357289…` 取得；
2026-10-17～18 的保护注入、旧两档 duty、电流与位置观测属于 `29AB2861…`。
它们不转写成当前 `8b7fc787…` 的实测。完整逐次记录见 [COMMISSIONING.md](history/first-board-commissioning.md)。

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
旧基础波形镜像覆盖 28588 字节，完整身份见 [公开基础照片索引](evidence/20261018-baseline/index.json)。
[精选基础照片](evidence/20261018-baseline/README.md)保留原来源、读数与哈希。

## 历史台架进入正式控制前的检查表

以下保留无电机 PWM 阶段的检查边界，不是当前正式 Run 全部未完成的清单。
后续电流环、标定与有限 Run 的证据按本页顶部矩阵及逐次记录分别判定；
动态功率能力、精确测量和 WCET 未因有限试转而补记通过。

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
| 10～14 TIMING_* | 正常、截止前后、跨周期、取消时序注入 | 零 payload、母线低于 4 V、不 enable；见 [PWM_TIMING.md](PWM_TIMING.md) |

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

上述台架入口现位于 `MDK-ARM/BLDC_G431_Validation.uvprojx`；
日常正式入口为 `MDK-ARM/BLDC_control V1.uvprojx` 的 Run。新增源码同步相应工程成员并 Rebuild。
核对 guard、stage/enable/safe-off、Trip 最终地址；使用同次 AXF/MAP 获取布局，先核对芯片与 Flash/HEX。
烧录与带电测试分开；连续采集使用 HOTPLUG 并核对 S_HALT=0，halt/reset/download 诊断不混入连续证据。

## 主机工具与候选身份

`validation/tools/` 不自动信任当前构建。新版使用显式 `--candidate-manifest`，
生成方法、逻辑时序命令见 [PWM_TIMING.md](PWM_TIMING.md)；工具路径可由 `--cli` / `--gdb` 指定。
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

- [COMMISSIONING.md](history/first-board-commissioning.md)：完整现场历史、测量接法与人工确认，历史“下一步”不是当前指令。
- [硬件基线](../../../../hardware/BLDC_G431/HARDWARE_BASELINE.md)：修复与物理事实。
- [保护流程](PROTECTION_PROBE.md)、[旧 duty 检查](DUTY_CHECK.md)、[电流复核](CURRENT_CHECK.md)：分项复用条件与限制。
- 本机 [保护汇总](local-evidence/20261017-153714-powered-static/protection-stage-summary.json)、[旧 duty 汇总](local-evidence/20261017-duty-check/duty-stage-summary.json)：原事务链。
- 本机 [当前 PWM 记录](local-evidence/20261018-powered-pwm)：当前候选 `03-early-dispatch/` 及原始构建/实测；[收口前原文](local-evidence/20261018-powered-pwm/docs-before-cleanup)按原样保留。

`local-evidence` 仅供本机追溯，不随仓库分发；公开读者使用本页照片入口与精选索引。
原图、测量条件与限制共同支持结果摘要，未公开全日志，不声称可据此独立重算全部寄存器结论。
