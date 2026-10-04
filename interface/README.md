# interface

Firmware 与 PC Host 的共享合同区域。

当前只冻结两个层次：

```text
Device Contract
    设备有哪些状态、命令、Fault、观测和参数，以及这些数据的单位与行为语义

Protocol Contract
    上述设备语义如何表示为请求、响应、事件和 Telemetry
```

第一阶段先建立 `DEVICE_CONTRACT.md`。只有 Device Contract 足够稳定后，再决定具体 packet/framing/CRC/schema，不沿用旧工程的对象字典作为默认前提。
