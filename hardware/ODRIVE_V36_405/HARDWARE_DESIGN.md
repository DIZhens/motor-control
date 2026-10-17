# ODRIVE_V36_405 硬件设计与证据整理

> **文档状态**：Living Document / 实物逆向基线  
> **硬件对象**：用户手中第三方 ODrive V3.6 兼容板  
> **商家资料名称**：驱动板405 / SimpleFOC 驱动板  
> **PCB 丝印**：V3.6-56V  
> **资料来源**：商家单排 MOS 原理图、使用说明、SimpleFOC 开箱测试、升级说明、示例源码、ODrive 固件二进制、用户实物照片  
> **最后更新**：2026-10-05

本文档记录这块具体实物板的硬件设计事实、证据来源和不确定项。它不是 ODrive 官方 V3.6 的硬件说明。

---

# 0. 证据分级

本板资料来自第三方商家，且不同文件存在版本差异，因此使用以下分级：

```text
A  实物 + 原理图/源码/运行结果可以交叉确认
B  商家原理图明确，且与实物布局一致
C  商家说明书/标称参数，但尚未实测
R  ODrive 官方或“官方版”参考资料，仅作设计来源对照
```

发生冲突时，不用参考 ODrive 原版资料覆盖本板事实。

---

# 1. 与官方 ODrive V3.x 的关系

这块板沿用了 ODrive V3.x 的 STM32F405 引脚组织和双轴控制思路，但重新实现了功率级。

本板：

```text
STM32F405
  ↓
M0: EG2134 → 6 × NMOS
M1: EG2134 → 6 × NMOS
AUX: EG2132 → 2 × NMOS
```

用户提供的 ODrive V3.6 参考原理图则是典型：

```text
STM32F405
  ↓
DRV8301
  ↓
每个开关位置并联 MOS
```

因此以下官方 ODrive 假设不能直接沿用：

- DRV8301 SPI 配置；
- `EN_GATE`；
- `nFAULT / nOCTW`；
- DRV8301 内部电流放大器增益；
- 官方板 0.5 mΩ Shunt；
- 官方并联 MOS 的电流/热能力；
- 官方 Hardware Fault 处理链。

商家提供的 ODrive 固件文件名包含 `v0.5.1_trs_1mR` 与 `v0.5.6_trs_1mR`。这证明商家使用的是修改版 ODrive 固件映像；只有二进制，没有对应修改源码，因此不能审计其所有硬件适配项。文件名中的 `1mR` 与本板 1 mΩ Shunt 一致，但不把文件名本身当作更深层软件修改的证据。

---

# 2. MCU 与资源映射

商家原理图 U2 为 STM32F405，LQFP64，引脚组织与 ODrive V3.x 接近。

## 2.1 Motor PWM

| Function | MCU pin | Timer-capable mapping |
|---|---|---|
| M0_AH | PA8 | TIM1_CH1 |
| M0_BH | PA9 | TIM1_CH2 |
| M0_CH | PA10 | TIM1_CH3 |
| M0_AL | PB13 | TIM1_CH1N |
| M0_BL | PB14 | TIM1_CH2N |
| M0_CL | PB15 | TIM1_CH3N |
| M1_AH | PC6 | TIM8_CH1 |
| M1_BH | PC7 | TIM8_CH2 |
| M1_CH | PC8 | TIM8_CH3 |
| M1_AL | PA7 | TIM8_CH1N |
| M1_BL | PB0 | TIM8_CH2N |
| M1_CL | PB1 | TIM8_CH3N |

M0 / M1 均具有由高级定时器生成三相互补 PWM 的硬件条件。

商家 SimpleFOC 示例中的具体 PWM 频率、中心对齐方式与 dead-time 属于该固件配置，不是 PCB 固定事实。

## 2.2 Analog Inputs

| Signal | MCU pin | Meaning |
|---|---|---|
| M0_SO1 | PC0 / ADC10 | M0 current sense 1 |
| M0_SO2 | PC1 / ADC11 | M0 current sense 2 |
| M1_SO2 | PC2 / ADC12 | M1 current sense 2 |
| M1_SO1 | PC3 / ADC13 | M1 current sense 1 |
| M0_TEMP | PC5 / ADC15 | M0 NTC |
| M1_TEMP | PA4 / ADC4 | M1 NTC |
| AUX_TEMP | PA5 / ADC5 | AUX NTC |
| VBUS_S | PA6 / ADC6 | bus voltage |

## 2.3 Position / Expansion

| Signal | MCU pin |
|---|---|
| GPIO1 | PA0 |
| GPIO2 | PA1 |
| GPIO3 / USART2_TX | PA2 |
| GPIO4 / USART2_RX | PA3 |
| M0_ENC_A | PB4 |
| M0_ENC_B | PB5 |
| M0_ENC_Z | PC9 |
| M1_ENC_A | PB6 |
| M1_ENC_B | PB7 |
| M1_ENC_Z | PC15 |
| SPI3_SCK | PC10 |
| SPI3_MISO | PC11 |
| SPI3_MOSI | PC12 |
| CAN1_RX | PB8 |
| CAN1_TX | PB9 |
| SWDIO | PA13 |
| SWCLK | PA14 |

AUX：

```text
PB11 → AUX_H
PB10 → AUX_L
```

---

# 3. Power Tree

商家单排 MOS 原理图显示：

```text
DCBUS
  │
  ├─ motor bridges
  │
  └─ XL7005A-class
       ↓ ~12.6 V
       EG2134 / EG2132 gate-driver supply
       ↓
       RY8411-class buck
       ↓ 5 V
       AMS1117-class
       ↓ VCC / ~3.3 V
```

关键器件值：

```text
XL7005A stage:
  R7  = 2.2 kΩ
  R8  = 20 kΩ
  L1  = 100 µH
  Vout annotation ≈ 12.6 V

5 V stage:
  R13 = 51 kΩ
  R14 = 10 kΩ
  L2  = 4.7 µH
```

母线电容阵列由多颗 100 µF 电容和 100 V 小电容并联构成。

商家原理图对部分芯片明确写有“型号仅供参考，以实物为准”，因此电源芯片名称记录为 schematic BOM fact，不作为不可变化的实物料号。

---

# 4. Motor Power Stage

M0 与 M1 结构基本镜像。

## 4.1 Gate Driver

每轴 1 × EG2134：

```text
HIN1 / HIN2 / HIN3
LIN1 / LIN2 / LIN3
        ↓
HO1/2/3 + LO1/2/3
        ↓
3-phase bridge
```

供电为 12 V 级。

每个 Gate 串联 22 Ω 电阻。

本原理图没有独立 `EN_GATE`、`nFAULT` 或 `nOCTW` 网络。

## 4.2 MOS Arrangement

每轴：

```text
3 high-side NMOS
3 low-side NMOS
= 6 MOS / motor axis
```

实物正面两轴共 12 颗主功率 MOS，与原理图一致。

商家说明书给出“60 V / 150 A MOS”字样，但没有提供明确 P/N，也没有给出散热条件；该数字只保留为 Vendor Claim。

---

# 5. Current Sense

## 5.1 Topology

每个轴只在两个低边支路放置 Shunt。

M0：

```text
phase A low-side → PGND directly

phase B low-side
  ↓
R35 = R001 ≈ 1 mΩ
  ↓
PGND

phase C low-side
  ↓
R36 = R001 ≈ 1 mΩ
  ↓
PGND
```

M1 的 R45 / R46 结构相同。

因此本板属于：

```text
2-shunt low-side current sensing
```

不是三 Shunt，也不是官方 ODrive 0.5 mΩ 版本。

## 5.2 Amplifier

每轴有双运放电流前端。

单通道结构：

```text
SP → 1 kΩ → op-amp input
SN → 1 kΩ → op-amp feedback network
feedback = 20 kΩ
bias = 1V65
output → 100 Ω → SOx
                 ↓
                2.2 nF
```

名义增益：

```text
20 kΩ / 1 kΩ = 20
```

Shunt：

```text
1 mΩ × 20 = 20 mV/A
```

1V65 由 10 kΩ / 10 kΩ 分压并经 U13 buffer 得到。

商家说明书同样给出：

```text
Current sense mode: Low-side
Shunt:             1 mΩ
Gain:              20
```

两份证据一致。

---

# 6. VBUS and Thermal Sensing

## 6.1 VBUS

```text
DCBUS
 ↓ R5 / 39 kΩ
VBUS_S
 ↓ R12 / 2.2 kΩ
GND
```

滤波电容为 C3（100 nF），并在 R12 两端。以上为商家单排 MOS 原理图标称值，
不等于实物阻值已完成测量。该图未在 DCBUS 到分压上臂之间画出串联二极管。

```text
VBUS_S → PA6 / ADC6
```

理想比例约 18.73:1。

## 6.2 NTC

M0、M1、AUX 各有一条温度网络：

```text
VCC → NTC10k → TEMP node → 3.3 kΩ → GND
                       ↓
                     2.2 µF
```

具体 NTC Beta、安装热耦合位置和温度换算尚未实物标定。

---

# 7. Encoder Interfaces

## 7.1 ABZ

板上有独立 M0 / M1 编码器排针。

```text
M0: PB4 / PB5 / PC9
M1: PB6 / PB7 / PC15
```

各 A/B/Z 信号在原理图中有 3.3 kΩ 上拉。

## 7.2 AS5600 / I2C-style Use

商家 SimpleFOC 资料定义：

```text
M0 A → SCL0
M0 B → SDA0

M1 A → SCL1
M1 B → SDA1
```

这是商家固件对 AB 引脚的复用方式，不表示 STM32 硬件 I2C 外设一定被使用；具体实现以对应源码为准。

## 7.3 SPI

共享 SPI3：

```text
PC10 SCK
PC11 MISO
PC12 MOSI
```

商家资料使用：

```text
GPIO1 / PA0 → M0 CS
GPIO2 / PA1 → M1 CS
```

示例资料列出 AS5047P、TLE5012B、MT6701 等磁编码器。

---

# 8. Communication

## 8.1 UART conflict resolution

两份商家说明文字存在冲突：

- 《SimpleFOC 开箱测试》一处写 GPIO3=RXD、GPIO4=TXD；
- 《驱动板405使用说明》写 GPIO3=TXD2、GPIO4=RXD2。

商家原理图 MCU 复用功能明确：

```text
PA2 / USART2_TX → GPIO3
PA3 / USART2_RX → GPIO4
```

因此工程硬件事实采用：

```text
GPIO3 = TX
GPIO4 = RX
```

外接 USB-TTL 时交叉连接。

## 8.2 CAN

商家原理图：

```text
PB8  CAN1_RX
PB9  CAN1_TX
     ↓
TJA1051T/3
     ↓
CANH / CANL
```

CANH / CANL 间有 120 Ω 电阻，并通过 SW1 控制终端接入。

## 8.3 USB

USB Type-C：

```text
PA11 USB_DM
PA12 USB_DP
```

Type-C CC1 / CC2 使用 5.1 kΩ 下拉。

## 8.4 SWD

顶部排针提供：

```text
3V3
GND
SWDIO
SWCLK
NRST
```

商家说明在普通 ST-Link 场景下强调 SWDIO、SWCLK、GND；隔离调试器还需要电压参考。

---

# 9. AUX / Brake Stage

AUX 电路为 1 个半桥：

```text
DCBUS
 ↓
Q7 high-side
 ↓
AUX switching node → terminal
 ↓
Q8 low-side
 ↓
PGND
```

Gate Driver 为 EG2132。

AUX 接口的正端连接 Q7 Source / Q8 Drain 的半桥中点，负端连接 PGND。
主电源应接板边 DC 接口，不从 AUX 输入。若电源正端误接 AUX 正端，
可经 Q7 从 Source 到 Drain 的体二极管给 DCBUS 充电和供电，形成一段结压降；
不把这条误供电路径视为主输入的保护设计。

```text
PB11 AUX_H
PB10 AUX_L
```

两路 MCU 输入端有 3.3 kΩ 下拉。

商家资料将其称为“ODrive 刹车接口”。目前只确认硬件半桥存在，不确认：

- brake resistor 额定阻值；
- 连续/峰值耗散功率；
- 过压阈值；
- PWM 极性；
- Fail-safe behavior；
- 软件自动制动策略。

---

# 10. Safety and Protection Assessment

这是本板和官方 ODrive 设计差异最大、也最不能凭名称推断的区域。

当前商家原理图没有看到：

```text
independent current comparator
TIM Break hardware path
DRV8301 nFAULT
DRV8301 nOCTW
independent gate-enable input
```

因此，在未来若为此板建立 Firmware Target，不能提前宣称满足当前架构中的 `Hardware Trip` capability。

需要至少补做：

1. 查清实物 EG2134 / EG2132 的准确型号与 datasheet；
2. 确认其 UVLO、shoot-through prevention、过流/过温等内部保护；
3. 确认保护发生后是否能在 CPU 失效时独立 safe-off；
4. 确认保护条件是否可观察、是否锁存、如何 re-arm；
5. 通过示波器 / 故障注入验证最终 Gate 输出状态。

在上述工作完成前，本板的软件安全只能按“Hardware Trip 未确认”处理。

---

# 11. Vendor Claims vs Engineering Baseline

| Item | Vendor material | Current engineering interpretation |
|---|---|---|
| Board voltage | 12–56 V / design 60 V | <=36 V conservative until verified |
| Maximum sampled current | 60 A | vendor claim only |
| MOS rating | 60 V / 150 A | exact P/N unknown |
| Current sensing | low-side, 1 mΩ, gain 20 | confirmed by schematic |
| Encoder support | AS5600 / AS5047P / TLE5012B / MA730 / MT6701 / ABZ | physical interfaces exist; protocol success must be verified per firmware |
| ODrive compatibility | modified v0.5.1 / v0.5.6 binaries provided | firmware is board-specific modified build, not proof of official hardware equivalence |

---

# 12. Existing Run Evidence

用户已经在本板上运行过商家提供的：

```text
5_Isample_M0_F4_2804_AS5600
```

并确认电机能够运行。

该验证支持：

```text
STM32F405
  ↓
M0 PWM
  ↓
EG2134
  ↓
M0 3-phase bridge
  ↓
motor

AS5600
  ↓
M0 encoder pins
  ↓
MCU position path
```

它不自动验证：

```text
current amplitude accuracy
current closed-loop stability
M1
CAN / USB
AUX brake
Hardware Trip
high-current / high-voltage thermal behavior
```

商家 2023-04-05 升级说明还记录：其软件曾针对 ADC DMA 连续采样错位问题改为单次转换 + DMA 中断重启，并同步更新电流采样、双电机与力矩反馈代码。该信息作为历史软件证据保留，不把具体 ADC 调度策略写成硬件不变量。

---

# 13. Open Items

后续如果真正启用此板作为 Firmware Target，优先确认：

```text
1. MCU 完整丝印 / Flash 容量
2. EG2134 / EG2132 实物丝印和 datasheet
3. Power MOSFET 精确 P/N
4. 12 V / 5 V / 3.3 V 实测
5. M0/M1 current-sense offset / polarity / gain
6. PWM → ADC synchronized sampling timing
7. Safe-off behavior
8. Hardware protection capability
9. AUX brake behavior
10. 36 V 以上是否存在足够瞬态裕量
```

这些未确认项不阻止把本板作为第二块硬件参考板，但在确认前不得进入公共 Hardware Capability Contract 的“已支持能力”。

---

# 14. Source Priority

```text
Physical board + measurement
        ↓
Seller single-row-MOS schematic
        ↓
Seller source verified on this board
        ↓
Seller usage / upgrade documents
        ↓
User-provided ODrive V3.6 reference schematic
        ↓
Official historical ODrive V3.x hardware
```

本优先级专门用于这块实物板；官方 ODrive 资料用于对照设计思想，不用于覆盖第三方改板的真实电路。
