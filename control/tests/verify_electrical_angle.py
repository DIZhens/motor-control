"""使用 ARMCC 与 Unicorn 对正式电角度 C 做独立双精度数值对照，不访问硬件。"""

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


def adjacent32(value: float, step: int) -> float:
    bits = struct.unpack("<I", struct.pack("<f", value))[0]
    return struct.unpack("<f", struct.pack("<I", bits + step))[0]


with tempfile.TemporaryDirectory(prefix="electrical-angle-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / "electrical_angle.c", ROOT / "tests/electrical_angle_model.c"]:
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
         "--entry=model_init", "--keep=electrical_angle_model.o(*)", "-o", str(executable),
         *objects], temporary,
    )
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(segment["p_vaddr"], segment.data()) for segment in elf.iter_segments()
                    if segment["p_type"] == "PT_LOAD"]
        initializers = [(section["sh_addr"], section.data()) for section in elf.iter_sections()
                        if (section["sh_flags"] & 2) and section["sh_type"] == "SHT_PROGBITS"]
        symbols = {symbol.name: symbol["st_value"]
                   for symbol in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self, poles=7, direction=1, offset=0.0, maximum_age=0.004,
                     maximum_velocity=6000.0) -> None:
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.configure(poles, direction, offset, maximum_age, maximum_velocity)

        def call(self, name: str) -> bool:
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=100000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, name
            return bool(self.integer("model_result"))

        def integer(self, name: str) -> int:
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def configure(self, poles=7, direction=1, offset=0.0, maximum_age=0.004,
                      maximum_velocity=6000.0) -> bool:
            self.config = (poles, direction, *map(float32, (offset, maximum_age,
                                                         maximum_velocity)))
            self.cpu.mem_write(symbols["model_config"], struct.pack("<Iifff", *self.config))
            return self.call("model_init")

        @property
        def output(self) -> tuple[float, float, float]:
            return struct.unpack("<fff", self.cpu.mem_read(symbols["model_output"], 12))

        def expect_empty(self, configured=True) -> None:
            assert self.output == (0.0, 0.0, 0.0), self.output
            assert self.integer("model_valid") == 0
            assert self.integer("model_configured") == int(configured)

        def update(self, position: float, velocity=0.0, age=0.0, *, with_trig=False) -> bool:
            for symbol, value in [("model_position", position), ("model_velocity", velocity),
                                  ("model_age", age)]:
                self.cpu.mem_write(symbols[symbol], struct.pack("<f", value))
            result = self.call("model_update_with_trig" if with_trig else "model_update")
            assert result == bool(self.integer("model_valid"))
            assert all(math.isfinite(value) for value in self.output), self.output
            if not result:
                self.expect_empty(bool(self.integer("model_configured")))
            return result

        def configure_trig(self, mode=0, sine=0.0, cosine=0.0, *, second=False) -> None:
            symbol = "model_second_trig_context" if second else "model_trig_context"
            self.cpu.mem_write(symbols[symbol], struct.pack("<IIfff", 0, mode, 0.0,
                                                           sine, cosine))

        def trig_context(self, *, second=False) -> tuple[int, int, float, float, float]:
            symbol = "model_second_trig_context" if second else "model_trig_context"
            return struct.unpack("<IIfff", self.cpu.mem_read(symbols[symbol], 20))

        def expect_reference(self, position: float, velocity=0.0, age=0.0) -> None:
            assert self.update(position, velocity, age)
            position, velocity, age = map(float32, (position, velocity, age))
            poles, direction, offset, _, _ = self.config
            # 独立双精度直接计算完整角度，不复刻 C 的分步归一化与整数截断。
            expected = (poles * direction * (position + velocity * age / 60.0) - offset) % 1.0
            phase, sine, cosine = self.output
            error = abs(phase - expected)
            assert min(error, 1.0 - error) <= 6e-6, (self.config, phase, expected)
            assert 0.0 <= phase < 1.0
            assert math.isclose(sine, math.sin(math.tau * expected), abs_tol=4e-5), (
                self.config, sine, expected)
            assert math.isclose(cosine, math.cos(math.tau * expected), abs_tol=4e-5), (
                self.config, cosine, expected)
            assert abs(sine * sine + cosine * cosine - 1.0) <= 3e-7

    model = Model()
    model.expect_empty()
    model.expect_reference(0.0)
    model.expect_reference(0.125)
    model.expect_reference(0.5)
    model.expect_reference(adjacent32(1.0, -1))
    passed("首个合法位置直接产生有效电角度；输出单位和周期范围正确")

    for poles in [1, 7, 64]:
        for direction in [1, -1]:
            for offset in [0.0, 0.25, 0.9999]:
                model = Model(poles, direction, offset)
                for position in [0.0, 0.00001, 0.125, 0.25, 0.75, 0.99999]:
                    model.expect_reference(position)
    passed("1、7、64 极对与双方向、零位偏置组合按独立双精度公式对照")

    for poles in [0, 65, 0xFFFFFFFF]:
        Model(poles=poles).expect_empty(False)
    for direction in [0, 2, -2, 2147483647, -2147483648]:
        Model(direction=direction).expect_empty(False)
    for offset in [-0.001, 1.0, 100.0, math.nan, math.inf, -math.inf]:
        Model(offset=offset).expect_empty(False)
    for maximum_age in [-0.001, math.nan, math.inf, -math.inf]:
        Model(maximum_age=maximum_age).expect_empty(False)
    for maximum_velocity in [0.0, -1.0, math.nan, math.inf, -math.inf]:
        Model(maximum_velocity=maximum_velocity).expect_empty(False)
    passed("极对数、方向、偏置、预测时间和速度配置的非法值明确拒绝")

    for maximum_age in [0.005, adjacent32(0.005, 1), 100000.0]:
        Model(maximum_age=maximum_age).expect_empty(False)
    model = Model(maximum_age=adjacent32(0.005, -1))
    model.expect_reference(0.9, 6000.0, adjacent32(0.005, -1))
    model.expect_reference(0.1, -6000.0, adjacent32(0.005, -1))
    passed("配置拒绝半圈及以上预测范围，紧邻合法上界仍可映射")

    for direction in [1, -1]:
        model = Model(7, direction, 0.31)
        for position, velocity, age in [(0.99, 300.0, 0.004), (0.01, -300.0, 0.004),
                                         (0.0, -6000.0, 0.004),
                                         (0.99999, 6000.0, 0.004),
                                         (0.4, 6000.0, 0.0), (0.4, 0.0, 0.004)]:
            model.expect_reference(position, velocity, age)
    passed("短时匀速预测支持正负跨机械零点及电角度零点，年龄边界包含上限")

    model = Model()
    for position, velocity, age in [(math.nan, 0.0, 0.0), (math.inf, 0.0, 0.0),
                                     (-math.inf, 0.0, 0.0), (-0.001, 0.0, 0.0),
                                     (1.0, 0.0, 0.0), (10.0, 0.0, 0.0),
                                     (0.3, math.nan, 0.0), (0.3, math.inf, 0.0),
                                     (0.3, -math.inf, 0.0),
                                     (0.3, adjacent32(6000.0, 1), 0.0),
                                     (0.3, -adjacent32(6000.0, 1), 0.0),
                                     (0.3, 0.0, math.nan), (0.3, 0.0, math.inf),
                                     (0.3, 0.0, -math.inf), (0.3, 0.0, -0.001),
                                     (0.3, 0.0, adjacent32(0.004, 1))]:
        model.expect_reference(0.6, 300.0, 0.001)
        assert not model.update(position, velocity, age)
        model.expect_empty()
        model.expect_reference(0.6, -300.0, 0.001)
    passed("非法输入和过期观测清空旧输出；合法恢复不依赖旧位置或旧预测")

    model = Model(maximum_age=0.0)
    model.expect_reference(0.9, 6000.0, 0.0)
    assert not model.update(0.9, 0.0, 0.000001)
    model = Model(maximum_age=0.0, maximum_velocity=float32(3.402823466e38))
    model.expect_reference(0.125, float32(3.402823466e38), 0.0)
    passed("关闭预测时仅接受零年龄，大而有限速度不制造中间乘法溢出")

    model = Model()
    model.expect_reference(0.3)
    model.call("model_reset")
    model.expect_empty()
    model.expect_reference(0.8)
    assert model.call("model_alias_config")
    model.expect_empty()
    model.expect_reference(0.3)
    assert not model.call("model_null_arguments")
    model.expect_reference(0.3)
    assert not model.call("model_null_config")
    model.expect_empty(False)
    assert not model.update(0.2)
    model.expect_empty(False)
    assert model.configure()
    model.expect_reference(0.2)
    passed("空指针、reset、内部配置别名和失效后重配行为确定")

    model = Model(64, 1, 0.6)
    model.expect_reference(0.7, 1000.0, 0.001)
    before = model.output
    assert model.call("model_second_instance")
    assert model.output == before
    model.expect_reference(0.7, 1000.0, 0.001)
    passed("两个独立实例互不影响，映射不累计历史或圈数")

    generator = random.Random(0xE1EC7)
    for _ in range(1200):
        poles = generator.choice([1, 7, 64, generator.randint(1, 64)])
        direction = generator.choice([-1, 1])
        offset = generator.random()
        maximum = generator.uniform(50.0, 12000.0)
        maximum_age = generator.uniform(0.0, 0.95 * 30.0 / maximum)
        model = Model(poles, direction, offset, maximum_age, maximum)
        position = generator.random()
        velocity = generator.uniform(-model.config[4], model.config[4])
        age = generator.uniform(0.0, model.config[3])
        model.expect_reference(position, velocity, age)
    passed("1200 组随机映射/预测与双精度参考一致，sin/cos 满足单位圆容差")

    model = Model(64, -1, 0.31)
    model.configure_trig()
    for index, values in enumerate([(0.99, 300.0, 0.004), (0.01, -300.0, 0.004),
                                    (0.125, 0.0, 0.0), (0.0, -6000.0, 0.004)], 1):
        model.expect_reference(*values)
        reference = model.output
        assert model.update(*values, with_trig=True)
        assert model.output == reference
        calls, _, received, _, _ = model.trig_context()
        assert calls == index and received == reference[0]
    passed("可替换后端只收到一次已预测/方向映射/偏置归一化结果，软件结果逐位一致")

    model = Model()
    model.configure_trig()
    for values in [(math.nan, 0.0, 0.0), (-0.1, 0.0, 0.0), (1.0, 0.0, 0.0),
                   (0.2, math.inf, 0.0), (0.2, 6001.0, 0.0),
                   (0.2, 0.0, -0.01), (0.2, 0.0, 0.005)]:
        model.expect_reference(0.3)
        assert not model.update(*values, with_trig=True)
        model.expect_empty()
        assert model.trig_context()[0] == 0
    model.expect_reference(0.3)
    assert not model.call("model_null_trig")
    model.expect_empty()
    model.expect_reference(0.3)
    before = model.output
    assert not model.call("model_null_angle_with_trig")
    assert model.output == before
    assert not model.configure(poles=0)
    assert not model.update(0.3, with_trig=True)
    assert model.trig_context()[0] == 0
    passed("配置/输入失败与 NULL 后端不调用回调并清旧输出，NULL 实例不影响已有实例")

    model = Model()
    for mode, sine, cosine in [(1, 0.6, 0.8), (3, 1.0, 0.0), (4, 0.0, 0.0),
                               (2, math.nan, 1.0), (2, 1.0, math.nan),
                               (2, math.inf, 0.0), (2, 0.0, -math.inf),
                               (2, 0.0, 0.0), (2, 1.0, 1.0),
                               (2, 3e38, 3e38), (2, math.sqrt(1.002), 0.0),
                               (2, math.sqrt(0.998), 0.0)]:
        model.expect_reference(0.3)
        model.configure_trig(mode, sine, cosine)
        assert not model.update(0.3, with_trig=True)
        assert model.trig_context()[0] == 1
        model.expect_empty()
        model.expect_reference(0.3)
    sine = float32(math.sqrt(1.0005))
    model.configure_trig(2, sine, 0.0)
    assert model.update(0.3, with_trig=True)
    assert model.output[1:] == (sine, 0.0)
    passed("后端拒绝、漏写、非有限及非单位相量失效清零；容差内结果保持原值不静默修正")

    model = Model()
    model.configure_trig()
    for index in range(1024):
        phase = float32(index / 1024.0)
        model.cpu.mem_write(symbols["model_position"], struct.pack("<f", phase))
        assert model.call("model_software_trig")
        _, sine, cosine = model.output
        assert math.isclose(sine, math.sin(math.tau * phase), abs_tol=6e-7)
        assert math.isclose(cosine, math.cos(math.tau * phase), abs_tol=6e-7)
    for phase in [-0.001, 1.0, math.nan, math.inf, -math.inf]:
        model.cpu.mem_write(symbols["model_position"], struct.pack("<f", phase))
        assert not model.call("model_software_trig")
        assert model.output[1:] == (0.0, 0.0)
    model.cpu.mem_write(symbols["model_position"], struct.pack("<f", 0.25))
    for pointer_mode, expected in [(1, (23.0, 0.0)), (2, (0.0, -17.0)),
                                   (3, (0.0, -17.0))]:
        model.cpu.mem_write(symbols["model_pointer_mode"], struct.pack("<I", pointer_mode))
        assert not model.call("model_software_trig")
        assert model.output[1:] == expected
    assert model.trig_context()[0] == 0
    passed("公开软件后端全周与双精度参考一致，拒绝非法角度/空或别名输出且不使用上下文")

    model = Model(64, 1, 0.6)
    model.configure_trig(2, 0.6, 0.8)
    model.configure_trig(2, -0.8, 0.6, second=True)
    assert model.update(0.7, 1000.0, 0.001, with_trig=True)
    first_output = model.output
    first_context = model.trig_context()
    assert model.call("model_second_instance_with_trig")
    assert model.output == first_output and model.trig_context() == first_context
    second_output = struct.unpack("<fff", model.cpu.mem_read(symbols["model_second_output"], 12))
    assert second_output == (0.875, float32(-0.8), float32(0.6))
    assert model.integer("model_second_valid") == 1
    assert model.trig_context(second=True)[0] == 1
    assert model.trig_context(second=True)[2] == 0.875
    model.configure_trig(1)
    assert not model.update(0.7, 1000.0, 0.001, with_trig=True)
    assert model.trig_context(second=True)[0] == 1
    model.configure_trig()
    assert model.update(0.7, 1000.0, 0.001, with_trig=True)
    assert model.output[0] == first_output[0]
    passed("实例与回调上下文相互独立，单实例后端失败和恢复不改变另一实例")

for index, name in enumerate(CASES, 1):
    print(f"PASS {index}: {name}")
print(f"electrical_angle: {len(CASES)} groups passed")
