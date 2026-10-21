# Communication

通信业务通过设备合同与 Motor 交互；线格式编解码不读取 Motor 私有数据，不直接操作功率，
不依赖 HAL、STM32 或 FreeRTOS。硬件字节流由 Target 提供。

现有 Run 使用 `vofa` / `motor_vofa`，Target 负责 UART 及装配。新的 Core Protocol 库当前
只进入独立 ARM 模型，尚未加入 Run / Validation 工程成员，不改变现有设备的串口格式。
两个协议没有自动探测或混发入口。

## Core Protocol 库

- [core_protocol.h](core_protocol.h)：CRC、COBS、完整帧及分片解析器。
- [core_protocol_values.h](core_protocol_values.h)：请求、响应、七种固定对象、72 B 遥测和事件。
- [byte_stream_transport.h](byte_stream_transport.h)：后续 Target 字节流合同，当前只有声明。

编号、单位、有效位、精确长度及会话规则以
[Wire Contract](../../../interface/CORE_PROTOCOL_V1.md)为准；C 类型不是线上结构体。
`CoreObject` 仅是本次编解码的值容器，不保存设备参数或承担对象数据库职责。
通信状态按字段名称装配，序列化逐字段确定偏移，不发送 C 内存布局。

接收调用顺序为：字节流连续性检查 → `core_parser_feed` → 完整可信帧 → 请求解码 → 后续
Server 的会话、事务、权限检查 → Motor 交接。当前库只实现到请求解码，不含后面三项。
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
统计自然 u32 回绕；私有 `flags_errors` 在后续通信状态装配时并入 `message_errors`，
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

覆盖全部固定布局、逐字段值、所有分片点、连续帧、随机 chunk、CRC 位翻转、错误后恢复、
精确容量、超时边界和时钟回绕。`--report <路径>` 可保存带来源摘要的本次结果。
这是离线库模型，不能证明串口吞吐、DMA 连续性、任务调度、会话去重或实际 Motor 行为。
