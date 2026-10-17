# firmware

MCU 电机控制固件。

## 实现边界

当前两个 Target 的 `main()` 都进入各自的 `validation/` 台架程序，尚未接入正式 Motor / ControlRuntime。公共 HCC 头文件已经定义，不等于全部 Target 实现和实机验收均已完成。

| 区域 | 当前内容 | 后续门槛 |
| --- | --- | --- |
| `platform_contract/` | 实时电流/驱动、位置、母线、ControlTime 和可选 Hardware Trip 的能力合同 | 逐个 Target 核对实现、失败语义与物理验收 |
| `targets/BLDC_G431/` | Platform、单次命令台架、无电机实测与模型测试 | 补真实 staging deadline 检查；电流增益、动态功率级及带载控制尚未验收 |
| `targets/ODRIVE_V36_405_M0/` | Platform、deadline/epoch 检查、功率关闭观测入口 | 当前固件实机验收；未提供可选 Hardware Trip，不能沿用 G431 的硬件保护结论 |
| `app/` | 尚未建立；以下为长期设计 | 先明确控制闭环范围与底层进入条件 |

公共应用使用 HCC，不直接依赖某一 Target 的 HAL、调试邮箱或保护注入命令。
两板验证结果互不替代；模型、静态检查、目标编译和实物验证分别记录。

## 长期结构

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
