# 总体架构

本文档是本仓库长期架构合同。它只记录需要长期遵守的职责、依赖方向、替换边界、状态所有权和安全语义；具体 API 以对应源码头文件和 `interface/` 合同为准。

文中的 `Single-Motor Encoder Phase-Current FOC Profile V1` 定义长期硬件与实时能力边界，不等于一次交付必须实现本文所有模块、控制模式和通信方式。展示目标包含 G431 单电机、MT6701、FOC 电流内环和速度外环、UART 自研协议及 FreeRTOS 后台集成；控制基线先使用现有 UART / VOFA。CAN 实物接入、Qt Host 与位置环后续再做。活动阶段的具体目标和验收边界以本地 Git 检查点正文为准，见 `DEVELOPMENT.md`。

## 1. 项目定位

本仓库实现一套可跨电机控制硬件复用的 BLDC/PMSM 控制系统。

“通用”不表示提前支持所有硬件和协议，而表示**在明确 Control Profile 内把替换成本局部化**。第一版目标收敛为：

```text
Single-Motor Encoder Phase-Current FOC Profile V1

one MCU runtime
    =
one Firmware Node
    =
one Motor
```

V1 面向能够运行本项目 Firmware 的单电机三相 BLDC/PMSM 控制节点。多电机系统通过多个 Controller Node 与通信层协调，不在一个 MCU Runtime 内预建多轴控制框架。

Target 不等同于某一块 PCB，而是一个确定的 **Target Configuration**：

```text
MCU
+ three-phase power stage
+ selected phase-current sensing configuration
+ selected encoder source
+ static timing / protection configuration
```

同一块物理板可以因采样拓扑、位置源或静态配置不同而形成不同 Target Configuration；核心板、功率板和传感器的组合也可以共同构成一个 Target。

当前明确保护两个变化轴：

```text
Motor-control Target：
Custom STM32G431
      ↕
ODrive-compatible / other suitable controller hardware
      ↕
future compatible Target Configuration

Communication：
UART ↔ USB ↔ CAN ↔ TCP
```

Hardware Capability Contract 本身不绑定 MCU 厂商，但当前实现与实机验证以 STM32 为主；“接口未阻止其他 MCU”不等价于项目已承诺支持其他 MCU。

V1 的通用边界遵守一条长期判断规则：如果新硬件差异只改变 Platform 如何兑现既有物理语义，而不改变 FOC 主实时数据流，则属于同一 Profile；如果差异迫使采样、调制、估计结构或一个 Node 的控制对象数量发生根本变化，则新增 Profile 或系统层，而不是继续扩宽 V1。

项目不以某个第三方生态为中心。成熟方案用于提供已经验证过的工程经验；通用成熟组件优先复用，核心设备语义由本项目拥有。

## 2. 顶层边界

```text
hardware
    ↓
firmware/targets/<target>/platform
    ↓
Hardware Capability Contract
    ↓
Firmware Motor / ControlRuntime
    ↓
control
    ↓
Device Contract
    ↓
Protocol
    ↓
Transport
    ↓
Host Device API
    ↓
Qt GUI
```

顶层目录职责：

```text
control/
    可移植算法，只回答“怎么算”。

protocol/
    可移植设备Profile、链路Frontend/事务/Codec与窄Transport Port；不依赖Firmware、HAL或RTOS。

interface/
    Firmware 与 Host 共同遵守的 Device / Protocol Contract。

firmware/
    MCU 上完整运行时，包括 Motor、ControlRuntime、Communication、
    SystemRuntime、Platform contract 与具体 Target。

host/
    PC 设备应用。负责连接、控制、配置、状态、Fault、Telemetry、
    曲线和数据保存，不建设通用上位机生态。

hardware/
    物理板卡事实，不保存软件业务规则。
```

## 3. control

`control/` 不依赖 STM32、HAL、CubeMX、Qt、通信协议或具体 PCB。

可以包含：

```text
Clarke / Park
PI
FOC
SVPWM
Measurement / Reconstruction
滤波
Estimator / Predictor
数值算法
算法自身历史状态
```

不能拥有：

```text
Motor 生命周期
Start / Stop / Clear Fault
设备协议
通信状态
PWM / ADC 中断
HAL 对象
板卡名称
```

硬件采样的物理通道映射、定时器和 ADC 实例属于 Platform。ADC / OPAMP / Shunt / 传感器硬件的名义传递关系以及 raw value → 物理相电流 [A] 的换算也属于 Platform；零电流 offset、每相增益误差等实验校准修正、缺相重构、估计器和调制算法属于可移植控制算法。Platform 不为了“方便”提前把板级采样拓扑伪装成已经完整重构的控制算法结果。

## 4. Firmware

Firmware 长期只有四类职责：

```text
Motor
Communication
SystemRuntime
Platform
```

### 4.1 Motor

Motor 是固件公共业务语义的唯一权威。

公共生命周期第一版保持最小：

```text
BOOT → IDLE → RUN
          ↑    ↓
          └─ Stop

BOOT / IDLE / RUN → FAULT
FAULT --显式 Clear 成功→ IDLE
```

生命周期和控制模式是不同维度。控制模式用于决定级联控制链入口，例如：

```text
CURRENT
VELOCITY
POSITION
```

外部 Command 必须形成完整业务意图，例如：

```text
control_mode + exactly one target
```

不能让通信层分别修改 mode 和 target 后再由 Motor 猜测组合状态。

Motor 拥有外部 Command、生命周期和业务验证；ControlRuntime 拥有实时 Active Command 与内部 Reference。Command 必须完整提交，并在确定的实时边界进入 Active 状态。第一版 RUN 中只允许更新当前模式的 target，改变 ControlMode 需要先回到 IDLE；以后若确实需要在线模式切换，再单独设计无扰切换。

Motor 对外发布一致性只读 Snapshot。Communication / Host 不直接读取正在被实时控制修改的 Runtime 私有变量。

### 4.2 ControlRuntime

ControlRuntime 是 Motor 内部的确定性实时执行机制，也是实时控制状态的单写者。

它负责：

```text
同步反馈接收
Pending Command → Active Command
输入快照与 freshness 判断
位置 / 速度估计状态
多速率调度
Reference / Feedback 实时真值
快速故障检测
控制流水线状态
```

同步相电流 sample-ready 是第一版唯一硬实时 fast tick。位置环、速度环、电流环等在同一确定性时基上做整数分频调度，不为各控制环建立彼此异步的独立定时中断。慢环输出在两次更新之间保持。

异步采集的 Position / Bus 等 Observation 可以有自己的采样节拍，但不得异步修改控制算法内部状态。ControlRuntime 在 fast tick 边界消费最新观测，根据 timing / freshness 更新 estimator，并把需要的角度等状态预测到当前控制时刻。

控制时间由 PWM / ADC 等确定性硬件事件产生，不依赖普通后台任务延时。

引入 FreeRTOS 后，sample-ready、控制计算和 PWM 提交仍在同一硬件同步实时边界执行，慢环继续整数分频；不为电流环、速度环建立独立任务。该高优先级控制 ISR 不调用 RTOS API、不等待队列或互斥锁。后台任务处理通信、遥测及非实时维护，通过有界命令交接和一致性 Snapshot 与 Motor 协作，保持实时状态单写者。

Target 负责 RTOS 移植、静态资源和中断优先级配置，`control/`、公共 Motor 语义及协议编解码不依赖 FreeRTOS。Cortex-M 上调用 `FromISR` 的通信中断必须满足内核系统调用优先级限制；具体优先级在集成时核对，依据 [FreeRTOS Cortex-M 官方说明](https://freertos.org/Documentation/02-Kernel/03-Supported-devices/04-Demos/ARM-Cortex/RTOS-Cortex-M3-M4)。控制时基、HAL 时基与 RTOS tick 分开核对，不能互相替代。

### 4.3 Platform 与 Hardware Capability Contract

跨 Target 复用的核心不是统一 ADC/TIM/SPI API，而是统一 V1 FOC Runtime 真正依赖的**硬件能力语义**。分析时可以讨论很多硬件能力概念，但公共 Platform API 不按概念数量机械拆分。

目录命名固定为：

```text
firmware/platform_contract/
    Hardware Capability Contract 的公共头文件，只定义跨 Target 稳定语义

firmware/targets/<target>/platform/
    当前 Target 对上述合同的具体实现，可以依赖 MCU / HAL / Timer / ADC / SPI 等细节
```

两者都属于 Platform 边界，但前者是 contract，后者是 implementation；不再使用同名顶层 `firmware/platform/` 目录，避免把公共合同与某个 Target 的实现混淆。

V1 的最小能力集合收敛为：

```text
Mandatory
    Realtime Motor I/O
        deterministic drive timing
        three-phase duty actuation
        synchronized / deterministic phase-current acquisition
        Power Enable
        software safe-off

    Position Observation
        encoder-derived mechanical position observation

    Bus Observation
        physical DC-bus voltage observation

Optional
    Hardware Trip
        CPU-independent hardware protection and safe-off
```

关系：

```text
                   Motor / ControlRuntime
                           │
              Hardware Capability Contract
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
      Realtime Motor I/O  Position       Bus
             │
             └──── optional Hardware Trip
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
        Custom G431   ODrive target   Future Target
```

V1 Target 至少需要满足：

```text
3-phase BLDC/PMSM inverter
one MCU/Firmware Node controls exactly one Motor
independent A/B/C phase-duty semantics
at least two independent phase-current observations
deterministic current-sampling / control timing
absolute or incremental encoder position source
DC-bus voltage observation
deterministic software safe-off
```

允许被 Platform 吸收而不改变 Profile 的典型差异包括：

```text
3PWM / 6PWM
2 / 3 phase-current sensing channels
low-side / inline / isolated phase-current sensing
internal / external current amplifier
absolute / incremental encoder
不同 MCU / Timer / ADC / DMA / Gate Driver 实现
```

V1 明确不以以下能力为准入目标：

```text
single DC-link shunt
Hall-only rotor feedback
pure sensorless FOC
single-MCU multi-motor runtime
sampling-aware overmodulation 作为必要工作模式
six-step / ACIM / stepper
black-box smart servo drive
```

这些能力未来如果形成真实需求，应建立新的 Control Profile 或系统层，而不是通过扩大现有接口语义强行兼容。

禁止在控制核心中用大量：

```c
#ifdef BOARD_X
```

传播 Target 差异。

#### 4.3.1 Realtime Motor I/O

Realtime Motor I/O 是 V1 最核心的硬实时硬件边界。它把共享同一确定时序事务的 Drive Timing、Power Stage Actuation、同步相电流采集、Power Enable 和 software safe-off 收拢在一起；这不表示建立巨大 Platform 模块，而是承认这些能力必须共同满足同一个 PWM / sample / control 一致性边界。

`control/` 负责 FOC、逆 Park 和 SVPWM，并向 Platform 提交归一化三相 `PhaseDutyRequest`。该请求表达“控制算法希望在 Target 声明的下一有效更新边界起实现的三相平均 duty”，不是 CCR、Timer count 或最终硬件波形。有效更新区间可以包含多个 PWM 载波周期；采样时刻、生效边界和保持时长必须由 Target 明确，并与 Runtime 的控制周期及角度预测参数一致。

Platform 负责：

```text
finite / range 检查
Target actuation constraints
3PWM / 6PWM 与具体 Timer realization
complementary polarity
hardware dead-time
minimum physically valid pulse
preload / update boundary
current sampling-window / trigger planning
Power Enable
software safe-off
```

Hardware dead-time 属于 Platform；为了补偿 dead-time 电压误差的 dead-time compensation 属于可移植控制算法。

第一版长期不变量：

```text
Drive Timing Start != Power Enable
```

Drive Timing 在系统装配完成后启动一次，并在 IDLE / RUN / FAULT 中持续运行；正常 Motor Stop / Fault 不通过停止实时基准实现安全关断。第一版不因为接口对称而要求提供正常运行期 `drive_timing_stop()`。

对于 low-side Shunt 等 PWM 命令会影响下一周期可测窗口的 Target，Platform 可以联合形成：

```text
PhaseDutyRequest
      ↓
Actuation + Acquisition Plan
    ├─ power-stage update
    └─ current-sample plan
```

两者必须一起 stage，并在同一 PWM update boundary 对应生效。Hardware Capability Contract 不把固定采样点、固定 B/C 两相或某个 Timer compare channel 写成长期假设。

V1 通过 Target 声明的有效 modulation envelope 保证可靠相电流采样，不要求为获取极限母线利用率而进行 sampling-aware PWM distortion。Single DC-link shunt 以及依赖采样窗口重塑的 overmodulation 不属于 V1。

正常 control saturation 应发生在 portable control / modulator 内。Platform 的 safety clamp 只是硬件最后保护，不应成为正常工作时持续修改控制请求的机制；如果 Platform 经常修改请求，视为控制配置或 Target envelope 不匹配。


#### 4.3.2 Phase Current、Position 与 Bus Observation

同步电流路径向上提供的是**已经换算到物理单位的真实相电流观测**，而不是 MCU raw ADC count：

```text
Hardware trigger
      ↓
Platform
    ADC / OPAMP / shunt / sensor hardware
    channel → phase mapping
    nominal raw → Amp conversion
    acquisition timing / hardware validity
      ↓
PhaseCurrentObservation
    measured phase value(s) [A]
    measured-phase information
    observation time
    acquisition validity
      ↓
sample-ready
      ↓
ControlRuntime
      ↓
portable Measurement
    zero / gain calibration correction
    missing-phase reconstruction
    plausibility / usability
      ↓
usable Ia / Ib / Ic
```

必须保持：

```text
sample-ready != sample-usable
```

Platform 只能声明这一拍实际测得的相位及其物理值，不用虚假的 0 A 表示“该相未测”，也不为了接口整齐把两相采样偷偷伪装成三路独立实测。

所有 Observation 的 observation time 属于同一个单调 **Control Time Domain**。该时间域用于比较同步电流、Position 与 Bus 的采集时刻，不是 UTC、系统墙上时间、HAL Tick 或 RTOS Tick；具体 tick 分辨率由 Target 声明。ControlRuntime 根据 observation time 计算 freshness、预测和时序关系，Platform 不额外发布通用“stale”业务状态。

Position Observation 与 Realtime Motor I/O 分离，因为其采集机制和节拍可以独立。Platform 屏蔽 SSI/SPI/I2C、ABZ counter、CPR、raw code 等硬件细节，只发布 encoder-derived mechanical position observation 及其 timing / validity / reference 所需语义。

Position 使用机械圈数（turn）作为公共单位，并区分两种 bind-time immutable topology：

```text
PERIODIC
    一圈内的规范化周期坐标，范围 [0, 1)

CONTINUOUS
    位置源真实提供的累计机械位置
```

Platform 不把单圈绝对编码器自行 unwrap 后伪装成 CONTINUOUS；需要 unwrap、速度估计、滤波和预测时，由 portable estimator 完成。referenced 是动态状态，与 topology 正交，不等于 pole pairs、motor direction 或 electrical offset。

Platform Position 不负责：

```text
velocity estimate
electrical angle
pole pairs
motor direction semantics
electrical offset
```

Motor / commissioning 参数把机械 Rotor State 转换为 electrical rotor state。Sensorless 如果未来加入，应在 RotorStateEstimate 层与 encoder path 汇合，不伪装成 Hardware PositionObservation。

Position acquisition 不得要求在 fast-control ISR 中阻塞等待 SPI。Position 可以异步发布 latest observation，但不得异步修改 estimator 状态；ControlRuntime 在 fast tick 边界统一消费。

Bus Observation 单独发布物理母线电压 [V] 及 timing / validity。VBUS 是 V1 必选物理量，不因为它和 NTC 等都可能来自 regular ADC 就建立万能 Analog/Sensor API。Thermal、Gate Driver diagnostic、Brake 等在出现真实需求前不进入 V1 公共合同。

Position / Bus 的 latest publication 必须满足：

```text
coherent
non-blocking
bounded-time
whole-observation copy
```

成功读取表示得到一份完整快照；读取失败只表示“当前没有可交付的 coherent publication”，不能伪造为 acquisition invalid。真正的采集失败由 Observation 自身的 acquisition_valid = false 表达。ControlRuntime 不保留指向 Platform 可变 publication 的指针，而是复制所需 observation 后在自身单写者边界处理 freshness 和 estimator 状态。


#### 4.3.3 Capability 绑定方式

当前一个 Firmware image 对应一个确定 Target Configuration，并且一个 MCU Runtime 只控制一个 Motor。第一版采用链接期静态实现，不建立万能 PlatformOps、Capability Registry、axis array 或运行时硬件发现机制。

```text
ControlRuntime → Platform
    通过稳定 capability API 提交动作

Platform → ControlRuntime
    仅对真正硬实时事件使用 callback + context
```

V1 中真正需要反向硬实时通知的事件主要是：

```text
PhaseCurrentObservation ready
Hardware Trip urgent rescan（仅支持该能力的 Target）
```

Phase Current sample-ready callback 是同步硬实时路径：必须 bounded、non-blocking、non-reentrant；Observation 指针只保证在 callback 调用期间有效，ControlRuntime 不得保留该指针。

Hardware Trip callback 只表示“请尽快重新检查 Hardware Trip authoritative snapshot”，不表示一条无损的新故障事件。它允许延迟、重复或在旧 protection episode 已恢复后才到达，因此 Firmware 不能仅凭 callback 到达次数建立 Fault 事实。

Position / Bus 优先发布 latest observation，由 ControlRuntime 在 fast tick 消费，不让 Runtime 退化成大量异步 callback 的事件处理器。

只有出现真实的运行时替换、多实例或测试注入需求时，才把对应小能力升级成 xxx_ops_t 接口对象；不因为使用 DIP / IoC 就预先给所有能力制造函数指针表。

可选 Hardware Trip 不通过空 stub 假装存在，也不通过 has_hardware_trip() 或通用 registry 做运行时发现。支持该能力的 Target 由 SystemRuntime 在组合阶段把一个窄的恢复依赖绑定给 Motor；不支持的 Target 根本不绑定这项依赖。该 optional binding 只服务于真实的 Hardware Trip 恢复差异，不扩展成通用 PlatformOps。


### 4.4 Hardware Trip 语义

Hardware Trip 是 V1 的**可选安全增强能力**，不是 Base Target 的准入条件。所有 V1 Target 都必须实现并验证 software safe-off；只有明确声明支持 Hardware Trip 的 Target，才必须满足本节 CPU-independent 保护合同。

Hardware Trip 的第一职责是**先切断危险功率输出**，软件观察和故障归因发生在其后：

```text
physical protection condition
        ↓
hardware trip
        ↓
power output safe-off
        ↓
Platform sticky observable protection episode
        ↓
urgent rescan notification
        ↓
Motor Fault Policy
```

公共合同描述的是 **observable protection episode**，不是无损物理事件流。Target 硬件无法区分的同类重复 Trip 可以合并在同一 episode 中；公共接口不承诺记录每一次物理边沿、事件次数或严格 generation。只有 Target 确实能可靠提供的故障类别才进入公共 cause；无法可靠归因时允许 UNKNOWN，更细的相位、比较器等来源只作为诊断 evidence。

Hardware Trip authoritative snapshot 至少包含：

```text
condition_active
    当前受该保护能力覆盖的物理保护条件是否仍 active

causes
    当前尚未被受控恢复的 observable protection episode 的 sticky cause evidence
```

两者必须 coherent。允许：

```text
condition_active = false
causes != NONE
```

表示物理条件已经消失，但旧 protection episode 尚未完成显式恢复。

Hardware Trip callback 不是 evidence authority，只用于 urgent rescan。旧 episode 的 callback 即使在 re-arm 成功后延迟到达，也不得仅凭 callback 重新制造 Fault；Firmware 必须重新读取 authoritative snapshot。

显式 Protection Re-arm 的成功语义必须同时满足：

```text
当前 protection condition 允许恢复
旧 observable protection episode 已被受控退休
硬件保护路径已经重新 armed
功率输出仍保持 disabled
建立了新的 observable protection episode boundary
```

该 boundary 之后形成的新 observable protection episode 不得被本次 re-arm 清除。如果 Target 在恢复过程中无法证明这些后置条件，应 fail-closed 并报告 re-arm 失败，而不是猜测成功。

必须保持：

```text
Protection condition gone
        != Observable episode retired
        != Protection Re-arm
        != Motor Clear Fault
        != Power Enable
        != Restart
```

Motor 对外仍只有统一的 Clear Fault 业务语义。Hardware protection re-arm 成功只说明保护链恢复完成，并不等价于整次 Motor Clear 成功；Runtime 私有 Fault 退休、最终 Fault 检查和 Motor 生命周期提交仍必须完成。允许 protection 已重新 armed、power disabled，而 Motor 继续保持 FAULT。

Motor Clear 有自己的业务提交边界。若新的 authoritative Fault evidence 在该提交前已经可观察，Clear 不得覆盖它；若新的物理故障只在提交后才成为软件可观察 evidence，则允许表现为 Clear 已成功、随后立即重新进入 FAULT，但 Hardware Trip 的物理 safe-off 和后续 Fault evidence 均不得丢失。Callback 到达时间不得被当作物理 Trip 的发生时间。

对于支持 Hardware Trip 的 Target，Motor 只需要一个窄的 optional recovery dependency：

```text
coherent trip snapshot
explicit protection re-arm
```

该依赖在 SystemRuntime 组合阶段绑定；不建立 FaultManager、Capability Registry 或通用 fault-source 表。

### 4.5 Target

Target 是一个确定的 MCU、功率级、相电流采样、位置源和静态时序/保护配置组合，不要求所有能力物理位于同一块 PCB。

当前 STM32 Target 保留一套完整目标工程：

```text
firmware/targets/<target>/
    platform/
    *.ioc
    Core/
    Drivers/
    MDK-ARM/
```

CubeMX / Keil 工程按正常 STM32 工程保存，不为了目录整齐拆散。共享源码位于 Target 外，由具体 Keil 工程引用。

V1 固定：

```text
one MCU runtime
    =
one Firmware Node
    =
one Motor
```

即使一块物理控制板具有多个功率轴，本项目的一个 Target Configuration 也只选择其中一个电机通道参与当前 Runtime；第一版不加入 `axis_id`、axis array、multi-axis context 或 shared-resource manager。多电机系统由多个 Controller Node 通过通信层协调。

`control/` 中 PI、Estimator、FOC 等算法模块仍按正常方式保持可实例化，但 Firmware V1 不承诺在同一 MCU Runtime 同时运行多个 Motor。

所有 Target 都必须把 software safe-off 最终落实到真实 Gate Driver / 功率桥的已定义安全状态，并通过对应硬件验证确认，不能只把 `MOE=0`、某个 GPIO 电平或某个 HAL 返回值当作安全证明。

只有声明支持 Hardware Trip 的 Target，还必须验证其 CPU-independent 保护路径、最终功率级 safe-off 行为以及受控恢复语义。


## 5. 硬实时、启动与安全

长期不变量：

```text
Drive Timing Start != Power Enable
```

控制时基可以持续运行，而功率输出由 Motor 状态与保护策略独立门控。

系统基础启动顺序遵守：

```text
Configure
  ↓
Bind
  ↓
保持 power disabled
  ↓
Arm required feedback / available protection / events
  ↓
Start drive timing
```

FOC 相关校准分成三个生命周期：

```text
每次上电的无功率自动校准
    例如 ADC 自校准、电流零偏、传感器健康检查

装机级主动标定
    例如 pole pairs / direction / electrical offset
    可以持久化，普通上电不自动让电机运动

每次 Start 的实时安全启动
    例如 Runtime reset、反馈同步、安全 duty staging、
    跨确定 PWM boundary 后再 power enable
```

IDLE 表示系统处于安全空闲且可以接受配置、诊断和 commissioning，不等价于“已经满足所有 RUN 条件”。IDLE → RUN 必须通过 Run Prerequisites 门控；缺少标定或必要反馈时保持 IDLE，而不是把“尚未准备好”伪装成硬件 Fault。

Fault 至少区分：

```text
Hardware Trip（仅支持该能力的 Target）
    CPU-independent 最快保护

Fast Runtime Fault
    软件实时保护并立即 safe-off

Motor Fault Policy
    active / latched / Clear / Restart 生命周期
```

ControlRuntime 是 Runtime 私有 Fault 状态的单写者。Clear 期间对 Runtime private fault 的退休 / reset 必须在其确定性单写者边界执行；Motor、SystemRuntime 或 ISR 不得通过异步普通清零覆盖刚产生的新 Runtime Fault。新的 Runtime Fault 与 reset 必须被排序为“reset 之前”或“reset 之后”的确定顺序。

Motor Clear 只在所有所需恢复步骤和最终 Fault 检查通过后提交 FAULT → IDLE。对于支持 Hardware Trip 的 Target，protection re-arm 可以先成功而后续软件 Fault 检查失败，此时 Motor 继续保持 FAULT、power disabled；不得把 Hardware re-arm 的完成时刻冒充整次 Clear 的业务成功时刻。

必须保持：

```text
Fault condition gone
        != Clear Fault
        != Protection Re-arm
        != Restart
```

通信恢复也不得自动等价于电机 Restart。

## 6. interface

`interface/` 是 Firmware 与 Host 之间的共享合同，不属于任一端内部实现。

分两层：

```text
Device Contract
    状态、命令、Fault、观测、参数、单位和行为语义

Protocol Contract
    消息、事务、异步数据、packet 与 framing 语义
```

Device Contract 高于具体 Transport。

```text
Device Contract / Motor Node Profile
      ↓
UART Frontend 或 CAN Frontend
      ↓
Byte-stream Port 或 CAN-frame Port
      ↓
Target硬件实现
```

`protocol/`拥有线格式和事务状态，Firmware Communication只负责共享设备值与
RuntimeRequest/ticket/Completion绑定；Target负责具体UART DMA或CAN控制器。
Device Port不泄漏Firmware类型，Runtime交接不加入链路身份。一个镜像以构建期静态选择唯一
Frontend和控制所有者，不建立运行时探测或Transport Registry。

UART、USB CDC、TCP 可以自然复用字节流 framing；CAN 是帧式链路，允许使用自己的分包/映射方式，但不得因此重新定义设备业务语义。

第一阶段不预设 CANopen Object Dictionary，也不让旧协议对象编号反向定义新 Motor API。

自研协议沿用上述边界，区分 Object、Command、Telemetry 和 Event。对象写入也要经过 Device / Motor 的权限、范围与状态判断；可写目标最终形成完整业务请求，不直接改 Runtime 目标、PI 状态或功率寄存器。请求关联与会话内重复检测属于通信事务语义，不改变 Motor 状态所有权，也不承诺跨复位恰好执行一次。

共享业务编号、结果与能力子集以[Motor Node Profile](interface/MOTOR_NODE_PROFILE_V1.md)为准；
UART精确表示与事务规则以[UART Mapping](interface/UART_MAPPING_V1.md)为准；
[设计说明](interface/PROTOCOL_DESIGN.md)解释边界，不重复维护线格式。协议实现和实物范围分别验收，
不能由合同冻结推断已接入串口或完成调度验证；Device Contract继续拥有设备业务语义。

## 7. Host

Host 是第一方设备应用，不是通用上位机平台。

以下是后续第一方 Qt Host 的技术基线与边界，不是展示版本的前置条件。控制基线使用现成 VOFA；自研协议阶段先提供最小 PC 协议客户端与记录工具，VOFA 适配按实际需要接入。配置、通道映射和操作说明由 `host/` 保存，设备语义仍由 `interface/` 定义。

首选技术基线：

```text
Python
PySide6 / Qt6
Qt Widgets
pyqtgraph
pytest
```

长期边界：

```text
GUI
 ↓
Host Device API
 ↓
Protocol Client
 ↓
Transport Interface
 ↓
UART / USB / CAN / TCP
```

固定原则：

- GUI 不直接拥有串口、CAN 或协议状态机；
- Protocol Client 不依赖具体 Widget；
- Transport 不理解 Start、Target、Fault 等 Motor 业务语义；
- Host 与 Firmware 共同遵守 `interface/`；
- Host 核心行为应可通过 mock / headless test 验证，不依赖人工点 GUI；
- VOFA、Serial Studio、PlotJuggler、Jupyter 可以辅助观测和分析，但不是 Device Contract 权威；
- 不预建插件系统、通用 Dashboard 编辑器、通用协议编辑器或多厂家设备平台。
