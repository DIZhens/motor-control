# BLDC_G431 Hardware Baseline

本文档是 `BLDC_G431` 在当前软件工程中的硬件事实入口。它只保留会长期影响 Firmware、Bring-up、接口和安全边界的硬件事实。

详细原理图、PCB、制造与设计说明见同目录 `HARDWARE_DESIGN.md` 及原始设计文件；发生冲突时，以最新制造文件、原理图/网表和实物验证为准。

## 1. Board Identity

```text
Board          BLDC_control V1
MCU            STM32G431CBT6
Package        LQFP48
MCU clock      170 MHz
Gate driver    DRV8300DPWR
Power stage    6 × MS50N06, 3-phase half bridge
Nominal bus    7.4–16 V
Current shunt  3 × 50 mΩ low-side
Encoder        MT6701 over SSI/SPI
CAN PHY        TCAN3414DR
```

V1 是低压 BLDC/PMSM 控制与实验板，不按 24 V 或高功率量产控制器使用。

## 2. PWM and Timing

TIM1 负责三相互补 PWM：

```text
TIM1 clock     170 MHz
PWM            20 kHz center-aligned mode 1
PSC            0
ARR            4250
CH1/2/3        phase A/B/C complementary PWM
CH4            sampling trigger, PWM2
CCR4           4240
TRGO2          OC4REF
```

当前 `.ioc` 中 TIM1 dead-time 配置为 0；V1 死区由 DRV8300 内部固定 dead-time 提供。

长期约束：

```text
Drive Timing Start != Power Enable
```

PWM 时基和功率输出使能必须分开管理。

## 3. Current Sensing

三相均使用低边 50 mΩ Shunt + STM32G431 内部 OPAMP。

硬件相位映射：

```text
Phase A → PB0 / PB2 → OPAMP3
Phase B → PA7 / PA5 → OPAMP2
Phase C → PA1 / PA3 → OPAMP1
```

必须统一理解：

```text
OPAMP1 = IC
OPAMP2 = IB
OPAMP3 = IA
```

当前基础 Injected 同步采样路径：

```text
ADC1 → VOPAMP1 → IC
ADC2 → VOPAMP2 → IB

IA = -(IB + IC)
```

因此 Firmware 不得沿用“ADC1=IA、ADC2=IB”的旧变量语义。

OPAMP 当前配置：

```text
PGA gain     16
Power mode   High Speed
```

Injected sampling time 当前为 12.5 ADC cycles，由 TIM1 TRGO2 触发。

固定采样点在高调制度或部分扇区可能失去有效低边采样窗口；这属于后续控制与采样策略设计问题，不得把固定采样点当成所有工况都有效的硬件保证。

## 4. Hardware Protection

当前硬件快速保护路径：

```text
A phase OC   COMP4 → TIM1 BRK2
B phase OC   COMP2 → TIM1 BRK2
C phase OC   COMP1 → TIM1 BRK2
VBUS OV      COMP3 → TIM1 BRK1
```

外部 BKIN 在 V1 硬件中取消，PB12 不作为外部保护输入使用。

Hardware Trip 属于 CPU-independent 快速保护能力。软件 Fault、Clear Fault、Protection Re-arm 与 Restart 必须保持独立语义。

## 5. Position and Communication Interfaces

```text
MT6701
    PA15  CS
    PB3   SPI1_SCK
    PB4   SPI1_MISO

USART1
    PB6   TX
    PB7   RX

USB FS
    PA11  DM
    PA12  DP

FDCAN1
    PB8   RX / BOOT0 shared pin
    PB9   TX

SWD
    PA13  SWDIO
    PA14  SWCLK
    NRST available on H1
```

TCAN3414DR 固定为正常工作模式，板上 CANH/CANL 间有 120 Ω 终端电阻；多节点时仅总线两端应保留终端。

## 6. BOOT0 Option Bytes

PB8 同时是 `BOOT0` 物理引脚和 `FDCAN1_RX`。V1 PCB 没有为 BOOT0 设置独立外部下拉，因此每颗新 MCU / 每块新板首次启用时必须确认：

```text
nSWBOOT0 = 0
nBOOT0   = 1
```

含义：

```text
BOOT0 不再取自 PB8 物理电平
        ↓
由 Option Byte 固定正常 Main Flash 启动
        ↓
PB8 可稳定作为 FDCAN1_RX 使用
```

该配置属于 MCU 非易失 Option Bytes：

- CubeMX 重新生成代码不会改变它；
- 普通 Keil Build / Download 不会改变它；
- 只有显式 Option Bytes 操作、特殊恢复流程或程序主动修改时才可能变化；
- 新焊接 MCU 不继承旧板设置，必须重新确认。

当前首板已通过 STM32CubeProgrammer / CLI 回读确认：

```text
nSWBOOT0 = 0
nBOOT0   = 1
RDP      未修改
```

## 7. Current Bring-up Baseline

当前已经验证：

```text
SWD connection                 PASS
Option Bytes readback          PASS
nSWBOOT0=0, nBOOT0=1          PASS
Keil full Rebuild ×2           0 Error / 0 Warning
Flash program                  PASS
Flash verify                   PASS
```

以上只证明 MCU 工程、SWD、启动配置和下载链路可用，不代表以下能力已经完成实机验证：

```text
3-phase PWM waveform
Gate VGS
Current-sense offset / gain
OCP / OVP / TIM1 Break
MT6701
UART / USB / FDCAN
Motor operation
FOC
```

后续验证必须按实际修改影响边界推进，不因为 Target 已能下载就提前宣称功率级或控制链路通过。

## 8. Source of Truth

优先级：

```text
最终 Gerber / Drill
    ↓
最新 Schematic + Netlist
    ↓
当前 *.ioc（MCU 静态配置）
    ↓
HARDWARE_DESIGN.md
    ↓
本文档的摘要
```

本文档用于 Firmware 快速读取硬件约束，不替代原始工程与制造文件。
