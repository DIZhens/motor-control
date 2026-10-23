# G431 后台运行时集成合同

本文件固定Target资源、启动和依赖选择；已实现及实机范围见[验证页](../VALIDATION.md)。
共享业务编号见[Motor Node Profile](../../../../interface/MOTOR_NODE_PROFILE_V1.md)，
UART线上合同见[UART Mapping V1](../../../../interface/UART_MAPPING_V1.md)。
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

以下资源已进入Run配置和启动核对；UART/DMA由Target的字节流Adapter独占。
G431有4个NVIC优先级位，保持PRIORITYGROUP_4；数值越小逻辑优先级越高。

| 资源 | 优先级 | 所有者与约束 |
| --- | --- | --- |
| TIM1 Update/Break | 0，保持 | Platform，20 kHz PWM及有效更新/保护；无RTOS API |
| ADC1_2 / ADC1 DMA1_Channel1 | 1，保持 | Platform/ControlRuntime，10 kHz唯一实时写者；无RTOS API |
| SPI1 | 2，保持 | MT6701有限SSI事务；无RTOS API |
| USART1 / RX DMA1_Channel2 / TX DMA1_Channel3 | 5 | UART Adapter，只记必要进度/错误并通知后台 |
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
Communication1224 B、Telemetry408 B、Idle40 B，仍有间接调用/汇编未知量及硬件异常压栈。
当前Run静态RAM为21184/32768 B，包含4 KiB主栈、原512 B C库堆保留区、512 B RX/130 B TX
及2832 B Server静态状态/工作区；无动态分配调用或FreeRTOS heap实现。Event8、Mailbox1及
发送元数据已计入；Server只peek原Mailbox，不添加第二个待发样本槽。
不能把静态占用当作栈安全证明；长期任务实测最小剩余栈必须≥初始25%，Bootstrap删除前记录自身数值。

调度器前：HAL/TIM7 tick、时钟和生成硬件初始化、最早safe-off、创建静态Bootstrap、启动
scheduler；不启动RX DMA、不开放命令、不等待Queue/Notification。
Bootstrap依次核对静态资源/内核时基/NVIC、创建长期任务与Mailbox、初始化协议和ticket/
Completion、装配Motor保持关断、启动Drive Timing/TIM6、绑定事件Sink，最后启动RX DMA。
任务句柄及业务装配必须先于任何可唤醒后台的UART事件。调度器返回、assert、栈溢出或
静态资源失败进入协议无关Fail-closed，不尝试重启调度器或电机。

ADC只形成Completion与1 kHz速度调参publication，不发Task notification或Queue。
Communication保留1ms超时唤醒，有界服务Server/Completion、RX及终态/Event/最新遥测发送。
Telemetry在有效会话中按配置周期生成Mailbox值和单写产生计数；Communication映射独立
线上序号，TX接受才退休，BUSY保留、下一份覆盖计数；epoch/generation不符的值直接丢弃。
同一产生身份复用既有wire编码工作区，Mailbox更新则重新编码，不保留旧值发送队列。
服务先处理RX、截止及Completion，再尝试续帧，最后读取最佳努力Event快照；不在TX完成
IRQ中编码、复制整帧或做统计，事件FIFO选择优先级及单帧传输所有权不变。
Motor装配和任务句柄就绪后，Bootstrap初始化
唯一Server，再执行Transport初始化、事件绑定及RX DMA启动，最后自删除。共享Device Port
通过Firmware `motor_node_binding.c`转换Runtime请求与完成；Target连接一致publication及关断，
协议头不包含Runtime、Motor或内核类型。Parser、Session、
事务、参数映射和响应缓存都在Communication上下文，无ISR协议工作。物理控制时间仍来自
PWM/ADC，HAL/RTOS tick不参与FOC dt，后台1ms睡眠上限不是调度延迟/WCET证明。

## 字节流与后台边界

[Byte-stream合同](../../../../protocol/ports/byte_stream_transport.h)不含HAL/RTOS/Motor类型，
由Target链接期静态实现。事件Sink仅作对应上下文的有界通知；事件可重复/合并/延迟。
权威事实为DMA生产位置、后台消费位置、连续性epoch和实际TX所有权。

RX只用512 B Circular DMA，初版无额外软件Ring；IDLE/HT/TC/Error IRQ不做COBS、CRC、
解析或整块复制。按NDTR位置差与HT/TC边界旗标共同核对；DWT观测间隔须小于8ms，
并以HAL tick交叉检查。512字节在本波特率下约11.1ms填满；观测超时、旗标不一致或
无法排除整圈推进时提升epoch并丢弃未知段，不能猜测字节。已知生产减消费超过512则
累计buffer_overruns并丢弃未读段。rx_bytes/produced只累计可观测的模增量，不能作为
未知整圈丢失量的精确统计。调试停核不保证连续性，恢复后必须尊重epoch。
复制在临界区外，复制前后复核DMA进度/epoch；失败返回0字节，不能交付可能覆盖的副本。
Server须在连续性破坏后作废半帧并丢弃至下一delimiter。

Transport接受完整TX后复制进内部130 B缓冲，完成前归Transport；只允许单帧在途，
DMA TC后仍BUSY，USART TC才退休并累计tx_bytes；BUSY不建立第二套传输队列。
10ms未完成由下一次Communication服务或UART/TX IRQ检查锁内部故障，不把迟到TC当成功；
这是有界服务检查而非额外独立硬件定时器。普通PE/FE/NE/ORE只恢复字节流，
不会直接Stop或清故障。FIFO内部不变量失败则调用有界safe-off和锁存入口，随后由实时边界
合入Motor；Session逻辑有效性从锁存时立即撤销，不从ADC访问协议对象。内部失败同时
停用RX/TX DMA和UART事件源，避免故障锁存后的中断反复重入，不自动重启Transport。
RX/TX DMA硬件仲裁均为LOW，保留ADC Channel1的通道次序优势；BASEPRI临界区仅屏蔽
优先级5及以下，控制优先级0..4可抢占，块复制不在临界区或ISR内。

72 B样本加Header/CRC为90 B，标准COBS及delimiter共92 B；460800/8N1提供46080 B/s。
50/250/500 Hz分别占4600/23000/46000 B/s。500 Hz已占99.83%，仅剩80 B/s给响应及帧间
空隙，必须记录请求频率和实际TX等待，不把压力窗口伪装成没有请求的纯吞吐测量。
P6按原工作点辨识第一个瓶颈；本预算不预告500 Hz实物通过，也不据此改波特率、降频或扩容。

Kernel静态分配和Cortex-M FromISR边界以供应包源码及
[FreeRTOS内存说明](https://www.freertos.org/Documentation/02-Kernel/02-Kernel-features/09-Memory-management/01-Memory-management)、
[Cortex-M说明](https://www.freertos.org/Documentation/02-Kernel/03-Supported-devices/04-Demos/ARM-Cortex/RTOS-Cortex-M3-M4)核对。
