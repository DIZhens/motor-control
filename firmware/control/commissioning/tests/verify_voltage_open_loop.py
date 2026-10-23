"""真实 ARMCC C / Unicorn 有限电压开环验证；不连接硬件、不声明实物保护已验收。"""

from pathlib import Path
import math
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = Path(__file__).resolve().parents[4]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
IDLE, RAMP, HOLD, ACCELERATE, ROTATE, RAMP_DOWN, COMPLETE, FAILED, STOPPED = range(9)
NONE, PREREQUISITE, TIME, FEEDBACK, BUS, NOMINAL_CURRENT, NUMERIC, TIMEOUT, FAULT, STOP = range(10)
FIELDS = [
    "ticks_per_second", "voltage_v", "target_electrical_hz", "max_voltage_ratio",
    "min_bus_voltage_v", "max_bus_voltage_v", "nominal_current_trip_a",
    "voltage_ramp_ticks", "alignment_hold_ticks", "frequency_ramp_ticks", "rotation_ticks",
    "voltage_ramp_down_ticks", "timeout_ticks", "max_step_ticks", "max_current_age_ticks",
    "max_bus_age_ticks", "profile", "excursion_turns",
]
DEFAULT = dict(zip(FIELDS, [
    20_000, 0.5, 5.0, 0.45, 10.0, 14.0, 0.25, 400, 400, 2000, 4000, 400,
    8000, 20, 20, 200, 0, 0.0,
]))
CASES = []


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def circular_error(first, second):
    return abs((first - second + 0.5) % 1.0 - 0.5)


def continuous_reference(config, elapsed):
    """独立连续时间定义：分段电压包络及频率解析积分，不读取 C 的内部相位。"""
    second = elapsed / config["ticks_per_second"]
    ramp = config["voltage_ramp_ticks"] / config["ticks_per_second"]
    hold = config["alignment_hold_ticks"] / config["ticks_per_second"]
    acceleration = config["frequency_ramp_ticks"] / config["ticks_per_second"]
    rotation = config["rotation_ticks"] / config["ticks_per_second"]
    down = config["voltage_ramp_down_ticks"] / config["ticks_per_second"]
    rotating_time = max(0.0, second - ramp - hold)
    accelerated_time = min(rotating_time, acceleration)
    frequency = config["target_electrical_hz"] * min(rotating_time / acceleration, 1.0)
    turns = config["target_electrical_hz"] * (
        accelerated_time ** 2 / (2 * acceleration) + max(0.0, rotating_time - acceleration))
    voltage = config["voltage_v"] * min(second / ramp, 1.0)
    if second >= ramp + hold + acceleration + rotation:
        voltage *= 1.0 - (second - ramp - hold - acceleration - rotation) / down
    return voltage, frequency, turns % 1.0


with tempfile.TemporaryDirectory(prefix="voltage-open-loop-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    objects = []
    for source in [Path(__file__).resolve().parents[1] / 'voltage_open_loop.c', REPOSITORY / 'firmware/control/foc_math.c',
                   REPOSITORY / 'firmware/control/electrical_angle.c', Path(__file__).resolve().parent / 'voltage_open_loop_model.c']:
        obj = temporary / (source.stem + ".o")
        checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
                 "--split_sections", "-g", "-I", str(ROOT), "-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=model_init", "--keep=voltage_open_loop_model.o(*)", "-o", str(executable),
             *objects], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(item["p_vaddr"], item.data()) for item in elf.iter_segments()
                    if item["p_type"] == "PT_LOAD"]
        initializers = [(item["sh_addr"], item.data()) for item in elf.iter_sections()
                        if (item["sh_flags"] & 2) and item["sh_type"] == "SHT_PROGBITS"]
        symbols = {item.name: item["st_value"]
                   for item in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self, overrides=None, valid=True, base_time=0):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address, size in [(0x08000000, 0x20000), (0x20000000, 0x8000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.config = DEFAULT | (overrides or {})
            self.cpu.mem_write(symbols["model_config"], struct.pack(
                "<I6f10If", *(self.config[field] for field in FIELDS)))
            self.now = base_time
            self.base_time = base_time
            self.current_time = base_time
            self.bus_time = base_time
            self.inputs = [0.0, 0.0, 0.0, 12.0]
            self.flags = 7
            assert self.call("model_init") == valid, (overrides, self.diag)
            self.sync()

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=100000)
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, "unbounded: " + name
            return bool(struct.unpack("<I", self.cpu.mem_read(symbols["model_result"], 4))[0])

        def sync(self):
            for name in ["now", "current_time", "bus_time"]:
                self.cpu.mem_write(symbols["model_" + name], struct.pack("<Q", getattr(self, name)))
            self.cpu.mem_write(symbols["model_inputs"], struct.pack("<4f", *self.inputs))
            self.cpu.mem_write(symbols["model_flags"], struct.pack("<I", self.flags))

        @property
        def diag(self):
            return struct.unpack("<4I", self.cpu.mem_read(symbols["model_diag"], 16))

        @property
        def values(self):
            return struct.unpack("<8f", self.cpu.mem_read(symbols["model_values"], 32))

        def start(self, expected=True):
            self.sync()
            assert self.call("model_start") == expected, self.diag
            if expected:
                self.base_time = self.now
                assert self.diag == (RAMP, NONE, 1, 0)

        def step(self, ticks=20, new_current=True, new_bus=True, entry="model_update"):
            self.now += ticks
            if new_current:
                self.current_time = self.now
            if new_bus:
                self.bus_time = self.now
            self.sync()
            result = self.call(entry)
            assert result == bool(self.diag[3])
            assert all(math.isfinite(value) for value in self.values)
            assert all(0 <= value <= 1 for value in self.values[:3])
            if not result:
                assert self.values[:7] == (0.5, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0)
            return result

        def expect_off(self, state, reason):
            assert self.diag == (state, reason, 1, 0), self.diag
            before = self.diag, self.values
            self.step()
            self.start(expected=False)
            assert (self.diag, self.values) == before

        def check_reference(self):
            voltage, frequency, phase = continuous_reference(self.config, self.now - self.base_time)
            duty_a, duty_b, duty_c, alpha, beta, angle, hz, _ = self.values
            assert math.isclose(math.hypot(alpha, beta), voltage, abs_tol=3e-7)
            assert math.isclose(hz, frequency, abs_tol=3e-5)
            assert circular_error(angle, phase) < 3e-5, (angle, phase, self.now)
            assert math.isclose(alpha, voltage * math.cos(math.tau * phase), abs_tol=1e-4)
            assert math.isclose(beta, voltage * math.sin(math.tau * phase), abs_tol=1e-4)
            # 从真实三相平均 duty 独立消去公共模，核对请求矢量真正传递到调制输出。
            assert math.isclose((2 * duty_a - duty_b - duty_c) * self.inputs[3] / 3,
                                alpha, abs_tol=1e-6)
            assert math.isclose((duty_b - duty_c) * self.inputs[3] / math.sqrt(3),
                                beta, abs_tol=1e-6)

    model = Model()
    assert model.diag == (IDLE, NONE, 1, 0)
    assert not model.step()
    model.start()
    model.start(expected=False)
    seen = set()
    while model.step():
        seen.add(model.diag[0])
        model.check_reference()
    assert seen == {RAMP, HOLD, ACCELERATE, ROTATE, RAMP_DOWN}, seen
    model.expect_off(COMPLETE, NONE)
    CASES.append("默认关闭；显式开始后完成五段有限试转，结束撤销且不自动重启")

    model = Model({"voltage_ramp_ticks": 10_000, "alignment_hold_ticks": 10_000,
                   "frequency_ramp_ticks": 10_000, "rotation_ticks": 20_000,
                   "voltage_ramp_down_ticks": 10_000, "timeout_ticks": 70_000,
                   "max_step_ticks": 2, "max_current_age_ticks": 2})
    model.start()
    updates = 0
    while model.step(2):
        updates += 1
        if updates % 100 == 0:
            model.check_reference()
    assert updates == 29_999
    model.expect_off(COMPLETE, NONE)
    CASES.append("20 kHz ControlTime / 10 kHz 更新完整执行三秒五段轨迹并有限结束")

    for frequency in [-17.0, 17.0, -200.0, 200.0]:
        model = Model({"target_electrical_hz": frequency})
        model.start()
        index = 0
        while model.step([1, 7, 20, 3, 11][index % 5]):
            model.check_reference()
            index += 1
        model.expect_off(COMPLETE, NONE)
    CASES.append("有符号电频逐拍积分跨多圈；非整齐步长跨升频边界仍与独立连续解析积分一致")

    for base in [0x100000000 + 17, 0xFFFFFFFFFFFFFFFF - 10_000]:
        model = Model(base_time=base)
        model.start()
        for _ in range(100):
            assert model.step()
            model.check_reference()
    model = Model(base_time=0xFFFFFFFFFFFFFFFF - 7999)
    model.start(expected=False)
    model.expect_off(FAILED, TIME)
    CASES.append("64 位绝对时间跨 32 位和接近上界正确；无法容纳总期限则开始前拒绝")

    invalid_configs = [
        {"ticks_per_second": 0}, {"voltage_v": 0.0}, {"voltage_v": math.nan},
        {"target_electrical_hz": 0.0}, {"target_electrical_hz": math.inf},
        {"target_electrical_hz": 250.01}, {"max_voltage_ratio": 0.58},
        {"max_voltage_ratio": 0.0}, {"min_bus_voltage_v": 0.0},
        {"max_bus_voltage_v": 9.0}, {"voltage_v": 4.6},
        {"nominal_current_trip_a": 0.0}, {"nominal_current_trip_a": math.nan},
        {"voltage_ramp_ticks": 0}, {"alignment_hold_ticks": 0},
        {"frequency_ramp_ticks": 0}, {"rotation_ticks": 0},
        {"voltage_ramp_down_ticks": 0}, {"timeout_ticks": 7200},
        {"max_step_ticks": 0}, {"max_step_ticks": 401},
        {"rotation_ticks": 0xFFFFFFFF},
    ]
    for override in invalid_configs:
        model = Model(override, valid=False)
        assert not model.step()
        model.start(expected=False)
    CASES.append("非法配置、时长求和溢出风险、电压包络和单步超过四分之一电周均拒绝")

    for flags, reason in [(6, PREREQUISITE), (5, FEEDBACK), (3, FEEDBACK), (15, FAULT)]:
        for after_start in [False, True]:
            model = Model()
            if after_start:
                model.start()
                model.step()
            model.flags = flags
            if after_start:
                model.step()
            else:
                model.start(expected=False)
            model.expect_off(FAILED, reason)
    for after_start in [False, True]:
        model = Model()
        if after_start:
            model.start()
            model.step()
        model.flags = 31  # Stop 与外部 fault 同拍，Stop 必须先撤销。
        if after_start:
            model.step()
        else:
            model.start(expected=False)
        model.expect_off(STOPPED, STOP)
    model = Model()
    model.start()
    model.step()
    model.call("model_stop")
    model.expect_off(STOPPED, STOP)
    CASES.append("主动许可撤销、反馈无效、外部故障与 Stop 在开始和运行边界立即锁存关闭")

    for index in range(3):
        for value in [math.nan, math.inf, -math.inf, -0.25, 0.25]:
            model = Model()
            model.start()
            model.inputs[index] = value
            model.step()
            model.expect_off(FAILED, FEEDBACK if not math.isfinite(value) else NOMINAL_CURRENT)
    for bus in [math.nan, math.inf, 9.99, 14.01]:
        model = Model()
        model.start()
        model.inputs[3] = bus
        model.step()
        model.expect_off(FAILED, BUS)
    model = Model()
    model.start()
    model.inputs[:3] = [-0.249, 0.249, 0.0]
    assert model.step()
    CASES.append("正负三相名义电流达到阈值、非有限采样及母线越界立即关闭，不升级校准可信度")

    for mutation in ["repeat_now", "backward_now", "jump", "repeat_current", "future_current",
                     "stale_current", "future_bus", "stale_bus", "backward_bus", "changed_bus"]:
        model = Model()
        model.start()
        assert model.step()
        ticks, new_current, new_bus = 20, True, True
        if mutation == "repeat_now":
            ticks = 0
        elif mutation == "backward_now":
            ticks = -1
        elif mutation == "jump":
            ticks = 21
        elif mutation == "repeat_current":
            new_current = False
        elif mutation == "future_current":
            model.current_time = model.now + 21
            new_current = False
        elif mutation == "stale_current":
            model.current_time = 0
            new_current = False
        elif mutation == "future_bus":
            model.bus_time = model.now + 21
            new_bus = False
        elif mutation == "stale_bus":
            for _ in range(10):
                model.step()
            model.bus_time = 0
            new_bus = False
        elif mutation == "backward_bus":
            model.bus_time = 19
            new_bus = False
        elif mutation == "changed_bus":
            model.inputs[3] = 12.1
            new_bus = False
        model.step(ticks, new_current, new_bus)
        reason = FEEDBACK if mutation in ["future_current", "stale_current", "future_bus", "stale_bus"] else TIME
        model.expect_off(FAILED, reason)
    model = Model()
    model.start()
    for _ in range(10):
        assert model.step(new_bus=False)
    CASES.append("重复/倒退控制或电流时间、漏拍、未来/过期快照、同时间母线变值均失败；新鲜母线可重复")

    for mode in range(5):
        model = Model()
        model.start()
        model.cpu.mem_write(symbols["model_trig_mode"], struct.pack("<I", mode))
        result = model.step(entry="model_update_trig")
        if mode == 0:
            assert result
            model.check_reference()
        else:
            model.expect_off(FAILED, NUMERIC)
    CASES.append("可替换三角后端正常输出；NULL、失败、NaN 和单位圆异常立即拒绝")

    # 最后一段 7190->7210 仍在合法 max_step 内，但跨过 7200 的完成点并到达总期限。
    # 同拍满足完成与超时必须报告超时，不能以阶段完成掩盖 hard deadline。
    model = Model({"timeout_ticks": 7210})
    model.start()
    for _ in range(359):
        assert model.step()
    assert model.step(10)
    assert not model.step(20)
    model.expect_off(FAILED, TIMEOUT)
    CASES.append("合法单步同时跨正常结束和总期限时超时优先；无调用时仍须执行方独立 guard")

    model = Model()
    assert not model.call("model_nulls")
    model.start()
    model.step()
    assert model.call("model_alias_init")
    assert model.diag == (IDLE, NONE, 1, 0)
    assert not model.call("model_null_start")
    model.expect_off(FAILED, FEEDBACK)
    assert model.call("model_alias_init")
    model.start()
    assert not model.call("model_null_update")
    model.expect_off(FAILED, FEEDBACK)
    assert not model.call("model_null_init")
    assert model.diag == (IDLE, NONE, 0, 0)
    CASES.append("空参数、实例内配置别名重新初始化行为确定；再次试验须显式初始化")

    survey = {"profile": 1, "target_electrical_hz": 0.0, "excursion_turns": 0.25,
              "voltage_ramp_ticks": 100, "alignment_hold_ticks": 200,
              "frequency_ramp_ticks": 100, "voltage_ramp_down_ticks": 100,
              "timeout_ticks": 1100, "max_step_ticks": 2}
    model = Model(survey)
    model.start()
    states = set()
    while model.step(2):
        t = model.now - model.base_time
        states.add(model.diag[0])
        if t < 100:
            phase, voltage, hz = 0.0, 0.5 * t / 100, 0.0
        elif t < 300:
            phase, voltage, hz = 0.0, 0.5, 0.0
        elif t < 400:
            phase, voltage, hz = 0.25 * (t - 300) / 100, 0.5, 50.0
        elif t < 600:
            phase, voltage, hz = 0.25, 0.5, 0.0
        elif t < 700:
            phase, voltage, hz = 0.25 * (700 - t) / 100, 0.5, -50.0
        elif t < 900:
            phase, voltage, hz = 0.0, 0.5, 0.0
        else:
            phase, voltage, hz = 0.0, 0.5 * (1000 - t) / 100, 0.0
        assert math.isclose(model.values[5], phase, abs_tol=1e-7), (t, model.values)
        assert math.isclose(model.values[6], hz, abs_tol=1e-5)
        assert math.isclose(model.values[3], voltage * math.cos(math.tau * phase), abs_tol=1e-6)
        assert math.isclose(model.values[4], voltage * math.sin(math.tau * phase), abs_tol=1e-6)
    assert states == {1, 2, 9, 10, 11, 12, 5}
    model.expect_off(COMPLETE, NONE)
    CASES.append("静态轨迹七段与独立解析值一致，三个保持角无漂移，返回零角后缓降并撤销")
    model = Model(survey | {"excursion_turns": 1.0})
    model.start()
    while model.step(2):
        t = model.now - model.base_time
        phase = (t-300)/100 if 300 <= t < 400 else (700-t)/100 if 600 <= t < 700 else 0.0
        phase %= 1.0
        voltage = 0.5 * (t/100 if t < 100 else (1000-t)/100 if t >= 900 else 1.0)
        assert 0 <= model.values[5] < 1 and math.isclose(model.values[5], phase, abs_tol=1e-7)
        assert math.isclose(model.values[3], voltage*math.cos(math.tau*phase), abs_tol=1e-6)
        assert math.isclose(model.values[4], voltage*math.sin(math.tau*phase), abs_tol=1e-6)
    model.expect_off(COMPLETE, NONE)
    CASES.append("完整一电角圈往返在一圈端点归一化为零，解析电压、相位和关断一致")
    Model(survey | {"excursion_turns": 1.0, "max_step_ticks": 100}, valid=False)
    CASES.append("完整行程不允许单拍超过四分之一电角圈")
    for override in [{"profile": 2}, {"target_electrical_hz": 1.0}, {"excursion_turns": 0},
                     {"excursion_turns": 1.001}, {"excursion_turns": math.nan},
                     {"timeout_ticks": 1000}, {"alignment_hold_ticks": 0xFFFFFFFF}]:
        Model(survey | override, valid=False)
    Model({"excursion_turns": 0.25}, valid=False)
    CASES.append("静态模式需显式选择、零目标电频和有界角幅；不放宽原旋转模式的零频拒绝")
    for flags, reason in [(6, PREREQUISITE), (5, FEEDBACK), (15, FAULT), (31, STOP)]:
        model = Model(survey); model.start(); model.step(2); model.flags = flags; model.step(2)
        model.expect_off(STOPPED if flags == 31 else FAILED, reason)
    model = Model(survey); model.start(); model.step(2); model.inputs[0] = 0.25; model.step(2)
    model.expect_off(FAILED, NOMINAL_CURRENT)
    model = Model(survey); model.start(); model.step(2); model.step(3)
    model.expect_off(FAILED, TIME)
    CASES.append("静态模式继续兑现 Stop、故障、名义电流与漏拍立即撤销，终态不重启")

print(f"PASS: {len(CASES)} VoltageOpenLoop ARM C groups")
for case in CASES:
    print("  " + case)
print("SCOPE: 无硬件纯数值验证；不证明实物转动、电流精度、提交期限或独立保护。")
