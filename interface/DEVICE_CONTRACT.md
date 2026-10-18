# Device Contract

本文件定义当前默认关断入口的最小设备语义。FOC、标定、速度限制和故障恢复尚未实现，后续在实现对应能力时补齐条件；不能按目标生命周期推断当前已经可以启动电机。公共业务 API 见 `firmware/app/motor/motor.h`，UART 表示见 [VOFA 协议](VOFA_PROTOCOL.md)。

## 生命周期与请求

| 值 | 状态 | 当前含义 |
| --- | --- | --- |
| 0 | BOOT | 装配尚未完成 |
| 1 | IDLE | 装配完成、功率关闭；不表示标定或控制已就绪 |
| 2 | RUN | 为后续控制保留，当前无进入路径 |
| 3 | FAULT | 至少一个故障已锁存，功率关闭 |

Motor 由实时采样边界的唯一所有者写入；后台只提交完整请求，读取一致性只读快照。初始化装配在开放该边界前完成。请求格式正确、业务接受、进入 RUN 和功率使能是不同结果。

| 请求 | 完整意图及当前结果 |
| --- | --- |
| Start Velocity | 速度模式 + 一个有限的机械转速目标，单位 rpm；IDLE 下返回 NOT_READY，保持零目标 |
| Update Velocity | 更新当前速度模式的 rpm 目标；当前不在 RUN，返回 WRONG_STATE |
| Stop | 清目标并保持功率关闭；不清故障，FAULT 保持 FAULT |
| Clear Fault | FAULT 下返回 NOT_READY，不直接清锁存、不 re-arm、不重新启动；健康 IDLE 下可幂等接受 |

NaN / Infinity 目标返回 BAD_TARGET；有锁存故障时运动请求返回 FAULT_ACTIVE。尚未确定可执行速度范围，因此当前拒绝所有启动，不能把“有限数”视为安全速度范围。模式和目标不能分开写入。以后实现 RUN 后，更新只适用于当前模式；切换模式先回 IDLE。

命令结果编码以 `MotorCommandResult` 为准：NONE=0、ACCEPTED=1、NOT_READY=2、BAD_TARGET=3、WRONG_STATE=4、FAULT_ACTIVE=5、BAD_COMMAND=6。ACCEPTED 表示该业务请求已处理，不表示电机必然在转。普通拒绝不修改目标或消除故障。

## 故障与有效性

当前 G431 Observe 的锁存故障位在 `runtime/target_runtime.h` 定义：bit 0 装配失败、bit 1 电流采集无效、bit 2 实时采样间隔异常、bit 3 Hardware Trip 快照不可用或有保护 episode、bit 4 意外功率使能、bit 5 时钟条件错误。Hardware Trip 通知仅要求重新扫描，不能单凭通知建立新故障。

独立监视器可以先关断功率和锁存原因，Motor 在下一实时边界合并原因。ADC 停止时旧的 Motor publication 可能仍完整，但已不能代表当前状态；此时遥测清有效位，不能显示旧 IDLE 作为当前健康结论。缺失的速度估计和 d/q 电流同样输出无效，不用有效的零值代替。

Clear、Hardware Trip re-arm 和新 Start 必须分开；当前没有可用恢复流程，需要复位后重新装配。接收恢复、连接或重连均不得自动启动或重放旧请求。

## 后续补齐边界

受控使能前还需确定电流零点与增益修正、编码器方向/极对数/电角度偏置、反馈 freshness、电流与速度限幅、母线门控及运行截止保护，并实际完成控制计算、失效关断和台架验收。参数只开放真实调试需要的最小集合，不预建对象字典或多协议框架。
