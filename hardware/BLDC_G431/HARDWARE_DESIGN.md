# BLDC_control V1 硬件设计报告

> **文档状态**：Living Document / PCB 制造冻结版  
> **硬件版本**：V1  
> **文档版本**：V0.4  
> **当前阶段**：原理图、PCB 与制造基线已冻结并下单；等待首板 Bring-up  
> **原理图基线**：`SCH_Schematic1_2026-09-09.pdf`  
> **网表基线**：`Netlist_Schematic1_2026-09-09.tel`  
> **PCB 几何审查基线**：`BLDC_PCB_Review_Fact_2026-09-09.html`  
> **最终制造基线**：`Gerber_PCB1_2026-09-09.zip`  
> **MCU**：STM32G431CBT6  
> **Gate Driver**：DRV8300DPWR  
> **最后更新**：2026-09-09

本文档是 BLDC_control V1 的长期硬件主文档，用于记录已经落实到原理图、PCB 或最终制造文件中的硬件事实，以及影响后续 Bring-up / 软件配置的关键约束。详细普通走线、每次小改动和试验日志不在本文逐条维护；首板调试过程建议单独记录到 `COMMISSIONING.md` / `BRINGUP.md`。

> **相位命名说明**：当前工程统一使用 **A / B / C 相**。功率相节点为 `MA / MB / MC`；电流模拟节点为 `IA+ / IB+ / IC+`。旧资料中的 U / V / W 与 A / B / C 一一对应。

> **冻结含义说明**：V0.4 的“冻结”表示 **V1 已下单制造的硬件基线不再继续修改**，不表示功能已经通过首板实测。所有电气性能、保护阈值、采样偏置和通信可靠性仍需 Bring-up 验证。

---

# 0. 版本变更记录

## V0.4 — 2026-09-09 — PCB Manufacturing Freeze / Ordered

相对 V0.3，V1 已从“PCB 设计进行中”进入“制造基线冻结并下单”。本版关键变化如下：

1. **原理图 / 网表更新至 2026-09-09**
   - 基线更新为 `SCH_Schematic1_2026-09-09.pdf` 与 `Netlist_Schematic1_2026-09-09.tel`。
   - 新增 PCB 调试测试点 `TP1 ~ TP12`。
   - 新增功率区独立 GND 排针 `H7`。
2. **测试点冻结**
   - TP1 / TP2 / TP3：IA+ / IB+ / IC+。
   - TP4 / TP5 / TP6 / TP7 / TP8 / TP9：HA / LA / HB / LB / HC / LC，位于 22 Ω Gate 电阻的 **MOSFET 一侧**。
   - TP10 / TP11 / TP12：VREF+ / VBUS / 3V3。
   - H7：功率区 GND，便于示波器 / 万用表接地。
3. **PCB 四层结构与地平面策略冻结**
   - 4 Layer / nominal 1.6 mm。
   - 指定叠层：`JLC04161H-3313`。
   - L2 作为主要 Top 参考 GND；三相桥高 dv/dt 区整体做 L2 局部无铜区，Buck SW 节点另设小范围 keepout。
   - L3 基本保持连续 GND；L4 负责大面积 VBUS 分配并承载部分 Bottom 信号。
4. **三相 Shunt Kelvin 实现完成并审查**
   - K+ 从 Shunt 对应焊盘直接引出到模拟前端。
   - K- 虽与全局 GND 同网，但从 Shunt GND 端独立引到 MCU VINM0，不在 Top 铺铜中提前并入功率地。
   - Kelvin 路径下方保留 L2 GND 参考面。
5. **PCB 细节优化完成**
   - NRST 的 100 nF C3 已移至 MCU NRST 引脚附近。
   - `ADC_VM` 敏感节点走线已缩短。
   - Gate 测试点最终改至 MOSFET Gate 实际节点（HA/LA/HB/LB/HC/LC）。
6. **机械安装冻结**
   - 板框 65 mm × 50 mm，四角约 R6 mm。
   - 4 × M2 安装孔最终制造孔径为 **Ø2.4 mm PTH**，无电气网络，用作 M2 螺丝 / 铜柱的间隙安装孔。
7. **设计检查与制造审查完成**
   - 嘉立创 EDA 最终 DRC：**0 errors**。
   - PCB Review Fact 已用于布局、网络、铺铜和关键回路审查。
   - 最终 Gerber / Drill 已进行制造文件交叉检查。
8. **下单制造配置冻结**
   - 4 层、1.6 mm、TG135。
   - 外层 1 oz，内层 0.5 oz。
   - 指定 `JLC04161H-3313`。
   - 绿色阻焊 / 白色字符。
   - 过孔塞油。
   - ENIG 沉金，1 µin（订单显示 `1u"`）。
   - 最小孔径工艺档 0.3 mm。
   - 无额外阻抗管控。
   - AOI 全测 + 飞针全测。
9. **V1 已提交制造订单**，后续硬件工作转入首板 Bring-up。

## V0.3 — 2026-09-08 — CAN FD Physical Layer Update

1. CAN 收发器由 SN65HVD230DR 更换为 **TCAN3414DR**。
2. 新增 R44 = 10 kΩ，将 TCAN3414DR Pin5 `SHDN` 下拉到 GND。
3. CAN 接口改为 2.54 mm 3Pin：H5 Pin1 CANH / Pin2 CANL / Pin3 GND。
4. 板载 R4 = 120 Ω 终端电阻保持不变。

## V0.2 — 2026-09-07 — PCB Design Stage

1. A/C 相电流采样 MCU / OPAMP 通道互换：
   - A → OPAMP3（PB0 / PB2）
   - B → OPAMP2（PA7 / PA5）
   - C → OPAMP1（PA1 / PA3）
2. 因此：`OPAMP1=IC`、`OPAMP2=IB`、`OPAMP3=IA`。
3. 基础 Injected 同步采样物理相位变为 IB + IC，第三相按 `IA = -(IB + IC)` 重构。
4. COMP 相位归属同步更新：COMP1=C、COMP2=B、COMP4=A。
5. 删除 XT30，输入只保留 VH3.96-2P。
6. 新增 4 个 M2 安装孔。

---

# 1. 版本目标与设计边界

V1 的目标不是高功率密度量产控制器，而是建立一块 **可观察、可保护、可反复实验** 的低压三相 BLDC/PMSM FOC 学习与调试平台。

核心原则：

- 充分利用 STM32G431 的高级定时器、双 ADC、内部 OPAMP、COMP、DAC 等电机控制外设，减少外部模拟器件。
- 三相低边均保留分流电阻，支持基础两相同步采样，并为后续三分流 / 扇区相关采样和诊断保留硬件条件。
- 优先保证 7.4~16 V、约 2 A 级实验范围内的可调试性与保护能力，不以 24 V 或大电流工作为目标。
- PC 上位机、UART / USB / CAN 调试优先于本地显示。
- PCB 优先保证功率回路、Gate loop、Shunt Kelvin、GND reference 与可测性，不追求极限紧凑。

## 1.1 目标电机与设计工况

| 项目 | 设计基线 |
|---|---|
| 电机类型 | 小型三相 BLDC/PMSM（2804 级） |
| 极对数 | 7 对极 |
| 额定电压 | 约 12 V |
| 计划工作母线 | 7.4~16 V |
| 额定电流 | 约 0.5 A |
| V1 首板实验最大电流 | 约 2 A |
| 控制目标 | FOC：电流环 → 速度环 → 位置 / 角度相关实验 |

## 1.2 V1 当前冻结范围

| 类别 | 状态 |
|---|---|
| 原理图拓扑与主要器件 | **冻结并已下单** |
| MCU 硬件引脚资源分配 | **冻结** |
| 三相功率级与电流采样结构 | **冻结** |
| USB / CAN FD / UART / SWD / MT6701 接口 | **冻结** |
| PCB 摆件 / 布线 / Kelvin / GND / Keepout | **冻结并已审查** |
| PCB Stackup / Design Rules / Gerber | **冻结并已下单** |
| 测试点 | **冻结：TP1~TP12 + H7** |
| 机械安装 | **4 × Ø2.4 mm PTH M2 安装孔** |
| Bring-up 参数 / 实测波形 / 校准系数 | **待首板验证** |

---

# 2. 系统硬件架构

| 模块 | 最终 V1 实现 |
|---|---|
| 输入与电源 | VIN → PTC → VBUS；VBUS 供三相桥与 DRV8300；TPS54202 生成 3.3 V |
| 控制核心 | STM32G431CBT6，170 MHz |
| 功率级 | DRV8300DPWR + 6 × MS50N06，三相半桥 |
| 电流采样 | 3 × 50 mΩ 低边 Shunt + STM32G431 内部 OPAMP1/2/3 |
| 硬件保护 | COMP1/2/4 Phase OC → TIM1 BRK2；COMP3 VBUS OV → TIM1 BRK1 |
| 位置反馈 | MT6701，SSI / SPI 单向读取 |
| 通信调试 | USB FS、FDCAN + TCAN3414DR、USART1、SWD |
| 温度监测 | 10 kΩ NTC → ADC2 |
| PCB | 4 Layer / 1.6 mm / JLC04161H-3313 |
| 表面处理 | ENIG 1 µin |
| 机械安装 | 4 × Ø2.4 mm PTH M2 安装孔 |

---

# 3. MCU 与关键资源分配

## 3.1 PWM、通信与通用功能

| 引脚 | 复用 / 模式 | 用途 |
|---|---|---|
| PA8 / PA9 / PA10 | TIM1_CH1/2/3 | A/B/C 高侧 PWM |
| PB13 / PB14 / PB15 | TIM1_CH1N/2N/3N | A/B/C 低侧 PWM |
| PA0 | ADC1_IN1 + COMP3_INP | VBUS 检测 / 过压 |
| PA4 | ADC2_IN17 | 功率区 NTC |
| PA15 / PB3 / PB4 | GPIO CS / SPI1_SCK / SPI1_MISO | MT6701 SSI |
| PA11 / PA12 | USB_DM / USB_DP | USB FS |
| PB11 | GPIO | USB VBUS detect |
| PB8 / PB9 | FDCAN1_RX / TX | CAN |
| PB6 / PB7 | USART1_TX / RX | UART 调试 |
| PA13 / PA14 | SWDIO / SWCLK | SWD |
| PC13 | GPIO Input | 用户按键 |
| PB5 | GPIO Output | 状态 LED |
| PB12 | NC（硬件原理图） | 外部 BKIN 已取消 |

## 3.2 三相电流采样引脚映射

| 相位 | VINP / COMP 输入 | VINM0 | 内部 OPAMP |
|---|---|---|---|
| A 相 | PB0 | PB2 | **OPAMP3** |
| B 相 | PA7 | PA5 | **OPAMP2** |
| C 相 | PA1 | PA3 | **OPAMP1** |

必须统一理解为：

```text
OPAMP1 = IC
OPAMP2 = IB
OPAMP3 = IA
```

## 3.3 启动配置

PB8 同时具有 BOOT0 物理功能。V1 计划通过 Option Bytes：

```text
nSWBOOT0 = 0
nBOOT0   = 1
```

使 PB8 正常作为 FDCAN RX 使用。该项属于首板 Bring-up 配置，不由 PCB 自动保证。

---

# 4. PWM、采样时序与 ADC 架构

| 参数 | 设计值 |
|---|---|
| TIM1 时钟 | 170 MHz |
| PWM | 20 kHz，中心对齐 Mode 1 |
| PSC | 0 |
| ARR | **4250** |
| CH1/2/3 | 三相 PWM1 + 互补输出 |
| MCU Dead-Time | 0；死区由 DRV8300 内部固定 dead-time 提供 |
| 采样触发 | TIM1 CH4，PWM2，CCR4=4240，TRGO2=OC4REF |
| ADC 公共时钟 | 42.5 MHz（170 MHz / 4） |
| ADC1 Injected | VOPAMP1，12.5 cycles，TIM1_TRGO2 |
| ADC2 Injected | VOPAMP2，12.5 cycles，TIM1_TRGO2 |
| ADC1 Regular | VBUS + VREFINT，慢速采样 |
| ADC2 Regular | NTC，慢速采样 |

中心对齐完整周期：

```text
ARR = 170 MHz / (2 × 20 kHz) = 4250
```

## 4.1 基础两相同步采样关系

当前基础 Injected 路径为：

```text
ADC1 → VOPAMP1 → IC
ADC2 → VOPAMP2 → IB
```

因此基础方案为：

```text
IB、IC 同步采样
IA = -(IB + IC)
```

软件若继续沿用旧版“ADC1=IA、ADC2=IB”的变量语义将产生硬件相位错配，必须同步更新。

## 4.2 时序边界

内部 OPAMP 输出直接送 ADC 时，电流 Injected 通道按 12.5 ADC cycles 采样时间设计。

固定采样点在高调制度 / 某些扇区可能失去有效低边采样窗口。后续软件与控制研究可进一步评估：

- 扇区相关动态采样点；
- 三 Shunt 共享相选择；
- OPAMP3 / ADC 路由的动态利用。

---

# 5. 输入电源与 3.3 V 电源

## 5.1 输入与母线

| 项目 | V1 实现 |
|---|---|
| 输入接口 | VH3.96-2P，连接 VIN |
| 输入保护 | BSMD1812-200-30V PTC |
| 母线 TVS | SMF16A |
| 母线储能 | C6 220 µF + 多颗陶瓷 / 局部去耦 |
| 正常母线 | 7.4~16 V |
| 设计边界 | 不作为 24 V 输入控制器使用 |

## 5.2 TPS54202 3.3 V Buck

| 参数 | V1 设计 |
|---|---|
| Buck | TPS54202DDCR |
| 输出 | 3.3 V |
| 电感 | 10 µH |
| 输出电容 | 22 µF × 2 |
| 反馈 | 100 kΩ / 22.1 kΩ |
| 前馈电容 | 56 pF |
| EN / UVLO | 511 kΩ / 100 kΩ 分压 |

PCB 中 Buck 区按参考布局思路实现，并对 SW 节点在 L2 做局部 GND keepout。

## 5.3 VDDA / VREF+

VREF+ 通过 R5 = 0 Ω 与 3.3 V 相连，本地设置：

- C12 = 10 nF
- C15 = 1 µF

电流采样偏置与 NTC 上拉均使用 VREF+。V1 不启用 VREFBUF。

---

# 6. Gate Driver 与三相逆变桥

| 项目 | V1 设计 |
|---|---|
| Gate Driver | DRV8300DPWR |
| 输入 | 6-PWM：INHA/B/C + INLA/B/C |
| GVDD | 直接连接 VBUS |
| GVDD 去耦 | 10 µF + 100 nF，本地放置 |
| Bootstrap | 100 nF × 3，BSTx-SHx |
| MOSFET | MS50N06 × 6，TO-252，60 V |
| Gate 串阻 | 22 Ω × 6 |
| Gate-Source | 10 kΩ × 6 |
| 并联二极管 | 1N4148WS × 6，形成快速关断旁路支路 |

## 6.1 功率桥结构

```text
VBUS
 ├─ QH_A ─ MA ─ QL_A ─ R22 50mΩ ─ GND
 ├─ QH_B ─ MB ─ QL_B ─ R32 50mΩ ─ GND
 └─ QH_C ─ MC ─ QL_C ─ R42 50mΩ ─ GND
```

## 6.2 设计边界

GVDD = VBUS 简化 V1，但 DRV8300 的 Gate Driver 供电瞬态直接受母线影响。V1 正常工作范围仍严格限定为：

```text
7.4~16 V
```

TVS 与 COMP3 过压保护不能等价理解为“可安全接 24 V”。

---

# 7. 三相低边电流采样

## 7.1 外部模拟网络

| 项目 | 设计值 |
|---|---|
| Shunt | 50 mΩ / 3 W / 1% × 3 |
| Shunt+ → VINP | 1.5 kΩ |
| VREF+ → VINP | 43 kΩ |
| VINP → GND | 15 kΩ |
| VINP 高频滤波 | 56 pF |
| Shunt- → VINM0 | Kelvin 负端 |
| 内部 OPAMP | PGA，Gain=16 or -15，High Speed |

## 7.2 相位与 OPAMP 映射

```text
A 相 Shunt → PB0 / PB2 → OPAMP3
B 相 Shunt → PA7 / PA5 → OPAMP2
C 相 Shunt → PA1 / PA3 → OPAMP1
```

## 7.3 理论量程

在 Shunt 负端接近 0 V 的近似条件下：

```text
VINP   ≈ 0.1014 V + 0.8811 × VSHUNT
VOPAMP ≈ 1.623 V + 14.10 × VSHUNT
```

RSHUNT = 50 mΩ 时：

```text
VOPAMP ≈ 1.623 V + 0.7049 × I(A)
```

因此：

- ±2 A 对应 OPAMP 输出约 0.213~3.033 V；
- 12-bit / 3.3 V 下理论电流量化约 1.14 mA/LSB。

## 7.4 PCB Kelvin 实现

V1 PCB 对三相 Kelvin 采用统一原则：

- K+ 从对应 Shunt Pad 独立分叉进入 1.5 kΩ / 43 kΩ / 15 kΩ / 56 pF 模拟前端。
- K- 与全局 GND **同网但不同路径**：从 Shunt GND Pad 独立引至 MCU VINM0，不依赖普通 GND 铺铜作为采样导线。
- Top GND Pour 在 Kelvin- 走廊处避免提前侧向并入该采样路径。
- Kelvin 区下方 **保留 L2 GND reference**，不因 Kelvin- 与 GND 同网而挖掉参考面。
- Shunt 功率 GND 端通过本地 Via Array 接入 GND plane，功率回流与采样路径在几何上分离。

PCB Review 已确认三相 K+ / K- 的实现与相位映射一致。

## 7.5 标定策略

- OPAMP 使用 ST factory trimming。
- ADC 上电执行内部自校准。
- PWM 关闭且确认无相电流时，分别统计 IA / IB / IC 零点偏置。
- 后续使用已知电流做每相增益标定，保存 Ka / Kb / Kc。
- 运行时采用：

```text
I = (ADC - offset) × gain
```

COMP 硬件过流定位为快速保护，不作为精密电流测量基准。

---

# 8. VBUS、温度与硬件保护

## 8.1 VBUS 采样

| 参数 | 值 |
|---|---|
| 分压 | 47 kΩ / 5.1 kΩ |
| 滤波 | 1 nF |
| ADC / COMP | PA0：ADC1_IN1 + COMP3_INP |
| 分压比 | ≈ 0.09789 |
| ADC 满量程对应母线 | ≈ 33.7 V（仅测量量程，不代表允许工作电压） |
| 16 V 时 ADC 节点 | ≈ 1.57 V |

```text
VADC = VBUS × 5.1 / (47 + 5.1)
     ≈ 0.09789 × VBUS

VBUS ≈ VADC × 10.216
```

V0.4 PCB 中 `ADC_VM` 走线已在冻结前缩短，降低高阻模拟 / 比较器输入节点的无必要绕行。

## 8.2 NTC 温度采样

| 项目 | 设计 |
|---|---|
| NTC | NCP18XH103F03RB，10 kΩ @25°C |
| 上拉 | 33 kΩ → VREF+ |
| 滤波 | 100 nF |
| ADC | PA4 / ADC2_IN17 |
| 用途 | 监视功率区 PCB 温度，不等同于 MOS 结温 |

## 8.3 硬件 Break 保护链

| 保护 | 当前硬件路径 |
|---|---|
| **A 相过流** | PB0 / COMP4 + DAC3_CH2 → TIM1 BRK2 |
| **B 相过流** | PA7 / COMP2 + DAC3_CH2 → TIM1 BRK2 |
| **C 相过流** | PA1 / COMP1 + DAC3_CH1 → TIM1 BRK2 |
| VBUS 过压 | PA0 / COMP3 + DAC1_CH1 → TIM1 BRK1 |
| 外部 BKIN | V1 取消；PB12 NC |

建议 Break 后由软件明确确认故障状态再重新使能，不做无条件自动恢复。

---

# 9. 编码器、通信与调试接口

## 9.1 MT6701 SSI

| 项目 | 设计 |
|---|---|
| 接口 | H3：3V3 / CSN / SCK / MISO / GND |
| CS | PA15，R43 = 10 kΩ 上拉到 3.3 V |
| SCK | PB3 / SPI1_SCK |
| DO | PB4 / SPI1_MISO |
| 模式 | SPI1 Master，RX-only |
| ABZ | V1 不作为核心接口 |

## 9.2 USB FS

| 项目 | 设计 |
|---|---|
| 接口 | USB Type-C，设备端 |
| CC1 / CC2 | 各 5.1 kΩ 下拉 |
| D- / D+ | PA11 / PA12 |
| ESD | USBLC6-2SC6 |
| ESD VBUS 本地电容 | 100 nF |
| VBUS Detect | 5.1 kΩ / 10 kΩ → PB11，网络 `USB_DET` |
| 供电关系 | USB +5 V 不给整板 3.3 V 供电，只用于接口 / 检测 |

PCB 中 Type-C → ESD → MCU 的 D+/D- 位于 Top，未使用中途 Via；冻结版两线长度高度匹配。

## 9.3 FDCAN / CAN FD 物理层

STM32G431：

```text
PB9 → FDCAN1_TX
PB8 ← FDCAN1_RX
```

外部收发器采用 **TCAN3414DR**：

| Pin | 当前连接 | 说明 |
|---|---|---|
| 1 TXD | CAN_TX / PB9 | MCU → Transceiver |
| 2 GND | GND | Ground |
| 3 VCC | 3V3 | 3.3 V |
| 4 RXD | CAN_RX / PB8 | Transceiver → MCU |
| 5 SHDN | R44 = 10 kΩ → GND | 固定不进入 Shutdown |
| 6 CANL | CANL | Bus L |
| 7 CANH | CANH | Bus H |
| 8 STB | GND | 固定 Normal Mode |

H5：

```text
Pin1 = CANH
Pin2 = CANL
Pin3 = GND
```

板载 R4 = 120 Ω 跨接 CANH / CANL。多节点使用时，仅总线两端应保留终端，是否焊接 R4 由系统拓扑决定。

## 9.4 UART / SWD / Reset

| 接口 | 实现 |
|---|---|
| UART | H2：USART1 PB6 TX / PB7 RX / GND |
| SWD | H1：3V3 / SWCLK / GND / SWDIO / NRST |
| Reset | 10 kΩ 上拉 + 100 nF + Reset 按键 |

PCB 冻结前已将 NRST 的 C3 = 100 nF 移到 MCU NRST 引脚附近，以提高局部抗干扰能力。

## 9.5 PCB 测试点

| 位号 | 网络 | 用途 |
|---|---|---|
| TP1 | IA+ | A 相电流模拟节点 |
| TP2 | IB+ | B 相电流模拟节点 |
| TP3 | IC+ | C 相电流模拟节点 |
| TP4 | HA | A 高侧 MOS Gate（Rg MOS 侧） |
| TP5 | LA | A 低侧 MOS Gate（Rg MOS 侧） |
| TP6 | HB | B 高侧 MOS Gate（Rg MOS 侧） |
| TP7 | LB | B 低侧 MOS Gate（Rg MOS 侧） |
| TP8 | HC | C 高侧 MOS Gate（Rg MOS 侧） |
| TP9 | LC | C 低侧 MOS Gate（Rg MOS 侧） |
| TP10 | VREF+ | ADC / OPAMP 参考电源 |
| TP11 | VBUS | 母线测试 |
| TP12 | 3V3 | 逻辑电源测试 |
| H7 | GND | 功率区示波器 / 万用表地 |

> **高侧 VGS 测量注意**：TP4 / TP6 / TP8 不能以普通接地示波器的 GND 夹直接参考 MA / MB / MC。高侧 VGS 应使用差分 / 隔离测量方式，测量 `VG - VS`。

---

# 10. PCB 最终实现与布局设计

## 10.1 PCB 基本参数与物理 Stackup

订单与 Layer Manager 对应的 V1 叠层为：

```text
L1  Top Copper       ≈ 35.0 µm   (1 oz)
    Dielectric1      ≈ 0.09939 mm, 3313 RC57% 4.2mil
L2  Inner1 Copper    ≈ 15.19 µm  (0.5 oz nominal)
    Core             ≈ 1.264996 mm
L3  Inner2 Copper    ≈ 15.19 µm  (0.5 oz nominal)
    Dielectric3      ≈ 0.09939 mm, 3313 RC57% 4.2mil
L4  Bottom Copper    ≈ 35.0 µm   (1 oz)
```

制造订单：

- 4 层；
- 成品板厚 1.6 mm；
- TG135；
- 指定 `JLC04161H-3313`；
- 外层 1 oz、内层 0.5 oz。

该结构使 L1↔L2 与 L3↔L4 的参考面距离均约 0.1 mm，Top / Bottom 信号分别可获得较紧密的 GND reference。

## 10.2 Layer 功能分配

| Layer | V1 主要职责 |
|---|---|
| L1 Top | 主器件、三相桥、Shunt、MCU 模拟与数字信号、USB / CAN、大部分布线 |
| L2 Inner1 | 主要 Top GND reference；三相桥区域整体 keepout；Buck SW 局部 keepout |
| L3 Inner2 | 基本连续 GND reference，特别服务 Bottom 信号与 L4 VBUS plane pair |
| L4 Bottom | 大面积 VBUS 分配、部分 Gate / SH / 辅助信号；剩余区域按实际铜形实现 |

## 10.3 Floorplan

V1 形成清晰的功能分区：

- 上部：MCU、USB、CAN、编码器、UART / SWD、按键 / LED；
- 中部：DRV8300、Bootstrap、三组电流模拟前端；
- 下部：三相 MOSFET 功率桥、Shunt、Motor Phase Connector；
- 右侧：输入电源与 TPS54202 Buck；
- Bottom 功率区附近：NTC。

PCB 机械尺寸为 **65 mm × 50 mm**，四角约 **R6 mm**。

## 10.4 三相功率桥与母线配电

- Q1~Q6 在下部集中排列，构成 A/B/C 三个半桥。
- MA / MB / MC 为高 dv/dt 相节点，Top 使用较宽功率铜连接至 H4。
- L4 使用大面积 VBUS 铜分配至三相高侧功率区；高侧 MOS VBUS 铜通过多 Via 连接至 Bottom VBUS。
- L3 基本连续 GND，与 L4 VBUS 形成紧耦合的 plane pair，降低母线分配阻抗。
- Shunt GND 端使用本地 Via Array 接 GND planes。

## 10.5 Gate / SH / Bootstrap

- C25 / C26 / C27 紧靠 DRV8300 的 BSTx / SHx 回路。
- Gate 串阻、快速二极管、Gate-Source 10 kΩ 贴近对应 MOS 区域。
- 部分较长 Gate / SH 路径放在 Bottom 并保持相邻走向，Bottom 由 L3 GND 提供参考。
- TP4~TP9 接在 22 Ω Gate resistor 的 MOSFET 一侧，用于观察 MOS Gate 实际波形。

V1 不追求三相 Gate 总长度严格一致；首板应通过实际 VGS 波形确认 ringing、overshoot、negative VGS 与 Miller 误导通情况。

## 10.6 Shunt / Kelvin

三组 Shunt 与模拟前端是 V1 PCB 的关键布局成果：

- R22 = A，R32 = B，R42 = C。
- K+ 从 Shunt Pad 独立引出，不从功率走线中途取样。
- K- 电气上仍属于 GND，但几何上独立引到对应 MCU VINM0。
- Top GND Pour 不在 Kelvin- 中途侧向吞并采样路径。
- L2 在 Kelvin / current-sense 区保持连续 GND reference。
- 三相桥 L2 keepout 的上边界位于 Shunt 功率 GND 区下方，使 Shunt / Kelvin 区仍有近参考面。

## 10.7 GND Plane 与 Switch-Node Keepout

### L2

V1 不是把三相 MA / MB / MC 分别挖成狭窄槽，而是将三相桥区域作为一个整体高 dv/dt 功率区：

- 上半板及模拟 / 数字区保持大面积连续 GND；
- 三相 MOS / Phase 区下方整体去除 L2 GND；
- Shunt、Kelvin、current-sense 区继续保留 L2 GND；
- Buck SW 下方另做小范围局部 keepout。

目的：降低 L1 大面积 Phase copper 与仅约 0.1 mm 间距的 L2 GND 之间寄生电容，同时避免将其余数字 / 模拟参考面无意义切碎。

### L3

L3 基本保持连续 GND，不因 Bottom Gate / SH 路由进行大面积开槽；Bottom 信号仍能获得连续参考，L4 VBUS 也与 L3 形成紧耦合 plane pair。

## 10.8 模拟与敏感信号

- IA+ / IB+ / IC+ 主要在 Top 路由，并远离大面积 Phase / Gate 高 dv/dt 区。
- `ADC_VM` 在 PCB 冻结前缩短，降低高阻模拟 / COMP 输入的耦合面积。
- VREF+ 使用本地去耦并提供 TP10。
- NRST 的 100 nF C3 贴近 U4 NRST。
- 8 MHz Crystal 及负载电容保持在 MCU 附近。

## 10.9 USB / CAN Layout

### USB

- Type-C → USBLC6-2 → MCU 顺序明确。
- D+ / D- 均在 Top，无中途 Via。
- ESD GND 连接短。
- V1 不申请额外阻抗管控；短距离 USB FS 路径按当前固定 Stackup 使用。

### CAN

- TCAN3414DR 靠近 H5 CAN 接口。
- CANH / CANL 位于 Top，短距离连接并远离三相高 dv/dt 区。
- 板载 120 Ω R4 位于接口区域。

## 10.10 测试可达性

V1 专门增加 12 个 SMD 测试点与一个功率区 GND 排针，以支持首板 Bring-up：

- 三路 current-sense；
- 六路实际 MOS Gate；
- VREF+ / VBUS / 3V3；
- 功率区 GND。

数字区不额外增加专用 GND TP，SWD / UART / Encoder / CAN 等现有排针均提供 GND 可用于普通低频调试。

## 10.11 Mechanical

- Board outline：65 mm × 50 mm，四角约 R6 mm。
- SCREW1~SCREW4：4 × M2 mounting hole。
- 最终 Gerber Drill：**Ø2.4 mm PTH × 4**。
- 四个安装孔不属于任何电气 Net；Ø2.4 mm 用于 M2 螺丝穿孔并配合铜柱 / 螺母固定。
- 元件重叠、丝印、接口方向和 3D 机械干涉由嘉立创 EDA 视图在下单前人工确认。

## 10.12 Design Rules / DRC

最终设计使用的主要规则（1 oz 默认规则）包括：

| 项目 | 规则 |
|---|---|
| Track 最小线宽 | 0.127 mm |
| Track 默认线宽 | 0.254 mm |
| Track-Track 最小间距 | ≈ 0.102 mm |
| 常见 Track-Pad / Via 间距 | ≈ 0.152 mm |
| Copper Zone clearance | 0.254 mm |
| 常见 Copper-to-Board Edge | ≈ 0.300 mm |
| Via 默认外径 / 钻孔 | ≈ 0.610 / 0.305 mm |
| Via 最小外径 / 钻孔 | ≈ 0.500 / 0.300 mm |
| Differential Pair 默认线宽 / 间距 | 0.254 / 0.1524 mm |
| Differential Pair 长度容差 | 0.254 mm |

最终 EDA DRC：

```text
0 errors
```

DRC=0 仅表示当前设计规则下未检测到违规，不等价于功能正确或 EMC / 动态电气行为已经验证。

## 10.13 PCB 几何事实与制造事实

### PCB Review Fact

`BLDC_PCB_Review_Fact_2026-09-09.html` 用于记录 PCB 设计阶段的：

- Components；
- Pads / Net；
- Tracks / Vias；
- Layers；
- Pour / Poured copper；
- Board outline。

该文件来源于 2026-09-08T16:39:06.047Z 的 FULL_BOARD Snapshot，SHA-256：

```text
4eb00af0656fc853a639b3ba5021b194c5c7e0d88afb78d57e56421bc8c6dcac
```

其 coverage 按 Review Skill 定义为 `incomplete`，不重建阻焊、Pad drill / 完整机械本体，也不代替 DRC 或制造数据。

**重要**：PCB Review Fact 生成后，四个 M2 安装孔又从原 Ø2.0 mm 修改为最终 Ø2.4 mm。因此安装孔最终尺寸必须以最新 Gerber / Drill 为准。

### Final Gerber

最终制造基线：

```text
Gerber_PCB1_2026-09-09.zip
```

其中包括：

- Top / Bottom Copper；
- Inner1 / Inner2；
- Top / Bottom Solder Mask；
- Top / Bottom Silkscreen；
- Top / Bottom Paste；
- Board Outline；
- PTH / NPTH / Via Drill；
- Drill Drawing；
- FlyingProbeTesting.json。

最终制造文件是“工厂实际生产什么”的最高优先级几何事实。

## 10.14 下单制造配置

| 项目 | 已下单配置 |
|---|---|
| 层数 | 4 Layer |
| 成品板厚 | 1.6 mm |
| 板材 | TG135 |
| 外层铜 | 1 oz |
| 内层铜 | 0.5 oz |
| 层压 | 指定层压顺序 |
| Stackup | JLC04161H-3313 |
| 阻抗管控 | 无额外要求 |
| 阻焊 | 绿色 |
| 字符 | 白色 |
| 过孔 | 塞油 |
| 表面处理 | ENIG 沉金 |
| 沉金厚度 | 1 µin（订单 `1u"`） |
| 最小孔径工艺档 | 0.3 mm |
| 金手指斜边 | 不需要 |
| 线路测试 | AOI 全测 + 飞针全测 |

---

# 11. 关键器件清单

| 位号 | 型号 / 参数 | 用途 |
|---|---|---|
| U4 | STM32G431CBT6 | MCU |
| U5 | DRV8300DPWR | 三相 Gate Driver |
| Q1~Q6 | MS50N06 | 60 V N-MOS，TO-252 |
| U3 | TPS54202DDCR | VBUS → 3.3 V Buck |
| U6 | TCAN3414DR | 3.3 V CAN FD Transceiver |
| D2 | USBLC6-2SC6 | USB ESD |
| D1 | SMF16A | VBUS TVS |
| F1 | BSMD1812-200-30V | 输入 PTC |
| R22 / R32 / R42 | 50 mΩ / 3 W / 1% | 三相低边 Shunt |
| R41 | NCP18XH103F03RB | 10 kΩ NTC |
| R44 | 10 kΩ | TCAN3414 SHDN 下拉 |
| X1 | 8 MHz Crystal | HSE |
| SCREW1~4 | Ø2.4 mm PTH / M2 | PCB 机械安装 |
| TP1~TP12 | SMD Test Point | 电流 / Gate / 电源调试 |
| H7 | 1Pin 2.54 mm Header | 功率区 GND 调试点 |

---

# 12. 当前已接受的局限与风险

| 项目 | 说明 |
|---|---|
| GVDD = VBUS | 结构简单，但 DRV8300 直接承受母线瞬态；V1 严格按 7.4~16 V 使用 |
| TVS 钳位窗口 | SMF16A 用于母线瞬态抑制，不能解释为对 DRV8300 绝对最大值的严格保证 |
| 基础两相采样 | 当前基础方案采样 IB / IC 并重构 IA；高调制度 / 某些扇区可能需要动态采样 / 共享相方案 |
| 低边 Shunt | 仍存在 MOS Source 抬升与地弹敏感性；V1 已通过 Kelvin PCB 实现降低该风险，但需首板实测验证 |
| 固定 CAN 终端 | R4 = 120 Ω；多节点系统仅总线两端应保留终端，是否焊接按系统拓扑管理 |
| 无 DRV nFAULT | DRV8300DPWR 不提供完整可读故障状态，诊断依赖 MCU COMP / ADC / TIM Break 与软件日志 |
| 相位映射非直觉 | OPAMP1=C、OPAMP2=B、OPAMP3=A；软件 / CubeMX / 日志必须保持一致 |
| PCB 尚未实测 | DRC / Gerber review 通过不代表功能已验证；电源、VGS、current-sense、Break、通信仍需 Bring-up |

---

# 13. HW / CubeMX / 软件同步清单

以下是硬件事实导致的软件 / MCU 配置要求，不等同于硬件缺陷。

| 项目 | V1 硬件事实 / 目标 | 软件 / CubeMX 动作 |
|---|---|---|
| A/C 电流采样映射 | OPAMP1=C、OPAMP2=B、OPAMP3=A | 更新 IA/IB/IC 变量语义、注释和相位映射 |
| ADC Injected 物理相位 | ADC1/VOPAMP1=IC；ADC2/VOPAMP2=IB | 基础重构使用 `IA = -(IB + IC)` |
| COMP 相位映射 | COMP1=C、COMP2=B、COMP4=A | 故障报告 / 日志使用正确相位标签 |
| PB12 | NC，不使用外部 BKIN | CubeMX 设为未使用 / Analog |
| ADC2 NTC sampling | 慢速 NTC | 使用长采样时间；项目目标 247.5 cycles |
| FDCAN bit timing | PB8/PB9 + TCAN3414DR | 按实际总线长度 / 节点冻结仲裁段与数据段速率 |
| USB HSI48 / CRS | USB 硬件已具备 | USB Bring-up 时确认 HSI48 / CRS 同步方案 |
| PB8 / BOOT0 | PB8 作为 CAN_RX | Bring-up 设置 `nSWBOOT0=0, nBOOT0=1` |
| Gate TP 命名 | TP4~TP9 为 HA/LA/HB/LB/HC/LC | 调试脚本 / 波形记录按 MOS-side Gate 节点命名 |

---

# 14. 硬件冻结状态与下一阶段

## 14.1 当前状态

```text
Schematic              Frozen for V1
PCB Layout              Frozen
PCB DRC                 0 errors
Stackup / Rules         Frozen
Final Gerber / Drill    Reviewed
Manufacturing Order     Submitted
Assembly / Bring-up     Pending
```

## 14.2 当前权威设计文件

V1 已下单基线至少包括：

```text
HARDWARE_DESIGN.md                      # 本文档（V0.4）
SCH_Schematic1_2026-09-09.pdf          # 电气图形基线
Netlist_Schematic1_2026-09-09.tel      # 电气连接基线
BLDC_PCB_Review_Fact_2026-09-09.html   # PCB 设计几何 / Net 审查事实（安装孔修改前快照）
Config_DesignRule_PCB1_2026-09-08.json # PCB Design Rules
Config_LayerManager_2026-09-08.json    # 物理 Stackup / Layer 定义
Gerber_PCB1_2026-09-09.zip             # 最终制造基线，含最终 Ø2.4 mm M2 孔
```

## 14.3 下一阶段：首板 Bring-up

本文档不再继续记录日常试验过程。板子到手后建议建立 / 更新 `COMMISSIONING.md`，按可复现方式记录：

1. 外观、短路与不上电检查；
2. VIN / 3V3 / VREF+ / GVDD；
3. SWD / MCU 基础运行；
4. PWM 与六路 VGS；
5. 三路 current-sense offset / gain；
6. OCP / OVP / TIM1 Break；
7. MT6701；
8. UART / USB / CAN；
9. 低压限流下三相桥与电机运行；
10. FOC current loop → speed loop → position / angle experiments。

只有当 Bring-up 发现 **硬件设计事实发生变化**（例如需飞线、改阻值、器件替换、V2 设计决策）时，再更新 `HARDWARE_DESIGN.md`。

---

# 附录 A. 关键公式与换算

| 项目 | 公式 |
|---|---|
| 中心对齐 PWM | `ARR = 170 MHz / (2 × 20 kHz) = 4250` |
| VBUS 分压 | `VADC = VBUS × 5.1 / (47 + 5.1) ≈ 0.09789 × VBUS` |
| VBUS 反算 | `VBUS ≈ VADC × 10.216` |
| 电流理论传递 | `VOPAMP ≈ 1.623 + 0.7049 × I(A)`（K- 接近 0 V 的近似） |
| 电流理论量化 | `≈ 1.14 mA / ADC LSB` |
| 基础三相重构 | `IA = -(IB + IC)` |

---

# 附录 B. A/B/C 相位与网络速查

| Phase | PWM Input | MOS Gate（MOS-side） | Phase Node | Shunt | OPAMP | COMP |
|---|---|---|---|---|---|---|
| A | INHA / INLA | HA / LA | MA | R22 | OPAMP3 | COMP4 |
| B | INHB / INLB | HB / LB | MB | R32 | OPAMP2 | COMP2 |
| C | INHC / INLC | HC / LC | MC | R42 | OPAMP1 | COMP1 |

电流 Injected 基础关系：

```text
ADC1 → OPAMP1 → IC
ADC2 → OPAMP2 → IB
IA = -(IB + IC)
```

---

# 附录 C. 事实来源与冲突优先级

不同文件回答不同层次的“事实”。发生冲突时按以下原则处理：

1. **最终制造几何：`Gerber_PCB1_2026-09-09.zip`**
   - 决定工厂实际生产的铜、孔、阻焊、丝印、板框。
2. **电气连接：最新 Netlist + Schematic**
   - `Netlist_Schematic1_2026-09-09.tel`
   - `SCH_Schematic1_2026-09-09.pdf`
3. **PCB 设计几何 / Net 语义：PCB Review Fact**
   - `BLDC_PCB_Review_Fact_2026-09-09.html`
   - 注意该快照生成后 M2 孔径又修改为 Ø2.4 mm，因此该机械细节由 Final Gerber 覆盖。
4. **物理 Stackup / Rules**
   - `Config_LayerManager_2026-09-08.json`
   - `Config_DesignRule_PCB1_2026-09-08.json`
5. **设计说明与决策：本文档**
   - `HARDWARE_DESIGN.md`

本文档不应凌驾于更低层的原始工程 / 制造数据；若发现不一致，应修正文档。

---

# 附录 D. 文档维护原则

- 本 Markdown 文件作为 Git 中硬件设计的长期主文档。
- 原理图 / PCB 修改只在达到明确设计变更或里程碑时更新本文档，不要求每个小编辑都同步。
- PCB 章节记录关键布局决策与原因，不记录所有普通走线细节。
- 首板 Bring-up 的测试步骤、条件、波形、阈值和失败记录优先放在 `COMMISSIONING.md` / `BRINGUP.md`。
- 软件参数与硬件事实分开：FDCAN 波特率、OCP DAC code、校准系数等属于可配置项，不写成不可变硬件事实。
- Git commit history 保存详细修改历史；本文只保留影响设计理解的关键版本变化。
- 当旧报告、聊天结论与最新原理图 / 网表 / Gerber 冲突时，以最新工程文件和最终制造文件为准，并显式更新本文。

