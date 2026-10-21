"""ARMCC + Unicorn 执行正式速度 PI；机械模型只用于数值验证。"""

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
DEFAULT_CONFIG = [0.002, 0.01, 50.0, 0.5, 0.8, 3000.0, 0.005]
DEFAULT_INPUT = [0.0, 0.0, 0.001]
CASES = []


def passed(name):
    CASES.append(name)


def run_checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def close(actual, expected, tolerance=3e-5):
    assert math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance), (actual, expected)


def reference(config, inputs, history):
    """独立双精度离散方程；配置/输入按 C 接口的 float32 量化。"""
    kp, ki, kaw, iq_limit, integral_limit, _, _ = config
    target, measured, dt = inputs
    error = target - measured
    requested = kp * error + history
    iq = max(-iq_limit, min(iq_limit, requested))
    next_integral = history + dt * (ki * error + kaw * (iq - requested))
    integral_limited = abs(next_integral) > integral_limit
    return ([requested, iq, max(-integral_limit, min(integral_limit, next_integral))],
            abs(requested) > iq_limit, integral_limited)


with tempfile.TemporaryDirectory(prefix="speed-controller-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / "speed_controller.c", ROOT / "tests/speed_controller_model.c"]:
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
         "--entry=model_init", "--keep=speed_controller_model.o(*)", "-o", str(executable),
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
            self.values = [0.0] * 3
            self.status = [0] * 5
            self.configure(DEFAULT_CONFIG if config is None else config)

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=100000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, name
            self.values = self.read("model_values", "3f")
            self.status = self.read("model_status", "5I")

        def read(self, name, fmt):
            size = struct.calcsize("<" + fmt)
            return list(struct.unpack("<" + fmt, self.cpu.mem_read(symbols[name], size)))

        def write(self, name, values, fmt):
            self.cpu.mem_write(symbols[name], struct.pack("<" + fmt, *values))

        def configure(self, config):
            self.config = list(map(float32, config))
            self.write("model_config", self.config, "7f")
            self.call("model_init")
            return bool(self.status[0])

        def update(self, inputs=None, flags=None):
            self.inputs = list(map(float32, DEFAULT_INPUT if inputs is None else inputs))
            self.write("model_inputs", self.inputs, "3f")
            self.write("model_input_valid", [1, 1] if flags is None else flags, "2I")
            self.call("model_update")
            assert self.status[0] == self.status[1]
            assert all(math.isfinite(value) for value in self.values)
            if self.status[0]:
                assert abs(self.values[1]) <= self.config[3]
                assert abs(self.values[2]) <= self.config[4]
            else:
                self.expect_invalid(bool(self.status[4]))
            return bool(self.status[0])

        def expect_invalid(self, configured=True):
            assert self.values == [0.0] * 3, self.values
            assert self.status == [0, 0, 0, 0, int(configured)], self.status

        def check_reference(self, history):
            expected, current_limited, integral_limited = reference(self.config, self.inputs, history)
            for actual, desired in zip(self.values, expected):
                close(actual, desired)
            assert bool(self.status[2]) == current_limited
            assert bool(self.status[3]) == integral_limited
            return expected[2]

        def seed_history(self):
            assert self.update([100.0, 0.0, 0.001])
            assert self.values[2] > 0.0

    model = Model()
    assert model.update()
    assert model.values == [0.0] * 3
    assert model.update([100.0, 40.0, 0.001])
    model.check_reference(0.0)
    close(model.values[0], 0.12)
    close(model.values[1], 0.12)
    close(model.values[2], 0.0006)
    passed("零误差输出零；机械转速正方向、A/rpm 与积分单位一致")

    model = Model()
    history = 0.0
    for index in range(200):
        inputs = [50.0, 20.0 + math.sin(index * 0.1), 0.001]
        assert model.update(inputs)
        history = model.check_reference(history)
        assert not model.status[2] and not model.status[3]
    passed("本拍使用旧积分、下拍才使用新积分；未饱和多拍匹配独立双精度参考")

    for target in [3000.0, -3000.0, 0.0]:
        model = Model()
        assert model.update([target, -target, 0.001])
        model.check_reference(0.0)
    for target in [3000.01, -3000.01]:
        model = Model()
        model.seed_history()
        assert not model.update([target, 0.0, 0.001])
    for measured in [100.0, -100.0]:
        model = Model()
        assert model.update([0.0, measured, 0.001])
        assert model.values[1] * measured < 0.0
    passed("目标边界含等号，正反转越界目标直接拒绝；有限超速反馈仍可用于制动计算")

    model = Model([0.01, 0.0, 0.0, 0.5, 0.8, 3000.0, 0.005])
    for target, expected, limited in [(50.0, 0.5, False), (-50.0, -0.5, False),
                                       (100.0, 0.5, True), (-100.0, -0.5, True)]:
        assert model.update([target, 0.0, 0.001])
        close(model.values[1], expected)
        assert bool(model.status[2]) == limited
    passed("正负 Iq 输出限幅对称；恰好达到上限不误报截断")

    model = Model([0.0, 10.0, 0.0, 0.5, 0.1, 3000.0, 0.005])
    assert model.update([100.0, 0.0, 0.001])
    close(model.values[0], 0.0)
    close(model.values[2], 0.1)
    assert model.status[3] and not model.status[2]
    model.call("model_reset")
    assert model.update([-100.0, 0.0, 0.001])
    close(model.values[2], -0.1)
    assert model.status[3] and not model.status[2]
    passed("积分正负硬上限独立于 Iq 输出上限，积分候选不提前影响本拍输出")

    tracking = Model([0.01, 0.02, 80.0, 0.5, 10.0, 3000.0, 0.005])
    no_tracking = Model([0.01, 0.02, 0.0, 0.5, 10.0, 3000.0, 0.005])
    for _ in range(500):
        assert tracking.update([200.0, 0.0, 0.001])
        assert no_tracking.update([200.0, 0.0, 0.001])
    assert abs(tracking.values[2]) < abs(no_tracking.values[2])
    close(tracking.values[2], -1.45, 0.001)
    close(no_tracking.values[2], 2.0, 0.001)
    # 稳态误差归零后，外部对象保持原转速，验证残留积分逐步退回 Iq 上限。
    for _ in range(250):
        assert tracking.update([0.0, 0.0, 0.001])
    assert abs(tracking.values[2]) <= tracking.config[3] + 1e-5
    passed("持续饱和用限幅后 Iq 回算抑制积分堆积；零误差后历史退回输出边界")

    for bad_dt in [0.0, -0.001, math.nan, math.inf, -math.inf, 0.005001]:
        model = Model()
        model.seed_history()
        assert not model.update([100.0, 0.0, bad_dt])
        assert model.update([100.0, 0.0, 0.001])
        model.check_reference(0.0)
    model = Model()
    assert model.update([100.0, 0.0, model.config[6]])
    passed("非法与超时 dt 清积分；等于最大 dt 合法，恢复后从零历史重新计算")

    for field in range(2):
        model = Model()
        model.seed_history()
        flags = [1, 1]
        flags[field] = 0
        assert not model.update(flags=flags)
    for field in range(3):
        for bad in [math.nan, math.inf, -math.inf]:
            model = Model()
            model.seed_history()
            inputs = list(DEFAULT_INPUT)
            inputs[field] = bad
            assert not model.update(inputs)
    passed("反馈/完整目标有效位与所有输入 NaN/Inf 都拒绝，输出与历史同时清零")

    for field in range(7):
        for bad in [-1.0, math.nan, math.inf, -math.inf]:
            config = list(DEFAULT_CONFIG)
            config[field] = bad
            model = Model(config)
            assert not model.status[0] and not model.status[4]
            assert not model.update()
            model.expect_invalid(False)
    for field in range(3, 7):
        config = list(DEFAULT_CONFIG)
        config[field] = 0.0
        assert not Model(config).status[0]
    model = Model([0.0, 0.0, 0.0, *DEFAULT_CONFIG[3:]])
    assert model.update([100.0, -100.0, 0.001])
    assert model.values == [0.0] * 3
    passed("逐字段配置拒绝非法范围；零比例/积分/跟踪增益合法，所有物理上限必须为正")

    model = Model()
    model.seed_history()
    model.call("model_alias_config")
    assert model.status[0] and model.status[4] and model.values[2] == 0.0
    model.call("model_null_config")
    assert not model.status[0] and not model.status[4] and model.values[2] == 0.0
    assert not model.update()
    for function in ["model_null_controller", "model_null_input", "model_null_output"]:
        model = Model()
        if function != "model_null_controller":
            model.seed_history()
        model.call(function)
        model.expect_invalid()
    passed("空实例/配置/输入/输出安全返回；配置别名可重初始化且不丢配置")

    for history in [math.nan, math.inf, -math.inf, 0.8001, -0.8001]:
        model = Model()
        model.write("model_history", [history], "f")
        model.update()
        model.call("model_corrupt_history")
        model.expect_invalid()
    model = Model()
    model.seed_history()
    broken = list(DEFAULT_CONFIG)
    broken[3] = -1.0
    model.write("model_config", broken, "7f")
    model.call("model_corrupt_config")
    model.expect_invalid()
    passed("内部积分非有限/越界及配置损坏拒绝，不复用旧电流请求")

    # 每项单独触发不同中间步骤的 float32 溢出；数值检查不能由最终限幅掩盖。
    overflow_cases = [
        ([1.0, 0.0, 0.0, 1.0, 1.0, 3e38, 1.0], [3e38, -3e38, 1.0], 0.0),
        ([3e38, 0.0, 0.0, 1.0, 1.0, 10.0, 1.0], [2.0, 0.0, 1.0], 0.0),
        ([3e38, 0.0, 0.0, 1.0, 3e38, 10.0, 1.0], [0.5, 0.0, 1.0], 3e38),
        ([0.0, 3e38, 0.0, 1.0, 1.0, 10.0, 1.0], [2.0, 0.0, 1.0], 0.0),
        ([1.0, 0.0, 3e38, 1.0, 1.0, 10.0, 1.0], [3.0, 0.0, 1.0], 0.0),
        ([0.0, 3e38, 1.0, 1.0, 3e38, 10.0, 1.0], [1.0, 0.0, 1.0], -3e38),
        ([0.0, 3e38, 0.0, 1.0, 1.0, 10.0, 2.0], [1.0, 0.0, 2.0], 0.0),
        ([0.0, 1.5e38, 0.0, 1.0, 3e38, 10.0, 1.0], [1.0, 0.0, 1.0], 3e38),
    ]
    for config, inputs, history in overflow_cases:
        model = Model(config)
        model.inputs = list(map(float32, inputs))
        model.write("model_inputs", model.inputs, "3f")
        model.write("model_input_valid", [1, 1], "2I")
        model.write("model_history", [history], "f")
        model.call("model_corrupt_history")
        model.expect_invalid()
        assert model.update()
        model.check_reference(0.0)
    passed("误差/比例/请求/积分/跟踪/导数/增量/历史求和溢出均 fail closed 并可恢复")

    model = Model()
    model.seed_history()
    first_history = model.values[2]
    model.write("model_selector", [1], "I")
    assert model.configure(DEFAULT_CONFIG)
    for _ in range(10):
        assert model.update([-100.0, 0.0, 0.001])
    assert model.values[2] < 0.0
    model.write("model_selector", [0], "I")
    assert model.update()
    close(model.values[0], first_history)
    model.call("model_reset")
    assert model.values[2] == 0.0 and model.status[4]
    assert model.update() and model.values == [0.0] * 3
    passed("多实例积分独立；reset 保留配置并清历史，零目标可正常计算制动电流")

    model = Model()
    randomizer = random.Random(4316701)
    history = 0.0
    for _ in range(1500):
        inputs = [randomizer.uniform(-2800.0, 2800.0),
                  randomizer.uniform(-3500.0, 3500.0), randomizer.uniform(0.0003, 0.004)]
        assert model.update(inputs)
        history = model.check_reference(history)
    passed("1500 拍正反目标、超速反馈、变 dt 及饱和序列匹配独立双精度离散参考")

    # 理想内环下 J*dω/dt = Kt*Iq - B*ω - 负载；不含真实采样、延迟、摩擦与参数误差。
    model = Model()
    inertia, viscous_damping, torque_constant = 0.00002, 0.00002, 0.03
    mechanical_rpm = 0.0
    saw_current_limit = False
    for target, load_torque in [(300.0, 0.0), (-200.0, 0.0), (150.0, 0.001), (0.0, 0.001)]:
        for index in range(2000):
            dt = [0.0008, 0.001, 0.0012][index % 3]
            assert model.update([target, mechanical_rpm, dt])
            saw_current_limit |= bool(model.status[2])
            omega = mechanical_rpm * (2.0 * math.pi / 60.0)
            equilibrium = (torque_constant * model.values[1] - load_torque) / viscous_damping
            omega = equilibrium + (omega - equilibrium) * math.exp(-viscous_damping * dt / inertia)
            mechanical_rpm = omega * (60.0 / (2.0 * math.pi))
            assert abs(mechanical_rpm) < 400.0
        close(mechanical_rpm, target, 0.02)
    assert saw_current_limit
    passed("简化惯量/粘滞阻尼对象在正反阶跃、外负载、变 dt 下收敛；仅验证数值行为")

for name in CASES:
    print("PASS", name)
print(f"Speed controller: {len(CASES)} groups passed (ARMCC + Unicorn; numerical only)")
