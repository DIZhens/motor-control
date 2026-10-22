# G431 后台运行时集成合同

本文件固定Target资源、启动和依赖选择；已实现及实机范围见[验证页](../VALIDATION.md)。
线上唯一合同见[Core Protocol V1](../../../../interface/CORE_PROTOCOL_V1.md)。
现有有限FOC参数及硬件能力合同保持不变。

## 供应包与集成方式

当前IOC固定STM32Cube FW_G4 V1.6.3，包中FreeRTOS头文件声明Kernel V10.3.1；
使用该包的`Middlewares/Third_Party/FreeRTOS/Source`及`portable/RVDS/ARM_CM4F`，
匹配现有ARM Compiler 5.06 update5（build528）和Cortex-M4硬件FPU。
供应文件位于[third_party/FreeRTOS](../../../third_party/FreeRTOS/README.md)，
保留原文、许可及来源哈希，手工加入Run工程；原生构建和真实异常切换分别验收。
不另取最新上游替换、不直接修改Kernel/Port，不编译CMSIS_RTOS/CMSIS_RTOS_V2和heap实现。

CubeMX仅负责TIM7 HAL timebase、UART/DMA/NVIC等硬件配置。可生成不提交的临时参考工程
核对Port、Handler、编译选项和FreeRTOSConfig默认值；正式Run手工加入Kernel/Port。
项目策略在FreeRTOSConfig、SystemRuntime及Target集成层，业务只使用native API及静态分配。
生成前后核对两个Keil工程成员，不允许CubeMX覆盖正式/验证隔离。

正式Run不编译旧target_vofa/target_uart；独立Validation继续裸机和VOFA/专项工具，
不链接Kernel、Port、默认Task或新通信任务。HAL TIM7 timebase实现只进入Run；裸机验证
保留自己的HAL tick归属，不能因共享Core而同时推进两个HAL tick。

## 资源与中断所有权

以下为资源边界；UART/DMA行待Transport接入，其余已进入Run配置和启动核对。
G431有4个NVIC优先级位，保持PRIORITYGROUP_4；数值越小逻辑优先级越高。

| 资源 | 优先级 | 所有者与约束 |
| --- | --- | --- |
| TIM1 Update/Break | 0，保持 | Platform，20 kHz PWM及有效更新/保护；无RTOS API |
| ADC1_2 / ADC1 DMA1_Channel1 | 1，保持 | Platform/ControlRuntime，10 kHz唯一实时写者；无RTOS API |
| SPI1 | 2，保持 | MT6701有限SSI事务；无RTOS API |
| USART1 / RX DMA1_Channel2 / TX DMA1_Channel3 | 5，待接入 | UART Adapter，只记必要进度/错误并通知后台 |
| TIM6 | 15，保持 | 原约50 µs独立守卫及1.5秒上限，不改成HAL/内核时基 |
| TIM7 | 15，新增 | HAL 1 kHz，调度器前后均推进；不通知RTOS |
| SysTick / PendSV | 15 | Kernel 1 kHz tick及上下文切换，正式Run由Port唯一拥有 |
| SVC | Port所有，保留启动调用可达性 | 正式Run不可另定义同名空Handler |

`configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY=5`（寄存器值0x50），
`configKERNEL_INTERRUPT_PRIORITY=0xF0`。UART/DMA FromISR不得使用0..4；控制/保护
不调用任何内核API。保留既有控制/保护相互抢占关系，Kernel临界区和新增低优先级IRQ仍须
在后续实际工作点验证，P0的数字选择不是最长延迟证明。
TIM7专供Run HAL时基，DMA1通道2/3为Transport保留；未来如发现冲突，停止扩大修改并回报。

IOC由CubeMX 6.18.1命令行设置SYS TIM7，关闭三个系统异常的生成Handler。
`stm32g4xx_hal_timebase_tim.c`只进入Run；共有Core的USER区为Validation保留裸机
SVC/PendSV/SysTick，并屏蔽其TIM7 HAL分发。Run由未修改的Port直接导出三个异常，
没有CMSIS包装或转发Handler。重新生成后仍需核对两份Keil工程的手工成员及条件入口。

## 静态资源及启动

配置：抢占1、时间片0、tick1000 Hz、tickless0、静态分配1、动态分配0、软件Timer0、
Task notification1、栈检查2、max priorities4。只链接实际使用的Kernel核心源码；
提供静态Idle task内存，静态创建任务/Queue/Mailbox，无运行期动态heap。

| 任务 | 优先级 | 初始栈word | 寿命/职责 |
| --- | --- | --- | --- |
| Bootstrap | 3 | 768 | 临时装配，完成前记录栈高水位后删除 |
| Communication | 2 | 768 | 唯一UART/Protocol/Transaction所有者，最多阻塞1 ms |
| Telemetry | 1 | 384 | 周期读最新publication，覆盖单槽Mailbox并通知Communication |
| Idle | 0 | 128 | Kernel维护，静态提供内存 |

栈按实际MAP调用链及FPU上下文预留，word为4字节。静态调用链已知部分为Bootstrap304 B、
Communication224 B、Telemetry296 B、Idle40 B，仍有间接调用/汇编未知量及硬件异常压栈。
当前Run静态RAM为17400/32768 B，包含4 KiB主栈及原512 B C库堆保留区；无动态分配调用
或FreeRTOS heap实现。后续512 B RX、130 B TX、协议缓存/FIFO还需继续预算。
不能把静态占用当作栈安全证明；长期任务实测最小剩余栈必须≥初始25%，Bootstrap删除前记录自身数值。

调度器前：HAL/TIM7 tick、时钟和生成硬件初始化、最早safe-off、创建静态Bootstrap、启动
scheduler；不启动RX DMA、不开放命令、不等待Queue/Notification。
Bootstrap依次核对静态资源/内核时基/NVIC、创建长期任务与Mailbox、初始化协议和ticket/
Completion、装配Motor保持关断、启动Drive Timing/TIM6、绑定事件Sink，最后启动RX DMA。
任务句柄及业务装配必须先于任何可唤醒后台的UART事件。调度器返回、assert、栈溢出或
静态资源失败进入协议无关Fail-closed，不尝试重启调度器或电机。

ADC只形成Completion与1 kHz速度调参publication，不发Task notification或Queue。
Communication保留最迟1ms唤醒边界，待Server加入后排空Completion；当前只消费遥测Mailbox，
不开放串口命令、不消费尚无事务所有者的Completion。Protocol/Session初始化、事件绑定及
RX DMA启动由后续增量加入Bootstrap末端。物理控制时间仍来自PWM/ADC，HAL/RTOS tick不参与FOC dt。

## 字节流与后台边界

[Byte-stream合同](../../../app/communication/byte_stream_transport.h)不含HAL/RTOS/Motor类型，
由Target链接期静态实现。事件Sink仅作对应上下文的有界通知；事件可重复/合并/延迟。
权威事实为DMA生产位置、后台消费位置、连续性epoch和实际TX所有权。

RX只用512 B Circular DMA，初版无额外软件Ring；IDLE/HT/TC/Error IRQ不做COBS、CRC、
解析或整块复制。生产减消费超过512即明确连续性破坏，作废半帧至下一delimiter。
必须证明HT/TC合并、回绕及延迟处理下的进度可靠，不能把NDTR单个模位置当作总生产量。
无法证明覆盖可检测时停止并报告，不借扩大缓冲掩盖。

Transport接受完整TX后复制进内部130 B缓冲，完成前归DMA；只允许单帧在途，
BUSY不建立第二套传输队列。10 ms未完成是内部可信性失败。普通PE/FE/NE/ORE只恢复字节流，
不会直接Stop或清故障。FIFO内部不变量失败则调用有界safe-off和锁存入口，随后由实时边界
合入Motor；Session逻辑有效性从锁存时立即撤销，不从ADC访问协议对象。

72 B样本加Header/CRC为90 B，标准COBS及delimiter共92 B；460800/8N1提供46080 B/s。
50/250/500 Hz分别占4600/23000/46000 B/s。500 Hz已占99.83%，仅剩80 B/s给响应及帧间
空隙，必须记录请求频率和实际TX等待，不把压力窗口伪装成没有请求的纯吞吐测量。
P6按原工作点辨识第一个瓶颈；本预算不预告500 Hz实物通过，也不据此改波特率、降频或扩容。

Kernel静态分配和Cortex-M FromISR边界以供应包源码及
[FreeRTOS内存说明](https://www.freertos.org/Documentation/02-Kernel/02-Kernel-features/09-Memory-management/01-Memory-management)、
[Cortex-M说明](https://www.freertos.org/Documentation/02-Kernel/03-Supported-devices/04-Demos/ARM-Cortex/RTOS-Cortex-M3-M4)核对。
