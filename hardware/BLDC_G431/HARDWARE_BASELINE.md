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

同日将 Hardware Trip 接入普通 Bring-up：bind → arm protection → start drive timing；callback 仅请求 rescan，故障事实来自 authoritative snapshot。正式 4 A OCP / 17 V OVP 配置下，全量构建为 0 Error / 0 Warning，烧录校验通过；连续 5 次重启均 arm 成功且无虚假 trip。约 22 秒无断点回归中，Current=20.00 kHz、invalid=0，Position/Bus=1 kHz，SSI CRC 抽样 25/25 通过；snapshot 有效、condition_active=false、causes=NONE，459333 次软件 MOE 检查未发现使能。供电条件仍为 ST-Link 3V3、无电池、无电机，未调用 Power Enable。

持续故障验证发现 active Break flag 无法清除会导致重复中断、阻塞后台 rescan。Target 已改为首次 Trip 后暂停 BIE 软件通知，保持 comparator 与 BKE/BK2E 硬件保护；显式 rearm 成功才恢复通知。约 2.3 秒持续 OVP 实测中通知次数不再增长，Current 保持约 20 kHz，SysTick 与 rescan 持续前进；BIE 暂停期间叠加 OCP，snapshot 仍保留两个 cause。6 次 active rearm 拒绝、5 次条件消失后恢复、新 episode boundary 竞争测试均通过，28 次观测 current invalid=0、SSI CRC 有效、MOE=0。一次性 rearm 验证入口已移除；普通固件重新全量构建、烧录校验通过，5 次重启与约 22 秒采集回归通过，456573 次软件 MOE 检查未发现使能。

此前进行 3S 电池供电、无电机、MOE=0 的静态检查：SWD 回读六路 MCU 驱动输入均低、保护 snapshot 无故障，Current/Position/Bus 采集正常；用户报告 VBUS 实测 12.04 V，3V3/VREF+ 和六路静态 VGS 符合预期。母线 ADC 约 11.9 V，该单点对照不构成测量校准。U5（DRV8300DPWR）随后被确认烫手；用户补充首次焊接后直接接电池时，该区域曾疑似短暂冒烟，来源未确认。故功率域静态验收未通过，停止带功率验证。

断电排查发现原 B/C 自举支路为几 Ω、A 为 MΩ 级；C26 离板高阻，原焊盘仍低阻。移除 U5 和 C25–C27 后三路空焊盘均恢复高阻（用户报告至少 100 MΩ）；旧 U5 相关引脚粗测为 MΩ 级，密脚接触可靠性未确认。U5 移除状态下，六路 Gate–Source 均约 10 kΩ；三相高侧/低侧 Drain–Source 二极管档正向约 0.45 V、反向超量程，未见明显硬短路，该筛查不构成高压漏电或开关性能验收。已更换备用 DRV8300DPWR 并复装三颗自举电容，用户报告三路 BST–SH 在板阻值均约 40 MΩ，原低阻未复现；用户更正复装后的断电读数：VBUS 对 GND 约 46 kΩ 后上升变慢，47 kΩ / 5.1 kΩ 分压电阻在板分别约 42 kΩ / 4 kΩ；这与分压及其他并联支路的量级相符，未见母线硬短路，不据此判定电阻精度或功率域验收通过。尚不能确定原异常来自芯片内部损伤还是拆焊消除的连锡；新芯片未上电验证，尚无温度或支路电流实测。未执行主动 PWM 或带功率 Break 测试。

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
