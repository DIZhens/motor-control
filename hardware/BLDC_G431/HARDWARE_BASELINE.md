# BLDC_G431 Hardware Baseline

本文档是 `BLDC_G431` 在当前软件工程中的硬件事实入口。它只保留会长期影响 Firmware、Bring-up、接口和安全边界的硬件事实。

详细原理图、PCB、制造与设计说明见同目录 `HARDWARE_DESIGN.md` 及原始设计文件；发生冲突时，以最新制造文件、原理图/网表和实物验证为准。

配套 [2804 电机与 MT6701 编码器资料](../actuators/2804-mt6701/README.md) 独立保存用户提供的商家参数、装配图及编码器板资料。其标称参数不属于本板实测或已完成的 FOC 标定；本页既有验证范围不因此改变。

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
CH1/2/3        physical phase C/B/A complementary PWM
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

物理引脚映射以原始网表为准，不按定时器通道顺序推定相名：

| 物理相 | MCU 高侧 / 低侧引脚 | LQFP48 管脚号 | 定时器通道 |
| --- | --- | --- | --- |
| A | PA10 / PB15 | U4.32 / U4.29 | CH3 / CH3N |
| B | PA9 / PB14 | U4.31 / U4.28 | CH2 / CH2N |
| C | PA8 / PB13 | U4.30 / U4.27 | CH1 / CH1N |

网表 `INHA/INLA` 接 U4.32/29，`INHB/INLB` 接 U4.31/28，`INHC/INLC` 接 U4.30/27。
旧版“CH1/2/3=A/B/C”说明有误；Platform 须将物理 A/B/C duty 分别写入 CCR3/2/1。
该修正不改变下一节的 B/C 电流采集物理通道；旧镜像的方向与电角度候选不能直接照搬。

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

截至 2026-10-18，当前首板完成修复后的无电机基础故障初筛，并补充保护注入及不同 duty 的检查。
以下功率波形与静态电源读数属于 2026-10-15 的验证条件。
供电为 UTP1306-II 的 12 V / 50 mA 限流，无电池、无电机；ST-Link 的 3V3 供电线断开。
静态用户读数为 VBUS 约 12.1 V、3V3 约 3.33 V、输入约 16 mA / CV，30 s 观察稳定。

| 项目 | 当前结论与证据范围 |
| --- | --- |
| 启动、构建及镜像 | SWD、Option Bytes、烧录/回读核对通过；本轮主动验证镜像 B5357289…，构建 0 Error / 0 Warning、63 个 ARM 模型用例通过 |
| 保护候选逻辑阶段 | 2026-10-17 Flash 已匹配 29AB2861…（29720 字节），本次未烧录；OUTPUT 关闭、ST-Link 3V3 供电，软件复位后 stage-only、4096 点采集及无 key 的 3/5/6/7 拒绝通过，使能尝试为 0。106 项 ARM 模型与 27 项主机工具检查分别记录，不替代带功率保护实测 |
| 保护候选 12 V 静态 | 用户报告 12 V 供电无异常；未再复位，直接读回启动健康、母线观测约 11.96～12.04 V、采样/Trip 正常、六路 LOW/MOE=0、使能尝试为 0。精确输入电流/CV 未另报；单次控制工具 59 项主机检查及只读预检通过，尚未注入 |
| OVP 注入关断及恢复 | 第 15/16/17 次均取得 COMP3/BRK1 与软件关断前 MOE 1→0 的寄存器证据；第 16 次 LA 照片确认包结束后低电平。用户确认断电后第 17 次专门补恢复，同一启动中故障持续拒绝恢复、清注入保留锁存、锁存拒绝 stage、显式 rearm 四步通过；最终保护健康且六路 LOW/MOE=0，无自动重启。本次引用旧波形并单列新恢复证据，不资格化 Gate 延迟或实际 17 V 阈值 |
| C 相 OCP 注入与恢复 | 第 18 次的 COMP1/BRK2 寄存器关断证据、LA 有限包结束照片及同一连续启动的四步恢复通过；该次结束时保护健康、六路 LOW/MOE=0，使能保持为 1，无自动重启。电源/DRV 未另附人工状态报告；实际 4 A 门限与 Gate 延迟未资格化 |
| 共享 A/B OCP 注入与恢复 | 第 19 次 COMP2/4 同时高、BRK2 与软件 safe-off 前 MOE 1→0 的寄存器证据通过；LA 波形为用户人工复核与前次一致，同轮四步恢复通过。最终保护健康、六路 LOW/MOE=0、采样正常，无自动重启；不单独资格化每条 OR 路由 |
| 30%/70% LA 基础响应 | 通过；LA 照片均为 20.0 kHz、峰值 12.2 V，30%/70% 的正脉宽分别 34.8/14.8 us，符合低侧互补预期。各次 COMPLETE、一次 enable、结束六路 LOW/MOE=0、保护健康且采样继续；30% 包结束人工确认，70% 后态由第 24 次人工复核。稳定照片和后态的请求来源分开记录，不资格化精确瞬态或其他相的不同 duty 波形 |
| 原低阻与异常发热 | 更换 DRV8300DPWR 和三颗自举电容后未复现；最后一轮用户报告电源/DRV 无异常。最初原因未能区分芯片损伤与拆焊消除连锡等可能 |
| 70% AUTO 后态观察 | 第 24 次后用户确认 OUTPUT 一直开启、LA 始终约 0 V，后态检查通过；最后只读复核仍为该次完成计数、六路 LOW/MOE=0、保护健康、采样继续且无新增使能。未保留短包末尾边沿或精确包宽 |
| 六路静态 VGS | 用户万用表报告均约 0 V；三相对 GND 的上电静态高电位不能单独判定 MOS 导通 |
| LA/LB/LC 低侧基本波形 | 20 kHz / 共同 50% 基本电平与周期检查通过；LA 照片，LB/LC 用户人工复核 |
| MA/MB/MC 相端基本波形 | 无负载周期切换、频率与占空比初筛通过；MA 照片，MB/MC 用户人工复核 |
| 高侧 A/B/C 稳定平台 | Gate/GND 与 Source/GND 双通道同一时刻的平台差粗查通过；A/C 照片，B 用户人工复核，导通 VGS 约 10～11 V、关断差接近 0 V |
| 结束后的软件/寄存器证据 | 共 14 次独立短脉冲；各次成功完成后核对 MOE=0、六路输入 LOW、测试计时停止、key 消费、保护无故障及无重放。计数不是 Gate 精确关断延迟测量 |
| 采样与逻辑保护诊断 | 无功率连续采样、软件截止/ADC IRQ 丢失及 Trip 通知/恢复诊断有独立通过记录，不替代真实带功率保护验收 |

以上为首板基础硬件检查的历史范围，后续固件能力与当前限制见 [台架验证](../../firmware/targets/BLDC_G431/validation/README.md)，阶段结论保存在 Git 检查点；原始逐轮资料只留本地。
HARDWARE_BASELINE 保留长期事实与当前边界，不继续逐条堆积操作流水。

以下能力仍未完成实机资格化：

- 实际瞬态峰值和精确动态 VGS / 死区；MA 负向尖峰尚未分离真实电路与鳄鱼夹地线引入的分量。
- 不同 duty、相间时序及带负载 PWM。
- 电流采样 offset / gain / 温漂校准；已有零点候选和噪声统计不作为校准完成。
- 实际 OCP / OVP 阈值、精确 Gate 关断延迟及独立 A/B OR 输入路由；OVP/BRK1、C 相和共享 A/B 的 BRK2 注入关断与恢复已有独立通过记录。
- 保护候选首次连接未取得有效运行状态，软件复位后正常；本轮 12 V 上电直接读取正常，初始异常原因仍未确定。
- MT6701 独立位置精度、USB、外部 CAN 物理总线及持续/带载控制能力；后续已有 UART 和有限 FOC 功能记录，不等于这些项目验收。

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
