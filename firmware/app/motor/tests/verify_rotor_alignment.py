"""真实 ARMCC C / Unicorn 静态对齐验证；独立理想转子不代表实物标定结果。"""

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
CASES = []
IDLE, RAMP, HOLD, SUCCEEDED, FAILED, STOPPED = range(6)
NONE, PREREQUISITE, TIME, FEEDBACK, BUS, OVERCURRENT, NUMERIC, TIMEOUT, FAULT, STOP = range(10)
CONFIG_FIELDS = [
    "ticks_per_second", "nominal_pole_pairs", "stator_angle_turns", "alignment_voltage_v",
    "max_voltage_ratio", "min_bus_voltage_v", "max_bus_voltage_v", "phase_current_trip_a",
    "min_excitation_current_a", "max_settle_span_turns", "max_settle_velocity_rpm",
    "ramp_duration_ticks", "minimum_hold_ticks", "settle_duration_ticks", "timeout_ticks",
    "max_step_ticks", "max_current_age_ticks", "max_position_age_ticks", "max_bus_age_ticks",
    "minimum_position_samples",
]
DEFAULT_CONFIG = dict(zip(CONFIG_FIELDS, [
    1_000_000, 7, 0.17, 1.2, 0.45, 10.0, 14.0, 0.8, 0.1, 0.0002, 0.2,
    10_000, 10_000, 20_000, 250_000, 1_000, 1_000, 4_000, 10_000, 10,
]))


def checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def circular_error(first: float, second: float) -> float:
    return abs((first - second + 0.5) % 1.0 - 0.5)


class IdealRotor:
    """合成定子电压→欧姆电流→磁转矩对象；双精度 RK4，不使用对齐模块的候选或角度。"""

    pole_pairs = 7
    resistance_ohm = 4.0
    inertia_kg_m2 = 0.00002
    damping_nm_s = 0.002
    torque_per_volt_nm = 0.01

    def __init__(self, direction: int) -> None:
        self.theta = 0.11 * math.tau
        self.omega = 0.0
        self.sensor_zero = 0.037
        self.direction = direction
        self.alpha_v = 0.0
        self.beta_v = 0.0

    def feedback(self):
        alpha = self.alpha_v / self.resistance_ohm
        beta = self.beta_v / self.resistance_ohm
        return ([alpha, -alpha / 2 + math.sqrt(3) / 2 * beta,
                 -alpha / 2 - math.sqrt(3) / 2 * beta],
                (self.direction * self.theta / math.tau + self.sensor_zero) % 1.0,
                self.direction * self.omega * 60 / math.tau)

    def advance(self, duty, requested: bool, dt=0.001):
        # 真实 C 给出的三相 duty 经公共模分量消除后才变成对象电压，不读取候选反推反馈。
        common = sum(duty) / 3
        phases = [(value - common) * 12.0 for value in duty] if requested else [0.0] * 3
        self.alpha_v = (2 * phases[0] - phases[1] - phases[2]) / 3
        self.beta_v = (phases[1] - phases[2]) / math.sqrt(3)

        def derivative(theta, omega):
            electrical = self.pole_pairs * theta
            # 静止磁偶极转矩：正交分量产生力矩，平行同向是稳定平衡点。
            torque = self.torque_per_volt_nm * (
                self.beta_v * math.cos(electrical) - self.alpha_v * math.sin(electrical))
            return omega, (torque - self.damping_nm_s * omega) / self.inertia_kg_m2

        k1 = derivative(self.theta, self.omega)
        k2 = derivative(self.theta + dt * k1[0] / 2, self.omega + dt * k1[1] / 2)
        k3 = derivative(self.theta + dt * k2[0] / 2, self.omega + dt * k2[1] / 2)
        k4 = derivative(self.theta + dt * k3[0], self.omega + dt * k3[1])
        self.theta += dt * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]) / 6
        self.omega += dt * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]) / 6


with tempfile.TemporaryDirectory(prefix="rotor-alignment-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / "rotor_alignment.c", REPOSITORY / "control/foc_math.c",
                   ROOT / "tests/rotor_alignment_model.c"]:
        obj = temporary / (source.stem + ".o")
        checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
                 "--split_sections", "-g", "-I", str(ROOT), "-c", str(source), "-o", str(obj)],
                temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=model_init", "--keep=rotor_alignment_model.o(*)", "-o", str(executable),
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
            self.config = DEFAULT_CONFIG | (overrides or {})
            self.cpu.mem_write(symbols["model_config"], struct.pack(
                "<II9f9I", *(self.config[field] for field in CONFIG_FIELDS)))
            self.now = base_time
            self.current_time = base_time
            self.position_time = base_time
            self.bus_time = base_time
            self.inputs = [0.2, -0.1, -0.1, 0.125, 0.0, 12.0]
            self.flags = 31
            assert self.call("model_init") == valid
            self.sync()

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=100000)
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, "unbounded: " + name
            return bool(struct.unpack("<I", self.cpu.mem_read(symbols["model_result"], 4))[0])

        def sync(self):
            for name in ["now", "current_time", "position_time", "bus_time"]:
                self.cpu.mem_write(symbols["model_" + name], struct.pack("<Q", getattr(self, name)))
            self.cpu.mem_write(symbols["model_inputs"], struct.pack("<6f", *self.inputs))
            self.cpu.mem_write(symbols["model_flags"], struct.pack("<I", self.flags))

        @property
        def diag(self):
            return struct.unpack("<9I", self.cpu.mem_read(symbols["model_diag"], 36))

        @property
        def values(self):
            return struct.unpack("<9f", self.cpu.mem_read(symbols["model_values"], 36))

        @property
        def candidate_times(self):
            return struct.unpack("<2Q", self.cpu.mem_read(symbols["model_candidate_times"], 16))

        def start(self, expected=True):
            self.sync()
            assert self.call("model_start") == expected, self.diag
            if expected:
                assert self.diag[:5] == (RAMP, NONE, 1, 0, 0)

        def step(self, ticks=1000, new_position=True, new_bus=True, new_current=True):
            self.now += ticks
            if new_position:
                self.position_time = self.now
            if new_bus:
                self.bus_time = self.now
            if new_current:
                self.current_time = self.now
            self.sync()
            result = self.call("model_update")
            assert result == bool(self.diag[3])
            assert all(math.isfinite(value) for value in self.values)
            assert all(0 <= value <= 1 for value in self.values[:3])
            if not result:
                assert self.values[:5] == (0.5, 0.5, 0.5, 0.0, 0.0)
            return result

        def finish(self, maximum_steps=300):
            for _ in range(maximum_steps):
                self.step()
                if self.diag[0] in [SUCCEEDED, FAILED, STOPPED]:
                    break
            return self.diag[0]

        def expect_off(self, state, reason):
            assert self.diag[:5] == (state, reason, 1, 0, 0), self.diag
            assert self.values == (0.5, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            before = self.diag
            self.step()
            assert self.diag == before
            self.start(expected=False)
            assert self.diag == before

    model = Model()
    assert model.diag[:5] == (IDLE, NONE, 1, 0, 0)
    model.step()
    assert model.diag[0] == IDLE and model.diag[3] == 0
    model.start()
    model.start(expected=False)
    for index in range(1, 10):
        assert model.step()
        voltage = math.hypot(*model.values[3:5])
        assert math.isclose(voltage, 1.2 * index / 10, abs_tol=2e-7), voltage
    assert model.step() and model.diag[0] == HOLD
    assert model.finish() == SUCCEEDED
    assert model.diag[3:5] == (0, 1)
    assert model.diag[7] >= 10 and model.diag[8] == 7
    assert model.candidate_times[1] - model.candidate_times[0] >= 20_000
    position, angle, positive, negative = model.values[5:9]
    assert circular_error(positive, (7 * position - angle) % 1) < 2e-6
    assert circular_error(negative, (-7 * position - angle) % 1) < 2e-6
    before = model.diag, model.values
    model.step(20_000)
    assert (model.diag, model.values) == before
    model.start(expected=False)
    CASES.append("默认无输出，显式 start 后有限线性缓升/保持，满足独立稳定窗口后关断并产生双方向候选")

    for poles in [1, 7, 64]:
        for angle in [0.0, 0.25, 0.9999]:
            for position in [0.0, 0.5, 0.99999]:
                model = Model({"nominal_pole_pairs": poles, "stator_angle_turns": angle})
                model.inputs[3] = position
                model.start()
                assert model.finish() == SUCCEEDED
                actual_position, actual_angle, positive, negative = model.values[5:9]
                assert 0 <= positive < 1 and 0 <= negative < 1
                assert circular_error(positive, (poles * actual_position - actual_angle) % 1) < 5e-6
                assert circular_error(negative, (-poles * actual_position - actual_angle) % 1) < 5e-6
    CASES.append("1/7/64 极对、定子角和机械边界组合均按独立双精度坐标公式形成双方向候选")

    invalids = {
        "ticks_per_second": [0], "nominal_pole_pairs": [0, 65],
        "stator_angle_turns": [-0.1, 1.0, math.nan, math.inf],
        "alignment_voltage_v": [0.0, -1.0, math.nan, math.inf, 4.6],
        "max_voltage_ratio": [0.0, 0.58, math.nan],
        "min_bus_voltage_v": [0.0, 15.0, math.nan],
        "max_bus_voltage_v": [9.0, math.inf], "phase_current_trip_a": [0.0, math.inf],
        "min_excitation_current_a": [0.0, 0.8, math.nan],
        "max_settle_span_turns": [0.0, 0.25 / 7, math.inf],
        "max_settle_velocity_rpm": [0.0, math.nan], "ramp_duration_ticks": [0],
        "minimum_hold_ticks": [0], "settle_duration_ticks": [0],
        "timeout_ticks": [40_000], "max_step_ticks": [0, 10_001],
        "minimum_position_samples": [0, 1, 65_536],
    }
    for field, values in invalids.items():
        for value in values:
            invalid = Model({field: value}, valid=False)
            invalid.start(expected=False)
            assert invalid.diag[:5] == (IDLE, NONE, 0, 0, 0), (field, value, invalid.diag)
    Model({"ramp_duration_ticks": 0xFFFFFFFF, "minimum_hold_ticks": 0xFFFFFFFF,
           "settle_duration_ticks": 0xFFFFFFFF, "timeout_ticks": 0xFFFFFFFF}, valid=False)
    CASES.append("非法数值/物理包络/时长/样本数配置拒绝，时长求和不会被 32 位溢出绕过")

    for flags, reason in [(30, PREREQUISITE), (29, PREREQUISITE), (27, FEEDBACK),
                           (23, FEEDBACK), (15, FEEDBACK), (63, FAULT), (95, STOP)]:
        for during_run in [False, True]:
            model = Model()
            if during_run:
                model.start()
                model.step()
            model.flags = flags
            if during_run:
                model.step()
            else:
                model.start(expected=False)
            model.expect_off(STOPPED if reason == STOP else FAILED, reason)
    CASES.append("显式开始及运行每拍检查主动许可/电流校准/观测有效性/故障/Stop，失败终态不自动重试")

    for state_steps in [0, 1, 15, 25, 45]:
        model = Model()
        model.start()
        for _ in range(state_steps):
            model.step()
        model.call("model_stop")
        model.expect_off(STOPPED, STOP)
    CASES.append("Stop 在 ramp、hold、稳定窗口以及成功后均撤销意图和候选")

    for kind in ["back_now", "same_now", "late_step", "old_current", "back_position", "torn_position",
                 "torn_velocity", "future_current", "future_position", "future_bus", "stale_current",
                 "stale_position", "stale_bus"]:
        model = Model(base_time=50_000)
        model.start()
        model.step()
        model.now += 1000
        model.current_time = model.position_time = model.bus_time = model.now
        if kind == "back_now":
            model.now -= 2000
            model.current_time = model.position_time = model.bus_time = model.now
        elif kind == "same_now":
            model.now -= 1000
            model.current_time = model.position_time = model.bus_time = model.now
        elif kind == "late_step":
            model.now += 1
        elif kind == "old_current":
            model.current_time -= 1000
        elif kind == "back_position":
            model.position_time -= 2000
        elif kind.startswith("torn_"):
            model.position_time -= 1000
            model.inputs[3 if kind == "torn_position" else 4] += 0.001
        elif kind.startswith("future_"):
            setattr(model, kind[7:] + "_time", model.now + 1)
        elif kind.startswith("stale_"):
            channel = kind[6:]
            setattr(model, channel + "_time", model.now - model.config["max_" + channel + "_age_ticks"] - 1)
        model.sync()
        assert not model.call("model_update")
        model.expect_off(FAILED, TIME if kind in ["back_now", "same_now", "late_step", "old_current",
                                                "back_position", "torn_position", "torn_velocity"] else FEEDBACK)
    CASES.append("控制/采集时间倒退、重复电流、撕裂位置、未来和过期快照均即时撤销输出")

    model = Model({"max_position_age_ticks": 500_000})
    model.start()
    for _ in range(250):
        model.step(new_position=False)
    model.expect_off(FAILED, TIMEOUT)
    model = Model({"max_position_age_ticks": 500_000})
    model.start()
    for _ in range(22):
        model.step()
    assert model.diag[5] == 3
    first_time = model.position_time
    for _ in range(50):
        model.step(new_position=False)
    assert model.diag[0] == HOLD and model.diag[5] == 3
    assert model.position_time == first_time
    model.step()
    assert model.diag[0] == HOLD and model.diag[5] == 4
    for _ in range(6):
        model.step()
    assert model.diag[0] == SUCCEEDED and model.diag[7] == 10
    CASES.append("重复同一位置发布不推进窗口时长或计数；持续同值但不同采集时间可以收敛")

    model = Model({"max_settle_span_turns": 0.001})
    model.inputs[3] = 0.9999
    model.start()
    expected = []
    for index in range(70):
        model.inputs[3] = (0.9999 + [0.0, 0.0002, -0.0001][index % 3]) % 1.0
        model.step()
        if model.diag[6]:
            expected.append(model.inputs[3])
        if model.diag[0] == SUCCEEDED:
            expected.append(model.inputs[3])
            break
    assert model.diag[0] == SUCCEEDED
    reference = math.atan2(sum(math.sin(math.tau * v) for v in expected),
                           sum(math.cos(math.tau * v) for v in expected)) / math.tau % 1.0
    assert circular_error(model.values[5], reference) < 2e-7
    CASES.append("跨机械零点的小幅抖动按圆差和圆均值判断，不误认为一整圈跨度")

    for disturbance in ["drift", "velocity", "no_current"]:
        model = Model()
        model.start()
        for index in range(35):
            if disturbance == "drift":
                model.inputs[3] = (0.125 + index * 0.00003) % 1
            elif disturbance == "velocity":
                model.inputs[4] = 0.3
            else:
                model.inputs[:3] = [0.0] * 3
            model.step()
        assert model.diag[0] == HOLD and model.diag[4] == 0
        model.inputs[:5] = [0.2, -0.1, -0.1, 0.125, 0.0]
        assert model.finish() == SUCCEEDED
    model = Model()
    model.start()
    for _ in range(30):
        model.step()
    assert model.diag[5] == 11
    model.inputs[:3] = [0.0] * 3
    model.step(new_position=False)
    assert model.diag[6] == 0
    model.inputs[:3] = [0.2, -0.1, -0.1]
    model.step()
    assert model.diag[5] == 1
    CASES.append("慢漂移用完整窗口跨度拒绝，速度/励磁不满足重开窗口；两次位置之间励磁丢失也不能沿用旧窗口")

    for common_mode in [0.2, -0.2]:
        model = Model()
        model.inputs[:3] = [common_mode] * 3
        model.start()
        assert model.finish() == FAILED
        model.expect_off(FAILED, TIMEOUT)
    model = Model()
    model.inputs[:3] = [0.5, 0.2, 0.2]
    model.start()
    assert model.finish() == SUCCEEDED
    CASES.append("正负三相公共模的实际 αβ 为零，不能满足最小励磁；公共模叠加真实定子矢量仍可收敛")

    invalid_inputs = [([math.nan, 0.0, 0.0], None, FEEDBACK),
                      ([0.0, math.inf, 0.0], None, FEEDBACK),
                      ([0.0, 0.0, -math.inf], None, FEEDBACK)]
    for phase in range(3):
        for sign in [-1, 1]:
            values = [0.0] * 3
            values[phase] = sign * struct.unpack("<f", struct.pack("<f", 0.8))[0]
            invalid_inputs.append((values, None, OVERCURRENT))
    for values, _, reason in invalid_inputs:
        model = Model()
        model.start()
        model.step()
        model.inputs[:3] = values
        model.step()
        model.expect_off(FAILED, reason)
    for index, value, reason in [(3, math.nan, FEEDBACK), (3, -0.1, FEEDBACK), (3, 1.0, FEEDBACK),
                                 (4, math.nan, FEEDBACK), (5, math.nan, BUS), (5, 9.99, BUS),
                                 (5, 14.01, BUS)]:
        model = Model()
        model.start()
        model.inputs[index] = value
        model.step()
        model.expect_off(FAILED, reason)
    CASES.append("Ia/Ib/Ic 任一正负过流达到阈值即失败，坏数值/位置范围/母线包络不产生可用 duty")

    for base in [0x100000000 + 123, 0xFFFFFFFFFFFFFFFF - 300_000]:
        model = Model(base_time=base)
        model.start()
        assert model.finish() == SUCCEEDED
        assert model.candidate_times[0] >= base
        assert model.candidate_times[1] - model.candidate_times[0] >= 20_000
    model = Model({"minimum_position_samples": 1000, "timeout_ticks": 50_000})
    model.start()
    assert model.finish() == FAILED
    model.expect_off(FAILED, TIMEOUT)
    model = Model({"timeout_ticks": 50_000})
    model.inputs[:3] = [0.0] * 3
    model.start()
    for _ in range(29):
        model.step()
    model.inputs[:3] = [0.2, -0.1, -0.1]
    for _ in range(21):
        model.step()
    model.expect_off(FAILED, TIMEOUT)
    CASES.append("64 位绝对时间跨 32 位和接近上界仍正确；总时限优先终止，采样数不够不能假成功")

    for direction in [1, -1]:
        model = Model({"ramp_duration_ticks": 50_000, "minimum_hold_ticks": 50_000,
                       "settle_duration_ticks": 50_000, "timeout_ticks": 1_000_000})
        rotor = IdealRotor(direction)
        currents, position, velocity = rotor.feedback()
        model.inputs[:5] = [*currents, position, velocity]
        model.start()
        peak_current = 0.0
        for _ in range(1000):
            currents, position, velocity = rotor.feedback()
            model.inputs[:5] = [*currents, position, velocity]
            model.step()
            peak_current = max(peak_current, *map(abs, currents))
            if model.diag[0] in [SUCCEEDED, FAILED]:
                break
            rotor.advance(model.values[:3], bool(model.diag[3]))
        assert model.diag[0] == SUCCEEDED, (direction, model.diag, rotor.theta, rotor.omega)
        true_offset = direction * rotor.pole_pairs * rotor.sensor_zero % 1.0
        candidate = model.values[7 if direction == 1 else 8]
        assert circular_error(candidate, true_offset) < 0.0005, (candidate, true_offset)
        assert peak_current < 0.31
        assert model.diag[3] == 0 and model.diag[4] == 1
    CASES.append("真实三相 duty 驱动独立双精度 RK4 理想转子，双编码器方向均给出一致零位候选且结束撤销输出")

    # 即使无运动的受阻转子也可能满足静态条件，专门保留此例说明候选不是物理标定证书。
    model = Model({"nominal_pole_pairs": 11})
    model.start()
    assert model.finish() == SUCCEEDED and model.diag[8] == 11
    assert model.values[5] == 0.125
    CASES.append("堵转或错误标称极对数也可能形成候选：结果只记录假设，不确认方向/极对数且不自动安装")

    model = Model()
    assert not model.call("model_nulls")
    model.start()
    model.step()
    before = model.diag, model.values
    assert model.call("model_other_instance")
    assert (model.diag, model.values) == before
    assert model.call("model_alias_init")
    assert model.diag[:5] == (IDLE, NONE, 1, 0, 0)
    assert not model.call("model_null_start")
    model.expect_off(FAILED, FEEDBACK)
    assert model.call("model_alias_init")
    model.start()
    model.step()
    assert not model.call("model_null_update")
    model.expect_off(FAILED, FEEDBACK)
    assert not model.call("model_null_init")
    assert model.diag[:5] == (IDLE, NONE, 0, 0, 0)
    CASES.append("空参数、实例内配置别名重配和双实例行为确定，重新试验必须显式初始化")

print(f"PASS: {len(CASES)} RotorAlignment ARM C / ideal rotor groups")
for case in CASES:
    print("  " + case)
print("SCOPE: 纯软件候选；未连接 Target，不证明真实电机参数、实时截止、相序或实机安全。")
