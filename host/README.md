# host

PC Host。目前仅保留技术选型和模块边界，尚无可运行实现。

第一阶段技术基线：

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

第一阶段只实现连接、Start/Stop、控制模式与目标、State/Fault、关键 Telemetry、曲线和数据保存。不要提前建设插件平台、Dashboard 编辑器或通用设备生态。
