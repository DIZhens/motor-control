# firmware

MCU 电机控制固件。

长期结构：

```text
firmware/
├── app/
│   ├── motor/
│   ├── communication/
│   └── system_runtime/
├── platform/
└── targets/
```

`app/` 保存与具体板卡尽量无关的 Firmware 业务与运行时。

`platform/` 保存当前 Control Profile 对硬件提出的 Hardware Capability Contract。

`targets/<board>/` 保存某块具体控制板的完整 CubeMX / Keil 工程和 Platform 实现，例如：

```text
targets/custom_g431/
├── platform/
├── motor-control.ioc
├── Core/
├── Drivers/
└── MDK-ARM/
```

第一步迁移现有 BLDC G431 工程时，先保持原始 CubeMX/Keil 结构并验证能正常打开、Rebuild 和下载，再开始架构重构。
