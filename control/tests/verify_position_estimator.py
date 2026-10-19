"""使用 ARMCC 与 Unicorn 执行正式周期位置估计器 C，不访问硬件。"""

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


def passed(name: str) -> None:
    CASES.append(name)


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


with tempfile.TemporaryDirectory(prefix="position-estimator-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / "position_estimator.c", ROOT / "tests/model.c"]:
        obj = temporary / (source.stem + ".o")
        run_checked(
            [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
             "--split_sections", "-g", "-I", str(ROOT), "-c", str(source), "-o", str(obj)],
            temporary,
        )
        objects.append(str(obj))
    executable = temporary / "model.axf"
    run_checked(
        [str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
         "--entry=model_init", "--keep=model.o(*)", "-o", str(executable), *objects],
        temporary,
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
        def __init__(self, maximum: float = 6000.0, tau: float = 0.0) -> None:
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.configure(maximum, tau)

        def call(self, name: str) -> None:
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=100000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, name

        def read(self, name: str, fmt: str = "I"):
            return struct.unpack("<" + fmt, self.cpu.mem_read(symbols[name], 4))[0]

        def write(self, name: str, value: int | float, fmt: str = "I") -> None:
            self.cpu.mem_write(symbols[name], struct.pack("<" + fmt, value))

        def configure(self, maximum: float, tau: float = 0.0) -> bool:
            self.cpu.mem_write(symbols["model_config"], struct.pack("<ff", maximum, tau))
            self.call("model_init")
            return bool(self.read("model_result"))

        def update(self, position: float, interval: float = 0.001) -> bool:
            self.write("model_position", position, "f")
            self.write("model_interval", interval, "f")
            self.call("model_update")
            result = bool(self.read("model_result"))
            assert result == bool(self.read("model_valid"))
            assert math.isfinite(self.velocity)
            if not result:
                assert self.velocity == 0.0
            return result

        @property
        def velocity(self) -> float:
            return self.read("model_velocity", "f")

        def expect(self, velocity: float, tolerance: float = 0.03) -> None:
            assert self.read("model_valid") == 1
            assert math.isclose(self.velocity, velocity, abs_tol=tolerance), (
                self.velocity, velocity)

        def expect_empty(self, configured: bool = True) -> None:
            assert self.velocity == 0.0 and self.read("model_valid") == 0
            assert self.read("model_has_position") == 0
            assert self.read("model_configured") == int(configured)

    model = Model()
    model.expect_empty()
    assert not model.update(0.25)
    assert model.read("model_has_position") == 1
    assert model.update(0.25)
    model.expect(0.0)
    assert model.update(0.25)
    model.expect(0.0)
    passed("首样本只建基线；相邻静止观测产生有效零速度")

    for maximum in [0.0, -1.0, math.nan, math.inf, -math.inf]:
        model = Model(maximum)
        model.expect_empty(False)
        assert not model.update(0.25)
        model.expect_empty(False)
        assert model.configure(6000.0)
    for tau in [-1.0, math.nan, math.inf, -math.inf]:
        model = Model(6000.0, tau)
        model.expect_empty(False)
    model = Model()
    model.call("model_null_arguments")
    assert model.read("model_result") == 0
    model.expect_empty()
    model.call("model_null_config")
    assert model.read("model_result") == 0
    model.expect_empty(False)
    assert model.configure(6000.0)
    model.call("model_alias_config")
    assert model.read("model_result") == 1
    model.expect_empty()
    passed("非法配置与空指针拒绝；合法重新初始化和配置别名保持确定行为")

    for positions, expected in [([0.99, 0.01, 0.03], 1200.0),
                                ([0.01, 0.99, 0.97], -1200.0),
                                ([0.0, 0.01, 0.02], 600.0),
                                ([0.02, 0.01, 0.0], -600.0)]:
        model = Model()
        assert not model.update(positions[0])
        for position in positions[1:]:
            assert model.update(position)
            model.expect(expected)
    passed("正负机械方向以及正负跨零采用最近周期差，单位为 rpm")

    model = Model()
    position = 0.95
    assert not model.update(position)
    for interval in [0.001, 0.0015, 0.0005, 0.0025, 0.004]:
        position = (position + 10.0 * interval) % 1.0
        assert model.update(position, interval)
        model.expect(600.0)
    passed("非固定采集间隔按实际 dt 计算，不把更新节拍当作固定值")

    for bad_position, bad_interval in [(math.nan, 0.001), (math.inf, 0.001),
                                       (-math.inf, 0.001), (-0.01, 0.001), (1.0, 0.001),
                                       (2.0, 0.001), (0.8, 0.0), (0.8, -0.001),
                                       (0.8, math.nan), (0.8, math.inf), (0.8, -math.inf)]:
        model = Model(6000.0, 0.003)
        assert not model.update(0.2)
        assert model.update(0.21)
        assert not model.update(bad_position, bad_interval)
        model.expect_empty()
        assert not model.update(0.8)
        assert model.update(0.81)
        model.expect(600.0)
    passed("非法位置或 dt 清除历史；重接后两点恢复，不沿用旧位置或滤波历史")

    for initial, ambiguous in [(0.0, 0.5), (0.75, 0.25)]:
        model = Model()
        assert not model.update(initial)
        assert not model.update(ambiguous)
        model.expect_empty()
    for interval in [float32(30.0 / 6000.0), 0.005001, 1.0]:
        model = Model()
        assert not model.update(0.2)
        assert not model.update(0.2, interval)
        model.expect_empty()
    model = Model()
    assert not model.update(0.0)
    assert model.update(0.01, 0.004999)
    model.expect(0.01 / float32(0.004999) * 60.0)
    passed("恰好半圈及理论半圈混叠边界拒绝，即使位置看似静止也不能证明可排歧")

    for delta in [0.2, -0.2, 0.100001, -0.100001]:
        model = Model()
        assert not model.update(0.5)
        assert not model.update(0.5 + delta)
        model.expect_empty()
        assert not model.update(0.1)
        assert model.update(0.11)
        model.expect(600.0)
    for delta in [0.0625, -0.0625]:
        model = Model(3750.0)
        assert not model.update(0.5)
        assert model.update(0.5 + delta)
        model.expect(delta / float32(0.001) * 60.0)
    passed("实测速度越界清历史；等于上界仍可接受，上界只用于观测约束")

    model = Model(6000.0, 0.003)
    assert not model.update(0.1)
    assert model.update(0.11)
    model.expect(600.0)
    assert model.update(0.13)
    model.expect(750.0)
    assert model.update(0.13, 0.002)
    model.expect(450.0)
    assert model.update(0.12)
    model.expect(187.5)
    model.call("model_reset")
    model.expect_empty()
    assert not model.update(0.8)
    assert model.update(0.79)
    model.expect(-600.0)
    passed("低通使用 dt/(tau+dt)，首个速度直接建滤波基线，reset 保留配置")

    model = Model()
    assert not model.update(0.1)
    assert model.update(0.11)
    previous = (model.velocity, model.read("model_has_position"))
    model.call("model_second_init")
    assert (model.velocity, model.read("model_has_position")) == previous
    for position in [0.8, 0.79, 0.78]:
        model.write("model_position", position, "f")
        model.write("model_interval", 0.001, "f")
        model.call("model_second_update")
        assert (model.velocity, model.read("model_has_position")) == previous
    assert model.update(0.12)
    model.expect(600.0)
    passed("多个估计器实例历史互不污染")

    model = Model(6000.0, 0.002)
    randomizer = random.Random(6701)
    position = float32(0.97)
    assert not model.update(position)
    expected_filtered = None
    for _ in range(600):
        interval = float32(randomizer.uniform(0.0005, 0.002))
        velocity = randomizer.uniform(-2500.0, 2500.0)
        next_position = float32((position + velocity / 60.0 * interval) % 1.0)
        # 参考模型使用已量化输入，检验回绕、变 dt 与滤波叠加的长期行为。
        delta = next_position - position
        if delta > 0.5:
            delta -= 1.0
        elif delta < -0.5:
            delta += 1.0
        raw = delta / interval * 60.0
        alpha = interval / (float32(0.002) + interval)
        expected_filtered = (raw if expected_filtered is None else
                             expected_filtered + alpha * (raw - expected_filtered))
        assert model.update(next_position, interval)
        model.expect(expected_filtered, 0.04)
        position = next_position
    passed("600 次变速、变 dt、跨圈与方向变化符合独立数值参考")

print(f"PASS: {len(CASES)} PositionEstimator C behavior groups")
for name in CASES:
    print("  " + name)
