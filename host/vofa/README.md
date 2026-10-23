# VOFA 观察

正式 Run 使用 460800 / 8N1 的自研 UART 协议。以下 PC 数据桥用于当前 Run；后文
115200 / JustFloat 直连和 ASCII 命令只适用于独立验证入口及历史镜像，不能混用。

## 正式 Run 的 PC 数据桥

`bridge.py` 由唯一 Core Protocol Client 独占串口，将完整校验且属于当前 Session 的遥测
记录为原始 JSONL，再向本机 `127.0.0.1:1347` 单向发送 JustFloat UDP。默认只发送两路速度
用于显示；`--view current` 显示三路电流，`--view all` 显示下面列出的十项。原始记录始终完整。
VOFA 使用网络连接，
不占用 MCU 串口。依据 [VOFA 接口说明](https://www.vofa.plus/docs/learning/) 和
[JustFloat 格式](https://www.vofa.plus/docs/learning/dataengines/justfloat/)，每帧为所选通道的小端 float
加 `00 00 80 7f` 帧尾。转发器不接收网络数据，没有 VOFA 到电机的命令通道。

1. 在 VOFA 选择 UDP，配置本地监听端口 `1347`，协议引擎选择 JustFloat，开启接收。
   若界面提供本地地址，选 `127.0.0.1`；远端发送地址不参与本桥接收。关闭串口连接及定时发送。
2. 放入波形控件，时间步长设 `4 ms`（250 Hz）。默认速度视图 I0/I1 为目标/实际速度；
   `current` 视图 I0/I1/I2 为 Iq reference / Iq feedback / Id feedback。
   `all` 视图才使用下表的十项顺序；状态/故障用数值查看，母线单独显示。
3. 从仓库根目录运行下列有限观察入口，日志目录须已存在，文件名须为新文件：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m host.vofa.bridge --port COM5 --seconds 30 --log "$env:TEMP\g431-observe-new.jsonl"
```

该入口只在有效、无故障 IDLE 建立会话、读取状态并配置 250 Hz 遥测；不写 PI，不发任何
运动或故障恢复命令，不自动重连，结束后关闭遥测。有限运行工具可复用同一 `TelemetryForwarder`
作为 Client 的记录入口，使 RPC 等待期间也持续记录/显示；不得另开第二个串口所有者。

| all 视图 VOFA 通道 | 数据 | 单位 |
| --- | --- | --- |
| I0 | 目标速度 | rpm |
| I1 | 实际速度 | rpm |
| I2 | Iq reference | 名义 A |
| I3 | Iq feedback | 名义 A |
| I4 | Id feedback | 名义 A |
| I5 | VBUS | V |
| I6 | Motor State | 枚举 |
| I7 | Fault | 位集合 |
| I8 | valid flags | 位集合 |
| I9 | limit flags | 位集合 |

顺序与旧固件十通道不同。无效测量和无效状态保留 NaN，不用零替代。JustFloat 中整数经过
float32 转换，仅用于显示；故障精确位、采样/控制时间、Session、序号、代际及缺帧分析以 JSONL
原始协议数据为准。UDP 显示是最佳努力，不回放、补点或反压串口；接收画面不能替代原始记录。
脚本只证明发出 UDP，实际 VOFA 接收、通道绑定和截图须在界面确认。不要把缺帧的图横轴当作
严格控制时间；指标分析使用日志中的控制时间和样本序号。

离线验证：`.venv/Scripts/python.exe -m unittest host.vofa.test_bridge -v`。

有限运行后可用控制时间生成描述指标：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m host.vofa.analyze capture.jsonl --output metrics-new.json
.\.venv\Scripts\python.exe -m unittest host.vofa.test_bridge host.vofa.test_analyze -v
```

分析按 Session、配置代际、活动 PI 代际和 RUN 内目标窗口分开；保留未达到 90%、窗口末尾
仍未进入误差带等未观测结果为 null。10%～90%、5%目标误差带及末尾20%统计窗口只是描述
约定，不是新增验收门槛。末尾窗口不能自动称为稳态，250 Hz 遥测也不证明 10 kHz 电流带宽。
曲线结果还须结合通信缺口、独立故障/守卫和停机后的关断证据解释。

清理旧图形缓存时，点击波形控件下方彩色滑条左下侧、灰色采样点计数左边的垃圾桶。
这只清理 VOFA 的曲线历史，不删除本仓库原始日志。原串口曲线与新桥的通道含义不同，
切换视图后先清缓存，再重新选择通道。清缓存不会改变发送帧的通道数量；需要减少自动建立的
通道时应选择 `speed` 或 `current`。IDLE 的 Iq reference 无效，all 视图 I2 显示 NaN 是正常状态。

## 旧串口直连与有限运行记录

固件有 [G431 Observe / Run](../../firmware/targets/BLDC_G431/runtime/README.md) 两种装配：
Observe只观察并拒绝启动，Run允许原板固定空载范围内的有限启停。实际验收范围见
[G431 验证页](../../firmware/targets/BLDC_G431/VALIDATION.md)。

## 软件位置

本机 Windows 程序为 `D:\c_tool\VOFA+\vofa+.exe`，快捷方式为 `D:\c_tool\VOFA+.lnk`。
程序及配套 DLL、插件、QML 文件整体保存在本机工具目录；本仓库保存操作说明与后续调试配置。
其他机器安装 VOFA+ 后按下述设置使用，固件和协议不依赖这个绝对路径。

## 串口连接与观察

1. 使用 3.3 V 逻辑的 USB 转串口，转换器 RX 接板 PB6 / USART1_TX，TX 接 PB7 /
   USART1_RX，GND 接 GND；板子按现场既定方式单独供电，转换器 VCC 不接板。
2. 在 VOFA+ 选择对应串口、115200 bit/s、8 数据位、无校验、1 停止位，数据引擎选 JustFloat。
3. 十通道的唯一字段顺序、单位与有效位见 [VOFA 协议](../../interface/VOFA_PROTOCOL.md)。
   速度在取得两份新的合法编码器位置后有效，过期或采集失效时为NaN；Observe的Id/Iq
   始终无效，Run在本启动校零及角度有效后发布名义A。不要以缺失曲线代替零值。
4. 下行发送ASCII，末尾带实际LF。Observe可单独发`stop`、`clear`检查结果；
   `start:120`在Observe返回NOT_READY。Run只按下节有限参数操作。结果/状态编码见
   [Device Contract](../../interface/DEVICE_CONTRACT.md)。

每次只发一个请求，等待新遥测；该最小协议没有逐命令可靠应答。接收出现丢字节或半行超时后，
先单独发一个 LF 使接收器回到行边界，再发完整请求。连接/重连不自动运行。
通道曲线先看控制时间、母线和状态，确认数据在更新，再观察通道 2 的机械速度。

## 有限 Run 操作

本流程仅用于当前G431、2804电机、原三相及MT6701接线、固定空载条件，
不适用于任意电机或持续运行。Run固定参数和保护量级以
[Runtime装配](../../firmware/targets/BLDC_G431/runtime/README.md#有限运行入口)为准。
核对主工程`BLDC_control V1.uvprojx`的唯一Target为`BLDC_G431_Run`，产物位于`build/run/`；
Observe在独立`BLDC_G431_Validation.uvprojx`中。编译、烧录和供电切换分开完成。
验收镜像SHA256从Target验证页取得，不用Observe或旧台架镜像套用此流程。

1. OUTPUT关闭时断开ST-Link的3V3，保留SWD/GND；原三相/编码器已接，撤去外部注入，
   电机固定且轴空载。电源设12.0 V／0.20 A，静态正常后保持转轴静止；
   若切换供电或接线时曾触碰转轴，按一次板上RST重新建立本启动零点。
2. 连接VOFA，确认时间持续递增、状态I6为IDLE（1）、故障I7为0、母线I5约12 V，
   静止速度I2接近0。等待内部4096点校零后，I3/Id和I4/Iq为有限数、I8有效位为511。
   IDLE本身不代表就绪。若Id/Iq持续NaN或Start返回NOT_READY，先停发运动请求；
   校零窗口失去静止/有效位置会锁定失败，不靠`clear`恢复，保持静止后人工RST一次再检查。
3. 下行设置为单次ASCII、实际LF结束，预先准备`start:60`、`speed:40`、`stop`三个发送项。
   每次只发一条并观察新遥测：Start成功后I6为RUN（2）、I1为活动目标；
   RUN内`speed:40`更新目标，Stop清目标并回IDLE。反向使用`start:-60`与`speed:-40`。
   关闭定时连发和自动重试，不在运行中发送反向Start；换方向先回IDLE并确认轴已静止。
4. 本版本每个被接受的Start最多1.5秒，调速不续期；未手动Stop也应自动回IDLE、目标0。
   正反转和时限已实测，但手工点击不能用于精确计量1.5秒。
   想提前停止时发送一次`stop`；软件Stop及自然到期都撤销输出，不保证主动减速至零。
5. FAULT时功率应关闭，保留故障值和曲线；`clear`尚不实现恢复，不反复Start或清寄存器。
   现场检查后再人工复位重装配。试验结束可关OUTPUT，不把闭环模型或少量试转当作精度、
   长期稳定性和全部保护实机验收。

曲线可分别显示I1/I2（目标与实际rpm）、I3/I4（名义A），状态/故障/有效位单独查看。
不同单位不共用一根纵轴。已保存的正向Stop、反向独立时限曲线均从Target验证页进入。

## 波形界面的最少设置

1. 将波形图控件拖到画布，在图内右键打开 Y 轴通道选择，先只勾选 I2（机械速度）。
   时间 I0 持续递增，不与速度叠加到同一纵轴。
2. 图下方 `Δt` 设为 `10 ms`，对应当前约 100 Hz 的遥测周期。
3. 点击右侧通道名称，选中名称输入框后用 `Ctrl+A` 完整替换，再按 Enter：
   I0 可命名为 `时间_s`，I2 可命名为 `机械转速_rpm`。名称只影响显示，字段含义以协议为准。

少量界面设置由操作者按步骤完成。配置完成后使用 VOFA+ 原生保存/导出功能留存模板；
取得当前版本的实际导出样例后，再决定是否生成或校验配置文件。当前仓库尚未提供可直接导入的模板，
运行中界面与本机缓存文件也可能不同，不直接覆盖运行中的缓存。

串口采集、解析和统计可以由脚本完成；VOFA+ 与脚本不能同时占用同一个串口，切换前先断开当前连接。
原生插件支持协议引擎和 QML 控件，但不据此假定可以控制整套界面；公开能力参见
[插件系统](https://www.vofa.plus/docs/learning/start/plugin_system/) 和
[控件 API](https://www.vofa.plus/docs/learning/widgets/development/)。

## 首次无功率反馈验收

该流程用于独立验证工程中的Observe，不是每次Run试验的前置回归。
保持电池与电机三相断开、稳压电源 OUTPUT 关闭，由已确认的单一 3V3 来源提供逻辑供电；
编码器保持原 SSI 接线。先烧录已完成离线检查的 Observe 镜像，再连接串口。

1. 确认十通道持续更新、state 为 IDLE、faults 为 0，Id/Iq 为 NaN；静止且编码器健康时
   速度应为接近零的有限数，不能把未取得反馈时的 NaN 当作静止。
2. 每次单独发送 `stop`、`clear`、`start:120` 并等待新帧，核对结果依次为 ACCEPTED、
   ACCEPTED、NOT_READY；状态保持 IDLE，目标保持零，无功率使能。
3. 手动向两个方向缓慢转动转轴，观察速度正负变化与停转后的回落。当前正负只对应编码器
   坐标，不推断顺/逆时针定义或已标定电机方向；没有独立转速基准时只验收响应与方向。

本流程验收新入口的 UART 收发、命令交接和手动反馈；不重复旧 PWM 抓图，不证明 FOC、
电角度对齐、速度精度或带载能力。现场结果由 Target 验证入口记录，未实测前不标记通过。
