# Communication

通信业务通过设备合同与 Motor 交互；线格式编解码不读取 Motor 私有数据，不直接操作功率，
不依赖 HAL、STM32 或 FreeRTOS。硬件字节流由 Target 提供。

正式Run已移除`vofa` / `motor_vofa`装配，使用Target的460800/8N1 DMA字节流，已接入
Codec及事务Server；单个普通在途、独立Stop、完成缓存与终态FIFO均为固定静态容量。
Validation保留裸机115200/VOFA验证入口，两个协议没有自动探测或混发入口。

## Core Protocol 库

- [core_protocol.h](core_protocol.h)：CRC、COBS、完整帧及分片解析器。
- [core_protocol_values.h](core_protocol_values.h)：请求、响应、七种固定对象、72 B 遥测和事件。
- [byte_stream_transport.h](byte_stream_transport.h)：Target字节流合同，G431已实现RX/TX DMA。

编号、单位、有效位、精确长度及会话规则以
[Wire Contract](../../../interface/CORE_PROTOCOL_V1.md)为准；C 类型不是线上结构体。
`CoreObject` 仅是本次编解码的值容器，不保存设备参数或承担对象数据库职责。
通信状态按字段名称装配，序列化逐字段确定偏移，不发送 C 内存布局。

接收调用顺序为：字节流连续性检查 → `core_parser_feed` → 完整可信帧 → 请求解码 →
Server的会话、事务、权限检查 → Runtime交接。Codec只实现到固定请求解码，不拥有Session。
请求编解码检查固定值域和 reserved；这不意味着当前设备状态允许执行。

`core_frame_decode` 的输入不含末尾 `0x00`；`core_frame_encode` 的输出包含分隔符。
COBS、长度或 CRC 错误不发布帧，解码目标保持原值。仅 `CORE_OK` 能进入正常请求处理。
`CORE_FRAME_VERSION/FLAGS/KIND` 返回完整性可信的 Header 和 payload，供 Server 计错或按
合同应答，不能把这些回调当成已接受命令。未知 message/object/command 在固定值解码层
单独返回错误。帧解码不做 Session/Sequence 合法性与重放检查。

每条流独占一个 `CoreParser`，由同一后台所有者调用；回调期间的帧指针不可保留、不可重入。
批量 feed 的时间参数是调用方提供的单调 u32 毫秒值，不在库内读取系统时钟。
空 chunk 不延长半帧寿命，20 ms 无字节、超长或显式 discontinuity 后一直丢弃到下一分隔符；
不能把下一段后缀解释成完整 Stop。后台无新字节时也须调用 `core_parser_poll`。
统计自然u32回绕；私有`flags_errors`在通信状态装配时并入`message_errors`，
不增加线上对象字段。库不定义 UART/DMA 错误计数的所有权。

固定值编码先在本地完成校验，空间不足或输入不合法时不改输出和返回长度；
解码失败不改目标值（`CoreRequest` 例外：保留可提取的 message/subject 供错误响应，
仅成功时其余字段可用）。输入输出缓冲不可重叠。无效观测使用规范 NaN 并清有效位；
请求的非有限浮点按 BAD_VALUE 拒绝，不作为缺失数据接受。

## 离线验证

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe firmware/app/communication/tests/verify_core_protocol.py
```

依赖和编译器路径见 [DEVELOPMENT.md](../../../DEVELOPMENT.md#44-本地验证依赖)。脚本使用
实际 ARMCC 编译正式 C 源码，再在 Unicorn Cortex-M4F 执行，与
[Python 编解码器](../../../host/protocol/codec.py)和
[固定黄金向量](../../../interface/test_vectors/core_protocol_v1.json)对照。测试仅读取期望值，
不调用被测编码器重新生成黄金字节；固定 CRC 由独立标准库参考构造。

## 事务Server

[core_server.h](core_server.h)由唯一Communication任务持有，不依赖HAL或FreeRTOS。
通过链接期窄端口读一致对象、提交完整RuntimeRequest及消费准确ticket Completion，
不直接访问Motor/PI私有状态。G431装配在`target_core_server.c`，不建立运行期Ops注册表。

Server保留一个普通在途和一个Stop在途、完成缓存4、终态Response4。规范化比较包含全部
Header语义及精确payload，不用CRC代替内容比较。新拒绝也退休序号并缓存；重复处理中
仅给IN_PROGRESS提示，完成后原终态重放。Stop取消未消费普通ticket并分别形成取消/Stop终态；
已完成请求保留原结果。新会话不改Motor或参数，已排队Stop保留原身份且其旧完成不进新缓存。
无会话Stop也完整校验，重复缓存只代表同一操作，新的Stop须分配新的非保留Sequence。

普通事务20ms截止先于迟到Completion处理；未知ticket/kind、内部交接错误、响应FIFO满及
Transport内部失败立即Fail-closed、撤销Session，不重执行或自动恢复。可发送时只为旧在途
保留关键系统终态；后续请求返回SYSTEM_FAIL_CLOSED，需人工复位。外部CRC/COBS/UART断续
只清半帧并同步至下一delimiter。TX优先选关键Stop/系统终态，再选普通终态；BUSY保留完整
逻辑响应，在途帧由Transport拥有。IN_PROGRESS提示不占终态FIFO，拥塞时可省略并等原终态。

当前提供七个对象、参数权威写与命令结果；Telemetry/Event发送尚未接入，因此DEVICE_INFO
暂不声明这两项能力，相关发送统计保持0。Header序号、运行时ticket与Motor状态各自独立。

```powershell
.\.venv\Scripts\python.exe -X utf8 firmware/app/communication/tests/verify_core_server.py
```

该模型执行真实ARM Server/Codec、Runtime交接、参数和Motor，并通过Python客户端对接；
Transport/实时推进及C库复制为替身。实物请求应答与记录器范围见
[G431验证页](../../targets/BLDC_G431/VALIDATION.md)及[Host说明](../../../host/protocol/README.md)。

覆盖全部固定布局、逐字段值、所有分片点、连续帧、随机 chunk、CRC 位翻转、错误后恢复、
精确容量、超时边界和时钟回绕。`--report <路径>` 可保存带来源摘要的本次结果。
这是离线库模型，不能证明串口吞吐、DMA 连续性、任务调度、会话去重或实际 Motor 行为。
G431字节流的有界进度、连续性epoch、单帧TX和实物范围见
[Target集成合同](../../targets/BLDC_G431/runtime/RTOS_INTEGRATION.md#字节流与后台边界)及
[验证页](../../targets/BLDC_G431/VALIDATION.md)。
