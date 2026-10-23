"""ARMCC + Unicorn 执行正式电流 PI；数值仿真不表示电机实测。"""

from pathlib import Path
import math
import os
import random
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4


ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
DEFAULT_CONFIG = [2.0, 2.5, 40.0, 50.0, 80.0, 8.0, 3.0, 0.45, 0.002]
DEFAULT_INPUT = [0.0, 0.0, 0.0, 1.0, 12.0, 0.0, 0.0, 0.00005]


def passed(name: str) -> None:
    CASES.append(name)


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def close(actual, expected, tolerance=3e-5):
    assert math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance), (actual, expected)


def reference(config, inputs, history, actuation=None):
    """双精度离散参考；输入/配置先量化为实际传给 C 的 float32。"""
    kp_d, kp_q, ki_d, ki_q, kaw, i_limit, _, ratio, _ = config
    alpha, beta, sine, cosine, bus, target_d, target_q, dt = inputs
    norm = math.hypot(sine, cosine)
    sine, cosine = sine / norm, cosine / norm
    actuation_sine, actuation_cosine = sine, cosine
    if actuation is not None:
        norm = math.hypot(*actuation)
        actuation_sine, actuation_cosine = [value / norm for value in actuation]
    measured_d = alpha * cosine + beta * sine
    measured_q = -alpha * sine + beta * cosine
    error_d, error_q = target_d - measured_d, target_q - measured_q
    requested_d = kp_d * error_d + history[0]
    requested_q = kp_q * error_q + history[1]
    alpha_v = requested_d * actuation_cosine - requested_q * actuation_sine
    beta_v = requested_d * actuation_sine + requested_q * actuation_cosine
    magnitude = math.hypot(alpha_v, beta_v)
    voltage_limited = magnitude > bus * ratio
    scale = bus * ratio / magnitude if voltage_limited else 1.0
    alpha_v *= scale
    beta_v *= scale
    applied_d = alpha_v * actuation_cosine + beta_v * actuation_sine
    applied_q = -alpha_v * actuation_sine + beta_v * actuation_cosine
    phases = [alpha_v / bus,
              (-alpha_v * 0.5 + math.sqrt(3.0) * beta_v * 0.5) / bus,
              (-alpha_v * 0.5 - math.sqrt(3.0) * beta_v * 0.5) / bus]
    common = (max(phases) + min(phases)) * 0.5
    duties = [0.5 + phase - common for phase in phases]
    next_history = [history[0] + dt * (ki_d * error_d + kaw * (applied_d - requested_d)),
                    history[1] + dt * (ki_q * error_q + kaw * (applied_q - requested_q))]
    integral_limited = any(abs(value) > i_limit for value in next_history)
    next_history = [max(-i_limit, min(i_limit, value)) for value in next_history]
    return ([measured_d, measured_q, requested_d, requested_q, applied_d, applied_q,
             *duties, *next_history], voltage_limited, integral_limited)


with tempfile.TemporaryDirectory(prefix="current-controller-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / 'foc_math.c', ROOT / 'current_controller.c',
                   ROOT / 'tests/current_controller_model.c']:
        obj = temporary / (source.stem + ".o")
        run_checked(
            [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3", "-Otime",
             "--split_sections", "-g", "-I", str(ROOT), "-c", str(source), "-o", str(obj)],
            temporary,
        )
        objects.append(str(obj))
    executable = temporary / "model.axf"
    run_checked(
        [str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
         "--entry=model_init", "--keep=current_controller_model.o(*)", "-o", str(executable),
         *objects], temporary,
    )
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(section["p_vaddr"], section.data()) for section in elf.iter_segments()
                    if section["p_type"] == "PT_LOAD"]
        initializers = [(section["sh_addr"], section.data()) for section in elf.iter_sections()
                        if (section["sh_flags"] & 2) and section["sh_type"] == "SHT_PROGBITS"]
        symbols = {symbol.name: symbol["st_value"]
                   for symbol in elf.get_section_by_name(".symtab").iter_symbols()}

    # 对应 Target Control 组的实际速度优化选项，确认标准分类没有退回逐项库调用。
    assert "__ARM_isfinitef" not in symbols, "热控制链重新引入逐项分类库调用"
    passed("显式 O3/Otime 与 Target Control 组策略一致，标准 finite 分类保持内联")

    class Model:
        def __init__(self, config=None):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.values = [0.0] * 11
            self.status = [0] * 5
            self.configure(config or DEFAULT_CONFIG)

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=100000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, name
            self.values = self.read("model_values", "11f")
            self.status = self.read("model_status", "5I")

        def read(self, name, fmt):
            size = struct.calcsize("<" + fmt)
            return list(struct.unpack("<" + fmt, self.cpu.mem_read(symbols[name], size)))

        def write(self, name, values, fmt):
            self.cpu.mem_write(symbols[name], struct.pack("<" + fmt, *values))

        def select(self, index):
            self.write("model_selector", [index], "I")

        def configure(self, config):
            self.config = list(map(float32, config))
            self.write("model_config", self.config, "9f")
            self.call("model_init")
            return bool(self.status[0])

        def update(self, inputs=None, flags=None, separate_actuation=False, actuation=None):
            self.inputs = list(map(float32, inputs or DEFAULT_INPUT))
            self.separate_actuation = separate_actuation
            self.actuation = list(map(float32, [0.0, 1.0] if actuation is None else actuation))
            self.write("model_inputs", self.inputs, "8f")
            self.write("model_input_valid", flags or [1, 1, 1, 1], "4I")
            self.write("model_separate_actuation", [int(separate_actuation)], "I")
            self.write("model_actuation_angle", self.actuation, "2f")
            self.call("model_update")
            assert self.status[0] == self.status[1]
            assert all(math.isfinite(value) for value in self.values)
            if not self.status[0]:
                self.expect_invalid(bool(self.status[4]))
            else:
                assert all(0.0 <= duty <= 1.0 for duty in self.values[6:9])
            return bool(self.status[0])

        def expect_invalid(self, configured=True, neutral=True):
            assert self.status == [0, 0, 0, 0, int(configured)], self.status
            assert self.values[:6] == [0.0] * 6, self.values
            if neutral:
                assert self.values[6:9] == [0.5] * 3, self.values
            assert self.values[9:] == [0.0, 0.0], self.values

        def check_reference(self, history):
            expected, voltage_limited, integral_limited = reference(
                self.config, self.inputs, history, self.actuation if self.separate_actuation else None)
            for actual, desired in zip(self.values, expected):
                close(actual, desired)
            assert bool(self.status[2]) == voltage_limited
            assert bool(self.status[3]) == integral_limited
            return expected[9:]

        def seed_history(self):
            inputs = list(DEFAULT_INPUT)
            inputs[5:7] = [0.3, -0.4]
            assert self.update(inputs)
            assert all(self.values[9:])

    model = Model()
    assert model.update()
    assert model.values == [0.0] * 6 + [0.5] * 3 + [0.0, 0.0]
    inputs = [0.4, -0.2, 1.0, 0.0, 12.0, 0.3, -0.1, 0.00005]
    assert model.update(inputs)
    model.check_reference([0.0, 0.0])
    close(model.values[0], -0.2)
    close(model.values[1], -0.4)
    passed("零误差产生中性占空比；Park 正方向、两轴比例电压与单位一致")

    for direct, quadrature, flags in [(1., 0., [1, 0]), (0., 1., [0, 1]), (1., 1., [1, 1])]:
        model = Model([0., 0., 1000., 1000., 0., .01, 3., .45, .002])
        inputs = list(DEFAULT_INPUT); inputs[5:7] = [direct, quadrature]
        assert model.update(inputs)
        model.check_reference([0., 0.])
        assert model.read('model_axis_limited', '2I') == flags
        assert bool(model.status[3]) == any(flags)
        assert not model.update(inputs, [0, 1, 1, 1])
        assert model.read('model_axis_limited', '2I') == [0, 0]
    passed('新增d/q独立积分限幅标志对应原判据，数值与合并标志不变，失效时两位撤销')

    model = Model()
    for index in range(120):
        history = model.values[9:]
        theta = index * 0.02
        inputs = [0.02 * math.cos(theta), 0.02 * math.sin(theta),
                  math.sin(theta), math.cos(theta), 24.0, 0.3, -0.2, 0.0001]
        assert model.update(inputs)
        model.check_reference(history)
        assert not model.status[2] and not model.status[3]
    passed("未限幅 PI 用上拍积分形成本拍电压，再提交下拍积分；双轴离散参考一致")

    model = Model([20.0, 20.0, 100.0, 100.0, 80.0, 8.0, 3.0, 0.45, 0.002])
    for index in range(180):
        theta = index * 2.0 * math.pi / 180.0
        bus = 6.0 + index % 12
        inputs = [0.0, 0.0, math.sin(theta), math.cos(theta), bus,
                  1.2, -1.6, 0.00005]
        history = model.values[9:]
        assert model.update(inputs)
        model.check_reference(history)
        close(math.hypot(*model.values[4:6]), float32(bus) * model.config[7], 5e-5)
        assert model.status[2] == 1
        duties = model.values[6:9]
        reconstructed_alpha = (2.0 * duties[0] - duties[1] - duties[2]) * bus / 3.0
        reconstructed_beta = (duties[1] - duties[2]) * bus / math.sqrt(3.0)
        sine, cosine = model.inputs[2:4]
        close(reconstructed_alpha * cosine + reconstructed_beta * sine, model.values[4])
        close(-reconstructed_alpha * sine + reconstructed_beta * cosine, model.values[5])
    passed("跨六扇区电压圆限幅保持方向；占空比可重构实际电压，回算使用 applied DQ")

    for norm_squared in [0.999001, 0.9992, 1.0008, 1.000999]:
        for theta in [0.0, 0.73, math.pi * 0.5, -2.1]:
            model = Model([1.0, 1.0, 0.0, 0.0, 100.0, 8.0, 3.0, 0.45, 0.002])
            phasor_scale = math.sqrt(norm_squared)
            inputs = [0.0, 0.0, math.sin(theta) * phasor_scale,
                      math.cos(theta) * phasor_scale, 12.0, 1.0, -0.5, 0.00005]
            for _ in range(1000):
                assert model.update(inputs)
                assert model.status[2] == 0
                close(model.values[4], model.values[2], 5e-7)
                close(model.values[5], model.values[3], 5e-7)
                assert max(map(abs, model.values[9:])) < 2e-6, model.values
    passed("单位圆上下容差内相量先归一化；Ki=0 多拍不产生模长误差导致的虚假回算")

    for norm_squared in [0.999001, 1.000999]:
        model = Model([8.0, 8.0, 0.0, 0.0, 100.0, 8.0, 3.0, 0.45, 0.002])
        reference_history = [0.0, 0.0]
        for index in range(360):
            theta = index * math.pi / 180.0
            scale = math.sqrt(norm_squared)
            inputs = [0.2, -0.1, math.sin(theta) * scale, math.cos(theta) * scale,
                      12.0, 1.2, -1.6, 0.00005]
            assert model.update(inputs)
            reference_history = model.check_reference(reference_history)
            assert model.status[2] == 1
            assert math.hypot(*model.values[4:6]) <= 12.0 * model.config[7] + 2e-6
    passed("上下单位圆容差及旋转角度下，饱和 applied DQ 守住电压圆并匹配归一化双精度参考")

    for targets, accepted in [((3.0, 0.0), True), ((0.0, -3.0), True),
                              ((1.8, 2.4), True), ((3.0001, 0.0), False),
                              ((2.2, 2.2), False), ((0.0, -3.01), False)]:
        model = Model()
        inputs = list(DEFAULT_INPUT)
        inputs[5:7] = targets
        assert model.update(inputs) == accepted
    passed("Id/Iq 目标按完整二维矢量限制，越界拒绝而非静默缩放")

    for bad_dt in [0.0, -0.001, math.nan, math.inf, -math.inf, 0.002001]:
        model = Model()
        inputs = list(DEFAULT_INPUT)
        inputs[5:7] = [0.3, 0.4]
        assert model.update(inputs)
        assert any(model.values[9:])
        inputs[7] = bad_dt
        assert not model.update(inputs)
        inputs[7] = 0.00005
        assert model.update(inputs)
        model.check_reference([0.0, 0.0])
    model = Model()
    inputs = list(DEFAULT_INPUT)
    inputs[7] = model.config[8]
    assert model.update(inputs)
    passed("非法与超时 dt 清两轴历史；等于最大 dt 合法，恢复从零积分开始")

    for validity in range(4):
        model = Model()
        model.seed_history()
        flags = [1] * 4
        flags[validity] = 0
        assert not model.update(flags=flags)
    for field in range(8):
        for bad_value in [math.nan, math.inf, -math.inf]:
            model = Model()
            model.seed_history()
            inputs = list(DEFAULT_INPUT)
            inputs[field] = bad_value
            assert not model.update(inputs)
    for sine, cosine in [(0.0, 0.0), (1.0, 1.0), (0.0, 1.01)]:
        inputs = list(DEFAULT_INPUT)
        inputs[2:4] = [sine, cosine]
        assert not Model().update(inputs)
    for bus in [0.0, -12.0]:
        inputs = list(DEFAULT_INPUT)
        inputs[4] = bus
        assert not Model().update(inputs)
    passed("测量/角度/母线/完整目标有效位、非有限值与非单位角度均 fail closed")

    for field in range(9):
        for bad_value in [math.nan, math.inf, -math.inf, -1.0]:
            config = list(DEFAULT_CONFIG)
            config[field] = bad_value
            model = Model(config)
            assert not model.status[0] and not model.status[4]
            assert not model.update()
            model.expect_invalid(False)
    for field in range(5, 9):
        config = list(DEFAULT_CONFIG)
        config[field] = 0.0
        assert not Model(config).status[0]
    config = list(DEFAULT_CONFIG)
    config[7] = 0.57736
    assert not Model(config).status[0]
    model = Model([0.0] * 5 + DEFAULT_CONFIG[5:])
    assert model.update()
    model.call("model_null_config")
    assert model.status[4] == 0
    assert not model.update()
    assert model.configure(DEFAULT_CONFIG)
    model.call("model_alias_config")
    assert model.status[0] and model.status[4]
    assert model.values[9:] == [0.0, 0.0]
    passed("配置逐字段拒绝非法范围；零增益合法，空配置清状态，配置别名可安全重初始化")

    for field in range(9):
        for bad_value in [math.nan, math.inf, -math.inf, -1.0]:
            model = Model()
            model.seed_history()
            changed = list(DEFAULT_CONFIG)
            changed[field] = bad_value
            model.write("model_config", changed, "9f")
            model.call("model_replace_config")
            model.expect_invalid()
    model = Model()
    model.seed_history()
    history = model.values[9:]
    model.config = list(map(float32, [3.0, 4.0, 40.0, 50.0, 80.0, 8.0, 3.0, 0.45, 0.002]))
    model.write("model_config", model.config, "9f")
    model.call("model_replace_config")
    assert model.status[0] and model.status[4]
    model.check_reference(history)
    passed("init之后直接改写公开配置仍逐字段拒绝坏值并清历史，合法新配置当拍生效而不使用旧缓存")

    for edge in [math.sqrt(0.999), math.sqrt(1.001)]:
        center = struct.unpack("<I", struct.pack("<f", edge))[0]
        for bits in range(center - 3, center + 4):
            cosine = struct.unpack("<f", struct.pack("<I", bits))[0]
            norm = float32(cosine * cosine)
            expected = abs(float32(norm - 1.0)) <= float32(0.001)
            for separate in [False, True]:
                model = Model()
                inputs = list(DEFAULT_INPUT)
                inputs[0:2] = [0.2, -0.3]
                inputs[5:7] = [0.7, -0.2]
                if separate:
                    accepted = model.update(inputs, separate_actuation=True, actuation=[0.0, -cosine])
                else:
                    inputs[2:4] = [0.0, cosine]
                    accepted = model.update(inputs)
                assert accepted == expected, (bits, separate, norm, model.status)
                if accepted:
                    model.check_reference([0.0, 0.0])
    passed("单位圆上下精确边界附近相邻float按原始norm判定，测量与独立施加角均不因去重而扩大接受域")

    for function in ["model_null_controller", "model_null_input"]:
        model = Model()
        assert model.update()
        model.call(function)
        model.expect_invalid()
    model = Model()
    model.call("model_null_output")
    model.expect_invalid(neutral=False)
    passed("空实例/输入/输出安全返回；有实例时清除历史，非空输出始终确定")

    for history in [(math.nan, 1.0), (1.0, math.inf), (9.0, 0.0), (0.0, -9.0)]:
        model = Model()
        model.write("model_history", history, "2f")
        model.call("model_corrupt_history")
        model.expect_invalid()
    config = list(DEFAULT_CONFIG)
    config[3] = 3.0e38
    model = Model(config)
    inputs = list(DEFAULT_INPUT)
    inputs[5:7] = [0.1, 2.0]
    assert not model.update(inputs)
    model.expect_invalid()
    inputs[5:7] = [0.1, 0.0]
    assert model.update(inputs)
    model.check_reference([0.0, 0.0])
    inputs = list(DEFAULT_INPUT)
    inputs[0:2] = [3.0e38, 3.0e38]
    inputs[2:4] = [math.sqrt(0.5), math.sqrt(0.5)]
    assert not Model().update(inputs)
    passed("历史损坏、极值乘加溢出拒绝；q 轴候选失败不会残留已计算的 d 轴积分")

    config = [3.0, 3.0, 800.0, 800.0, 0.0, 0.1, 3.0, 0.45, 0.002]
    model = Model(config)
    inputs = list(DEFAULT_INPUT)
    inputs[5:7] = [1.0, -1.0]
    for _ in range(10):
        assert model.update(inputs)
    assert model.status[3] == 1 and model.status[2] == 0
    close(model.values[9], 0.1)
    close(model.values[10], -0.1)
    passed("积分硬上限同时约束正负两轴，并与电压圆限幅标志独立")

    no_tracking = Model([2.0, 2.0, 80.0, 80.0, 0.0, 20.0, 3.0, 0.45, 0.002])
    tracking = Model([2.0, 2.0, 80.0, 80.0, 120.0, 20.0, 3.0, 0.45, 0.002])
    inputs = list(DEFAULT_INPUT)
    inputs[4:8] = [2.0, 0.0, 2.0, 0.001]
    for _ in range(200):
        assert no_tracking.update(inputs)
        assert tracking.update(inputs)
    assert no_tracking.values[10] > 19.9
    assert -2.0 < tracking.values[10] < -1.0
    inputs[4:8] = [24.0, 0.0, 0.0, 0.001]
    assert no_tracking.update(inputs)
    assert tracking.update(inputs)
    assert abs(tracking.values[5]) < abs(no_tracking.values[5]) * 0.2
    passed("持续饱和时 back-calculation 抑制积分累积；解除饱和后残留电压显著小于无跟踪实例")

    model = Model()
    inputs = list(DEFAULT_INPUT)
    inputs[5:7] = [0.5, -0.5]
    assert model.update(inputs)
    first_history = model.values[9:]
    model.select(1)
    assert model.configure(DEFAULT_CONFIG)
    for _ in range(10):
        assert model.update(inputs)
    assert model.values[9:] != first_history
    model.select(0)
    assert model.update()
    for actual, expected in zip(model.values[2:4], first_history):
        close(actual, expected)
    model.call("model_reset")
    assert model.values[9:] == [0.0, 0.0] and model.status[4]
    assert model.update()
    assert model.values[:6] == [0.0] * 6
    passed("多实例积分相互独立；显式 reset 保留配置并清空两轴历史")

    model = Model()
    randomizer = random.Random(4316701)
    reference_history = [0.0, 0.0]
    for _ in range(1000):
        theta = randomizer.uniform(-math.pi, math.pi)
        radius = randomizer.uniform(0.0, 2.9)
        target_theta = randomizer.uniform(-math.pi, math.pi)
        inputs = [randomizer.uniform(-1.0, 1.0), randomizer.uniform(-1.0, 1.0),
                  math.sin(theta), math.cos(theta), randomizer.uniform(3.0, 24.0),
                  radius * math.cos(target_theta), radius * math.sin(target_theta),
                  randomizer.uniform(0.00003, 0.0015)]
        assert model.update(inputs)
        reference_history = model.check_reference(reference_history)
    passed("1000 拍变化电角度/母线/完整目标/时间间隔对照双精度离散参考")

    # 简化静止 dq 的解耦 RL 数值对象，不含反电动势、交叉耦合与真实采样链路。
    config = [0.6, 0.6, 300.0, 300.0, 500.0, 6.0, 3.0, 0.45, 0.0001]
    model = Model(config)
    current_d, current_q = 0.0, 0.0
    resistance, inductance = 2.0, 0.001
    target_sequences = [(0.0, 1.0), (0.5, -0.8), (-0.5, 0.0), (0.0, 0.0)]
    maximum_error = 0.0
    for target_d, target_q in target_sequences:
        for index in range(1600):
            dt = [0.00004, 0.00005, 0.00006][index % 3]
            theta = index * 0.05
            sine, cosine = math.sin(theta), math.cos(theta)
            inputs = [current_d * cosine - current_q * sine,
                      current_d * sine + current_q * cosine,
                      sine, cosine, 12.0, target_d, target_q, dt]
            assert model.update(inputs)
            decay = math.exp(-resistance * dt / inductance)
            current_d = current_d * decay + model.values[4] / resistance * (1.0 - decay)
            current_q = current_q * decay + model.values[5] / resistance * (1.0 - decay)
            assert math.hypot(current_d, current_q) < 1.3
            assert math.hypot(*model.values[4:6]) <= 12.0 * model.config[7] + 1e-5
            maximum_error = max(maximum_error, abs(current_d - target_d), abs(current_q - target_q))
        close(current_d, target_d, 0.002)
        close(current_q, target_q, 0.002)
    assert maximum_error < 1.8
    passed("简化解耦 RL 数值对象在正负阶跃、变 dt 与旋转观测坐标下收敛；仅验证算法行为")

    inputs = [0.25, -0.4, 0.0, 1.0, 12.0, 0.75, 0.6, 0.00005]
    separate = Model()
    same = Model()
    assert separate.update(inputs, separate_actuation=True, actuation=[1.0, 0.0])
    assert same.update(inputs)
    separate.check_reference([0.0, 0.0])
    close(separate.values[0], 0.25)
    close(separate.values[1], -0.4)
    for actual, previous in zip(separate.values[:6], same.values[:6]):
        close(actual, previous)
    assert any(abs(actual - previous) > 0.1 for actual, previous in zip(separate.values[6:9], same.values[6:9]))
    duty_a, duty_b, duty_c = separate.values[6:9]
    alpha_v = (2 * duty_a - duty_b - duty_c) * 12 / 3
    beta_v = (duty_b - duty_c) * 12 / math.sqrt(3)
    close(alpha_v, -separate.values[3])
    close(beta_v, separate.values[2])
    passed("测量角不随施加角改变，独立90度施加角旋转实际 duty，回DQ仍属于请求电压坐标")

    config = [1.0, 1.0, 0.0, 0.0, 100.0, 8.0, 3.0, 0.45, 0.002]
    for norm_squared in [0.999001, 1.0, 1.000999]:
        model = Model(config)
        for index in range(1200):
            measured_theta = index * 0.07
            actuation_theta = measured_theta + 0.3 + 0.2 * math.sin(index * 0.04)
            scale = math.sqrt(norm_squared)
            inputs = [0.0, 0.0, math.sin(measured_theta), math.cos(measured_theta),
                      12.0, 1.0, -0.5, 0.00005]
            assert model.update(inputs, separate_actuation=True,
                                actuation=[math.sin(actuation_theta) * scale, math.cos(actuation_theta) * scale])
            assert not model.status[2]
            close(model.values[4], model.values[2], 7e-7)
            close(model.values[5], model.values[3], 7e-7)
            assert max(map(abs, model.values[9:])) < 3e-6, model.values
    passed("不同测量/施加角与施加相量上下容差下，Ki=0不把坐标差或模长误差变成虚假回算积分")

    for actuation in [[0.0, 0.0], [1.0, 1.0], [0.0, 1.01],
                      [math.nan, 1.0], [0.0, math.inf], [-math.inf, 0.0]]:
        model = Model()
        model.seed_history()
        assert not model.update(separate_actuation=True, actuation=actuation)
        model.expect_invalid()
        assert model.update(separate_actuation=True, actuation=[1.0, 0.0])
        model.check_reference([0.0, 0.0])
    model = Model()
    model.seed_history()
    inputs = list(DEFAULT_INPUT)
    inputs[2:4] = [0.0, 0.0]
    assert not model.update(inputs, separate_actuation=True, actuation=[1.0, 0.0])
    model.expect_invalid()
    passed("独立施加角的NaN/Inf/非单位圆无效，测量角也必须有效；失败清双积分并可从零恢复")

    inputs = [0.2, -0.1, 0.0, 1.0, 12.0, 0.8, -0.4, 0.00005]
    for ignored in [[math.nan, math.inf], [0.0, 0.0], [10.0, -10.0], [-math.inf, math.nan]]:
        model = Model()
        assert model.update(inputs, separate_actuation=False, actuation=ignored)
        model.check_reference([0.0, 0.0])
    legacy = Model()
    legacy.call("model_legacy_input")
    model = Model()
    assert model.update(inputs)
    for actual, expected in zip(legacy.values, model.values):
        close(actual, expected)
    passed("关闭独立施加角时忽略附加相量包括NaN/Inf，真实旧聚合初始化继续走同角路径")

    model = Model([8.0, 10.0, 20.0, 30.0, 80.0, 8.0, 3.0, 0.45, 0.002])
    history = [0.0, 0.0]
    randomizer = random.Random(4316702)
    for index in range(1200):
        measured_theta = randomizer.uniform(-math.pi, math.pi)
        actuation_theta = measured_theta + randomizer.uniform(-0.8, 0.8)
        measured_scale = math.sqrt([0.99901, 1.0, 1.00099][index % 3])
        actuation_scale = math.sqrt([1.00099, 1.0, 0.99901][index % 3])
        inputs = [randomizer.uniform(-0.3, 0.3), randomizer.uniform(-0.3, 0.3),
                  math.sin(measured_theta) * measured_scale, math.cos(measured_theta) * measured_scale,
                  randomizer.uniform(2.0, 8.0), 1.2, -1.6, randomizer.uniform(0.00003, 0.001)]
        assert model.update(inputs, separate_actuation=True,
                            actuation=[math.sin(actuation_theta) * actuation_scale, math.cos(actuation_theta) * actuation_scale])
        history = model.check_reference(history)
        assert model.status[2]
    passed("1200拍独立双角、模长容差、母线和变dt饱和过程匹配双精度参考，回算使用施加坐标")

for name in CASES:
    print("PASS", name)
print(f"Current controller: {len(CASES)} groups passed (ARMCC + Unicorn; numerical only)")
