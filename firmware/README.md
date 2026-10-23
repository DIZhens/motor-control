# Firmware

公共算法和业务代码由具体 Target 的 Keil 工程直接引用，不复制到板级目录。

```text
firmware/
├── control/       可移植算法；commissioning/ 保存独立试验算法
├── motor/         Motor 生命周期、Snapshot 与正式 ControlRuntime
├── platform/      跨 Target 的硬件能力公共头文件
├── targets/       具体 MCU / 板卡工程、实现与验证
└── third_party/   原样供应源码、许可和来源清单
```

| 入口 | 阅读内容 |
| --- | --- |
| [control](control/README.md) | 电流换算后的校正、重构、FOC、PI、滤波及估计 |
| [motor](motor/README.md) | Start / Update / Stop、反馈与实时状态所有权 |
| [platform](platform/README.md) | 实时驱动、位置、母线、控制时间及可选 Trip 的物理语义 |
| [G431](targets/BLDC_G431/README.md) | 正式 Run、Target Runtime/通信及独立 Validation |
| [F405 M0](targets/ODRIVE_V36_405_M0/README.md) | 第二套 Platform 实现和功率关闭观测工程 |

G431 的 SystemRuntime 和协议到设备的绑定目前随 G431 装配维护，分别位于该 Target 的 `runtime/` 和 `communication/`；F405 没有为目录对称预建后台任务或通信模块。

可移植协议位于 [communication/protocol](../communication/protocol/README.md)，共享合同位于 [communication/spec](../communication/spec/README.md)。控制算法不依赖 HAL、RTOS 或协议；Motor 通过公共 Platform 能力运行，具体外设实现留在 Target。

G431 的构建与实测结论统一由 [Validation](targets/BLDC_G431/validation/README.md)记录。两板的编译、模型与实物结论分别验收，长期职责见[架构](../ARCHITECTURE.md)，工具链规则见[开发说明](../DEVELOPMENT.md)。
