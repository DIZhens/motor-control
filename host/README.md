# host

PC 调试工具资料与后续第一方 Host。目前尚无可运行的第一方 Host 实现。

## V1：UART 与 VOFA

V1 使用现成 VOFA 工具，围绕 G431 单电机的 FOC 电流内环与速度外环提供关键观测、启停、速度设置和故障查看，不开发 Qt 界面。

本目录随 UART 实现保存实际可用的 VOFA 配置、通道映射和最小操作说明：

- 配置说明包含串口参数、数据格式和导入方式；
- 通道映射对应 `interface/` 中的字段、单位与有效条件，不另定义设备语义；
- 操作说明覆盖连接、观测、启停、设置速度、查看故障和显式恢复的实际步骤及限制。

配置与说明在对应能力可用时加入；当前不把计划写成已具备的操作入口。设备命令和观测合同以 [interface/](../interface/README.md) 为准，实机验收从 [G431 验证页](../firmware/targets/BLDC_G431/VALIDATION.md) 进入。

默认关断入口的首次串口设置见 [vofa/README.md](vofa/README.md)；目前为离线验证后的待上板说明，尚无实测导出的 VOFA 工程配置。

## 后续 Qt Host

后续第一方 Host 的技术基线：

```text
Python
PySide6 / Qt6
Qt Widgets
pyqtgraph
pytest
```

长期边界：

```text
GUI
 ↓
Host Device API
 ↓
Protocol Client
 ↓
Transport
```

GUI 不直接拥有串口或协议状态机。

后续按实际需要实现连接、Start/Stop、控制模式与目标、State/Fault、关键 Telemetry、曲线和数据保存；不提前建立空模块、插件平台、Dashboard 编辑器或通用设备生态。
