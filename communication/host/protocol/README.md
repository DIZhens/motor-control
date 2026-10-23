# Core Protocol 客户端

正式G431 Run使用460800/8N1、自研COBS/CRC二进制协议；VOFA可通过
[PC单向数据桥](../vofa/README.md#正式-run-的-pc-数据桥)观察解码遥测，串口仍由协议客户端独占。
Wire合同见[Core Protocol V1](../../spec/CORE_PROTOCOL_V1.md)，实物范围见
[G431验证页](../../../firmware/targets/BLDC_G431/validation/README.md)。

## 连接与命令

先按[开发依赖](../../../DEVELOPMENT.md#44-本地验证依赖)安装`.venv`。串口按板上实际PB6/TX、
PB7/RX连接，与VOFA及其他串口工具互斥。以下命令从仓库根目录运行；`inspect`只读，
连接时显式新建Session并读回Snapshot、Staged及Active，不恢复旧目标或运动请求。

```powershell
.\.venv\Scripts\python.exe -m communication.host.protocol --port COM5 inspect
.\.venv\Scripts\python.exe -m communication.host.protocol --port COM5 read 0x0003
.\.venv\Scripts\python.exe -m communication.host.protocol --port COM5 --log "$env:TEMP\g431-session.jsonl" inspect
```

`write-pi <kp> <ki>`整组写Staged，`telemetry <0|1> <50|250|500>`整组写遥测配置；
设备只在健康IDLE/OFF接受，重复请求不再次增加generation。线上Telemetry/Event已接入；
50/250Hz无功率通信及250Hz单次有限运动已有证据，默认enabled本身不授予运动许可。

`start <rpm>`、`speed <rpm>`、`clear`对应完整业务请求；`stop`使用唯一允许的无会话Stop。
这些是显式操作入口。新协议镜像已完成一次固定空载`+60→+40 rpm→无会话Stop`，
同身份Start重复帧只执行一次；证据见G431验证页，不能外推为持续或带载许可。实际运动
仍需满足原固定空载台架、供电和有限包络。协议OK、Motor结果及
Snapshot状态分别判断；Motor ACCEPTED不代表已经RUN，Stop不承诺主动减速至零。

## 重试与记录

[client.py](client.py)由单一所有者同步使用，一次只处理一个普通请求。响应按message、
Session、Sequence和subject关联，过滤旧会话响应。普通请求每次等待100ms，最多重发两次，
重发完整相同字节及身份；IN_PROGRESS不是终态。总超时表示结果未知，撤销本地会话资格，
后续须显式建立新会话，不自动重连或恢复旧Start。串口写失败/部分写入不自动补发。
客户端把pyserial读取等待收紧至最多5ms、写等待收紧至请求timeout，避免默认无限等待；
自定义流同样须提供有界read/write，100ms是Host等待策略，不是硬实时保证。
无会话Stop的序号从随机非保留值开始后单调递增，减少不同客户端误收迟到应答的机会；
它只在途去重，完成后即使原字节重发也可再次执行Stop，不承诺跨执行次数的恰好一次。
这是最佳努力通信路径，不能代替独立守卫或物理断电。

连接同一已知Session时保留本实例序号；重连优先使用默认新随机ID，不手工复用另一实例
遗失的旧ID。设备复位后Session消失，主机不能把上一启动的缓存结果当成当前状态。

`JsonlLogger`新建文件并拒绝覆盖，记录发送字节/重试次数、接收帧身份及关联的解码结果，
时间为Host单调秒；无效浮点保存为null并保留有效位。RPC等待和`Client.poll()`均持续接收
Telemetry/Event，按Session/config generation分别统计序号缺口、保留最新解码值；事件序号
独立。目录须预先存在，重复运行选择新日志文件名。

Host先排出串口驱动已有字节，不能以Python调度/日志延迟推断线上半帧空闲；观察到空读时
仍按20ms清半帧并等待delimiter。设备端20ms超时及Host请求100ms/最多两次原身份重发不变。

无运动压力入口（需要先现场确认OUTPUT关闭和逻辑供电）：

```powershell
.\.venv\Scripts\python.exe -m communication.host.protocol.pressure --port COM5 --rate 250 --seconds 30 --request-hz 1 --log capture.jsonl --report summary.json
```

入口只读取状态和整组写遥测配置，拒绝非健康IDLE；先关闭遥测、建立新会话，给该工作点
独立序号起点，然后启用指定速率、每秒穿插一次COMMUNICATION_STATUS读取，最后关闭遥测
并排空在途帧。报告保留实际时长、请求量/最大延迟、重发、两端错误及覆盖/缺口，不自动
重试整轮，不发送Start/Update/Clear。500Hz仅IDLE，缺口须与覆盖一致且损失低于1%；50/250Hz
要求两者均0。该入口不代替功率关断、控制时序及MCU栈的独立读回。
本阶段按250Hz完成通信验收；500Hz实测1.92%仍是上述原门槛下的FAIL，保留为容量边界，
压力脚本没有放宽判据或将该结果改为PASS。

## 离线验证

```powershell
.\.venv\Scripts\python.exe -X utf8 communication/protocol/tests/uart/verify_core_server.py
```

同一测试在真实ARM Server/Codec、Runtime交接和参数模型上运行Python Client，覆盖应答丢失
后的原身份重发、重连不恢复Start、旧响应过滤及JSONL。Transport和实时推进是替身，
另行实物验证串口，不代表吞吐、运动或WCET通过。
