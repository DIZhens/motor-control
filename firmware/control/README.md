# Control

可移植控制算法：根据输入、配置和算法历史计算输出，不拥有 Motor 生命周期、通信、HAL、功率门控或系统故障策略。

| 入口 | 职责 |
| --- | --- |
| [position_estimator.h](position_estimator.h) | 周期位置解绕、机械速度估计与低通 |
| [current_measurement.h](current_measurement.h) | 零偏校正、有符号增益、缺相重构与 Clarke |
| [electrical_angle.h](electrical_angle.h) | 机械位置到电角度映射、时间预测及三角函数后端合同 |
| [foc_math.h](foc_math.h) | Park、逆 Park、圆限幅和居中 SVPWM |
| [current_controller.h](current_controller.h) | DQ 电流 PI、积分回算和调制 |
| [speed_controller.h](speed_controller.h) | 速度 PI 和有界 Iq 参考 |
| [current_feedback_filter.h](current_feedback_filter.h) | 控制反馈支路的一阶低通 |
| [commissioning](commissioning/README.md) | 独立电压开环和定向算法 |
| [tests](tests) | ARMCC / Unicorn 数值和状态模型 |

调用方持有实例，在同一执行边界串行更新；参数单位、合法范围及失败语义以公共头文件为准。ADC 到名义物理电流的换算属于 Platform；校零 READY 后 offset 冻结，`process()` 不重新估计或更新零偏。

滤波不替代原始反馈的有效性和保护检查；无效零值不能解释为静止或零电流。数值配置合法、模型收敛和中性 duty 均不授予功率许可。

## 验证

从仓库根目录运行相应入口，例如：

```powershell
.venv/Scripts/python.exe -X utf8 firmware/control/tests/verify_current_measurement.py
.venv/Scripts/python.exe -X utf8 firmware/control/tests/verify_current_controller.py
.venv/Scripts/python.exe -X utf8 firmware/control/tests/verify_speed_controller.py
```

其他算法的 `verify_*.py` 与模型同目录。依赖见[开发规则](../../DEVELOPMENT.md#44-本地验证依赖)。这些检查证明算法行为；实机采样、控制时序、稳定性和精度由 [Target 验证](../targets/BLDC_G431/validation/README.md)分别验收。级联与状态所有权见 [Motor](../motor/README.md)。
