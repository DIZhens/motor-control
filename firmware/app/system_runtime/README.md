# SystemRuntime

本目录保存协议无关的后台到实时边界交接、参数值生命周期及原生RTOS任务装配。
它不解析字节、拥有Session或执行FOC。`runtime_exchange`、`runtime_parameters`保持纯C，
不依赖HAL/FreeRTOS；`system_runtime`单独依赖native API，由具体Target选择链接。

## 后台任务与启动

`system_runtime_start`只由Run的main调用。先由Target核对调度器前HAL tick，再静态创建
最高优先级Bootstrap并启动调度器。Bootstrap阻塞2个Kernel tick以核对调度后HAL与Kernel
两个时基，创建单槽Queue Mailbox及Communication/Telemetry任务，然后通过链接期
`system_runtime_target.h`完成板级Motor装配；此时长期任务优先级较低，尚不能处理业务。
Bootstrap记录自身高水位后自删除，Idle内存由静态Hook提供。启动失败、调度器返回、
assert或栈溢出直接调用Target的协议无关关断，不等待ADC或队列，不自动重启。

任务优先级为Bootstrap 3、Communication 2、Telemetry 1、Idle 0，栈分别为
768/768/384/128个32位word。Telemetry按20/4/2ms周期读取已有publication，覆盖Mailbox；
Communication等待通知或1ms超时。当前只验证任务及最新值交接，Communication暂不消费
Completion、不开放Host命令或发送UART；DMA Transport与事务Server接入后由它唯一持有。
高水位每秒保存，静态容量和离线模型都不代替实机任务栈余量或调度延迟验收。

## 请求与完成

[runtime_exchange.h](runtime_exchange.h)只定义三种完整请求：Motor命令、速度PI整组参数、
遥测配置整组参数。线上对象编号与内部枚举没有直接对应关系。
Target在短临界区提交完整副本并返回本启动非零ticket；提交成功仅表示已排队。
ticket不进入Motor或Wire，不因切换协议Session而重置，用尽后锁内部故障而不重用身份。

普通槽与Stop槽各一个。Stop在后台撤销尚未消费的普通请求并返回其ticket，后台据此形成
取消终态；不再为同一次取消向Completion重复投递。已被实时边界消费的请求保留原结果。
不同Stop撞上既有Stop时返回BUSY，不能覆盖旧身份。TIM6等安全来源只请求内部Stop，
由ADC取消旧请求并产生取消Completion；内部Stop本身不占用外部ticket。

Completion为深度4的静态SPSC：ADC唯一生产，后台唯一消费。slot复制先于head发布，
消费复制先于tail发布，Target提供实际CPU/编译器屏障。满时不覆盖，立即走Fail-closed。
完成记录携带准确ticket及独立outcome；正常Motor结果忠实保留，不从“最近命令结果”推测。
参数完成还保存当次权威值及generation，随后写入不能改变旧应答。
取消仅以ticket匹配原事务，不能依赖其其他字段。PRECONDITION_FAILED表示尚未交给Motor。

这些槽不替代通信事务管理。Session、请求去重、同身份重发、响应FIFO及20ms完成等待上限
由后续Communication Server持有；当前还没有这部分实现。

## 参数生命周期

[runtime_parameters.h](runtime_parameters.h)只保存Default、Staged、Active和遥测配置值。
开放采样前初始化，之后只由ADC修改；后台读取Target发布的一致副本，参数写也必须经过请求槽。
应用时检查无故障IDLE、功率关闭和Runtime OFF，不能把两拍启动准备当成可写空闲。

G431 Default为0.001/0.004，Staged generation从1开始；每个新的合法写整组替换并自然u32加1。
非法写不改值或修订。当前尚无协议去重，内部重新提交本身就是新的请求。
Motor接受Start后，实时所有者在START_QUIET安装当时Staged增益到真正的速度PI实例，再锁存
Active及generation。Default静态配置、其他控制参数和数值公式保持；仅ACCEPTED不表示已RUN。
拒绝Start不改Active；Stop/Fault撤销in_use并保留最后值，generation=0也可能是合法回绕后的修订。
没有RUN在线改PI、Flash保存或自动恢复。

遥测配置默认enabled、250Hz、generation1，只在相同健康IDLE边界写入；500Hz仅用于IDLE压力，
G431提交和实时消费两处都阻止Start进入Motor。该配置目前尚未驱动后台发送周期。

## 发布与内部故障

[speed_commissioning.h](speed_commissioning.h)是固定观测值，不是线上帧；不依赖C结构padding。
Target在RUN实际速度环更新边界、OFF/启动期间每10个fast tick复制已有状态及有效位。
采样序号在实时侧自然递增，与后台是否读取或发送无关；位置时间仍是实际编码器采集时间。
位置是编码器原点的单圈坐标，正方向与速度一致，跨圈允许跳变，不是累计圈数或定位零点。
d/q限幅位观察各轴已有积分限幅，公共电压矢量限幅另占一位，不改变控制数值。
后台使用有界奇偶版本读取；竞争失败保持输出，心跳过期或未合并故障撤销使用资格。

G431的`target_runtime_fail_closed`在短临界区经原HCC立即safe-off、锁首因并计数，
不等待ADC、不调用内核或动态分配；后续Start/Update/参数写被拒绝，Session逻辑资格立即失效。
ADC下一边界把bit7合入Motor；先前的其他故障和Profile历史保留，Clear仍不恢复。
队列满、身份/计数不变量损坏、静态装配/调度内部失败属于此路径；外部坏帧、串口断续或
Telemetry覆盖不属于内部故障。真正的Session与事务清理由后续Server响应锁存状态完成。

## 当前装配与验证

只有正式G431 Run链接本模块。Run已移除旧VOFA/UART源码装配，新协议接管前不开放串口命令；
旧邮箱兼容代码仍供隔离控制模型使用，不能建立第二个后台所有者。
Validation工程保持裸机旧入口，不链接本目录或FreeRTOS。

仓库根目录运行真实ARM模型：

```powershell
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py --finite-run --exchange
.\.venv\Scripts\python.exe -X utf8 firmware/targets/BLDC_G431/runtime/tests/verify_system_runtime.py
```

模型通过正式Run装配执行请求、参数、完成FIFO、发布和Fail-closed，HCC/MMIO是替身；
不代表电气安全、ISR WCET、调度延迟或新镜像实物通过。任务模型执行实际Kernel/Port及任务代码，
但在异常切换/任务阻塞处替代硬件上下文切换；新协议Server和UART DMA尚未接入。证据与构建入口见
[G431验证页](../../targets/BLDC_G431/VALIDATION.md)。
