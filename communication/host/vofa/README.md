# VOFA 观察

正式 Run 由 PC 协议客户端独占 UART，数据桥向 VOFA 单向转发 JustFloat UDP；VOFA 不占用 MCU 串口，也不向电机发送命令。

| 入口 | 用途 |
| --- | --- |
| [bridge.py](bridge.py) | 校验当前 Session 遥测、完整 JSONL 记录及 UDP 转发 |
| [analyze.py](analyze.py) | 按控制时间、Session、配置代际和目标窗口分析 |
| [replay.py](replay.py) | 按原始控制时间有限回放记录，不连接设备 |

## 最少设置

1. VOFA 选择 **UDP / JustFloat**，本地端口 `1347`，可选本地地址时填 `127.0.0.1`；关闭串口连接和定时发送。
2. 放入波形控件，250 Hz 的时间步长设 `4 ms`。默认 `speed` 视图 I0/I1 为目标/实际速度；`current` 视图为 Iq reference、Iq feedback、Id feedback。`all` 显示完整十通道。
3. 从仓库根目录运行有限观察，使用已存在目录中的新日志文件名：

```powershell
.venv/Scripts/python.exe -X utf8 -m communication.host.vofa.bridge --port COM5 --seconds 30 --log "$env:TEMP/g431-observe-new.jsonl"
```

观察入口只在健康 IDLE 建会话、配置 250 Hz 遥测；不发运动、PI 或故障恢复命令，不自动重连。结束时关闭遥测。原始记录始终完整，视图选择只影响显示通道。

清旧曲线用波形下方彩色滑条左下侧的垃圾桶；它只清缓存，不改变发送通道数。切换视图后重新绑定通道。无效值保留 NaN，不能作为零速或零电流；UDP 是最佳努力，精确状态和缺帧以 JSONL 为准。

## 分析与回放

```powershell
.venv/Scripts/python.exe -X utf8 -m communication.host.vofa.analyze capture.jsonl --output metrics-new.json
.venv/Scripts/python.exe -X utf8 -m communication.host.vofa.replay capture.jsonl --view speed
.venv/Scripts/python.exe -m unittest communication.host.vofa.test_bridge communication.host.vofa.test_analyze -v
```

输入是已有记录；回放截图须标明源日志和“记录回放”，不能作为新的实物试验。描述指标不替代速度性能、10 kHz 电流带宽或 WCET 验收，未观测到的稳定时间保留为空。

## 独立验证入口与软件位置

Observe 的 `115200 / 8N1 / JustFloat` 串口直连遵循 [VOFA 验证协议](../../spec/VOFA_PROTOCOL.md)，与正式 Run 的自研协议不混用。USB 串口 RX→PB6、TX→PB7、GND 共地、VCC 不接；接线与供电按现场条件单独确认。

本机软件位于 `D:/c_tool/VOFA+/vofa+.exe`，程序和 DLL/插件整体放在工具目录，不进入 Git。界面布局用 VOFA 原生保存/导出功能留存；仓库没有可直接导入的布局模板。

完整设备语义见[通信规范](../../spec/README.md)，能力限制见 [G431 Validation](../../../firmware/targets/BLDC_G431/validation/README.md)。
