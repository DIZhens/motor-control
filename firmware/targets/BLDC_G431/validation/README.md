# G431 Validation

保存 G431 独立观测、台架和时序验证代码，以及可重复离线模型和现场工具；正式运行入口位于 [Runtime](../runtime/README.md)。

## 目录

| 目录 | 职责 |
| --- | --- |
| [runner](runner) | Observe 与旧台架入口、事件和状态装配 |
| [bringup](bringup) | PWM、采样和保护注入验证 |
| [control](control) | 独立有限控制试验 |
| [communication](communication) | 验证专用 VOFA 与 CAN 内部回环 |
| [timing](timing) | 无功率计算、IRQ 和提交窗口诊断 |
| [tests](tests) | ARM 模型、主机工具检查与最小固定输入 |
| [tools](tools) | 构建冻结、记录解析及有明确前置条件的现场入口 |

测试固定输入放在 `tests/fixtures/`，详细输出写入 ignored 的 `local-evidence/`。历史资料只保留本地；阶段结果由 Git 检查点记录，不作为自动测试的隐式依赖。

## Keil Target

打开 `MDK-ARM/BLDC_G431_Validation.uvprojx`：

| Target | 用途 |
| --- | --- |
| `BLDC_G431_Observe` | 默认关断、位置/母线/电流观测，拒绝 Start |
| `BLDC_G431_Timing` | 无功率专项计时 |
| `BLDC_control V1` | legacy validation 台架流程及私有 ABI |

三个 Target 不链接 FreeRTOS，不与正式 Run 同时链接入口。Keil 红色排除图标表示 `IncludeInBuild` 隔离，并非文件丢失。共享 Motor、ControlRuntime 和 Platform 源码由工程直接引用。

正式 Run 位于另一份工程，身份生成与构建步骤见 [Runtime](../runtime/README.md#构建与入口)。构建、烧录、复位和运动是分别授权的操作；离线入口不会连接设备。

## 当前边界

| 能力 | 已有范围 | 限制 |
| --- | --- | --- |
| 默认关断 | 启动、校零及无效反馈拒绝的模型和实物记录 | IDLE 不等于具备 Start 许可；校零失败不自动重采 |
| PWM / 采样 | 物理 A/B/C 映射，B/C 双 ADC 同步采样及有限波形检查 | 不保证任意 duty、负载和调制度下有效 |
| Hardware Trip | 比较器到 Break 的注入关断、锁存及显式恢复 | 未资格化实际 OCP/OVP 阈值及精确 Gate 延迟 |
| 有限 FOC | 固定空载的正反启停、设速、Stop 和独立时限 | 电流为名义 A；不作为速度性能或长期能力验收 |
| UART / FreeRTOS | 自研事务、原生静态任务、DMA、250 Hz 记录及有限闭环 | 压力、调度和运动记录各有原镜像及条件 |
| CAN | Codec/Server/绑定模型、双静态构建及控制器内部回环 | 不证明外部收发器、总线、ACK/仲裁或 CAN 电机运动 |

旧实物结果仍属于原硬件、接线和镜像。重命名、目录整理或离线二进制等价均不生成新的实物证据。

## 主要离线入口

从仓库根目录执行；依赖和 ARMCC 路径见[开发规则](../../../../DEVELOPMENT.md#44-本地验证依赖)。

```powershell
.venv/Scripts/python.exe -X utf8 firmware/targets/BLDC_G431/validation/tests/verify_platform.py
.venv/Scripts/python.exe -X utf8 firmware/targets/BLDC_G431/validation/tests/runtime/verify_runtime.py --finite-run --exchange
.venv/Scripts/python.exe -X utf8 firmware/targets/BLDC_G431/validation/tests/runtime/verify_foc_runtime.py
.venv/Scripts/python.exe -X utf8 firmware/targets/BLDC_G431/validation/tests/runtime/verify_system_runtime.py
.venv/Scripts/python.exe -X utf8 communication/protocol/tests/uart/verify_core_server.py
.venv/Scripts/python.exe -X utf8 communication/protocol/tests/can/verify_can.py
.venv/Scripts/python.exe -X utf8 communication/protocol/tests/verify_boundaries.py
```

其余 `verify_*.py`、`test_*.py` 随模块保存。模型使用正式 C 与明确替身，证明数值、事务及状态行为；不把模型耗时或构建成功当作物理时序验收。

## 固定输入与现场工具

- `tests/fixtures/current-filter/`：12 V 静态 ADC 原码与滤波参考结果，用于确定性回放。
- `tests/fixtures/rotor-mapping/`：原硬件映射结果，供有限工具冻结候选；不适用于任意电机或接线。
- 现场工具的显式输入、前检与有限动作以对应 CLI 和源码为准；历史候选必须由操作者提供或保留本地锚点。

`tools/` 含可连接设备的入口，不能作为离线套件整体执行。不得自动重试运动或清故障；结果与完整日志按[开发规则](../../../../DEVELOPMENT.md)留在本地。

## 已知限制

- **500 Hz 历史压力未通过**：丢失率 1.92%，未达到原小于 1% 门槛；250 Hz 是正式通信工作点。
- **速度性能尚未最终验收**：有限窗口未持续进入目标误差带，重复性及独立转速参照仍不足。
- **独立绝对电流精度未验收**：零偏、名义比例和方向检查不替代增益/温漂精度标定。
- **外部 CAN 物理总线未验收**：内部回环不覆盖收发器、终端、真实仲裁及总线故障。
- **完整 WCET 与最坏调度未证明**：局部计时、调用深度及已测栈水位不能替代完整证明。
- **持续、带载和长期能力未验收**：正式运行仍受有限时长和净位移边界约束。
- **持久标定未实现**：控制配置绑定原电机、编码器与接线，启动零偏不跨复位保存。
- **硬件实际 Trip 门限未资格化**：注入关断与恢复不代表真实电流/电压门限及动态延迟合格。
