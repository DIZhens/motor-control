# Core Protocol V1：UART 映射

本文件是本阶段唯一权威的 Wire Contract。Firmware 与 Python Host 的实现及黄金向量均以
本文件为准；[设备合同](DEVICE_CONTRACT.md)继续拥有 Motor 语义，
[设计说明](PROTOCOL_DESIGN.md)只保留设计边界。合同冻结不表示固件、Host或实物验收已完成。
VOFA 是独立验证构建，正式镜像不自动探测或混发两种格式。CAN 后续另定帧映射。

## 1. 字节与帧

USART1 使用 460800 bit/s、8N1、无流控。多字节整数为 Little-Endian，浮点为 IEEE754
binary32，显式序列化，不发送 C struct。无效浮点统一为位型 `0x7FC00000` 且清有效位；
请求中的 NaN/Inf 不是无效值占位，必须按 BAD_VALUE/BAD_TARGET 拒绝。

线上帧为 `COBS(header + payload + crc16) + 0x00`，没有额外 SOF。

| 原始偏移 | 字节 | 字段 | 约束 |
| --- | --- | --- | --- |
| 0 | 1 | version | 1 |
| 1 | 1 | kind | 下表四类 |
| 2 | 1 | flags | V1所有位为0 |
| 3 | 1 | header_size | 16 |
| 4 | 2 | payload_length | 0..110 |
| 6 | 2 | message_id | 下表编号 |
| 8 | 4 | session_id | 会话身份 |
| 12 | 4 | sequence | 消息序号 |
| 16 | N | payload | 精确布局 |
| 16+N | 2 | crc16 | Little-Endian |

decoded 长度严格等于 `16 + payload_length + 2`，最大128 B；COBS候选最大129 B，
含分隔符最大130 B。超长进入 discarding，直到下个0x00再恢复。空候选忽略；非法COBS、
CRC或不可信长度不产生业务请求或响应。不得接受尾随垃圾。

CRC-16/CCITT-FALSE：width16、poly0x1021、init0xFFFF、refin/refout=false、xorout0。
输入仅为16 B header与payload，不含CRC、COBS和delimiter；`123456789`结果为0x29B1。
先校验COBS、长度和CRC，再检查Header语义。已通过完整性检查的REQUEST可按下表返回版本、
flags、消息或payload错误；不能用不可信Header猜测Stop。

| kind | 编号 | message_id |
| --- | --- | --- |
| REQUEST | 0x01 | OPEN_SESSION=0x0001，READ_OBJECT=0x0002，WRITE_OBJECT=0x0003，EXECUTE_COMMAND=0x0004 |
| RESPONSE | 0x02 | 回显REQUEST的message_id |
| TELEMETRY | 0x03 | SPEED_COMMISSIONING_SAMPLE=0x1001 |
| EVENT | 0x04 | STATE_CHANGED=0x2001，FAULT_LATCHED=0x2002 |

设备只执行REQUEST；错误kind或未知message计入message_errors，不解释为业务命令。
未知REQUEST message可返回UNKNOWN_MESSAGE。所有reserved字段为0，接收请求非0按BAD_PAYLOAD拒绝。

## 2. 请求、响应与结果

| 请求 | 精确payload | 成功响应body |
| --- | --- | --- |
| OPEN_SESSION | u32 proposed_session_id，非0 | u32 accepted_session_id |
| READ_OBJECT | u16 object_id | object_value |
| WRITE_OBJECT | u16 object_id + 精确write value | authoritative read value |
| EXECUTE_COMMAND | u16 command_id + 精确command payload | u16 motor_result + u16 reserved=0 |

所有响应payload先放 `u16 result_code + u16 subject_id`，再放body。Header回显原REQUEST
的message_id、session_id、sequence；OPEN_SESSION响应Header仍为session0，成功body才放会话。
subject_id为object/command ID，OPEN_SESSION及无法取得subject的错误为0。
READ/WRITE和OPEN_SESSION失败只有4 B前缀；EXECUTE_COMMAND即使事务失败仍带4 B body，
未交给Motor时motor_result=0xFFFF。执行过Motor时result_code=OK，motor_result忠实回传
MotorCommandResult（0..6），包括NOT_READY等业务拒绝，不把拒绝改成通信超时。

| 域 | result_code |
| --- | --- |
| 成功 | OK=0x0000 |
| 请求 | BAD_REQUEST=0x0101，UNKNOWN_MESSAGE=0x0102，BAD_PAYLOAD=0x0103，UNSUPPORTED_FLAGS=0x0104，UNSUPPORTED_VERSION=0x0105，UNKNOWN_COMMAND=0x0106 |
| 会话/序号 | SESSION_REQUIRED=0x0201，SESSION_INVALID=0x0202，SEQUENCE_CONFLICT=0x0203，IN_PROGRESS=0x0204，SEQUENCE_INVALID=0x0205，SESSION_BUSY=0x0206 |
| 对象 | UNKNOWN_OBJECT=0x0301，READ_ONLY=0x0302，BAD_VALUE=0x0303，OBJECT_WRONG_STATE=0x0304，LENGTH_MISMATCH=0x0305 |
| 事务 | BUSY=0x0401，CANCELLED_BY_STOP=0x0402，PRECONDITION_FAILED=0x0403 |
| 系统 | SYSTEM_FAIL_CLOSED=0x0501，INTERNAL_ERROR=0x0502 |

完整但字段组合非法用BAD_REQUEST；对象值精确长度错误用LENGTH_MISMATCH，请求外壳错误用
BAD_PAYLOAD。请求的单位、范围和业务权限由本合同及Device Contract共同约束。
每次普通执行只形成一个终态；重复请求的IN_PROGRESS是非终态提示，不写入已完成缓存。

## 3. Session与Sequence

OPEN_SESSION Header.session_id=0，proposed_session_id非0。普通事务在途时返回SESSION_BUSY，
旧会话不变；否则同一proposed ID幂等返回已有会话，不重置序号、缓存或Profile。
替换会话清旧去重缓存、未发送普通响应和Event FIFO，重置会话事务与消息序号。
已被TX DMA接受的旧帧不可撤回，Host按Header中的Session过滤。
新会话不改变Motor、Staged/Active、目标、功率或1.5 s时限。复位使会话消失。
Host建会话后重新读MOTOR_SNAPSHOT（含fault_flags）、SPEED_PI_STAGED/ACTIVE，不恢复旧运动请求。

除OPEN_SESSION外，只有 `session_id=0 + EXECUTE_COMMAND(STOP)` 可以无会话执行。
它仍完整通过COBS/长度/CRC/Header/payload检查，关断效果幂等，不建会话、不清Fault，
不改变当前Session或其Sequence水位。无会话Stop仅在途去重：同一帧在途时可返回IN_PROGRESS，
不创建第二个并发Stop；终态形成并释放在途记录后，相同帧再次到达须作为新Stop提交。
不为session0保存或查询完成缓存；迟到的旧Stop也可以再次执行安全关断，不能将其应答
解释为跨执行次数的恰好一次证明。
响应是最佳努力，响应丢失不撤销Stop；它不替代Hardware Trip、TIM6或物理断电。
其他session0业务返回SESSION_REQUIRED，不同于当前会话的非0 ID返回SESSION_INVALID。

所有线上Sequence使用1..0xFFFFFFFE；0和0xFFFFFFFF保留。普通请求单调递增，V1不处理回绕；
用尽前Host重建会话。Request、Telemetry、Event拥有独立序号空间。Telemetry/Event耗尽时
停止相应发送并等待新会话，不回绕、不自动改变会话或Motor。

有效非0 Session内的完成缓存深度4，保存Session、Sequence、完整规范化请求和完整逻辑终态Response。
相同Session/Sequence且请求完全相同：处理中返回IN_PROGRESS，完成后重发原终态，不重复执行。
相同身份但内容不同返回SEQUENCE_CONFLICT。已退休且不在缓存的旧序号返回SEQUENCE_INVALID。
规范化内容包括version/kind/flags/header_size/message_id及精确payload，不用CRC替代请求比较。
当前有效Session内被明确拒绝的新请求也退休其序号并缓存终态；不把一次拒绝留成可重新执行的
新请求。外来/失效Session的拒绝不改变当前会话的序号水位。

V1只保留一个普通在途事务及一条独立Stop交接路径，不靠多普通请求队列提高吞吐。
不同Stop在已有Stop在途时可返回BUSY，但不得覆盖或撤销已排队Stop。
session-bound Stop继续按Session/Sequence及完成缓存去重；sessionless Stop仅按上述在途规则去重。
会话替换、请求重发和串口恢复都不延长运行时限。

## 4. 对象

健康IDLE写条件：无Motor/guard/内部故障、功率关闭、Runtime OFF且无待启动过程；
不把Start已接受但尚未进入RUN的两拍准备误当作可写空闲。参数读写不修改积分或功率寄存器。

| object_id | 对象 | read B | write B | 写权限 |
| --- | --- | --- | --- | --- |
| 0x0001 | DEVICE_INFO | 24 | — | 只读 |
| 0x0002 | MOTOR_SNAPSHOT | 48 | — | 只读 |
| 0x0003 | COMMUNICATION_STATUS | 104 | — | 只读 |
| 0x0101 | SPEED_PI_DEFAULT | 8 | — | 只读 |
| 0x0102 | SPEED_PI_STAGED | 12 | 8 | 健康IDLE |
| 0x0103 | SPEED_PI_ACTIVE | 16 | — | 只读 |
| 0x0201 | TELEMETRY_CONFIG | 8 | 4 | 健康IDLE |

以下表中的偏移均相对于object_value；没有隐式padding。

### DEVICE_INFO（24 B）

| 偏移 | 类型 | 字段 |
| --- | --- | --- |
| 0 | u16 | protocol_version=1 |
| 2 | u16 | device_contract_version=1 |
| 4 | u16 | target_id=1（G431） |
| 6 | u16 | control_profile_id=1 |
| 8 | u32 | capability_flags |
| 12 | u32 | control_time_ticks_per_second=20000 |
| 16 | u64 | firmware_build_id |

capability_flags：bit0对象读、bit1显式有限速度命令、bit2 Staged速度PI、bit3本合同遥测、
bit4事件、bit5 sessionless Stop；其余0。仅表示固件提供接口，不表示当前可Start或可恢复Fault。
firmware_build_id由实际正式源码/配置的规范化内容摘要截取64 bit确定，不用编译时间戳；
构建脚本排除生成身份文件本身，保存完整来源摘要及HEX，短ID不替代完整镜像核验。

### MOTOR_SNAPSHOT（48 B）

| 偏移 | 类型 | 字段 |
| --- | --- | --- |
| 0 | u64 | control_time_ticks |
| 8 | u32 | control_time_ticks_per_second |
| 12 | u32 | motor_state |
| 16 | u32 | fault_flags |
| 20 | u32 | motor_command_result |
| 24 | u32 | valid_flags |
| 28 | f32 | target_velocity_rpm |
| 32 | f32 | velocity_rpm |
| 36 | f32 | id_a |
| 40 | f32 | iq_a |
| 44 | f32 | bus_voltage_v |

valid_flags沿用既有业务映射：bit0时间/频率、1目标、2速度、3 Id、4 Iq、5母线、6状态、
7故障、8命令结果；未定义位0。status_valid对应1/6/7/8同组，其他位独立。
它映射协议无关MotorSnapshot；UART/RTOS统计不进入该对象。

### COMMUNICATION_STATUS（104 B）

按下列顺序紧排26个u32（偏移为索引×4），所有计数自然u32回绕，无RESET_STATS：

```text
session_id rx_bytes tx_bytes event_drop_count rx_discontinuities rx_dma_overruns
uart_pe uart_fe uart_ne uart_ore
cobs_errors crc_errors length_errors version_errors message_errors parser_timeouts
response_fifo_high_water completion_fifo_high_water event_fifo_high_water telemetry_overwrites
communication_stack_min_words telemetry_stack_min_words internal_fault_reason internal_fault_count
tx_busy_count tx_errors
```

栈以32 bit word计，尚未有效测量时为0xFFFFFFFF；真实测量值0必须保持0，不作未测量解释。
internal_fault_reason为首次内部原因：
NONE=0、BOOTSTRAP=1、RTOS_INTERNAL=2、STACK_OVERFLOW=3、COMPLETION_OVERFLOW=4、
RESPONSE_OVERFLOW=5、TRANSACTION_CORRUPT=6、TRANSPORT_INTERNAL=7、RUNTIME_TIMEOUT=8。
计数为内部Fail-closed调用次数；外部坏帧不进入该计数或故障域。

### 速度PI对象

DEFAULT依次为f32 kp/ki（0/4），固定0.001/0.004。STAGED读在其后加u32 generation（8），
写仅8 B kp/ki；范围kp=0.0005..0.005、ki=0..0.02且必须有限。ACTIVE在相同12 B后加
u8 currently_in_use（12）、u8 reserved[3]（13），共16 B。

复位：Staged=Default、generation1；Active值=Default、generation0、in_use=false。
每次新的合法写整组替换Staged并generation+1（u32自然回绕），重复请求不再次增加。
generation只表示本启动内的配置修订，不是跨复位唯一身份；不能单凭0判断从未使用。
合法Start被Motor接受时才在实时边界锁存当时Staged及generation、in_use=true；
被拒绝的Start不改变Active。Stop/Fault保留最后Active及generation，in_use=false。
只有实时所有者更新生命周期和PI实例；禁止RUN在线改PI，不持久化。

### TELEMETRY_CONFIG（读8 B／写4 B）

依次为u8 enabled（0）、u8 reserved=0（1）、u16 rate_hz（2）、读时附u32 generation（4）。
enabled仅0/1，rate_hz仅50/250/500。默认enabled1/rate250/generation1。
仅健康IDLE整组写入并generation+1，重复请求不重复增加；generation回绕语义同Staged。
500 Hz只用于IDLE压力，配置为500时Start返回PRECONDITION_FAILED且不进入Motor。

## 5. 命令与内部交接

| command_id | 命令 | command payload B |
| --- | --- | --- |
| 0x0001 | START_VELOCITY | f32 target_velocity_rpm（4） |
| 0x0002 | UPDATE_VELOCITY | f32 target_velocity_rpm（4） |
| 0x0003 | STOP | 0 |
| 0x0004 | CLEAR_FAULT | 0 |

Start/Update沿用Device Contract的完整速度模式意图与±80 rpm限制，不分别写mode/target。
Clear忠实返回现有Motor结果，不清寄存器或伪造恢复。`QUEUED ≠ Motor ACCEPTED ≠ RUN`。

线上身份为Session/Sequence，内部ticket为本启动非0 u32，不进入协议或Motor。
普通完整请求由后台交接给ADC唯一所有者；Completion FIFO深度4，ADC生产、Communication
Task消费，静态SPSC、内存屏障、不得覆盖/阻塞/调用内核。参数写也必须完成权威实时交接。
Stop在短临界区撤销尚未消费的普通ticket并返回其身份，后台将其终态定为CANCELLED_BY_STOP；
Stop自身经实时边界得到Motor结果。已完成请求不回头改历史。安全守卫退休的待消费请求也必须
有明确取消结果，由ADC通道发布；ADC不再推进时20 ms完成上限触发Fail-closed，不重执行。

内部Fail-closed立即safe-off、锁首因/计数、禁止Start/Update/参数写，逻辑上立即使Session
失效（后台随后清理其数据），下一实时边界合入Motor故障。ADC不推进也不得等待它才关断。
Kernel/Port、UART字节或协议结构不进入Motor。FIFO溢出、身份不一致、请求完成20 ms超时、
静态资源失败、栈溢出或不可恢复的传输内部错误触发该路径；只等待人工复位。
CRC/COBS/Host失联/半帧超时/普通UART错误只恢复字节流，不自动Stop或Fail-closed。

## 6. Telemetry与Event

SPEED_COMMISSIONING_SAMPLE payload为72 B，无对齐padding：

| 偏移 | 类型 | 字段 |
| --- | --- | --- |
| 0 | u32 | sample_sequence |
| 4 | u64 | control_time_ticks |
| 12 | u64 | position_observation_time |
| 20 | f32 | target_velocity_rpm |
| 24 | f32 | velocity_rpm |
| 28 | f32 | mechanical_position_turns |
| 32 | f32 | iq_reference_a |
| 36 | f32 | iq_feedback_a |
| 40 | f32 | id_feedback_a |
| 44 | f32 | bus_voltage_v |
| 48 | u32 | motor_state |
| 52 | u32 | fault_flags |
| 56 | u32 | valid_flags |
| 60 | u16 | limit_flags |
| 62 | u16 | reserved=0 |
| 64 | u32 | active_profile_generation |
| 68 | u32 | telemetry_config_generation |

valid_flags：bit0时间、1位置时间及机械坐标、2目标、3速度、4 Iq reference、5 Iq feedback、
6 Id feedback、7母线、8状态/故障、9两个generation；其余0。未有效整数字段填0，浮点填
规定NaN；generation0可表示尚未接受Start。limit_flags bit0速度PI输出限幅、1 d轴电流限幅、
2 q轴电流限幅、3电压矢量/调制限幅；其余0，控制无效时为0，不能据0推断测量有效。
当前电流PI的bit1/2分别观察既有d/q轴积分限幅；共同电压矢量限幅只置bit3。
机械位置为编码器原点的单圈坐标，正向与速度一致，跨圈允许跳变，不是累计圈数或定位零点。

实时侧在RUN的1 kHz速度更新边界复制已有状态；OFF/启动期间按相同1 kHz节拍发布。
sample_sequence自然u32回绕，不因后台读取或发送才推进；实时侧不计算
协议、统计或调用RTOS。Telemetry Task按50/250/500 Hz读取最新完整publication并以
xQueueOverwrite写深度1 Mailbox。线上Telemetry sequence在生成Mailbox值时分配，
覆盖旧值计telemetry_overwrites，故Host序号缺口可与覆盖数量对应；不在成功TX后才编号。
会话/配置切换期间的序号域分别核对，不将重建Session计成丢帧。仅有效Session发送。
内部由Telemetry Task唯一推进产生计数，Communication唯一拥有Session及其序号映射基准；
Mailbox值带会话epoch/配置generation供核对，不从另一任务直接重写产生计数。
新会话丢弃旧epoch值，避免清队列后迟到的旧值混入新会话。

STATE_CHANGED body= u32 old_state + u32 new_state + u64 control_time_ticks（16 B）。
FAULT_LATCHED body= u32 newly_latched_faults + u32 all_faults + u64 control_time_ticks（16 B）。
Event使用独立Sequence和当前Session，无ACK，最佳努力；深度8，满丢最旧并event_drop_count++。
后台从有效快照变化形成事件，不能替代实时Motor Snapshot或承诺捕获全部瞬时跳变。
新会话清旧Event FIFO。Event丢失、Telemetry覆盖不触发Fail-closed。

TX优先级：Stop/关键系统终态 > 普通终态 > Event > 最新Telemetry；已在途单帧不得抢写。
Terminal Response FIFO深度4，不能覆盖；满是内部不变量失败，safe-off而非静默丢响应。

## 7. 时限、容量和验证

半帧超时20 ms，Runtime完成上限20 ms，TX DMA完成上限10 ms，Communication Task最长睡眠
1 ms。Host普通请求等100 ms、同一身份最多重发2次；重发完全相同的帧，不自动恢复旧Start。
活动半帧超时后丢弃至下一0x00；RX连续性破坏同样清半帧并重新同步。

RX Circular DMA512 B、decoded128 B、COBS候选129 B、TX DMA130 B；Completion4、
终态Response4、Event8、完成缓存4、Telemetry Mailbox1。Transport不额外排队协议帧，
第一版不增加软件RX Ring。权威生产/消费位置与连续性epoch决定数据有效性，通知可合并。

最大对象响应为4+104=108 B payload，EXECUTE响应8 B，样本72 B，事件16 B，均≤110 B。
50 Hz×10 s与250 Hz×30 s在无运动条件下要求无链路错误、响应丢失、遥测缺口或覆盖；
500 Hz×10 s仅IDLE，要求无响应/内部故障、遥测丢失<1%且缺口与覆盖一致。
250 Hz名义运行下不能丢字节或响应；先识别瓶颈，不扩大缓冲或降工作点制造PASS。
