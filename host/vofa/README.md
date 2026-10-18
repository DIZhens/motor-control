# VOFA 串口观察

固件入口为 [G431 Observe](../../firmware/targets/BLDC_G431/runtime/README.md)，
当前只完成代码及离线验证，首次 UART 实机验收待做，不具备启动电机功能。

1. 使用 3.3 V 逻辑的 USB 转串口，转换器 RX 接板 PB6 / USART1_TX，TX 接 PB7 /
   USART1_RX，GND 接 GND；板子按现场既定方式单独供电，转换器 VCC 不接板。
2. 在 VOFA+ 选择对应串口、115200 bit/s、8 数据位、无校验、1 停止位，数据引擎选 JustFloat。
3. 十通道的唯一字段顺序、单位与有效位见 [VOFA 协议](../../interface/VOFA_PROTOCOL.md)。
   未实现的速度、Id、Iq 是 NaN；不要以缺失曲线代替零值。
4. 下行发送 ASCII，末尾带实际 LF。可先发 `stop` 和 `clear` 检查结果；
   `start:120` 当前返回 NOT_READY，功率保持关闭。结果/状态编码见
   [Device Contract](../../interface/DEVICE_CONTRACT.md)。

每次只发一个请求，等待新遥测；该最小协议没有逐命令可靠应答。接收出现丢字节或半行超时后，
先单独发一个 LF 使接收器回到行边界，再发完整请求。连接/重连不自动运行。
通道曲线先看控制时间、母线和状态，确认数据在更新，再讨论后续控制观测。
