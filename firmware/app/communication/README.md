# Firmware通信绑定

正式UART的通用编解码、Parser和事务Server已移动到顶层
[protocol/](../../../protocol/README.md)，由Keil直接引用，不保留源码副本。
当前G431的对象快照映射、RuntimeRequest提交/Completion连接及后台服务装配仍在
`targets/BLDC_G431/runtime/target_core_server.c`；后续仅把可共享Firmware绑定移入本目录。
协议层不直接访问Motor/ControlRuntime私有实例，不修改PI历史或PWM/MOE。

本目录的 `vofa.*`、`motor_vofa.*` 及对应模型继续服务独立Validation及历史控制基线，
正式Run不编译它们。本次不因目录对称迁移这些验证入口。

UART合同与模型入口见 [Protocol说明](../../../protocol/README.md)，实际构建和实机证据见
[G431验证页](../../targets/BLDC_G431/VALIDATION.md)。
