# VOFA 协议

本文件是 V1 VOFA 字节格式、通道顺序和有效位的唯一协议合同。Motor 状态、故障、命令结果、
速度限制及运行前置条件以 [Device Contract](DEVICE_CONTRACT.md) 和 Motor 公共 API 为准。
编解码实现在 `firmware/app/communication/vofa.h/.c`，是无 HAL / Qt 依赖的纯 C 模块。

## PC 设置与速率

PC 使用现成 VOFA+，上行选择 JustFloat 数据引擎，下行选择 Str / ASCII 命令。
G431 Observe / Run 集成为 UART 115200 bit/s、8N1，遥测约 100 Hz；44 字节帧在该速率下占用
4400 byte/s，即 8N1 单向带宽约 38.2%。串口实测结论见对应 Target 验证页。
发送和接收在后台处理，不阻塞 fast-control ISR；遥测来自 Motor 一致性只读快照。

## 下行：完整请求

每行一条命令，大小写敏感，无前后空格，LF 结束；允许 LF 前有一个 CR。

| ASCII 内容 | 请求 |
| --- | --- |
| `start:<rpm>\n` | 以速度模式和该机械转速目标形成完整 Start 请求 |
| `speed:<rpm>\n` | 明确请求更新速度模式的机械转速目标 |
| `stop\n` | Stop 请求 |
| `clear\n` | Clear Fault 请求 |

例如 `start:120\n`、`speed:-60.5\n`。表中的 `\n` 表示实际 LF 字节 `0A`，
不是反斜杠和字母 n。VOFA+ 可以使用 `start:%f\n`、`speed:%f\n` 模板；
需要确认发送结果确实带 LF。`start` 不允许省略目标，也没有独立 mode 写入命令。

`<rpm>` 是十进制数，可有正负号、小数点和 `e` / `E` 指数，至少有一个尾数数字；
若有指数则必须有指数数字。例如 `1`、`-0.5`、`.5`、`1.`、`+1e2`。
全部内容必须被消费，不接受空白、十六进制、NaN、Infinity、尾随垃圾、NUL 或非 ASCII。
不能表示为有限 binary32 的上溢结果被拒绝；极小的数按目标编译器的 float 下溢语义可成为零。
编解码器不做业务速度范围校验，有限负数或较大数仍需 Motor 判断是否合法。

LF 前最多 47 字节，CRLF 的 CR 计入该上限。超过上限或发现非法控制字节后，解析器丢弃
整行直至下一个 LF，再从新行恢复；不会把超长行尾部的 `stop` 等内容当作另一条命令。
空行也被拒绝。逐字节接口接受任意碎片和连续多帧，每个 LF 至多产生一个完整请求，
仅成功时写入 `VofaCommand`，拒绝或尚未完整的输入不改输出 Command。

解析成功只表示请求格式正确，不表示 Motor 已接受、进入 RUN 或使能功率。
Motor 独占状态、模式、目标、Fault 和功率门控。`speed` 的允许状态和模式由 Motor 校验，
`clear` 不等于 Start。连接、断线、重连以及接收恢复都不得自动 Start 或重放旧请求；
新字节流开始时应调用 `vofa_parser_init()`，丢弃旧连接未完成的输入。

当前 USART 轮询没有连接会话识别。UART 硬件错误和连续 100 ms 没有新字节的未完成行，
也转入丢弃至 LF 的恢复状态；超时不解除已有的丢弃状态。完整请求在实时采样边界消费，
单槽邮箱拥塞或格式错误记为 BAD_COMMAND；Stop 优先并丢弃先前排队请求。发送端逐条操作，
重新同步时先发一个 LF，再发完整命令。

## 上行：固定十通道 JustFloat

每帧为十个 IEEE 754 binary32 小端浮点数，随后是固定四字节 `00 00 80 7F`，共 44 字节。
不发送 C 结构体的原始内存，不添加文本、CRC 或额外通道。PC 通道顺序固定如下：

| 通道 | 字段 | 单位 / 含义 | `valid_flags` 位 |
| --- | --- | --- | --- |
| 0 | `time_s` | 单调控制时间，s | 0 |
| 1 | `target_velocity_rpm` | RUN 当前活动机械转速目标，rpm；启动准备期间为零 | 1 |
| 2 | `velocity_rpm` | 有效机械转速估计，rpm | 2 |
| 3 | `id_a` | 有效 d 轴电流，A | 3 |
| 4 | `iq_a` | 有效 q 轴电流，A | 4 |
| 5 | `bus_voltage_v` | 有效物理母线电压，V | 5 |
| 6 | `state` | Motor 生命周期枚举数值 | 6 |
| 7 | `faults` | Motor 故障位图数值 | 7 |
| 8 | `valid_flags` | 本帧数据有效位图，整数数值 | 始终有效 |
| 9 | `command_result` | 最近一次命令处理结果枚举数值 | 8 |

`state`、`faults`、`command_result` 和 `valid_flags` 在 C 输入中为 `uint32_t`，
在线上仍转换成浮点数值。前三者仅允许 `0..16777215`，超出该范围时对应通道输出 NaN、
清除有效位，避免 float 静默丢失枚举或故障位图精度；有效位只使用 bit 0..8。

置位表示该通道来源已实现、已知且当前有效。尚未实现、尚未取得或已经失效的反馈必须清除
对应位；不得拿初始化的 0 当作有效观测。编码器对未置位通道输出 canonical quiet NaN
（小端 `00 00 C0 7F`），并将非有限浮点输入规范化为同样的 NaN、清除对应位。
上行只保留有限有效数或 NaN，无效数据不会作为 0 发送；`+Infinity` 的位模式正好与
JustFloat 帧尾相同，因此不能作为有效负载发送。接收方同时检查有效位和有限性。

遥测没有请求序号，也不是逐命令可靠应答通道；`command_result` 是快照字段，连续请求
可能在两帧遥测之间覆盖结果。需要判断执行状态时，以新的有效 `state` / `faults` /
`command_result` 观测和 Device Contract 为准。该最小协议不声明事务、重试或自动恢复启动。

## 可重复离线验证

运行 `firmware/app/communication/tests/verify_vofa.py`：使用本机 ARMCC 编译正式
`vofa.c`，再由 Unicorn 执行编译后的 ARM 代码。脚本需要 Python 的 `unicorn` 和
`pyelftools`；`KEIL_ARMCC_BIN` 可覆盖默认 `D:\keil5\ARM\ARMCC\bin`。
验证四类命令、碎片 / 多帧、严格非法输入、溢出丢弃与恢复、字节序、通道顺序、有效位、
非有限规范化、整数精度和短输出缓冲，不访问 ST-Link，不代替 UART 或电机实机验收。

## 外部格式依据

上行格式遵循 VOFA+ 官方 [JustFloat 文档](https://www.vofa.plus/docs/learning/dataengines/justfloat/)。
Str 命令和 `%f` 参数机制参见官方
[数据、命令、参数](https://www.vofa.plus/docs/learning/start/data_cmd_parameter/)。
本项目的命令名、业务映射、通道表及有效位由本文件定义。
