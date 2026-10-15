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

截至 2026-10-15，当前首板完成本轮修复后的无电机基础故障初筛。
供电为 UTP1306-II 的 12 V / 50 mA 限流，无电池、无电机；ST-Link 的 3V3 供电线断开。
静态用户读数为 VBUS 约 12.1 V、3V3 约 3.33 V、输入约 16 mA / CV，30 s 观察稳定。

| 项目 | 当前结论与证据范围 |
| --- | --- |
| 启动、构建及镜像 | SWD、Option Bytes、烧录/回读核对通过；本轮主动验证镜像 B5357289…，构建 0 Error / 0 Warning、63 个 ARM 模型用例通过 |
| 原低阻与异常发热 | 更换 DRV8300DPWR 和三颗自举电容后未复现；最后一轮用户报告电源/DRV 无异常。最初原因未能区分芯片损伤与拆焊消除连锡等可能 |
| 六路静态 VGS | 用户万用表报告均约 0 V；三相对 GND 的上电静态高电位不能单独判定 MOS 导通 |
| LA/LB/LC 低侧基本波形 | 20 kHz / 共同 50% 基本电平与周期检查通过；LA 照片，LB/LC 用户人工复核 |
| MA/MB/MC 相端基本波形 | 无负载周期切换、频率与占空比初筛通过；MA 照片，MB/MC 用户人工复核 |
| 高侧 A/B/C 稳定平台 | Gate/GND 与 Source/GND 双通道同一时刻的平台差粗查通过；A/C 照片，B 用户人工复核，导通 VGS 约 10～11 V、关断差接近 0 V |
| 结束后的软件/寄存器证据 | 共 14 次独立短脉冲；各次成功完成后核对 MOE=0、六路输入 LOW、测试计时停止、key 消费、保护无故障及无重放。计数不是 Gate 精确关断延迟测量 |
| 采样与逻辑保护诊断 | 无功率连续采样、软件截止/ADC IRQ 丢失及 Trip 通知/恢复诊断有独立通过记录，不替代真实带功率保护验收 |

原始阶段记录及每轮证据说明见 [首板实物调试记录](../../firmware/targets/BLDC_G431/COMMISSIONING.md)；
当前操作步骤与状态表见 [台架验证](../../firmware/targets/BLDC_G431/VALIDATION.md)。
HARDWARE_BASELINE 保留长期事实与当前边界，不继续逐条堆积操作流水。

以下能力仍未完成实机资格化：

- 实际瞬态峰值和精确动态 VGS / 死区；MA 负向尖峰尚未分离真实电路与鳄鱼夹地线引入的分量。
- 不同 duty、相间时序及带负载 PWM。
- 电流采样 offset / gain / 温漂校准；已有零点候选和噪声统计不作为校准完成。
- 实际 OCP / OVP 阈值与 TIM1 Break 带功率关断。
- MT6701 物理时序 / 位置精度、UART / USB / FDCAN、Motor operation 与 FOC。

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
