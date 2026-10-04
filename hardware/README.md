# hardware

保存已经确认的物理硬件事实。

每块实际控制板建立自己的目录，例如：

```text
hardware/custom_g431/
└── HARDWARE_BASELINE.md
```

这里记录 MCU、功率级、电流采样、编码器接口、保护链、关键引脚和硬件限制。

硬件目录不拥有 Motor 业务语义，也不保存 Host/Protocol 设计。
