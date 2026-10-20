# 有限静止坐标电流环

本入口在现有 PWM 20 kHz／采样与计算 10 kHz 时序下，使用真实电流反馈和已有
`current_controller` 验证负反馈与电流跟踪。控制坐标系明确固定为 αβ：恒等 Park 变换，
不使用或伪造转子电角零位、极对数和方向标定。它不等同于转子 FOC 或速度闭环验收。

## 电流方向与幅值

本板名义换算的正方向为 Shunt+→GND；电机相电流规定桥→电机为正。
因此先扣冻结的名义零偏，再将实测 B、C 同时取反，最后重构 A 和 Clarke。
依据为原始网表及 [物理相映射修正后的静态对照](evidence/pwm-phase-mapping/summary.json)：
正α下 B/C 名义值同正、正β下 B负/C正，均与电机电流约定相反。
这足以确认符号，不能确认名义增益的绝对精度。

Observe 的 `signed_gain={1,-1,-1}` 使用同一方向约定；其零偏统计及诊断原始值继续保留
校正前的名义值。正式 `control.enabled`、电流增益和电角标定确认保持关闭。
本次不再以独立电流源未到为由重复方向检查，也不把精度待测写成方向未知。

## 单次试验边界

- 默认目标 α=+0.06、β=0 名义 A；目标矢量模长仅允许 0.01～0.08 A。
- 实际 enable 后 0.5 s 升流、2 s 保持、0.5 s 降流，然后 safe-off。
- Kp=0.5 V/A、Ki=10 V/(A·s)、回算系数50/s、积分限值0.3 V，属于保守试验参数，
  尚未按实测绕组电感整定。使用现有双轴 PI，不新增速度环。
- 调制电压比固定为0.4/14；允许母线10～14 V，因此算法电压请求矢量不超过0.4 V。
  这不是对死区、器件压降或实际端电压精度的保证。
- 校正方向后的实测 B/C 及重构 A 任一绝对值达到0.25名义A时立即关断。
  它是辅助阈值，未完成独立增益标定，不能等同于真实硬件保护门限。
- 复用两拍中性预装载再使能、同启动4096点真实关断零点、采样间隔与观测新鲜度检查，
  以及 TIM6 独立3.5 s总期限／125 µs采样心跳监视。失效不自动重试、清故障或恢复使能。
- 最后0.5 s保持窗口保存5000个连续样本的αβ均值、电压请求均值、误差峰值；
  主机只读冻结结果，不用零散SWD读数冒充连续采集。

台架新增命令17，独立8字节 `CurrentHoldConfig` 和84字节 `CurrentHoldSnapshot`；
旧邮箱20字节、旧快照184字节、命令15/16的参数及结果语义保持不变。
错误码18沿用有限试验独立超时、21沿用计算拒绝；只有正常轨迹完成才返回1。
快照是ADC单写者；ADC停止时须另外核对独立stop_reason和实际MOE，不能沿用旧active。

## 操作与工具

先在 OUTPUT 关闭、VBUS放电低于1 V后切为单独逻辑3V3，安装默认关断候选。
电机和编码器接线可保留，无外部注入。安装后只读核对镜像与默认关断；随后在OUTPUT
关闭时断开ST-Link的3V3，保留SWD/GND，使用原12 V／0.10 A、固定空载电机接法。
现场确认正常后采本启动零点，留5秒准备，只发一次默认3秒请求；无需示波器或串联万用表。
本入口仍通过SWD邮箱，不是VOFA启停接口。操作者可用OUTPUT立即停机，带电期间不暂停CPU。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/validation/tools/current_hold_once.py --output <新候选目录>
.\.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/validation/tests/verify_current_hold_tool.py
.\.venv\Scripts\python.exe -B -X utf8 firmware/targets/BLDC_G431/validation/tests/verify_model.py
```

默认只离线冻结HEX/AXF/MAP、源文件和DWARF布局；`inspect`只读，`zero`仅采真实关断零点，
`execute`须提供候选、本启动零点锚、现场接线确认与母线实测参考，才可写入唯一命令17。
完整payload读回匹配后才提交sequence；失败只允许一次SAFE_OFF，没有烧录、复位或重启路径。
命令15/16工具不因本入口而扩大写入能力。

跟踪筛选比较均值矢量误差≤0.01名义A、保持窗口误差峰值≤0.03名义A。
正常完成、跟踪筛选和实物增益精度是三个不同结果，不自动安装标定或开放正式Motor RUN。
