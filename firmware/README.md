# firmware

MCU 电机控制固件。

长期结构：

```text
firmware/
├── app/
│   ├── motor/
│   ├── communication/
│   └── system_runtime/
├── platform_contract/
└── targets/
```

`app/` 保存与具体板卡尽量无关的 Firmware 业务与运行时。

`platform_contract/` 保存当前 Control Profile 对硬件提出的 Hardware Capability Contract；它只定义公共能力语义，不包含任何具体 Target 的 HAL / Timer / ADC 实现。

`targets/<target>/` 保存某个确定 Target Configuration 的完整 CubeMX / Keil 工程和 `platform/` 实现。Target Configuration 由 MCU、功率级、相电流采样、位置源及静态时序/保护配置共同定义，不要求所有硬件位于同一块 PCB。例如：

```text
targets/custom_g431/
├── platform/
├── motor-control.ioc
├── Core/
├── Drivers/
└── MDK-ARM/
```

具体工程入口：

- [BLDC_G431](targets/BLDC_G431/)：G431 自制板 Target。
- [ODRIVE_V36_405_M0](targets/ODRIVE_V36_405_M0/README.md)：F405、M0、MT6701 / SPI3 Target；当前为功率关闭的观测入口，实机验收尚未完成。
