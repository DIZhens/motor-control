# 总体架构

本文档是本仓库长期架构合同。它只记录需要长期遵守的职责、依赖方向、替换边界、状态所有权和安全语义；具体 API 以对应源码头文件和 `interface/` 合同为准。

## 1. 项目定位

本仓库实现一套可跨电机控制板复用的 BLDC/PMSM 控制系统。

“通用”不表示提前支持所有硬件和协议，而表示**替换成本局部化**。

当前明确保护两个变化轴：

```text
控制板：
Custom STM32G431
      ↕
ODrive 等可运行我们固件的控制板
      ↕
未来其他 STM32 电机控制板

通信：
UART ↔ USB ↔ CAN ↔ TCP
```

项目不以某个第三方生态为中心。成熟方案用于提供已经验证过的工程经验；通用成熟组件优先复用，核心设备语义由本项目拥有。

## 2. 顶层边界

```text
hardware
    ↓
firmware/targets/<board>/platform
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

硬件采样的物理通道映射、定时器和 ADC 实例属于 Platform；电流 offset、比例换算、相电流重构、估计器和调制算法属于可移植控制算法。Platform 不为了“方便”提前把板级采样拓扑伪装成某个固定控制算法结果。

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

### 4.3 Platform 与 Hardware Capability Contract

跨板复用的核心不是统一 ADC/TIM/SPI API，而是统一当前 FOC Runtime 真正需要的**硬件能力语义**。

典型能力：

```text
Drive Timing
    确定 PWM / sample / control 的时间关系

Synchronized Feedback
    在规定时刻提供一组一致的同步硬件观测

Position Observation
    提供带时序语义的位置观测

Bus Observation
    提供母线等必要物理量观测

Power Stage Actuation
    在规定边界应用三相桥命令，并支持 software safe-off

Hardware Trip
    提供不依赖 CPU 的快速硬件保护能力及可观测故障事实
```

关系：

```text
               ControlRuntime
                     │
          Hardware Capability Contract
             ┌───────┼────────┐
             ▼       ▼        ▼
        Custom G431 ODrive  Future Board
          Platform Platform   Platform
```

如果只是 MCU、PCB、Gate Driver、ADC/TIM 实例或编码器器件变化，而上述能力语义仍一致，变化应限制在 Target / Platform。

禁止在控制核心中用大量：

```c
#ifdef BOARD_X
```

传播板级差异。

若采样拓扑、功率拓扑或安全能力确实改变了控制语义，可以形成明确的 capability 差异，而不是伪造统一接口。

#### 4.3.1 Power Stage 与 Drive Timing

`control/` 负责 FOC、逆 Park 和 SVPWM，并向 Platform 提交归一化三相 `PhaseDuty`。Platform 负责最终 finite/range 检查、硬件安全 clamp、最小脉宽、CCR 换算、互补输出极性、preload 和确定边界同步生效；Control 不直接看到 CCR 或 TIM。

第一版长期不变量：

```text
Drive Timing Start != Power Enable
```

Drive Timing 在系统装配完成后启动一次，并在 IDLE / RUN / FAULT 中持续运行；正常 Motor Stop / Fault 不通过停止实时基准实现安全关断。第一版不因为接口对称而要求提供正常运行期 `drive_timing_stop()`。

对于低边 Shunt 等会让 PWM 命令影响下一周期可测窗口的 Target，具体 Platform 允许联合形成：

```text
PhaseDuty
    ├─ Power-stage update
    └─ Current-acquisition plan
```

两者必须一起 stage，并在同一 PWM update boundary 对应生效。Hardware Capability Contract 不把固定采样点或固定 B/C 两相采样写成长期假设。

#### 4.3.2 Synchronized Feedback 与普通 Observation

同步电流路径只承诺“这一组硬件观测在同一规定采样事件中真实取得”。Platform 应使用相位等硬件语义，而不是暴露 `ADC1/ADC2` 这类 MCU 实例；同时不把 offset、比例换算和三相重构偷偷固化进板级实现。

```text
Hardware trigger
      ↓
Platform coherent raw observation
      ↓
sample-ready
      ↓
ControlRuntime
      ↓
control/measurement
    offset / scale / reconstruction / usability
```

必须保持：

```text
sample-ready != sample-usable
```

Platform 可以提供采样窗口、实际相位对等硬件事实；最终控制可用性由可移植 measurement / Runtime 根据算法约束判断。

Position、Bus、Thermal 等不要求成为 fast tick 的观测按独立 capability 发布“最新值 + timing/freshness 所需信息 + validity”。它们不因为也是 ADC/SPI 数据就并入一个万能 Sensor API，也不主动触发完整 FOC 周期。

Position acquisition 不得要求在 fast-control ISR 中阻塞等待 SPI。具体 Target 可以选择接近 fast tick 的采样频率，也可以较低频采样；采样频率不是 Hardware Capability Contract。可移植 estimator 负责 unwrap、速度估计和角度预测。

#### 4.3.3 Capability 绑定方式

当前每个 Firmware image 只对应一个确定 Target，因此第一版采用链接期静态实现，不建立万能 `PlatformOps`。

```text
ControlRuntime → Platform
    通过稳定 capability API 提交动作

Platform → ControlRuntime
    对真正的硬实时事件使用 callback + context 反向通知
```

只有出现真实的运行时替换、多实例或测试注入需求时，才把对应小能力升级成 `xxx_ops_t` 接口对象；不因为使用 DIP / IoC 就预先给所有能力制造函数指针表。

### 4.4 Hardware Trip 语义

Hardware Trip 的第一职责是**先切断危险功率输出**，软件观察和故障归因发生在其后。

```text
physical protection condition
        ↓
hardware trip
        ↓
power output safe-off
        ↓
software event / evidence
        ↓
Motor Fault Policy
```

公共合同只承诺 Target 能可靠提供的故障类别，不伪造硬件无法保证的精度。更细的相位、比较器等来源可以作为诊断 evidence，但 Motor 的安全状态不得依赖诊断归因一定成功。

Hardware Trip 至少需要支持：

```text
当前保护条件是否仍 active
已经发生的 trip event / cause
显式 protection re-arm
```

并保持：

```text
Protection Re-arm != Power Enable
```

re-arm 只恢复保护链可再次工作的条件，并保持功率输出 disabled；重新进入 RUN 必须经过新的显式 Start。

### 4.5 Target

每块 MCU 控制板拥有一套完整目标工程：

```text
firmware/targets/<board>/
    platform/
    *.ioc
    Core/
    Drivers/
    MDK-ARM/
```

CubeMX / Keil 工程按正常 STM32 工程保存，不为了目录整齐拆散。

共享源码位于 Target 外，由具体 Keil 工程引用。

具体 Target 必须把 software safe-off 和 Hardware Trip 的最终输出状态落实到真实 Gate Driver / 功率桥的“全关断”语义，并通过对应板卡的实机验证确认，不能只把 `MOE=0`、某个 GPIO 电平或某个 HAL 返回值当作安全证明。

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
Arm feedback / protection / events
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

IDLE 表示系统处于安全空闲且可以接受配置、诊断和 commissioning，不等价于“已经满足所有 RUN 条件”。`IDLE → RUN` 必须通过 Run Prerequisites 门控；缺少标定或必要反馈时保持 IDLE，而不是把“尚未准备好”伪装成硬件 Fault。

Fault 至少区分：

```text
Hardware Trip
    CPU-independent 最快保护

Fast Runtime Fault
    软件实时保护并立即 safe-off

Motor Fault Policy
    active / latched / Clear / Restart 生命周期
```

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
Device Contract
      ↓
Core Protocol
      ↓
Transport
 ┌────┼────┬────┐
UART USB  TCP   CAN
```

UART、USB CDC、TCP 可以自然复用字节流 framing；CAN 是帧式链路，允许使用自己的分包/映射方式，但不得因此重新定义设备业务语义。

第一阶段不预设 CANopen Object Dictionary，也不让旧协议对象编号反向定义新 Motor API。

## 7. Host

Host 是第一方设备应用，不是通用上位机平台。

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
- Serial Studio、PlotJuggler、Jupyter 可以辅助观测和分析，但不是 Device Contract 权威；
- 不预建插件系统、通用 Dashboard 编辑器、通用协议编辑器或多厂家设备平台。

## 8. 第一阶段边界

第一阶段目标硬件：

```text
Custom STM32G431 BLDC board
```

第一条链路：

```text
UART
```

Host 第一阶段只需要：

```text
Connect
Start / Stop
Control mode + target
State / Fault
少量必要参数
关键 Telemetry
实时曲线
数据保存
```

Firmware 第一阶段围绕真实 G431 硬件建立 FOC 所需 Hardware Capability Contract，并完成控制主线。

明确不做：

```text
ODrive target
USB / CAN / TCP 多链路实现
CANopen
ESP32 Gateway
多设备管理
通用实验平台
通用 Host 插件框架
```

只有真实需求出现后才扩展。
