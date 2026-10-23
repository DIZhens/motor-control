# Commissioning 算法

这里保存独立试验算法：`rotor_alignment.*` 计算有限静态定向意图，`voltage_open_loop.*` 计算有限开环电压磁场。它们只接收值并输出计算结果，不依赖 Target、HAL、RTOS 或通信。

算法本身不能保证真实关断；调用方负责许可、跨边界提交、独立时限和功率执行。成功也不自动安装标定结果。当前装配与物理验证范围见 [G431 Validation](../../targets/BLDC_G431/validation/README.md)。

独立 ARM 模型位于 [tests](tests)，从仓库根目录运行 `firmware/control/commissioning/tests/verify_rotor_alignment.py` 或 `verify_voltage_open_loop.py`。
