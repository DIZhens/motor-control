# 自研设备协议设计边界

[Motor Node Profile](MOTOR_NODE_PROFILE_V1.md)拥有共享业务编号、结果与能力子集，
[Device Contract](DEVICE_CONTRACT.md)拥有Motor语义；
[UART Mapping](UART_MAPPING_V1.md)拥有精确UART表示与事务规则。
CAN使用独立帧映射，共享设备意图与实时交接，不共享UART的COBS/CRC/Header。
本项目借鉴Modbus的设备访问/请求响应及CANopen的节点/对象/实时数据/事件分类思想，
不声明兼容这两个标准，不复制完整寄存器表、对象字典或NMT/SDO/PDO。

## 实现与依赖方向

```text
communication/protocol/device：设备Profile与Device Port
communication/protocol/uart：字节流Codec、Parser、Session/Sequence、去重与终态
communication/protocol/transport：窄链路合同
    ↓ 链接期固定端口
firmware/targets/BLDC_G431/communication：设备值/RuntimeRequest/Completion规范化绑定
    ↓ Target一致publication、提交与关断适配
Motor / ControlRuntime：既有实时单写者
```

协议层不依赖HAL、STM32、FreeRTOS、Motor.h或运行时私有结构。Firmware Binding不拥有
COBS/CRC、UART Header、CAN ID/DLC或事务状态。Target拥有DMA/IRQ/硬件配置与静态Frontend
选择；不复制协议实现，不引入运行时Registry、插件、多主权或第二长期Communication任务。
当前UART已经通过共享绑定；CAN具体实现与实物范围按阶段证据分别验收。

## 有界事务与恢复

事务原则共享，但UART与CAN的Session/Sequence大小和生命周期可分别定义。
普通请求单在途，独立优先Stop、固定完成缓存和Response容量；完整校验前不猜测命令。
有效非零会话的重复请求只执行一次；无会话Stop只在途去重，完成后相同帧可以再次关断。
内部ticket不等于线上身份，Runtime不感知重试。新会话和链路恢复不延长运行期限，不重放旧运动。

CRC/COBS/UART错误及Host失联只恢复字节流；现有1.5秒与运动包络继续保护有限运行。
不可恢复的FIFO/身份/资源不变量失败请求协议无关Fail-closed，不自动清故障或重启。
CAN控制器错误、Bus-off与恢复另由CAN映射和Target策略规定，内部回环不代表外部总线资格化。

## 执行环境与观测

静态FreeRTOS后台拥有通信和遥测，ADC/FOC ISR不调用内核或等待后台。
设备命令通过ticket/Completion在原实时边界执行，通信不直接写PI历史/PWM/MOE。
CubeMX配置硬件，Run手工集成供应包原生Kernel/RVDS Port，Validation保持独立裸机。
具体资源与优先级见[G431集成合同](../../firmware/targets/BLDC_G431/runtime/RTOS_INTEGRATION.md)。

VOFA用于有限运行观测；需要时由PC侧转换自研协议为观测格式，不在MCU自动识别或混发。
Qt、标准Modbus/CANopen、位置环、Flash持久化与通用上位机框架不因共享Profile提前进入。
