# Firmware通信绑定

[motor_node_binding.c](motor_node_binding.c)实现顶层
[Device Port](../../../protocol/common/protocol_device_port.h)：读取一致设备值，将完整设备意图
映射为RuntimeRequest，提交ticket，消费Completion并规范化结果，请求Target Fail-closed。
它不拥有Session/Sequence、COBS/CRC/Header、CAN ID/DLC或协议事务，不直接写Motor目标、
ControlRuntime私有实例、PI历史或PWM/MOE。

[motor_node_binding_target.h](motor_node_binding_target.h)是链接期Target绑定：提供本机身份、
Motor/参数publication、现有提交/完成和关断入口，无运行时Ops注册表。
G431的`target_motor_node_binding.c`只连接这些端口；UART专属统计和Server服务仍在
`target_core_server.c`，不会把UART统计对象伪装成通用CAN状态。

通用Codec、Parser、事务、设备Profile与链路端口位于[protocol/](../../../protocol/README.md)，
Keil直接引用，不保留源码副本。本目录VOFA与Motor VOFA仍仅服务独立Validation和历史基线；
正式Run不编译旧VOFA。RuntimeRequest/Completion结构及实时单写者保持不变。

```powershell
.\.venv\Scripts\python.exe -X utf8 protocol/tests/uart_v1/verify_core_server.py
```

模型执行真实绑定、Runtime交接/参数/Motor和UART Server，Target读端口与硬件推进为替身；
不替代[G431实物验证](../../targets/BLDC_G431/VALIDATION.md)。
