# Motor 与实时控制

`motor.h/.c` 拥有命令验证、生命周期与活动目标；`control_runtime.h/.c` 在确定性采样边界
拥有反馈、估计、待执行命令和控制流水线。后台读取协议无关的 `MotorSnapshot` 副本，
不直接修改实时算法历史。长期边界见 [总体架构](../../ARCHITECTURE.md)。

## 控制反馈与角度后端

`ControlRuntimeConfig.angle_trig` 与上下文由装配方固定持有；NULL 使用软件实现，
测量角与施加角共用所选后端，失败立即使角度无效，不静默回退。Target 硬件适配器仍在
Target 内，公共 Runtime 不依赖 HAL。后端选择不改变电流、电角度和时序的物理确认门槛。

RUN 的 Id/Iq 直接采用本拍成功电流控制器实际采用的反馈，复用其已归一化测量角，避免再做一次
观测 Park；IDLE、待启动及 Stop 仍独立计算未滤波电流的观测 DQ。RUN 在控制器返回前只检查原始反馈
及新鲜度，随后由控制器完成变换检查，失败同拍关断并清历史。合法有限输入导致旋转溢出
时，RUN 归入 CONTROL 数值计算失败；IDLE 仍撤销观测有效性并拒绝 Start。

`ControlRuntimeControlConfig.current_feedback_filter_coefficient` 固定控制输入支路的
alpha/beta 低通系数，默认 0 旁路；它不是在线参数或硬件采样配置。算法与历史类型位于
`firmware/control/current_feedback_filter.h`，Runtime 决定何时更新、停止与清除。`CurrentMeasurement`
继续保留未滤波的校正相电流与 Clarke 输出，快速软件过流仍使用这些原始校正相值。
首个 RUN 计算用当前输入初始化滤波，Stop、故障、反馈失效与重新启动不复用旧历史。
滤波失败与其它控制计算失败一样同拍关断，不用旧滤波值替代无效采样。

专属 Runtime ARM 模型见 `firmware/targets/BLDC_G431/validation/tests/runtime/verify_foc_runtime.py`。
同一套启停与保护检查同时执行旧 20 kHz 电流节拍和当前 10 kHz 电流／1 kHz 速度节拍。
当前节拍的独立对象按采样后 75 µs 生效、100 µs 保持分段积分；125 µs 仅用于施加角的
作用中点预测。数值闭环与模型保护通过不表示真实提交截止或实物标定通过。

## 独立试验算法

有限静态定向和电压开环已归入 [control/commissioning](../control/commissioning/README.md)。正式 Motor 目录只保存业务状态、Snapshot 与 ControlRuntime；试验算法不新增公共生命周期或自动安装标定。
