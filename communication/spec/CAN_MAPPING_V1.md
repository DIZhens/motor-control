# CAN Mapping V1

本文件拥有Classical CAN精确帧映射与CAN事务规则；共享业务编号和结果见
[Motor Node Profile](MOTOR_NODE_PROFILE_V1.md)，Motor生命周期/权限见
[Device Contract](DEVICE_CONTRACT.md)。不套用UART的COBS、CRC、长度外壳或16 B Header。
合同和纯软件模型不代表FDCAN或外部物理总线已经验收。

## 链路与寻址

500 kbit/s、11-bit Standard ID、Classical CAN、DLC固定8、Little-Endian，不使用RTR或扩展帧。
一个静态Node控制一个Motor，默认Node 1，构建期可选择1…127；Node 0永久保留。

| CAN ID | 方向 | 语义 |
| --- | --- | --- |
| 0x000 | Host→all | 广播无会话Stop |
| node（0x001…0x07F） | Host→node | 指定节点无会话Stop |
| 0x080+node | node→Host | Fault Event |
| 0x100+node | Host→node | Session/Command Request |
| 0x180+node | node→Host | Command Response |
| 0x200+node | node→Host | State/Heartbeat |
| 0x280+node | node→Host | Motion Telemetry |

除明确的0x000广播Stop外，各段base+0均不作为普通节点帧。
1…127的所有服务ID必须全局唯一；指定Stop直接使用node，不能恢复为会与Fault段重叠的
`0x040+node`。本修正保留其他ID、布局、优先级和默认Node 1。
设备接收过滤器仅允许0、自己的node、0x100+自己的node；其余ID、扩展帧和RTR拒绝。

## Session/Command Request与Response

Request的0/2/4/5/6字节依次为：u16 session_tag、u16 sequence、u8 service_id、u8 flags、
i16 target_rpm_x100。flags固定0。

| service_id | 语义 |
| --- | --- |
| 0x00 | OPEN_SESSION |
| 0x01 | START_VELOCITY |
| 0x02 | UPDATE_VELOCITY |
| 0x03 | STOP |
| 0x04 | CLEAR_FAULT |

OPEN的tag由Host提出且非0，sequence=0、target=0。普通请求使用当前非0 tag，sequence为
1…0xFFFE，单调增加、不回绕；用尽前重新建会话。START/UPDATE目标范围±8000（±80 rpm），
OPEN/STOP/CLEAR的target必须0。CAN service编号只在此映射中定义，不能把OPEN映射为Motor命令。

Response固定为u16 session_tag、u16 sequence、u16 result_code、u16 motor_result。
包括OPEN在内均回显请求tag/sequence；OPEN成功以相同tag确认，不使用UART的session0响应外壳。
进入Motor后result_code=OK并忠实回传业务结果；未进入Motor为0xFFFF，包括OPEN、格式/会话拒绝、
IN_PROGRESS与取消。请求外壳ID/DLC/标准数据帧不可信时不提取响应身份，计数并丢弃。
完整普通帧的flags/未知service/target错误分别用共享UNSUPPORTED_FLAGS/UNKNOWN_COMMAND/
BAD_VALUE或BAD_PAYLOAD；OPEN组合非法用BAD_REQUEST。Codec校验表示，Server校验会话和序号。

OPEN在没有普通事务在途时接受/替换，普通在途则SESSION_BUSY。同tag幂等，不重置水位或缓存；
新tag清旧完成缓存、未发送普通响应与Event，重置接收水位并替换内部Telemetry订阅。保留已排队Stop的原身份。
任何OPEN不改变Motor、目标、Fault、Active/Staged或运行期限，不重放运动；复位使会话消失。

同tag/sequence、完整ID与8 B内容相同：在途仅返回最佳努力IN_PROGRESS，完成缓存内重放原终态，
不再执行。相同身份不同ID/内容为SEQUENCE_CONFLICT。已退休且不在4项缓存内为SEQUENCE_INVALID。
新拒绝也退休并缓存；外来tag拒绝不动当前水位。一次只有一个普通在途与一个独立Stop在途。
Stop仍经共享Device Port→Runtime优先Stop槽，取消未消费普通ticket并形成CANCELLED_BY_STOP；
已完成请求不改历史。完成响应和ticket准确关联，Motor最近一次结果不能作为事务完成证明。

## 无会话Stop

ID为0或本node；payload为u16 stop_sequence、u16 reserved0=0、u32 reserved1=0。
stop_sequence是任意u16（包括0与0xFFFF），不占用普通序号域。完整DLC/ID/reserved校验后才执行；
保留字段非法直接丢弃，不让错误高优先级帧取得Stop资格。
Response使用本节点0x180+node，tag=0，sequence回显stop_sequence。

完全相同帧在途只提交一次；不同Stop不能覆盖已有Stop。相同tag0/sequence但广播/指定ID不同，
属于不同内容，返回SEQUENCE_CONFLICT；不同sequence在已有Stop在途时BUSY。
完成后相同帧必须能够再次提交并完成；不查写session0完成缓存，不建立/替换会话或改变普通水位。
响应最佳努力，丢响应不撤销关断，也不代表跨多次执行恰好一次。

## State/Heartbeat、Motion与Fault

Heartbeat的0/2/3/4字节为u16 heartbeat_counter、u8 motor_state、u8 status_flags、u32 fault_flags。
默认100 ms产生，计数自然u16回绕；无会话也可发布，不把计数当作命令sequence。

| status_flags bit | 语义 |
| --- | --- |
| 0 | snapshot_valid |
| 1 | session_valid |
| 2 | error_warning |
| 3 | error_passive |
| 4 | bus_off_or_recovering |
| 5 | internal_fail_closed |

其余位0。snapshot无效时state/fault填0并清bit0，不以旧健康IDLE冒充当前真值。
Bus-off/内部Fail-closed可能令链路无法继续发送，不能以未收到bit4/5推断健康或保证最后通知。

Motion的0/2/4/6字节为u16 sample_sequence、i16 target_velocity_rpm_x100、
i16 velocity_rpm_x100、u16 valid_limit_flags。sample_sequence取实时publication序号低16位，
自然回绕；不因成功发送才编号，不分片完整Motor Telemetry。仅有效会话发送。
Server经Device Port取得SystemRuntime分配的非零subscription；新Session替换订阅并清旧槽，
同tag幂等OPEN不重建。仅发送与当前订阅匹配的值，复制期间迟到旧值不进入新域。
Bus-off/内部Fail-closed撤Session并结束订阅；恢复后仍无订阅，不发送旧Motion，显式新OPEN后
才恢复Motion。subscription与publication产生计数只用于内部资格，不替代实时sample_sequence。

valid_limit_flags bit0为目标有效、bit1为实际速度有效；bit8/9/10/11分别为速度PI输出限幅、
d轴积分限幅、q轴积分限幅、电压矢量/调制限幅。其他位0。
无效或不可表示数值填0并清相应有效位；不能把它显示为有效零速。
浮点转换先以IEEE binary32乘100，最近取整、半值远离0；舍入超出i16或非有限时判无效，
不钳位伪造合法数值。请求已经是i16定点，不进行二次浮点量化。

Fault Event为u32 newly_latched_faults、u32 all_faults，无时间戳或分片。
从两份有效设备快照的新增位形成最佳努力事件，首份仅建立基线；固定FIFO4满丢最旧并计数，
不替代Motor Fault真值或承诺捕获全部瞬态。Heartbeat/Motion为latest-value，Response不得覆盖。

## 时限、调度与控制器错误

Runtime完成上限20 ms，普通和Stop各一槽，完成缓存4、Response FIFO4、Fault Event FIFO4；
固定容量、无动态内存，不增加第二Communication任务。TX优先级为Stop/系统关键Response、
普通Response、Fault Event、Heartbeat、Motion；已经接受的单个硬件在途帧不抢写。
RX FIFO message lost、未知Completion/ticket/kind、Response容量损坏/溢出等内部不变量失败
进入Target Fail-closed；非法应用帧不建立Motor Fault。

Error Warning/Passive只记录和发布，不立即关断。Bus-off停止接受新请求，作废Session，
清未发送普通响应/Event/Motion，通过既有优先路径请求Stop，不重放旧Start/Update。
已有Stop保留；新增内部Stop无虚构Host响应。Target在任务上下文受控恢复硬件，并在所有旧
在途请求准确退休后确认恢复；无法证明一致则Fail-closed。恢复后仍无会话，Host需显式建新会话。
CAN 映射不得改变 Motor 生命周期、采样/计算/提交时序及 Target 的时长、位移或其他安全阈值。

## 验证与范围

固定黄金向量位于[can_v1.json](test_vectors/can_v1.json)，C/Python独立对照。
纯模型需覆盖全Node ID唯一性与base+0拒绝、元数据/值域/舍入、会话/重复/冲突/退休、
Stop在途一次与终态后再次执行、准确ticket/Completion、优先发送、溢出/未知完成Fail-closed。
控制器内部回环与正式Normal配置分开记录，内部回环不证明收发器、CANH/L、终端电阻、
真实ACK、仲裁、外部Bus-off/恢复、双节点或CAN电机运动。
