# G431 默认关断观察入口

`BLDC_G431_Observe` 在现有 Platform 上装配公共 Motor、编码器速度观测和 UART / VOFA。
当前装配的 `control.enabled=false`，只执行观察与诊断，所有 Start 均拒绝进入 RUN。
公共 Runtime 已有受门控的 FOC 控制链；当前实机电流、电角度和时序许可尚未确认。
它为反馈与标定接入提供入口；实际验收范围统一见
[Target 验证页](../VALIDATION.md)。

## 构建与入口

打开 `../MDK-ARM/BLDC_control V1.uvprojx`：

| Keil Target | 入口 | 输出 |
| --- | --- | --- |
| `BLDC_control V1` | 原 `validation/target_validation.c`，保持原台架工具 ABI | `BLDC_control V1/` |
| `BLDC_G431_Observe` | 本目录 `target_runtime.c` + `app/motor`、`app/communication`、`control` | `build/observe/` |

选择 Target 后使用 Rebuild。CLI 示例（`UV4.exe` 路径按本机安装调整）：

```powershell
& 'D:\keil5\UV4\UV4.exe' -r 'firmware\targets\BLDC_G431\MDK-ARM\BLDC_control V1.uvprojx' -t BLDC_G431_Observe -o "$env:TEMP\g431-observe-build.log"
```

`BLDC_G431_OBSERVE_ENTRY` 仅在新 Target 定义；`main()` 和 TIM6 IRQ 的 USER CODE
选择对应入口。两者共享 CubeMX 配置及 Platform，不改生成区，不同时绑定两个 consumer。
烧录是独立的现场步骤；编译不自动连接 ST-Link 或下载镜像。

## 执行职责

- 装配先 safe-off，再核对时钟、绑定 sample / trip、arm position / bus / realtime / protection，
  最后启动持续运行的 Drive Timing；这不等于功率使能。装配失败保持 FAULT 和关断。
- Target 保留硬件装配、板级时序/采样检查、独立守卫、邮箱临界区和 UART 字节收发。
  ADC sample callback 调用 Motor 内部 `control_runtime_step()`；该实时边界是运行期
  Motor、观测缓存和估计器的唯一写者。故障和 Stop 优先，随后更新反馈并消费其余完整命令。
  Stop 优先于普通排队请求，并丢弃更早的待处理命令。
- TIM6 每 50 µs 检查独立 DWT 时钟和采样心跳；间隔超过 75 µs 锁存故障并 safe-off。
  最迟在后续一次 TIM6 检查发现；它不是完整 ISR WCET 或未来带载控制截止证明。
- 后台以不等待的寄存器轮询收发 USART1，每次至多一字节；44 字节发送缓存完成前不被
  新快照覆盖。遥测来自有界重试的一致性 publication，不把 UART 放入 ADC 回调。
- 实时侧 publication 保存协议无关的 `MotorSnapshot`；后台经 `motor_vofa` 映射到
  原十通道格式。publication 按 100 Hz 或命令/故障/反馈有效性变化生成，后台约 100 Hz
  编码。当前可发布状态、结果、目标零值、控制时间、有效母线电压与机械速度；Id/Iq 无效。

## 编码器反馈

当前仅支持 Platform 提供的周期单圈位置，每 20 个 fast tick 读取一次 latest observation。
只在采集时间戳前进时更新纯 C `PositionEstimator`，以实际采集间隔计算最近周期差，
不把重复轮询的旧位置当作新采集。速度正负沿编码器机械坐标，尚未标定为电机正转方向。

首份合法位置只建立基线，第二份开始给出速度；低通时间常数为 10 ms。观测排歧界限暂设
3000 rpm，仅说明估计器的运动假设，不是电机运行限值，也不能保证识别超过该假设的混叠。
位置年龄超过 100 tick（5 ms）、时间回退/超前、采集无效、未 referenced 或坐标非法时，
丢弃估计历史。恢复需要两份新的合法位置。暂时读不到 coherent publication 时保留尚未
过期的缓存，不伪造采集失败。母线也保留尚未过期的完整缓存。

反馈有效性变化立即更新 publication，不等到下一次 100 Hz 周期；已经开始发送的完整旧帧
仍按历史快照发送完毕。缺少速度反馈时保持观察状态 IDLE，不把尚未具备运行条件伪装成
硬件 Fault。该入口仍未完成电角度标定或 FOC，获得有效速度也不能接受 Start。

ControlRuntime 已接入纯 C 电角度映射与短时预测：缓存合法原始机械位置，以其采集时间
计算年龄，在每个同步 fast tick 更新角度。极对数、方向和偏置通过数值检查后，还需
装配方独立确认实际标定。当前 Target 的该确认标志为 false、参数为空，电角度始终无效。
采样时间重复、跳拍或回退时清位置与角度历史、重置观测分频，异常拍不消费新位置。
位置失效后恢复需要两份新的合法观测；单纯预测超时只撤销角度，仍保留尚新鲜的速度。

预测使用当前低通后的编码器坐标速度；加减速误差、SSI 实际延迟与 PWM 生效时刻的关系
仍需在实际控制前验收。电角度方向确认后，速度反馈也按该方向统一坐标；未确认时保留
当前编码器坐标。仅电角度配置不能开放功率或有效 Id/Iq，还需独立的电流和时序确认。

## 无功率电流零偏

当前 Observe 构建的启动校零许可仅适用于电机母线电源、电机三相及外部注入源均已断开的现场条件。
该许可不是硬件检测结果，不能照搬为正式带电入口的通用启动策略。
Motor 内部 ControlRuntime 在健康 IDLE、当拍 MOE 关闭、有效速度绝对值不超过 5 rpm
持续 200 拍后，收集连续 4096 拍 B/C 名义电流；20 kHz 下窗口约 204.8 ms。
相邻采样时间必须相差一个 fast tick，重复、跳拍或回退均撤销零偏资格。

每相窗口均值绝对值上限暂为 0.10 A，峰峰值上限 0.05 A；这两个数是本次工程候选门限，
不是传感器精度、运行电流限值或已通过实测的统计范围。合格窗口后冻结 offset，再逐拍
校正 B/C、重构 A 并计算 αβ；历史零偏不硬编码到本次窗口。
校零窗口中失去静止反馈、离开 IDLE 或功率关闭条件，会锁定 FAILED，不自动重试。
READY 后只校正，不重新学习；正常 RUN 与 Stop 保留冻结偏置，坏采样、时间异常和故障撤销校准资格。

`s_control_runtime.current_measurement` 保存状态、首因、样本数、窗口均值/极值、
冻结 offset 和相电流/αβ 有效性，供本地诊断。它不改变十通道 VOFA 格式；当前实机的
控制配置和可信标定尚未开放，Id/Iq 仍为 NaN。READY 只证明本次零偏窗口满足软件门限，增益、极性和实际采样窗口仍待实测。

可移植电流 PI、速度 PI、Park 和 SVPWM 已接入公共 Runtime，当前装配仍关闭控制配置；
最新构建和已完成的无功率诊断实测身份见 Target 验证页；这不开放实际控制链。

## 受门控的控制链

`ControlRuntimeConfig.control` 装配电流/速度 PI、慢环分频、母线范围、相电流阈值和电压作用
时延。数值参数与三项实物确认分别检查：电流增益/方向、电角度、采样/调制时序均可信，
且本拍反馈完整、新鲜、在范围内，才允许接受 Start。控制关闭时整组参数不参与观察流程。
电压利用因子、控制时基与周期还须符合 HCC facts。

Start 当拍先 safe-off 清旧 epoch，并保持 IDLE/零活动目标；下一拍 stage 三相 0.5 duty，
再下一拍跨过确定 update boundary、重新 stage 中性 duty 后调用 enable，成功才提交 RUN。
RUN 每拍执行电流环并 stage；速度环在首次运行拍计算，其后整数分频，输出在两次计算间保持。
Stop 清目标、待启动请求和两环积分，保留正常冻结零偏；任何活动反馈、计算、提交或功率错误
均主动关断、锁故障，不自动重启。普通非法运动目标只拒绝命令，保留已有合法 RUN 目标。

电流预处理按实测相执行 `signed_gain*(nominal-offset)`，再重构缺相和 Clarke；未启用增益
修正时按 1 处理。零偏窗口及其门限始终使用名义 A，修正不会回写窗口。每相系数的数值检查
与实物标定确认分离。
本拍 Park 使用同步采样角；inverse Park 与限幅回算使用独立的电压作用参考角。
`actuation_prediction_time_s` 由装配显式提供，不能从“下一周期”推定准确物理延迟。
速度 PI 只跟踪自身 Iq 限值，电流 PI 跟踪调制器电压限幅；实际电压饱和下的级联动态仍需整体验收。

当前 G431 控制配置为零、运行许可关闭，Target 仍把意外 MOE 视为故障；模型中的合法使能
不表示这个实机入口已开放。尤其三角函数、PI 和 SVPWM 的真实执行时间必须满足当前约
15 µs 的 stage 前段预算；模型与编译不证明该预算。

## 后台无功率计算计时

`compute_probe.h/.c` 使用独立算法状态和明确的合成输入，调用正式电流预处理、电角度、
速度 PI 与电流 PI。六例覆盖正常、速度保持、独立施加角、限幅及两条预期拒绝路径，
每例 16 次，共 96 次。主循环每轮至多执行一例，duty 只保存为诊断，不提交硬件。
它不调用真实 ControlRuntime，不改物理标定确认，不 stage、不 enable、不清故障。

主机只向 `g_target_runtime_compute_request` 写一次非零新身份。后台是实例、身份消费和
结果发布的唯一写者；开始及终态通过 `g_target_runtime_compute_sequence` 发布
`g_target_runtime_compute_result`。ACTIVE 只含开始状态，COMPLETE 才含完整统计。
重复身份不重放；活动请求遇新身份锁存 BUSY，不隐式运行替代请求。

ADC 同拍发布 IDLE/OFF、控制关闭且母线有效、处于 0..3.6 V 的诊断许可；许可变化立即
发布。后台在计算前后另查 MOE、guard、邮箱、DWT 与采样心跳；只在极短临界区读取心跳
时间对，计算期间不屏蔽中断、不清零 DWT。Stop 在后台直接取消，即使 ADC 已消费并
清掉 Stop pending，也不能漏取消或同轮重开。最后一步后失去健康条件会撤销 COMPLETE。

cycle 读数包含中断抢占和两次测量调用的开销，按 170 MHz 配置换算；200 µs 的单例诊断
上限只用于拒绝异常读数，不是实际控制提交预算。它不是完整 ISR 时间、WCET 或 stage
截止验收。背景诊断也会延后 UART 轮询，不能同时把它作为串口高负荷测试。

[`tools/compute_probe.py`](tools/compute_probe.py) 默认只冻结并核对同次 HEX/AXF/MAP、
真实 DWARF 字段和短枚举。只有显式 `--measure --logic-only-confirmed --vbus-dmm-v <实测值>`
才 HOTPLUG 进行一次请求；失败不补发，不烧录、不复位。现场须由操作者确认母线电源、
电池、三相和外部注入源断开。实测结果及解释见 [验证页](../VALIDATION.md)。

```powershell
.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tools/compute_probe.py --build-dir <同次构建目录> --expected-hex-sha256 <完整SHA256> --output <新目录>
```

## 已知电流诊断采集

`current_capture.h/.c` 是此 Target 的无功率诊断统计器，不属于 Motor 命令、控制算法或
UART 协议。仅在健康 IDLE、当拍功率关闭、零偏 READY 且位置速度有效静止时，接受一次
固定 4096 点的原始 B/C 电流窗口。请求当拍是首点，20 kHz 下窗口约 204.8 ms。
start 时复制冻结零偏；窗口只计算原始均值、极值及无偏样本方差，不学习或应用增益。
主机另以原始均值/极值减冻结零偏得到校正统计，方差保持不变。

调试入口仅允许主机向 `g_target_runtime_current_request` 写一个非零新序号；保持同一
请求序号不会重放。开始、完成或失败时，用奇偶版本号锁存
`g_target_runtime_current_capture`，并同拍更新现有业务快照。
ACTIVE 的锁存内容不逐拍变化，只有 COMPLETE 且 count=4096 才是完整结果。
Stop 优先撤销窗口；采样、时间、位置、静止条件或功率/故障状态异常会失败锁存。
活动窗口收到另一新请求时丢弃旧统计并返回 BUSY，不开始替代窗口。
ADC 停滞时结果可能留在 ACTIVE，主机仍须检查独立 guard 并有界超时。

配套 CLI 为 [`tools/current_capture.py`](tools/current_capture.py)。默认仅离线冻结并核对
HEX/AXF/MAP、派生类型布局，不接设备；`--acquire` 才执行 HOTPLUG 前检和一次 request 写入。
它核对镜像、CPU running、MOE、guard、健康时基/母线、冻结零偏及请求空闲状态，
采后复核完整结果和前后状态，保存原始数据与万用表参考值；失败不补发请求。
版本竞争只允许有界的只读复核，不重新采集。ARMCC 枚举宽度从同次 AXF 派生，不固定假设四字节。

实际采集需先确认电机母线电源及电机三相断开，启动校零时外部注入源断开；
零偏 READY 后再接入限流注入支路。注入期间不得复位或重新启动自动校零。
脚本不烧录、不复位，也不提供功率命令；该统计入口的非零电流采集尚未执行，器材与焊点确认后再做针对性实测。
参考方向、接点与既有台架记录见 [电流复核](../validation/CURRENT_CHECK.md#后续已知电流对照准备)。

## 通信与运行监视

UART 错误、超长/非法行和 100 ms 未完成片段均丢弃至下一 LF；UART 无连接会话检测，
不会因重连自动启动。邮箱容纳一个普通请求，拥塞或非法帧记录 BAD_COMMAND；Stop
覆盖排队请求。收到 Stop 后、消费前到达的普通请求也丢弃，调用方不要把遥测当可靠逐条应答。

启动成功但 ADC 没有首拍，以及 ADC 停滞后尚未合并的 guard 原因，都会使旧遥测失效。
已在途的 UART 帧是过去的快照，接收端还需留意数据是否持续更新。当前故障恢复未实现，
Clear 不会清锁存；独立 guard 原因可在 Watch 中查看 `g_target_runtime_guard_faults`。

## 离线验证与下一步

从仓库根目录，使用 [开发依赖](../../../../DEVELOPMENT.md#44-本地验证依赖) 中的 Python：

```powershell
python firmware/app/motor/tests/verify_motor.py
python firmware/app/motor/tests/verify_rotor_alignment.py
python firmware/app/communication/tests/verify_vofa.py
python firmware/app/communication/tests/verify_motor_vofa.py
python control/tests/verify_position_estimator.py
python control/tests/verify_current_measurement.py
python control/tests/verify_electrical_angle.py
python control/tests/verify_speed_controller.py
python firmware/targets/BLDC_G431/runtime/tests/verify_current_capture_model.py
python firmware/targets/BLDC_G431/runtime/tests/test_current_capture_tool.py
python firmware/targets/BLDC_G431/runtime/tests/verify_compute_probe_model.py
python firmware/targets/BLDC_G431/runtime/tests/test_compute_probe_tool.py
python firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py
python firmware/targets/BLDC_G431/runtime/tests/verify_foc_runtime.py
```

测试编译并执行实际 ARM C，覆盖默认关断、完整命令、Stop 优先、错误丢弃、启动失败、
ADC 丢失、旧快照失效、低优先级代码被 ADC 抢占的交错，以及位置跨圈、真实采集间隔、
重复/过期/无效观测、两点恢复和无命令干预的有效性发布。电流增量覆盖显式许可、
静止等待、有限零偏窗口、冻结校正及故障/运动/时间异常下的失效锁定。电角度增量覆盖
未确认标定、延迟消费、真实采集间隔、低通后外推、预测超时与观测过期的不同恢复条件，
以及时间异常和故障撤销角度。独立 FOC Runtime 模型使用真实 ARM C 和 HCC 边界替身，
覆盖受控启停、内外环分频、正反目标、有符号电流修正、测量/施加角和失败关断。
它通过实际跨拍 duty 驱动独立双精度 PMSM，合成对象包含反电势、坐标耦合和机械负载，
采用固定理想母线；不包含真实回灌、死区、采样噪声或指令执行周期。
诊断采集增量覆盖冻结偏置后的连续原始统计、
锁存/请求身份、重复请求、BUSY、Stop 与异常中止；主机替身验证前检失败零写入、
单地址一次写、应答丢失不补发和采后复核。模型不代替 UART 链路、
实机速度精度或 ISR WCET；软件与实机结论分别记录在 Target 验证页。
既有 UART、手转反馈、逻辑供电零偏及本次后台计算计时各有有限实测；下一步先对计算与
抢占开销作分段定位，并推进已知电流增益/方向对照，随后安排受控电角度标定和 FOC。
各轮镜像与实测范围、既有台架证据仍从
[VALIDATION.md](../VALIDATION.md) 进入，按具体修改影响选择复测。

可为独立 MATLAB 复算导出轨迹：

```powershell
python firmware/targets/BLDC_G431/runtime/tests/verify_foc_runtime.py --trace-output "$env:TEMP\motor-foc-model\closed-loop.csv"
```

同目录同时生成 `closed-loop.metadata.json`，记录合成对象、控制配置和每行积分前状态／
随后 ZOH 电压的时间语义。MATLAB 从仓库根目录调用
`verify_foc_trace(csv_path, metadata_path, report_path)`，先将本目录 `tests/` 加入 path。
仅依赖基础 `ode45`，不需要 Simulink；抽取起步、降速、反转切换和 Stop 附近的连续区段，
每段只取首状态，随后累计独立积分比较。误差表示两个数值对象的一致性，不能解释为传感器精度。
[集成检查点 130a410 的软件模型摘要与曲线](../validation/evidence/foc-runtime-model/summary.json)
保留原源码身份，明确区分模型与实物范围；后续后台诊断的实测不改写这份模型证据。
