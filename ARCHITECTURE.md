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
滤波
估计器
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

### 4.2 ControlRuntime

ControlRuntime 是 Motor 内部的确定性实时执行机制。

它负责同步反馈、控制计算、多速率调度、Reference / Feedback 实时真值、快速故障检测以及控制流水线状态。

控制时间由 PWM / ADC 等确定性硬件事件产生，不依赖普通后台任务延时。

### 4.3 Platform 与 Hardware Capability Contract

跨板复用的核心不是统一 ADC/TIM/SPI API，而是统一当前 FOC Runtime 真正需要的**硬件能力语义**。

典型能力：

```text
Drive Timing
    确定 PWM / sample / control 的时间关系

Synchronized Feedback
    在规定时刻提供完整一致的相电流等同步观测

Position Observation
    提供位置 / 角度观测

Bus Observation
    提供母线等必要观测

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

### 4.4 Target

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

## 5. 硬实时与安全

长期不变量：

```text
Drive Timing Start != Power Enable
```

控制时基可以持续运行，而功率输出由 Motor 状态与保护策略独立门控。

启动顺序遵守：

```text
Configure
  ↓
Bind
  ↓
Arm feedback / events
  ↓
保持 power disabled
  ↓
Start drive timing
```

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
