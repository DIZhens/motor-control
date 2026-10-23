# Motor Node Profile V1

本文件拥有UART/CAN共享的Object ID、Command ID与服务结果编号。
[Device Contract](DEVICE_CONTRACT.md)继续拥有Motor状态、Fault、完整命令、单位、权限前提与
参数生命周期；具体链路只规定表示、关联和容量，不重新定义设备业务。
合同定义不等于某链路已完成实现或实机验收。

```text
一个MCU运行时 = 一个Firmware Node = 一个Motor
Motor Node Profile / Device Port
    ↓
UART Frontend 或 CAN Frontend
    ↓
Byte-stream Port 或 CAN-frame Port
```

## 命令编号

| Command ID | 命令 | 完整业务意图 |
| --- | --- | --- |
| 0x0001 | START_VELOCITY | 速度模式与一个有限rpm目标；受Motor运行许可约束 |
| 0x0002 | UPDATE_VELOCITY | RUN中更新当前速度模式目标；不延长运行期限 |
| 0x0003 | STOP | 撤销目标/功率；不清Fault，不创建或替换Session |
| 0x0004 | CLEAR_FAULT | 忠实返回Motor结果，不伪造恢复、re-arm或启动 |

当前G431有限目标为±80 rpm；NaN/Inf与越界不得进入有效运动命令。
MotorCommandResult编号与生命周期只在[Device Contract](DEVICE_CONTRACT.md)定义。
协议未进入Motor时以0xFFFF表示结果不可用，不能把它解释为Motor拒绝或成功。
入队、Completion、Motor ACCEPTED与RUN是不同事实。

## 对象编号与能力子集

| Object ID | 对象 | 业务权限 |
| --- | --- | --- |
| 0x0001 | DEVICE_INFO | 只读身份与能力，不表示具备本次启动许可 |
| 0x0002 | MOTOR_SNAPSHOT | 只读一致观测与逐组有效位 |
| 0x0003 | COMMUNICATION_STATUS | 链路专属统计；UART已定义，不能套作CAN状态布局 |
| 0x0101 | SPEED_PI_DEFAULT | 只读Target默认值 |
| 0x0102 | SPEED_PI_STAGED | 健康IDLE整组写，读权威修订 |
| 0x0103 | SPEED_PI_ACTIVE | 只读最近Start锁存值与使用状态 |
| 0x0201 | TELEMETRY_CONFIG | 健康IDLE整组写；实际允许频率由链路Profile规定 |

共享编号不要求每个Frontend支持全部对象。UART提供上述七对象；Classical CAN首版只映射
会话、有限速度命令、Stop、心跳、紧凑运动遥测和Fault Event，不增加大对象分片或在线PI写入。
CAN线上service_id与共享Command ID不是同一编号空间，必须按CAN映射明确转换。

## 结果编号

| 域 | result_code |
| --- | --- |
| 成功 | OK=0x0000 |
| 请求 | BAD_REQUEST=0x0101，UNKNOWN_MESSAGE=0x0102，BAD_PAYLOAD=0x0103，UNSUPPORTED_FLAGS=0x0104，UNSUPPORTED_VERSION=0x0105，UNKNOWN_COMMAND=0x0106 |
| 会话/序号 | SESSION_REQUIRED=0x0201，SESSION_INVALID=0x0202，SEQUENCE_CONFLICT=0x0203，IN_PROGRESS=0x0204，SEQUENCE_INVALID=0x0205，SESSION_BUSY=0x0206 |
| 对象 | UNKNOWN_OBJECT=0x0301，READ_ONLY=0x0302，BAD_VALUE=0x0303，OBJECT_WRONG_STATE=0x0304，LENGTH_MISMATCH=0x0305 |
| 事务 | BUSY=0x0401，CANCELLED_BY_STOP=0x0402，PRECONDITION_FAILED=0x0403 |
| 系统 | SYSTEM_FAIL_CLOSED=0x0501，INTERNAL_ERROR=0x0502 |

COBS/CRC或CAN帧边界/硬件状态不是Motor业务结果。非法帧拒绝、链路错误、内部不变量失败与
Motor Fault分别处理，不把普通坏帧变成新运动命令或自动恢复。

## 共享值与实时交接

rpm为机械转速，电流为本机已声明精度范围内的A，电压为V；无效值必须保留有效性语义，
不能用有效0代替未知。UART binary32/NaN与CAN定点/有效位的具体规则属于各自映射。
速度PI的Default/Staged/Active与合法值域以Device Contract为准，不由Frontend直接改积分。

纯C定义位于`protocol/common/`，沿用既有`CORE_*`业务标识符以保持API与编号兼容。
Device Port只包含完整设备意图、规范化Completion、publication与内部ticket；不含Motor指针、
ControlRuntime实例、RuntimeRequest、Session、Sequence、CAN ID/DLC、HAL或FreeRTOS类型。
`firmware/app/communication/motor_node_binding.c`负责与既有RuntimeRequest/Completion转换；
Runtime结构和ADC单写者不因新增链路改变。STOP仍通过同一优先交接，内部不变量失败请求
Target Fail-closed，不绕过Motor修改功率。

UART映射见[UART_MAPPING_V1.md](UART_MAPPING_V1.md)；CAN映射另行形成独立合同。
