# ODRIVE_V36_405 Hardware Baseline

本文档是用户手中第三方 ODrive V3.6 兼容板在当前软件工程中的硬件事实入口。它只保留会长期影响 Firmware、Bring-up、接口与安全边界的事实。

详细来源、证据冲突与电路说明见同目录 `HARDWARE_DESIGN.md`。本板不是 ODrive 官方 V3.6 原版硬件，禁止把 ODrive 官方 DRV8301 / 并联 MOS 功率级参数直接套用到本板。

## 1. Board Identity

```text
Board family       第三方 ODrive V3.6 兼容板 / loop222 驱动板405
PCB silkscreen     V3.6-56V
MCU                STM32F405RGT6，LQFP64
External clock     8 MHz
Axes               2 × 3-phase motor axis: M0 / M1
Gate driver        2 × EG2134（商家原理图标注，实物料号仍以芯片丝印为准）
Power stage        M0: 6 × NMOS；M1: 6 × NMOS
Aux stage          EG2132 + 2 × NMOS half bridge
Current shunt      2 × 1 mΩ low-side per motor axis
Current gain       20 V/V
Current bias       1.65 V nominal
CAN PHY            TJA1051T/3（商家原理图标注）
USB                USB FS over USB Type-C
```

MCU 顶部丝印已由用户实物确认：`STM32F405RGT6`。

## 2. Supply and Voltage Boundary

商家资料同时存在三种表述：

```text
设计耐压       60 V
开箱测试范围   DC 12–56 V
详细说明推荐   8–36 V / 建议控制在 36 V 内
```

当前工程安全基线采用：

```text
Conservative operating boundary: <= 36 V
```

56 V 只记录为商家资料中的声称范围，不作为未经验证的安全工作点。功率 MOS 的精确料号、母线瞬态裕量、散热能力与连续电流能力尚未完成实物验证。

商家原理图中的电源树为：

```text
DCBUS
  ↓
XL7005A-class buck → ~12.6 V
  ↓
RY8411-class buck  → 5 V
  ↓
AMS1117-class LDO  → VCC / ~3.3 V
```

以上器件型号来自商家原理图，实物替代料以板上丝印为准。

当前台架接线下，实测 DC 输入正极金属脚到背面功率母线电容正端存在压差，
负极回路在本轮测量分辨率下未见明显压差。具体压降器件或连接处尚未核实，
不据此认定板上具有正常防反接保护。
母线观测应对照内部 DCBUS 的实际电压，不能按外部输入电压直接添加固定补偿。

## 3. PWM and Power Stage

### M0

```text
PA8   → M0_AH
PA9   → M0_BH
PA10  → M0_CH
PB13  → M0_AL
PB14  → M0_BL
PB15  → M0_CL
```

对应 STM32F405 TIM1 三相互补输出体系。

### M1

```text
PC6  → M1_AH
PC7  → M1_BH
PC8  → M1_CH
PA7  → M1_AL
PB0  → M1_BL
PB1  → M1_CL
```

对应 STM32F405 TIM8 三相互补输出体系。

每个电机轴由 1 颗 EG2134 驱动 6 颗 NMOS，三相每个开关位置只有 1 颗 MOS。商家板与 ODrive 官方 DRV8301 + 并联 MOS 功率级不同。

Gate 电阻在商家原理图中为 22 Ω。

当前没有确认独立的 `EN_GATE` 硬件门控；安全关断不能直接沿用官方 DRV8301 的 `EN_GATE / nFAULT` 语义。

## 4. Current Sensing

每个电机轴采用两分流低边电流采样。

以 M0 为例：

```text
B low-side → 1 mΩ shunt → current amplifier → M0_SO1
C low-side → 1 mΩ shunt → current amplifier → M0_SO2
A low-side → direct PGND, no dedicated shunt
```

M1 结构相同。

模拟前端：

```text
Shunt resistance   1 mΩ
Amplifier gain     20 V/V
Nominal scale      20 mV/A
Bias reference     ~1.65 V
Output RC          100 Ω + 2.2 nF
```

1.65 V 由 10 kΩ / 10 kΩ 分压后经缓冲得到。

MCU ADC 映射：

```text
PC0 / ADC10 → M0_SO1
PC1 / ADC11 → M0_SO2
PC2 / ADC12 → M1_SO2
PC3 / ADC13 → M1_SO1
```

商家声明最大采样电流 60 A；该数值当前只视为商家规格，不视为已经验证的连续/峰值安全电流。

用户已验证商家提供的 `6双电机控制_F4/6_DoubleMotor_2804_AS5600` 示例工程可以在本板上驱动电机运行。该事实确认了至少一条 M0 PWM → Gate Driver → 三相桥 → 电机及 AS5600 位置反馈链路可工作；不能据此单独宣称电流比例、同步采样与电流闭环已经完成精度验证。

## 5. Bus and Thermal Observation

母线检测：

```text
DCBUS → 39 kΩ / 2.2 kΩ divider → VBUS_S → PA6 / ADC6
```

商家单排 MOS 原理图中，上臂为 R5（39 kΩ），下臂为 R12（2.2 kΩ），
C3（100 nF）并在 R12 两端；R12 下端接 GND。器件标称值仍需与实物区分。

理想分压比例约为：

```text
(39 kΩ + 2.2 kΩ) / 2.2 kΩ ≈ 18.73
```

温度通道：

```text
M0_TEMP   → PC5 / ADC15
M1_TEMP   → PA4 / ADC4
AUX_TEMP  → PA5 / ADC5
```

商家原理图为 10 kΩ NTC + 3.3 kΩ 下拉/偏置网络；热敏器件实际安装位置与 Beta 值尚未实测确认。

## 6. Position Interfaces

板上提供两组 M0 / M1 编码器接口。

正交 / ABZ 基础映射：

```text
M0 ENC A → PB4
M0 ENC B → PB5
M0 ENC Z → PC9

M1 ENC A → PB6
M1 ENC B → PB7
M1 ENC Z → PC15
```

商家还复用这些接口支持 AS5600：

```text
M0 A/B → SCL0/SDA0
M1 A/B → SCL1/SDA1
```

SPI 使用：

```text
PC10 → SPI3_SCK
PC11 → SPI3_MISO
PC12 → SPI3_MOSI
GPIO1 / PA0 → M0-class CS
GPIO2 / PA1 → M1-class CS
```

商家资料列出的可用编码器包括 AS5600、AS5047P、TLE5012B、MA730、MT6701 与 ABZ 类接口；具体协议与电气兼容性应按实际固件和传感器逐项确认。

## 7. Communication and Debug

### UART

商家原理图明确：

```text
GPIO3 = PA2 / USART2_TX
GPIO4 = PA3 / USART2_RX
```

外接 USB-TTL：

```text
GPIO3 / TX → USB-TTL RX
GPIO4 / RX → USB-TTL TX
GND        → GND
```

商家不同说明文档曾出现 TX/RX 文字方向互换；当前以原理图的 MCU 复用功能为准。

### CAN

```text
PB8  → CAN1_RX
PB9  → CAN1_TX
PHY  → TJA1051T/3
```

板上带 120 Ω CAN 终端电阻和切换开关。

### USB

```text
PA11 → USB_DM
PA12 → USB_DP
Connector → USB Type-C
```

### SWD

```text
PA13 → SWDIO
PA14 → SWCLK
NRST available
```

## 8. AUX / Brake Half Bridge

AUX 为独立半桥：

```text
PB11 → AUX_H
PB10 → AUX_L
EG2132 → 2 × NMOS
```

商家使用说明将其称为 ODrive 刹车接口。当前尚未验证制动电阻值、允许功率、控制极性和过压联动策略，因此不能把 AUX 视为已经完成的安全制动能力。

## 9. Hardware Protection Baseline

与官方 ODrive V3.x 的 DRV8301 方案不同，当前商家原理图未显示：

```text
DRV8301 nFAULT / nOCTW
独立 EN_GATE
板级相电流 Comparator → Timer Break
明确的 CPU-independent overcurrent trip
```

因此在没有芯片数据手册、实物测量和故障注入验证前：

```text
Hardware Trip capability = NOT CONFIRMED
```

EG2134 / EG2132 自身可能包含内部保护功能，但其具体保护类型、阈值、锁存方式和是否能满足本项目 Hardware Trip 语义，当前均不得假定。

## 10. Verified Baseline

当前已有的实物验证事实：

```text
Seller SimpleFOC example on this board          PASS
M0 motor can run                                PASS
AS5600-based position path used successfully    PASS
```

目前未形成工程级验证的项目：

```text
Current-sense gain / offset accuracy
PWM-to-ADC synchronized sampling timing
Current closed loop
M1 axis
CAN
USB
Official-style ODrive runtime behavior
AUX brake stage
Thermal calibration
Hardware overcurrent trip
36 V upper-bound operation
High-current thermal capability
```

## 11. Source of Truth

针对这块第三方实物板，优先级固定为：

```text
用户手中的实际 PCB / 实测结果
    ↓
与实物布局匹配的商家“单排 MOS”原理图
    ↓
已在该板运行验证的商家源码
    ↓
商家“驱动板405”使用/升级说明
    ↓
用户提供的 ODrive V3.6 参考原理图
    ↓
ODrive 官方 V3.x 历史硬件资料
```

后两项仅用于理解 ODrive 设计来源和差异，不得覆盖这块实物板的硬件事实。
