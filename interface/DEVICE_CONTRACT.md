# Device Contract

本文件定义公共 Motor 与受门控 ControlRuntime 的设备语义。控制链及跨拍启停已实现，具体装配决定是否具备运行许可；G431 Observe 关闭控制配置，仍拒绝启动。Run 使用本板有限空载配置，已完成正式 UART 正向启停与调速，其参数、实物依据及限制见 [Runtime 入口](../firmware/targets/BLDC_G431/runtime/README.md)。反向、独立电流精度标定、持续运行和故障恢复尚未完成。公共业务 API 见 `firmware/app/motor/motor.h`，UART 表示见 [VOFA 协议](VOFA_PROTOCOL.md)。

## 生命周期与请求

| 值 | 状态 | 当前含义 |
| --- | --- | --- |
| 0 | BOOT | 装配尚未完成 |
| 1 | IDLE | 装配完成、功率关闭；不表示标定或控制已就绪 |
| 2 | RUN | 本次受控使能成功后提交；当前 G431 Observe 配置仍不可进入 |
| 3 | FAULT | 至少一个故障已锁存，功率关闭 |

Motor 由实时采样边界的唯一所有者写入；后台只提交完整请求，读取一致性只读快照。初始化装配在开放该边界前完成。请求格式正确、业务接受、进入 RUN 和功率使能是不同结果。

| 请求 | 完整意图及当前结果 |
| --- | --- |
| Start Velocity | 速度模式 + 一个有限且在装配目标范围内的 rpm 目标；运行许可完整才接受，否则 NOT_READY |
| Update Velocity | 仅 RUN 接受当前速度模式的有界 rpm 目标；其他状态返回 WRONG_STATE |
| Stop | 清目标并保持功率关闭；不清故障，FAULT 保持 FAULT |
| Clear Fault | FAULT 下返回 NOT_READY，不直接清锁存、不 re-arm、不重新启动；健康 IDLE 下可幂等接受 |

NaN / Infinity 或超出已配置目标范围时返回 BAD_TARGET；有锁存故障时有限运动请求返回 FAULT_ACTIVE。
普通拒绝保留原有 RUN 目标。模式和目标不能分开写入；更新只适用于当前模式，切换模式先回 IDLE。

运行许可要求显式开启控制装配，电流增益/方向、电角度和实时采样/调制条件均已确认，且当拍
电流、位置、预测角、母线和数值控制配置有效。Observe 没有该运行许可，仍返回 NOT_READY；
Run 的有限许可不扩大成其他硬件、精度或持续运行承诺。
Start 接受时保持 IDLE 和零活动目标，完整待启动目标由 Runtime 保存；当拍 safe-off 清旧 epoch，
下一拍 stage 中性 duty，再下一拍跨过更新边界、重新 stage 后才 enable。enable 成功才提交 RUN
和活动目标。准备期间重复 Start 不覆盖旧请求，Update 仍为 WRONG_STATE，Stop 撤销准备流程。
ACCEPTED 不能作为功率已经使能的证明。

命令结果编码以 `MotorCommandResult` 为准：NONE=0、ACCEPTED=1、NOT_READY=2、BAD_TARGET=3、WRONG_STATE=4、FAULT_ACTIVE=5、BAD_COMMAND=6。ACCEPTED 表示该业务请求已处理，不表示电机必然在转。普通拒绝不修改目标或消除故障。

## 故障与有效性

G431 的锁存故障位在 `runtime/target_runtime.h` 定义：bit 0 装配失败、bit 1 电流采集无效、bit 2 实时采样间隔异常、bit 3 Hardware Trip 快照不可用或有保护 episode、bit 4 意外功率使能、bit 5 时钟条件错误；Run 另用 bit 6 表示有限净位移越界。公共 `control_runtime.h` 另定义 bit 8 活动反馈失效、bit 9 控制计算/状态失败、bit 10 stage/enable 失败、bit 11 功率状态不符、bit 12 校正相电流超阈值。Hardware Trip 通知仅要求重新扫描，不能单凭通知建立新故障。

独立监视器可以先关断功率和锁存原因，Motor 在下一实时边界合并原因。ADC 停止时旧的 Motor publication 可能仍完整，但已不能代表当前状态；此时遥测清有效位，不能显示旧 IDLE 作为当前健康结论。尚未取得或已经失效的速度估计，以及未完成可信标定的 d/q 电流，均输出无效，不用有效的零值代替。

Stop 清活动目标和控制积分、使旧 actuation epoch 失效，保留正常冻结的电流零偏；活动反馈、
控制或功率失败则关断并锁故障，撤销校准资格，不自动重试。Clear、Hardware Trip re-arm 和新
Start 必须分开；当前没有可用恢复流程，需要复位后重新装配。接收恢复、连接或重连均不得自动启动或重放旧请求。

## 后续补齐边界

G431 Run 承接原板相序、电流符号、编码器方向/极对数/偏置的有限台架证据；正式 Motor / UART 已完成正向启停与调速，反向、时长边界实机触发及长期稳定性仍需补齐。独立电流幅值精度、持久标定及故障恢复另行补齐，数值模型不替代实物条件。参数只开放真实调试需要的最小集合，不预建对象字典或多协议框架。
