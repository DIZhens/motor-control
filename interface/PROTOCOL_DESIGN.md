# 自研设备协议设计边界

[Core Protocol V1](CORE_PROTOCOL_V1.md)是当前阶段唯一权威Wire Contract，已固定UART framing、
编号、精确布局和事务语义。本页只说明设计理由和后续变化轴，不维护第二套字段表。
合同冻结不表示实现或实物验收通过；具体事实从阶段检查点及Target验证页恢复。

## 分层与设备语义

Motor / Device API → Protocol Server / Transaction → Protocol Codec → Communication Task
→ Byte-stream Transport Contract → Target UART DMA Adapter。

Device Contract拥有状态、单位、故障和完整命令；协议负责对象访问、会话、去重和消息表示。
Codec不依赖HAL、FreeRTOS或Motor私有状态；Server不依赖USART/DMA；Transport只传字节。
内部ticket与线上Session/Sequence分离，Motor不感知请求重试。入队、业务接受和进入RUN分层。
自研协议借鉴简单对象访问及遥测/事件分类，不声明Modbus或CANopen兼容，不复制其全部对象字典。

## 有界资源与恢复

只实现本阶段实际使用的对象，不建立通用参数数据库、Transport Registry或插件框架。
普通请求单在途，固定完成缓存与有界FIFO；Stop有独立优先交接路径，完整验证前不猜测任何命令。
响应优先于事件和latest-value遥测，TX DMA单帧在途；新会话不重启或复原旧运动。

CRC/COBS/UART错误及Host失联只恢复字节流，现有1.5秒与运动包络继续保护运行。
内部FIFO/身份/资源不变量失效才Fail-closed，立即关断并等待人工复位。
后续解除有限时长前再设计Host Lease，不把本轮方案扩大成持续运行失联保护。

## 实施顺序与执行环境

依次完成合同、PC/ARM纯编解码、Runtime身份/完成/Profile、静态RTOS启动、UART DMA、
无功率事务与压力，最后仅一次有限闭环操作。硬件、调度、通信和业务变化分开验证。
控制ISR仍使用硬件同步采样时基，不调用内核、不等待后台；通信和遥测分别有实际后台职责。

CubeMX配置硬件，正式Run手工集成当前供应包Kernel/RVDS Port，业务使用native API和静态分配。
验证工程继续裸机；详见[G431集成合同](../firmware/targets/BLDC_G431/runtime/RTOS_INTEGRATION.md)。
VOFA仅在独立构建保留；需要时PC侧转换或绘图，不在MCU自动识别或混发协议。

## 后续CAN

CAN实物不属于本阶段。共用设备与事务语义，采用Frame Transport，不强行套UART的COBS、
Length或链路CRC；节点身份、仲裁/过滤、Classical CAN/CAN FD和必要分段在有实物需求时另定。
Qt、标准Modbus/CANopen、位置环、Flash持久化与通用上位机框架也不提前进入。
