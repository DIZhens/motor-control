# control

可移植电机控制算法区域。

只负责“给定输入、参数和算法历史状态，如何计算输出”，不拥有 Motor 生命周期、通信、HAL、板级资源或系统 Fault 策略。

第一阶段不要预建大量子目录。真正出现 FOC、PI、变换、调制、估计等实现后，再按职责自然拆分。

目前实现 `position_estimator.h/.c`：输入单圈机械位置 `[0, 1)` 与相邻观测采集间隔
`interval_s`，按最近周期差输出机械速度 rpm，可选一阶低通。算法不依赖 HAL、HCC、
Motor 或通信协议；调用方拥有实例，负责在同一执行边界串行更新，并判断观测是否新鲜。
重复读取同一观测时不得重复调用估计器，也不能用调用时间代替采集时间。

`max_velocity_rpm` 是最近周期差能够成立的观测排歧约束，必须有限且大于零，
不等于电机允许运行速度或保护阈值。每次采集间隔必须严格小于
`30 / max_velocity_rpm` 秒；即使两个位置相同，超过该间隔也不能排除中途转过整圈。
实际速度违反这一假设时，任何仅靠周期位置的算法都不能保证识别混叠。

`update()` 返回本次是否得到有效速度。首个合法位置只建立位置基线；第二个合法位置
开始产生有效速度，首个速度直接建立滤波基线。随后低通系数为 `dt / (tau + dt)`，
`velocity_filter_time_s == 0` 时直接使用差分速度。非法位置、非法 dt、半圈歧义、
混叠风险或估计速度越界均清空历史并将速度置零、标记无效；恢复需要两个连续合法位置。
无效零值不能解释为电机静止。`reset()` 清历史并保留配置；非法 `init()` 配置使实例未配置。

在仓库根目录执行 `.venv\Scripts\python.exe control\tests\verify_position_estimator.py`，
使用 ARMCC 编译正式 C 后由 Unicorn 执行，覆盖跨圈、变采样间隔、滤波和失效重接。
该验证证明算法行为，不代替实机编码器采集、时序或速度精度验收。

## 相电流零偏与重构

`current_measurement.h/.c` 消费名义物理相电流，支持固定 AB、AC、BC 或 ABC 实测集合。
Platform 继续负责 ADC 到名义 A 的换算；零偏修正、缺相重构和 Clarke 属于本模块。
调用方拥有实例，并负责串行调用、采样连续性和真实零电流条件。

生命周期为 WAITING → CALIBRATING → READY；必须显式确认零电流才可开始有限采样窗口。
各实测相窗口均值成为 offset，峰峰值和均值绝对值分别受装配配置限制。窗口中失去确认、
坏输入或越限会锁住 FAILED，保留首因与已有窗口诊断，显式 reset 后才允许重试。
仅关闭 PWM 不足以证明零电流，调用方还需确保静止且无外部注入。

READY 后 offset 冻结；`process()` 不重新学习。启用增益修正时，各实测相先计算
`signed_gain*(nominal-offset)`；系数可为负，表示已确认的方向修正。禁用时按 1 处理，
忽略附加系数；未实测相的系数不参与检查。零偏窗口与门限始终使用名义 A。
数值配置合法不证明物理增益或相电流方向已验收。双相按三相和为零重构缺相，
ABC 保持实测值、不消除零序。幅值不变 Clarke 约定为
`alpha=(2A-B-C)/3`、`beta=(B-C)/sqrt(3)`，与
[TI Clarke 实现](https://software-dl.ti.com/processor-industrial-sw/esd/motor_control_sdk/am261x/2025_00_00_06/docs/api_guide_am261x/clarke_8h_source.html)
的坐标约定一致。无效输出清除 valid，不能把数值零当作有效反馈。
零偏就绪不证明增益、相电流正方向、电角度或 FOC 已标定；本模块不计算 Id/Iq。

执行 `.venv\Scripts\python.exe control\tests\verify_current_measurement.py`，
使用 ARMCC / Unicorn 验证真实 C，包括四种相集合的 4096 点均值参考、随机变换、
失败锁定、冻结偏置和数值异常。不连接硬件，也不代替实物电流对照。

## FOC 变换与电流 PI

`foc_math.h/.c` 提供 Park、逆 Park、电压矢量圆限幅和居中 SVPWM。
α 对准 A 相，正电角度从 α 向 β，d 轴沿电角度、q 轴正向超前 90 度；
变换输入由调用方提供同拍 sin/cos，不依赖 MCU 三角函数或 CORDIC。
底层变换只检查相量范数容差，不静默归一化。调制比例处于 `(0, 1/sqrt(3)]`，
当前 G431 的 0.45 比例在全周数值检查中保留 10%～90% duty 余量；这不代替实物采样窗口验证。
非法输入清空输出并清 valid；合法零矢量得到三个 0.5 duty，该值不表示功率关断。
坐标与调制公式参考 TI 官方 [Park](https://software-dl.ti.com/processor-industrial-sw/esd/motor_control_sdk/am261x/2025_00_00_06/docs/api_guide_am261x/park_8h_source.html)、
[逆 Park](https://software-dl.ti.com/processor-industrial-sw/esd/motor_control_sdk/am261x/2025_00_00_06/docs/api_guide_am261x/ipark_8h_source.html) 与
[SVPWM](https://software-dl.ti.com/processor-industrial-sw/esd/motor_control_sdk/am243x/2025_00_00_06/docs/api_guide_am243x/svgen_8h_source.html)。

`current_controller.h/.c` 将上述运算组成电流环单拍计算：
αβ 电流 → Park → d/q PI → 逆 Park → 圆限幅/SVPWM，并将调制器采用的电压换回 DQ 作积分回算。
原始相量通过容差检查后在本模块内归一化，避免非单位相量制造虚假回算。
默认各变换使用同一角；启用 `separate_actuation_angle` 时，Park 使用测量角，
逆 Park 和调制电压回 DQ 使用独立的施加角，两份相量分别验证和归一化。
本拍以旧积分产生请求，随后按 `I_next = I + dt*(Ki*error + Kaw*(applied-requested))`
计算下一拍积分并施加积分限幅；两轴候选都合法后才一起提交。
完整电流目标超过配置矢量上限时拒绝；输入失效、dt 超限或数值异常会清双轴历史，输出无效。

调用方负责电流增益/方向、标定后的电角度、观测新鲜度、采样时序、故障与功率门控；
本模块不根据商家参数填入默认控制增益。配置数值合法不代表闭环稳定，增益需结合实际对象整定。
调用方在物理标定和时序验收前应关闭控制许可，不得把数值结果直接视作功率请求。

验证入口：

```powershell
.venv\Scripts\python.exe -X utf8 control\tests\verify_foc_math.py
.venv\Scripts\python.exe -X utf8 control\tests\verify_current_controller.py
```

检查包含全周/六扇区/随机独立参考、圆限幅和电压重构、积分回算、失效清历史及多实例。
简化解耦 RL 数值对象的阶跃收敛仅验证算法实现；没有证明实际电机闭环、带载或 20 kHz ISR WCET。

## 电角度映射与预测

`electrical_angle.h/.c` 将单圈机械位置映射为电角度及同拍 sin/cos：
`theta_e = wrap(pole_pairs * direction * (position + rpm/60 * age) - offset)`。
速度使用编码器原坐标，方向符号在映射时应用一次；偏置以电角度圈为单位，最后相减。
先归一化机械位置再乘极对数，避免无界圈数参与单精度计算。

配置支持 1～64 极对、方向 ±1、偏置 `[0,1)`，并显式限制速度和预测年龄。
非零预测上限必须严格小于 `30/max_velocity_rpm`，使机械预测位移保持半圈以内。
非法输入或年龄超限立即清输出和 valid；本模块不累计位置历史。
数值配置合法只说明可以计算，实际极对数、方向、零位的可信度由调用方单独门控。

调用方应以位置采集时间到同步采样时刻的年龄逐拍预测。使用低通后的机械速度时，
短时匀速假设在加减速期间会产生误差；进入实际电流环前，还需验收编码器延迟、预测误差、
电流测量角度与电压施加角度的时间关系。`sinf/cosf` 已通过数值测试，实机执行周期尚未验收。

默认 `electrical_angle_update()` 继续使用软件 `sinf/cosf`。针对真实的三角函数替换需求，
`electrical_angle_update_with_trig()` 在纯 C 中完成相同的输入检查、预测和角度归一化，
随后仅调用一次 `ElectricalAngleTrig`，由调用方提供后端和上下文；本模块不保存或共享上下文。
软件参考后端为 `electrical_angle_sincos_software()`。后端只接收 `[0,1)` 电角度圈，
返回同角度的 sin/cos；NULL、失败、非有限结果和单位圆平方范数误差大于 0.001 均清空输出。
容差内结果不会被静默归一化；单位圆检查不能识别错误相位，硬件后端需另外对照软件角度精度。
后端必须有界、非阻塞且由调用方串行使用；CORDIC 等硬件调用留在 Target 适配区域，
不把 HAL 或外设状态带入本模块。已有 Runtime 调用保持默认软件路径，新增接口本身不开放功率许可。

## 速度 PI

`speed_controller.h/.c` 输入完整目标与反馈转速、外环实际时间间隔，输出有界 Iq 目标。
本拍用旧积分计算请求，限流后按 `I_next = I + dt*(Ki*error + Kaw*(Iq-requested))`
回算下一拍积分，并施加独立积分限幅。非法目标、失效反馈、非法 dt 或数值异常清积分、
输出无效；reset 保留配置。增益和电流上限均由调用方提供，没有实机默认整定值。

本模块只跟踪自身 Iq 限值。内环电压饱和、动态降额、速度正方向与转矩方向的一致性，
以及状态切换时积分如何复位，由调用方的级联控制流程明确；本模块不直接操作功率。

上述两个模块的验证入口：

```powershell
.venv\Scripts\python.exe -X utf8 control\tests\verify_electrical_angle.py
.venv\Scripts\python.exe -X utf8 control\tests\verify_speed_controller.py
```

ARMCC 编译正式 C 后由 Unicorn 执行，电角度覆盖双方向、跨零、预测边界、独立双精度随机参考，
以及可替换后端的输入/调用次数、失败清零和多实例上下文隔离；
速度 PI 覆盖正反限流、积分回算、溢出失效、多实例和连续序列参考。
简化机械对象的阶跃、反转与负载收敛仅验证数值行为，不表示实机速度闭环已经验收。
