# ODRIVE_V36_405

本目录记录用户手中第三方 ODrive V3.6 兼容驱动板的物理硬件事实。

该板来自 loop222 / “驱动板405”资料体系，PCB 丝印为 `V3.6-56V`，但其功率级并不是 ODrive 官方 V3.x 的 DRV8301 + 并联 MOS 方案，而是 STM32F405 + EG2134 + 单管三相桥的第三方重设计。

目录职责与 `hardware/BLDC_G431/` 相同：

- `HARDWARE_BASELINE.md`：Firmware 快速读取的长期硬件事实与安全边界；
- `HARDWARE_DESIGN.md`：更完整的电路、接口、证据来源、冲突与待确认项。

本目录只记录硬件事实，不表示当前阶段已经启用 ODrive Firmware Target。当前阶段软件目标仍以根目录 `ARCHITECTURE.md` 为准。
