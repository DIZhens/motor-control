# 有限电压开环试转

本流程用于在现有器材条件下验证三相驱动、电机转动、SSI 位置反馈及带电采样的基础关系。
它不要求先完成外部分流注入精度标定，也不将电流增益、电角度或 FOC 闭环标记为已验收。
运行许可与正式 Motor Start 分开；不改变默认关断的 Observe 入口或公共 IDLE 语义。

## 构建与复用范围

使用现有 Keil Target `BLDC_control V1`。`target_validation.c` 增加单次开环命令，
旧命令、20 字节邮箱和原诊断快照布局保持兼容，不增加第四个 Target。
纯 C 的 `firmware/app/motor/voltage_open_loop.c` 只生成电压与 duty 意图，
台架负责实际 staging、使能、独立期限及故障关断。它不是另一个长期电机应用。

复用 20 kHz PWM、10 kHz 同步提交、硬件 Break、TIM6 独立守卫、母线采样与 SSI。
本轮不运行电流 PI 或速度 PI；编码器只作反馈记录与有效性检查。
三相名义电流扣除本次冻结零偏后用于辅助异常监视，不能作为已经校准的独立电流保护。

## 本轮候选包络

| 项目 | 限制或初始请求 |
| --- | --- |
| 实际母线 | 10～14 V；现场使用 12 V |
| 电压矢量模长 | 初始 0.3 V；固件与工具硬上限 0.5 V |
| 有符号电频 | 初始 +2 Hz；绝对值不超过 5 Hz，禁止零值 |
| 固定方向缓升电压 | 0.5 s |
| 固定方向保持 | 0.5 s |
| 电频从零缓升 | 0.5 s |
| 恒频旋转 | 1 s |
| 保持电频、缓降电压至零 | 0.5 s |
| 正常功率流程 | 约 3 s，另有 staging 开销 |
| 独立总期限 | 从命令接收计 3.5 s；到期关断并报告超时 |
| 名义相电流辅助撤销阈值 | 任一相绝对值达到 0.25 A；尚非实物精度或硬件保护资格 |

电频是磁场每秒旋转的电角圈数，不是机械转速。在转子连续同步跟随且极对数确为 7 时，
2 Hz 对应约 17.1 rpm；失步时该换算不能代表实际速度。
实际电压、初始摩擦与负载可能使低幅值试验只表现为定向或轻微运动，不能据此直接增加参数。

## 一次现场流程

1. 在无外部母线条件下安装并核对候选镜像。安装不提交开环请求，也不自动启动。
2. OUTPUT 关闭时，断开 ST-Link 到板子的 3V3 供电线，保留 SWD 和 GND；
   恢复正常三相与 SSI 接线，撤去所有外部电流注入支路，固定电机、使轴无外部负载。
   接线与实际供电由操作者确认后，再恢复 12 V 供电。
3. 功率保持关闭、转轴静止时，只采一次现有 `CAPTURE_CURRENT` 的 4096 点窗口
   （10 kHz 下约 0.4096 s）。均值绝对值不超过 0.10 A、峰峰值不超过 0.05 A，
   完整窗口合格后将 B/C 均值作为本次试验冻结零偏。该窗口不证明增益或电流正方向。
4. 核对镜像、持续推进的采样、健康保护、实际关闭状态、母线实测与软件读数、
   静止 SSI 和同次启动零点证据，再显式提交一次开环命令。
5. 观察轴的定向与运动；程序正常缓降结束或出现异常时关断，不自动重试。
   读取最终位置位移、名义电流峰值、提交/使能计数、终止原因和实际 MOE。

本轮不需要示波器持探头，也不需要给 R32 焊接引线。
本台架通过配套 SWD 工具提交单次命令、读取结果，尚未接入 UART / VOFA 命令；
现场需要立即停机时使用电源 OUTPUT。带电试验期间不暂停 CPU 或设置断点。
电源 CV/CC 和输入电流用于观察母线供电，不能将母线平均电流直接当作相电流。
发生 CC、明显抖动、异常发热或声响时，由操作者关闭 OUTPUT，并保留失败记录。

## 邮箱和结果边界

新命令编号为 `15`，保留原时序命令 `10..14`。功率钥匙仍为 `0x4310A11E`，
邮箱 `duration_us=3500000`、`common_duty=0.5` 固定；另一个 8 字节 payload
`g_target_validation_open_loop_config` 提供电压模长和有符号电频。
先写完整参数，再写邮箱 payload，最后提交新的非零 sequence；消费后钥匙立即清除。

`g_target_validation_open_loop` 独立发布本次试验结果，使用前后相同偶数版本读取。
ADC 不再运行时，该快照可能停留在旧状态；独立 `g_target_validation_stop_reason`
及真实 TIM1 MOE 用于确认关断，不能仅以旧 `active` 字段判断当前仍在运行。
主机超时不得再次发送原功率请求，也不得自动复位、清故障或重新使能。

后续进入电流闭环前仍须核对电流通道、极性与增益量级，完成电角度映射和参数确认。
这里得到的机械运动或统计结果不自动安装为可信标定。

## 离线检查与工具

```powershell
.\.venv\Scripts\python.exe -B -X utf8 firmware/app/motor/tests/verify_voltage_open_loop.py
.\.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/validation/tests/verify_model.py
.\.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/validation/tests/verify_open_loop_tool.py
```

配套工具默认只冻结编译产物、读取 DWARF 并核对 HEX / AXF / MAP，不连接硬件：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/validation/tools/open_loop_once.py --output <新的候选目录>
```

`inspect` 只读检查供电正常的目标；`zero` 在确认现场条件后采零；`execute`
需指定同次启动的零点报告，并显式确认接线、供电与允许轴转动；`stop` 仅发送 SAFE_OFF。
所有硬件动作都必须显式给出 `--action` 和冻结的 `--prepared`，不会由 prepare 隐式触发。
各参数及现场确认项以 `--help` 为准。实际连接前先完成候选核对，禁止根据地址旧记录直接写 RAM。

运行期 SWD 可能恰逢 TIM1 UP / BRK IRQ 为防止通用 HAL 消费 UIF 而暂时清 UIE，
不能将单个 UIE=0 读数当成时序停止。新工具保留静态配置核对，并检查两份观测之间
10 kHz callback 和 20 kHz ControlTime 的持续推进；实际采样停滞仍拒绝。
对于已经完整采样、只因该 Host 检查误判而结束的零点，可以通过内部只读恢复函数，
严格核对原始发布、唯一 SAFE_OFF 链及当前同次启动计数后沿用原窗口。
其他失败不因此自动获得恢复资格，失败记录也不改写为成功。
