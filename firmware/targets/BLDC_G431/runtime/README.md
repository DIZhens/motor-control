# G431 默认关断观察入口

`BLDC_G431_Observe` 在现有 Platform 上装配公共 Motor、完整命令交接和 UART / VOFA。
当前只观察：没有三相 duty stage、power enable、保护注入或闭环计算调用；所有 Start
均拒绝进入 RUN。它为反馈与标定接入提供入口，尚未通过 UART 或新入口实机验收。

## 构建与入口

打开 `../MDK-ARM/BLDC_control V1.uvprojx`：

| Keil Target | 入口 | 输出 |
| --- | --- | --- |
| `BLDC_control V1` | 原 `validation/target_validation.c`，保持原台架工具 ABI | `BLDC_control V1/` |
| `BLDC_G431_Observe` | 本目录 `target_runtime.c` + 公共 `app/motor`、`app/communication` | `build/observe/` |

选择 Target 后使用 Rebuild。CLI 示例（`UV4.exe` 路径按本机安装调整）：

```powershell
& 'D:\keil5\UV4\UV4.exe' -r 'firmware\targets\BLDC_G431\MDK-ARM\BLDC_control V1.uvprojx' -t BLDC_G431_Observe -o "$env:TEMP\g431-observe-build.log"
```

`BLDC_G431_OBSERVE_ENTRY` 仅在新 Target 定义；`main()` 和 TIM6 IRQ 的 USER CODE
选择对应入口。两者共享 CubeMX 配置及 Platform，不改生成区，不同时绑定两个 consumer。
烧录是独立的现场步骤；编译不自动连接 ST-Link 或下载镜像。

## 执行职责

- 装配先 safe-off，再核对时钟、绑定 sample / trip、arm position / bus / realtime / protection，
  最后启动持续运行的 Drive Timing；这不等于功率使能。装配失败保持 FAULT 和关断。
- ADC sample callback 是运行期 Motor 的唯一写者，先核对采样、保护和独立监视原因，
  再消费完整命令。Stop 优先于普通排队请求，并丢弃更早的待处理命令。
- TIM6 每 50 µs 检查独立 DWT 时钟和采样心跳；间隔超过 75 µs 锁存故障并 safe-off。
  最迟在后续一次 TIM6 检查发现；它不是完整 ISR WCET 或未来带载控制截止证明。
- 后台以不等待的寄存器轮询收发 USART1，每次至多一字节；44 字节发送缓存完成前不被
  新快照覆盖。遥测来自有界重试的一致性 publication，不把 UART 放入 ADC 回调。
- publication 按 100 Hz 或命令/故障变化生成；后台约 100 Hz 编码。当前可发布状态、
  结果、目标零值、控制时间与有效母线电压。速度、Id、Iq 尚未实现，保持无效。

UART 错误、超长/非法行和 100 ms 未完成片段均丢弃至下一 LF；UART 无连接会话检测，
不会因重连自动启动。邮箱容纳一个普通请求，拥塞或非法帧记录 BAD_COMMAND；Stop
覆盖排队请求。收到 Stop 后、消费前到达的普通请求也丢弃，调用方不要把遥测当可靠逐条应答。

启动成功但 ADC 没有首拍，以及 ADC 停滞后尚未合并的 guard 原因，都会使旧遥测失效。
已在途的 UART 帧是过去的快照，接收端还需留意数据是否持续更新。当前故障恢复未实现，
Clear 不会清锁存；独立 guard 原因可在 Watch 中查看 `g_target_runtime_guard_faults`。

## 离线验证与下一步

从仓库根目录，使用 [开发依赖](../../../../DEVELOPMENT.md#44-本地验证依赖) 中的 Python：

```powershell
python firmware/app/motor/tests/verify_motor.py
python firmware/app/communication/tests/verify_vofa.py
python firmware/targets/BLDC_G431/runtime/tests/verify_runtime.py
```

测试编译并执行实际 ARM C，覆盖默认关断、完整命令、Stop 优先、错误丢弃、启动失败、
ADC 丢失、旧快照失效和低优先级代码被 ADC 抢占的交错。模型不代替 UART 链路或功率实测。
后续先验收 UART 与反馈/零点，再实现受控标定和 FOC；既有台架证据仍从
[VALIDATION.md](../VALIDATION.md) 进入，按具体修改影响选择复测。
