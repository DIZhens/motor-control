# FreeRTOS 供应源码

来源为 STM32Cube FW_G4 V1.6.3 的
`Middlewares/Third_Party/FreeRTOS/Source`，Kernel V10.3.1，Port 为
`portable/RVDS/ARM_CM4F`，匹配 G431 Run 的 ARM Compiler 5.06 update 5。
仅收录实际使用的 `tasks.c`、`queue.c`、`list.c`、公共头文件和该 Port；
供应文件保持原文及文件头许可，逐文件 SHA256 见 [UPSTREAM.json](UPSTREAM.json)。

正式工程手工加入上述四个 C 文件，不编译 heap 实现、CMSIS-RTOS 包装、软件 Timer
或生成的默认 Task。项目策略在 Target 的 `runtime/FreeRTOSConfig.h`、Target 装配和
`app/system_runtime/system_runtime.c`，不修改 Kernel/Port。Validation 不链接本目录。
