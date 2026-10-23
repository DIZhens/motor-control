"""使用 ARMCC 与 Unicorn 执行正式 Park、逆 Park 和居中 SVPWM C，不访问硬件。"""

from pathlib import Path
import cmath
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


def reference_modulation(alpha: float, beta: float, bus: float, ratio: float):
    # 独立双精度扇区停留时间参考，不重复被测 C 的最大/最小共模注入公式。
    requested = complex(alpha, beta)
    magnitude = abs(requested)
    limit = ratio * bus
    limited = magnitude > limit
    applied = requested * (limit / magnitude) if limited else requested
    if applied == 0j:
        return [0.0, 0.0, 0.5, 0.5, 0.5], limited
    angle = cmath.phase(applied) % (2.0 * math.pi)
    sector = min(int(angle / (math.pi / 3.0)), 5)
    local_angle = angle - sector * math.pi / 3.0
    active_scale = math.sqrt(3.0) * abs(applied) / bus
    first = active_scale * math.sin(math.pi / 3.0 - local_angle)
    second = active_scale * math.sin(local_angle)
    zero = (1.0 - first - second) / 2.0
    duties = [
        (zero + first + second, zero + second, zero),
        (zero + first, zero + first + second, zero),
        (zero, zero + first + second, zero + second),
        (zero, zero + first, zero + first + second),
        (zero + second, zero, zero + first + second),
        (zero + first + second, zero, zero + first),
    ][sector]
    return [applied.real, applied.imag, *duties], limited


with tempfile.TemporaryDirectory(prefix="foc-math-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / 'foc_math.c', ROOT / 'tests/foc_math_model.c']:
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
         "--entry=model_init", "--keep=foc_math_model.o(*)", "-o", str(executable),
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
        def __init__(self) -> None:
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))

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

        def operation(self, name: str, inputs: list[float]) -> tuple[bool, list[float]]:
            self.inputs = list(map(float32, inputs))
            self.cpu.mem_write(symbols["model_input"], struct.pack("<ffff", *self.inputs))
            result = self.call("model_" + name)
            count = 5 if name == "svpwm" else 2
            output = list(struct.unpack("<" + "f" * count,
                                       self.cpu.mem_read(symbols["model_output"], 4 * count)))
            assert result == bool(self.integer("model_valid"))
            assert all(math.isfinite(value) for value in output), output
            if not result:
                assert output == [0.0] * count, output
                if name == "svpwm":
                    assert self.integer("model_limited") == 0
            return result, output

        def transform(self, name: str, first: float, second: float, angle: float) -> list[float]:
            result, output = self.operation(name, [first, second, math.sin(angle), math.cos(angle)])
            assert result
            first, second, sine, cosine = self.inputs
            rotation = complex(cosine, -sine if name == "park" else sine)
            expected = complex(first, second) * rotation
            assert math.isclose(output[0], expected.real, rel_tol=0.000001, abs_tol=0.00001)
            assert math.isclose(output[1], expected.imag, rel_tol=0.000001, abs_tol=0.00001)
            return output

        def modulation(self, alpha: float, beta: float, bus=12.0, ratio=0.45) -> list[float]:
            result, output = self.operation("svpwm", [alpha, beta, bus, ratio])
            assert result, self.inputs
            expected, limited = reference_modulation(*self.inputs)
            assert all(math.isclose(actual, target, rel_tol=0.000002, abs_tol=0.000002)
                       for actual, target in zip(output, expected)), (output, expected, self.inputs)
            # 圆边界附近 float 舍入可影响 limited，其他情况必须和参考一致。
            magnitude = math.hypot(*self.inputs[:2])
            limit = self.inputs[2] * self.inputs[3]
            if abs(magnitude - limit) > max(limit * 0.000001, 1e-40):
                assert bool(self.integer("model_limited")) == limited
            assert all(0.0 <= duty <= 1.0 for duty in output[2:]), output
            assert math.hypot(*output[:2]) <= limit * 1.000002
            # 三相占空比去零序后重构静止电压，验证报告的 applied 确实对应输出。
            reconstructed = sum(duty * cmath.exp(2j * math.pi * phase / 3.0)
                                for phase, duty in enumerate(output[2:])) * self.inputs[2] * 2.0 / 3.0
            assert math.isclose(reconstructed.real, output[0], rel_tol=0.00001,
                                abs_tol=self.inputs[2] * 0.000001)
            assert math.isclose(reconstructed.imag, output[1], rel_tol=0.00001,
                                abs_tol=self.inputs[2] * 0.000001)
            return output

    model = Model()
    assert not model.call("model_null_arguments")
    for name in ["park", "inverse_park"]:
        assert model.operation(name, [0.0, 0.0, 0.0, 1.0]) == (True, [0.0, 0.0])
    assert model.modulation(0.0, 0.0) == [0.0, 0.0, 0.5, 0.5, 0.5]
    assert model.integer("model_limited") == 0
    passed("空指针确定拒绝；零变换有效，零电压调制为三相 0.5")

    for angle in [0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0]:
        for first, second in [(1.0, 0.0), (0.0, 1.0), (-2.0, 3.0)]:
            for name in ["park", "inverse_park"]:
                model.transform(name, first, second, angle)
    passed("四个基准角核对 d/q 与 alpha/beta 正方向和幅值不变约定")

    for index in range(721):
        angle = index * math.pi / 360.0
        direct, quadrature = model.transform("park", 3.5, -1.25, angle)
        stationary = model.transform("inverse_park", direct, quadrature, angle)
        assert math.isclose(stationary[0], 3.5, abs_tol=0.000003)
        assert math.isclose(stationary[1], -1.25, abs_tol=0.000003)
        assert math.isclose(math.hypot(direct, quadrature), math.hypot(3.5, -1.25), abs_tol=0.000003)
    passed("721 个全周电角度符合双精度复数旋转、幅值和正逆变换往返参考")

    for name in ["park", "inverse_park"]:
        for argument in range(4):
            for bad in [math.nan, math.inf, -math.inf]:
                assert model.operation(name, [1.0, 2.0, 0.0, 1.0])[0]
                values = [1.0, 2.0, 0.0, 1.0]
                values[argument] = bad
                assert not model.operation(name, values)[0]
        for sine, cosine in [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0), (0.0, -2.0),
                             (0.0, math.sqrt(1.0012)), (0.0, math.sqrt(0.9988)), (1e30, 1e30)]:
            assert not model.operation(name, [1.0, 2.0, sine, cosine])[0]
        for sine, cosine in [(0.0, math.sqrt(1.0008)), (0.0, math.sqrt(0.9992))]:
            assert model.operation(name, [1.0, 2.0, sine, cosine])[0]
    passed("NaN/Inf 及坏单位圆清除旧输出；单位圆容差内可用且不静默归一化")

    for edge in [math.sqrt(0.999), math.sqrt(1.001)]:
        center = struct.unpack("<I", struct.pack("<f", edge))[0]
        for bits in range(center - 3, center + 4):
            cosine = struct.unpack("<f", struct.pack("<I", bits))[0]
            expected = abs(float32(float32(cosine * cosine) - 1.0)) <= float32(0.001)
            for name in ["park", "inverse_park"]:
                accepted, values = model.operation(name, [2.0, -3.0, 0.0, cosine])
                assert accepted == expected, (bits, name, values)
                if accepted:
                    close_values = [float32(2.0 * cosine), float32(-3.0 * cosine)]
                    assert values == close_values
                else:
                    assert values == [0.0, 0.0]
    passed("公共变换的单位圆精确边界相邻float保持原判定和未归一化幅值，私有复用未放宽外部入口")

    maximum = float.fromhex("0x1.fffffep127")
    for name in ["park", "inverse_park"]:
        assert not model.operation(name, [maximum, maximum, math.sqrt(0.5), math.sqrt(0.5)])[0]
        model.transform(name, 1e30, -2e30, 0.71)
        model.transform(name, 1e-30, -2e-30, -0.24)
    passed("旋转结果溢出整拍失效；可表示的大数与小数仍符合参考")

    for magnitude in [0.01, 1.0, 3.0, 5.39, 5.4, 6.0, 12.0, 1e20]:
        for index in range(72):
            angle = index * math.pi / 36.0
            model.modulation(magnitude * math.cos(angle), magnitude * math.sin(angle))
    passed("六扇区各幅值与圆限幅符合独立停留时间参考，applied 可由占空比重构")

    for index in range(361):
        angle = index * math.pi / 180.0
        output = model.modulation(12.0 * math.cos(angle), 12.0 * math.sin(angle), ratio=0.45)
        assert all(0.1 <= duty <= 0.9 for duty in output[2:]), output
        assert model.integer("model_limited") == 1
    passed("目标 ratio=0.45 的全周饱和电压均保留 10% 到 90% 占空比余量")

    for sector in range(7):
        boundary = sector * math.pi / 3.0
        before = model.modulation(8.0 * math.cos(boundary - 1e-6),
                                  8.0 * math.sin(boundary - 1e-6))
        at = model.modulation(8.0 * math.cos(boundary), 8.0 * math.sin(boundary))
        after = model.modulation(8.0 * math.cos(boundary + 1e-6),
                                 8.0 * math.sin(boundary + 1e-6))
        assert all(abs(left - center) < 0.000002 and abs(right - center) < 0.000002
                   for left, center, right in zip(before[2:], at[2:], after[2:]))
    passed("六扇区边界及 0/2pi 接缝占空比连续，没有切扇区跳变")

    for index in range(361):
        angle = index * math.pi / 180.0
        model.modulation(100.0 * math.cos(angle), 100.0 * math.sin(angle),
                         ratio=float32(1.0 / math.sqrt(3.0)))
    for bus in [1.0, 4.0, 12.0, 24.0, 48.0, 1000.0]:
        for ratio in [0.01, 0.1, 0.3, 0.45]:
            model.modulation(bus * 0.8, bus * -0.6, bus, ratio)
    passed("线性调制圆上界无过调制；母线与装配限幅比例变化保持相同归一化关系")

    for argument in range(4):
        for bad in [math.nan, math.inf, -math.inf]:
            model.modulation(1.0, 2.0)
            inputs = [1.0, 2.0, 12.0, 0.45]
            inputs[argument] = bad
            assert not model.operation("svpwm", inputs)[0]
    for bus in [0.0, -1.0]:
        assert not model.operation("svpwm", [1.0, 2.0, bus, 0.45])[0]
    for ratio in [0.0, -0.1, 0.577351, 1.0]:
        assert not model.operation("svpwm", [1.0, 2.0, 12.0, ratio])[0]
    assert not model.operation("svpwm", [1.0, 2.0, 1.401298464324817e-45, 0.01])[0]
    passed("坏电压/母线/比例与数值下溢拒绝，清除旧 applied、占空比和限幅状态")

    for alpha, beta, bus in [(maximum, maximum, 12.0), (-maximum, maximum, 24.0),
                             (maximum, -maximum, maximum), (maximum, 0.0, maximum),
                             (1e-30, -2e-30, 1e-29), (0.0, 0.0, maximum)]:
        model.modulation(alpha, beta, bus)
    passed("FLT_MAX 电压请求及母线安全缩放，极小可表示电压仍输出有限有效结果")

    randomizer = random.Random(4316701)
    for _ in range(1000):
        angle = randomizer.uniform(-10.0 * math.pi, 10.0 * math.pi)
        first, second = [randomizer.uniform(-100.0, 100.0) for _ in range(2)]
        model.transform("park", first, second, angle)
        model.transform("inverse_park", first, second, angle)
        bus = randomizer.uniform(1.0, 80.0)
        ratio = randomizer.uniform(0.01, 1.0 / math.sqrt(3.0) - 1e-6)
        alpha, beta = [randomizer.uniform(-2.0 * bus, 2.0 * bus) for _ in range(2)]
        model.modulation(alpha, beta, bus, ratio)
    passed("1000 组随机旋转及调制符合独立双精度参考，并验证限幅方向和电压重构")

print(f"PASS: {len(CASES)} FocMath C behavior groups")
for name in CASES:
    print("  " + name)
