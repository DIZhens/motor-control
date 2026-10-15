# G431 受控保护注入

当前阶段：离线完成、尚未烧录/实测。仅用于 12 V / 50 mA、无电池、无电机的首板台架。
原有 14 次实物请求属于旧镜像；不得把它们当作本入口的验证结果。

## 实现与边界

target_validation.c 仍拥有单次动作、fresh staging、一次使能和 DWT/TIM6 截止。
protection_probe.c 是 Target 私有测试钩子，仅改变一个现有 DAC 阈值，不修改
Platform 的硬件保护实现、比较器路由或 BKE/BK2E。正常启动没有注入和自动 PWM。

| 命令 | 阈值 | 预期比较器输出位图 | 预期标志 |
| --- | --- | --- | --- |
| 5 BREAK_OVP | DAC1_CH1 → 0 | COMP3，0x4 | BIF，0x80，BRK1 |
| 6 BREAK_OCP_C | DAC3_CH1 → 0 | COMP1，0x1 | B2IF，0x100，BRK2 |
| 7 BREAK_OCP_AB | DAC3_CH2 → 0 | COMP2 与 COMP4，0xA | B2IF，0x100，BRK2 |

三个命令均固定 common_duty=0.5、duration_us=2000，逐次提供 power_key。
沿用健康保护快照、有效 B/C 采集、10～14 V 新鲜母线及两次 fresh staging 的前置条件。
使能后再经过四个 ADC 回调（nominal 200 us）才进行一次注入；总截止仍从接收请求计算。
如果在此之前电流/母线/保护条件失效、stage 失败或收到 SAFE_OFF，就取消动作而不写 DAC。
AB 阈值是共享的，观察到两个比较器翻转加 BRK2 不能证明每条 OR 输入单独连通。

注入前再次检查比较器输入配置、DAC 工作模式、Break 路由、BIE、BKE/BK2E、AOE 关闭、
无旧 Break 标志、全部比较器低、MOE=1，以及阈值未被外部改动。
短暂屏蔽可屏蔽中断后写 DAC=0；在**任何软件 safe-off 之前**记录比较器、TIM1 SR 和 BDTR。
仅当预期比较器全部为高、没有其他比较器为高、预期 Break 标志成立且 MOE=0，
才记为 OBSERVED。随后在恢复中断前无条件 safe-off，硬件不响应也不能继续输出。

轮询有 1700 个 DWT 周期（10 us）和 128 次两道上限；10 us 只限制等待段，
不代表整个临界区或 Gate 关断延迟。实际 consumer/ISR 用时需新镜像上板复核。
DAC 电气时间参考 [STM32G431 数据手册](https://www.st.com/resource/en/datasheet/stm32g431cb.pdf)，
此窗口是台架观察预算，不是保护性能承诺。

## 证据与恢复

g_target_validation_probe 为独立 68 字节序号快照；读取前后 sequence 必须是同一偶数。
请求接收后发布 PREPARED，执行后发布 OBSERVED / REJECTED / NOT_OBSERVED。
必须核对 request_sequence 与本次实际动作相同，不能使用上次残留的成功记录。

核对 bdtr_before 的 MOE=1、comp_before=0、sr_before 无 BIF/B2IF；
再核对 bdtr_after 的 MOE=0、comp_after 与 expected_comp_mask、sr_after 与 expected_break_flag。
这是屏蔽软件关断期间的寄存器证据；还要在示波器上确认实际 Gate 包停止。
elapsed_cycles 是从阈值写入前到本次轮询取样的观察区间，包含 DAC 建立、读取和指令时间，
不能当作 comparator→Gate 传播延迟，也不能资格化 4 A / 17 V 的实际触发精度。

注入后（包括未观察到预期 Break 的情况）保持阈值为 0，held=1。
顺序为：

1. 先确认 MOE=0、六路 MCU 输入 LOW、TIM6 已关、运行计数继续。
2. 保持注入，单独发送 REARM：预期返回 REARM_REJECTED，物理条件和锁存仍在。
3. 发送 CLEAR_INJECTION：只恢复保存的原阈值；不清 Break/sticky cause，不解锁。
4. 条件回到正常后，锁存仍阻止新的 PWM；单独 REARM 成功才退休旧 episode。
5. rearm 后仍保持输出关闭，不自动重启。下一次 PWM 必须是新的明确请求和一次性 key。

CLEAR/REARM 在动作中返回 BUSY；SAFE_OFF 随时可以取消且不会暗中清除注入。
CLEAR 发现阈值被外部改为非零时拒绝覆盖。复位会按正常启动重新配置名义阈值，
丢弃旧命令且保持功率关闭；不能拿复位代替本轮“故障持续时拒绝恢复”的验收。

## 下一轮实物步骤

先关闭 OUTPUT、断开电池/电机，再临时接 ST-Link 3V3，完成新镜像烧录、回读与无母线检查。
烧录前后分别记录镜像身份；新版读取布局必须来自同次 AXF/MAP，不能沿用旧地址或旧控制脚本。
无母线时先确认默认 OBSERVE、MOE=0、计数正常，以及不带 key 的请求不能使能。

随后 OUTPUT 关闭时断开 ST-Link 3V3，以电源供电完成两次只读前置检查。
先只接 CHA：探头尖端 LA、地夹 GND，实体与菜单 X10，DC，5 V/div，100 us/div，
CHA 上升沿约 5 V，单次触发。Gate 波形应为少量 20 kHz 周期后结束；
这组设置用于观察整个包，不用于测尖峰、死区或精确关断延迟。

操作员准备好后回复“开始”；主控先完成只读预检，再宣布 8 秒可中断倒计时，
到时重新检查并只发送一次 BREAK_OVP。结束后立即告知可以放下探头，
再核对故障证据、持续故障拒绝恢复、清除/解锁不自动重启。
OVP 通过后才安排 C 和 AB；任一步证据不完整即停在输出关闭，不自动重试。

## 离线结果

106 个 ARMCC / Unicorn 模型用例通过，包含实际注入模块及缺失 MOE 清除、
缺失标志、错误比较器、AB 少一路、时钟停滞、阈值改动、取消、保持/恢复等反例。
模型 HCC 与 MMIO 效果是替身，不能代替上述实物验收。
Keil 全量 Rebuild：0 Error / 0 Warning。最终 AXF 反汇编核对了
DAC 写入到证据采集之间无软件 MOE 写入，以及调用者在恢复中断前 safe-off。
本机冻结 HEX/AXF/MAP、源码哈希、布局和日志位于
[构建清单](local-evidence/20261015-protection-probe/BUILD_MANIFEST.json)。
