"""使用 ARMCC 与 Unicorn 执行正式相电流零偏、重构和 Clarke C，不访问硬件。"""

from pathlib import Path
import cmath
import math
import os
import random
import statistics
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
UNCONFIGURED, WAITING, CALIBRATING, READY, FAILED = range(5)
NONE, ABORTED, UNCONFIRMED, INVALID_SAMPLE, NOISE, OFFSET, NUMERIC = range(7)
MASKS = (3, 5, 6, 7)


def passed(name: str) -> None:
    CASES.append(name)


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def reference(phases: list[float]) -> list[float]:
    # 使用三相空间矢量的独立双精度复数投影，不重复被测 C 的代数表达式。
    vector = sum(value * cmath.exp(2j * math.pi * index / 3.0)
                 for index, value in enumerate(phases)) * (2.0 / 3.0)
    return phases + [vector.real, vector.imag]


with tempfile.TemporaryDirectory(prefix="current-measurement-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / "current_measurement.c", ROOT / "tests/current_measurement_model.c"]:
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
         "--entry=model_init", "--keep=current_measurement_model.o(*)", "-o", str(executable),
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
        def __init__(self, mask=6, count=4, maximum=0.1, noise=0.05,
                     gain_enabled=False, gains=None) -> None:
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.configure(mask, count, maximum, noise, gain_enabled, gains)

        def call(self, name: str) -> bool:
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=100000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, name
            return bool(self.read("model_result"))

        def read(self, name: str):
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def write(self, name: str, value: int) -> None:
            self.cpu.mem_write(symbols[name], struct.pack("<I", value))

        def floats(self, name: str, count=3) -> list[float]:
            return list(struct.unpack("<" + "f" * count,
                                     self.cpu.mem_read(symbols[name], 4 * count)))

        def configure(self, mask=6, count=4, maximum=0.1, noise=0.05,
                      gain_enabled=False, gains=None) -> bool:
            self.mask, self.count = mask, count
            gains = [0.0] * 3 if gains is None else gains
            self.write("model_gain_enabled", int(gain_enabled))
            self.cpu.mem_write(symbols["model_signed_gain"], struct.pack("<3f", *gains))
            self.cpu.mem_write(symbols["model_config"], struct.pack("<IIff", mask, count,
                                                                  maximum, noise))
            return self.call("model_init")

        def sample(self, phases, valid=None) -> None:
            self.cpu.mem_write(symbols["model_sample"], struct.pack("<fffI", *phases,
                                                                  self.mask if valid is None else valid))

        def start(self, confirmed=True) -> bool:
            self.write("model_confirmed", int(confirmed))
            return self.call("model_start")

        def calibrate(self, phases, valid=None, confirmed=True) -> bool:
            previous_state = self.read("model_state")
            self.sample(phases, valid)
            self.write("model_confirmed", int(confirmed))
            result = self.call("model_calibrate")
            if previous_state != READY:
                self.empty()
            return result

        def process(self, phases, valid=None) -> bool:
            self.sample(phases, valid)
            result = self.call("model_process")
            assert result == bool(self.read("model_valid"))
            if not result:
                self.empty()
            return result

        def empty(self) -> None:
            assert self.read("model_valid") == 0
            assert self.floats("model_output", 5) == [0.0] * 5

        def expect(self, phases: list[float], tolerance=0.00001) -> None:
            expected = reference(phases)
            assert self.read("model_valid") == 1
            actual = self.floats("model_output", 5)
            assert all(math.isfinite(value) for value in actual)
            assert all(math.isclose(value, target, abs_tol=tolerance, rel_tol=0.000001)
                       for value, target in zip(actual, expected)), (actual, expected)

        def ready(self, offsets=None) -> None:
            assert self.start()
            offsets = offsets or [0.0, 0.0, 0.0]
            for index in range(self.count):
                assert self.calibrate(offsets) == (index == self.count - 1)
            assert self.read("model_state") == READY

    model = Model()
    assert model.read("model_state") == WAITING
    assert not model.process([0.0, 0.0, 0.0])
    assert not model.start(False)
    assert model.read("model_state") == WAITING
    assert not model.calibrate([0.0, 0.0, 0.0])
    assert model.read("model_count") == 0
    assert model.start()
    for index in range(4):
        assert model.calibrate([math.nan, 0.03125, -0.0625]) == (index == 3)
        assert model.read("model_count") == index + 1
        if index < 3:
            assert model.floats("model_offsets") == [0.0, 0.0, 0.0]
            assert not model.process([0.0, 0.0, 0.0])
    assert model.floats("model_offsets") == [0.0, 0.03125, -0.0625]
    passed("显式零电流确认后启动有限连续窗口，完整合格前不得使用零偏和输出")

    for mask in [0, 1, 2, 4, 8, 15, 0xFFFFFFFF]:
        model = Model(mask=mask)
        assert model.read("model_state") == UNCONFIGURED
    for count in [0, 1, 16777217, 0xFFFFFFFF]:
        model = Model(count=count)
        assert model.read("model_state") == UNCONFIGURED
    for maximum, noise in [(-0.01, 0.05), (math.nan, 0.05), (math.inf, 0.05),
                           (-math.inf, 0.05), (0.1, -0.01), (0.1, math.nan),
                           (0.1, math.inf), (0.1, -math.inf)]:
        model = Model(maximum=maximum, noise=noise)
        assert model.read("model_state") == UNCONFIGURED
        assert not model.start()
        assert not model.process([0.0] * 3)
    model = Model(count=16777216, maximum=0.0, noise=0.0)
    assert model.read("model_state") == WAITING
    assert model.configure()
    assert not model.call("model_null_arguments")
    assert model.call("model_alias_config")
    assert not model.call("model_null_config")
    assert model.read("model_state") == UNCONFIGURED
    assert model.configure()
    passed("非法相集合、窗口长度、NaN/Inf/负阈值及空指针拒绝；合法重配和配置别名可用")

    for mask in MASKS:
        model = Model(mask=mask)
        offsets = [0.03125, -0.0625, 0.015625]
        supplied = [value if mask & (1 << index) else math.nan
                    for index, value in enumerate(offsets)]
        model.ready(supplied)
        measured = [1.5, -0.25, -1.25]
        raw = [float32(measured[index] + offsets[index]) if mask & (1 << index) else math.inf
               for index in range(3)]
        assert model.process(raw)
        model.expect(measured)
        frozen = model.floats("model_offsets")
        assert not model.start()
        assert not model.calibrate([0.01, 0.02, 0.03])
        for _ in range(4):
            assert model.process(raw)
            assert model.floats("model_offsets") == frozen
    passed("AB/AC/BC/ABC 的偏置扣除、缺相重构和 Clarke 正确，未实测相 NaN/Inf 不读取")

    model = Model(mask=7)
    model.ready([0.015625, -0.03125, 0.0625])
    assert model.process([1.015625, 1.96875, 3.0625])
    model.expect([1.0, 2.0, 3.0])
    assert model.process([2.015625, 1.96875, 2.0625])
    model.expect([2.0, 2.0, 2.0])
    passed("ABC 保持非零和实测三相；Clarke 不把零序误作 alpha/beta，也不篡改相电流")

    for invalid in [([0.0, math.nan, 0.0], 6), ([0.0, math.inf, 0.0], 6),
                    ([0.0, 0.0, -math.inf], 6), ([0.0] * 3, 2),
                    ([0.0] * 3, 4), ([0.0] * 3, 0), ([0.0] * 3, 14)]:
        model = Model()
        assert model.start()
        assert not model.calibrate([0.0, 0.01, -0.01])
        saved = [model.floats(name) for name in ("model_means", "model_minima", "model_maxima")]
        assert not model.calibrate(*invalid)
        assert model.read("model_state") == FAILED and model.read("model_failure") == INVALID_SAMPLE
        assert model.read("model_count") == 1
        assert saved == [model.floats(name) for name in ("model_means", "model_minima", "model_maxima")]
        assert not model.start()
        assert not model.calibrate([0.0] * 3)
        assert model.read("model_count") == 1
        model.call("model_abort")
        assert model.read("model_failure") == INVALID_SAMPLE
        model.call("model_reset")
        assert model.read("model_state") == WAITING
        assert model.read("model_failure") == NONE and model.read("model_count") == 0
        model.ready()
    model = Model()
    assert model.start()
    assert not model.call("model_null_sample_calibrate")
    assert model.read("model_failure") == INVALID_SAMPLE
    passed("窗口缺相、非法掩码、NaN/Inf 和空样本使失败锁定；坏相不提交半拍诊断，显式 reset 才重试")

    model = Model()
    assert model.start()
    assert not model.calibrate([0.0, 0.01, -0.01])
    assert not model.calibrate([0.0] * 3, confirmed=False)
    assert model.read("model_state") == FAILED and model.read("model_failure") == UNCONFIRMED
    assert model.read("model_count") == 1
    for ready in [False, True]:
        model = Model()
        if ready:
            model.ready()
            assert model.process([0.0, 1.0, -1.0])
        else:
            assert model.start()
            assert not model.calibrate([0.0, 0.01, -0.01])
        count = model.read("model_count")
        model.call("model_abort")
        assert model.read("model_state") == FAILED and model.read("model_failure") == ABORTED
        assert model.read("model_count") == count
        assert model.floats("model_offsets") == [0.0] * 3
        assert not model.process([0.0] * 3)
        assert not model.start()
    passed("窗口失去真实零电流确认即失败；显式 abort 同时撤销 READY 零偏与输出且保留诊断")

    for sign in [-1.0, 1.0]:
        model = Model(count=4, maximum=0.0625, noise=0.125)
        assert model.start()
        for index in range(4):
            assert not model.calibrate([0.0, sign * 0.125, 0.0])
            assert model.floats("model_offsets") == [0.0] * 3
        assert model.read("model_state") == FAILED and model.read("model_failure") == OFFSET
    model = Model(count=2, maximum=0.0625, noise=0.125)
    assert model.start()
    assert not model.calibrate([0.0, 0.0, -0.125])
    assert model.calibrate([0.0, 0.125, 0.0])
    assert model.floats("model_offsets") == [0.0, 0.0625, -0.0625]
    model = Model(noise=0.03125)
    assert model.start()
    assert not model.calibrate([0.0, -0.03125, 0.0])
    assert not model.calibrate([0.0, 0.03125, 0.0])
    assert model.read("model_state") == FAILED and model.read("model_failure") == NOISE
    assert model.read("model_count") == 2
    assert model.floats("model_minima")[1] == -0.03125
    assert model.floats("model_maxima")[1] == 0.03125
    passed("完整窗口的正负均值超限和窗口中峰峰噪声超限拒绝，等于配置边界仍接受")

    model = Model()
    model.ready([0.0, 0.03125, -0.0625])
    frozen = model.floats("model_offsets")
    for bad, valid in [([0.0, math.nan, 0.0], 6), ([0.0, math.inf, 0.0], 6),
                       ([0.0] * 3, 2), ([0.0] * 3, 8)]:
        assert model.process([0.0, 1.03125, -1.0625])
        assert not model.process(bad, valid)
        assert model.read("model_state") == READY
        assert model.floats("model_offsets") == frozen
        assert model.process([math.nan, 1.03125, -1.0625], 7)
        model.expect([0.0, 1.0, -1.0])
    assert not model.call("model_null_sample_process")
    model.empty()
    model.call("model_reset")
    assert model.read("model_state") == WAITING and model.read("model_count") == 0
    assert model.floats("model_offsets") == [0.0] * 3
    passed("READY 后坏输入仅撤销当拍输出，恢复沿用冻结零偏；reset 清除零偏且不隐式开始")

    largest = float32(3.402823466e38)
    model = Model(mask=7, count=2, maximum=largest, noise=largest)
    assert model.start()
    assert not model.calibrate([largest, 0.0, 0.0])
    assert not model.calibrate([-largest, 0.0, 0.0])
    assert model.read("model_state") == FAILED and model.read("model_failure") == NUMERIC
    model = Model()
    model.ready()
    assert not model.process([0.0, largest, largest])
    assert model.read("model_state") == READY
    model = Model(mask=7)
    model.ready()
    assert model.process([largest, largest, largest])
    assert all(math.isfinite(value) for value in model.floats("model_output", 5))
    model = Model(mask=7, count=2, maximum=largest, noise=0.0)
    model.ready([-largest, 0.0, 0.0])
    assert not model.process([largest, 0.0, 0.0])
    assert model.read("model_state") == READY
    passed("有限极值引发的均值或缺相重构溢出返回无效；Clarke 避免不必要的倍乘中间溢出")

    model = Model()
    model.ready([0.0, 0.015625, -0.03125])
    first = model.floats("model_offsets")
    model.write("model_second", 1)
    assert model.configure(mask=3)
    model.ready([0.0625, -0.0625, math.nan])
    assert model.process([2.0625, -1.0625, math.nan])
    model.expect([2.0, -1.0, -1.0])
    model.call("model_abort")
    model.write("model_second", 0)
    model.mask = 6
    model.call("model_select")
    assert model.read("model_state") == READY and model.floats("model_offsets") == first
    assert model.process([math.nan, 1.015625, -1.03125])
    model.expect([0.0, 1.0, -1.0])
    passed("多个实例的相集合、窗口、冻结零偏和 abort 状态相互独立")

    randomizer = random.Random(4316701)
    for mask in MASKS:
        model = Model(mask=mask, count=4096)
        samples = [[float32(randomizer.uniform(-0.015, 0.015) + base)
                    for base in [0.015, -0.025, 0.035]] for _ in range(4096)]
        assert model.start()
        for index, values in enumerate(samples):
            assert model.calibrate(values) == (index == 4095)
        offsets = [statistics.fmean(values[index] for values in samples)
                   if mask & (1 << index) else 0.0 for index in range(3)]
        assert all(math.isclose(actual, target, abs_tol=0.0000003)
                   for actual, target in zip(model.floats("model_offsets"), offsets))
        for index in range(3):
            if mask & (1 << index):
                assert model.floats("model_minima")[index] == min(values[index] for values in samples)
                assert model.floats("model_maxima")[index] == max(values[index] for values in samples)
        for _ in range(150):
            raw = [float32(randomizer.uniform(-10.0, 10.0)) for _ in range(3)]
            corrected = [raw[index] - offsets[index] if mask & (1 << index) else 0.0
                         for index in range(3)]
            if mask != 7:
                missing = next(index for index in range(3) if not mask & (1 << index))
                corrected[missing] = -sum(corrected)
                raw[missing] = math.nan
            assert model.process(raw)
            model.expect(corrected)
    passed("四种相集合各 4096 样本均值/极值符合双精度参考，600 次随机重构/复数 Clarke 参考通过")

    for mask in MASKS:
        offsets = [0.03125, -0.0625, 0.015625]
        gains = [2.0, -0.5, 1.25]
        model = Model(mask=mask, gain_enabled=True, gains=gains)
        model.ready([offsets[index] if mask & (1 << index) else math.nan for index in range(3)])
        frozen = model.floats("model_offsets")
        nominal = [0.5, -0.75, 1.0]
        raw = [float32(offsets[index] + nominal[index]) if mask & (1 << index) else math.nan
               for index in range(3)]
        corrected = [nominal[index] * gains[index] if mask & (1 << index) else 0.0 for index in range(3)]
        if mask != 7:
            missing = next(index for index in range(3) if not mask & (1 << index))
            corrected[missing] = -sum(corrected)
        assert model.process(raw)
        model.expect(corrected)
        assert model.floats("model_offsets") == frozen
    passed("启用非单位正负增益后先校正实测相，再按 AB/AC/BC 重构缺相并计算独立复数 Clarke")

    for mask in MASKS:
        gains = [1.5 if mask & (1 << index) else math.nan for index in range(3)]
        model = Model(mask=mask, gain_enabled=True, gains=gains)
        model.ready([0.0 if mask & (1 << index) else math.inf for index in range(3)])
        assert model.process([1.0 if mask & (1 << index) else math.nan for index in range(3)])
        expected = [1.5 if mask & (1 << index) else 0.0 for index in range(3)]
        if mask != 7:
            expected[next(index for index in range(3) if not mask & (1 << index))] = -sum(expected)
        model.expect(expected)
    passed("启用修正时仅使用实测相增益，缺相增益与缺相样本 NaN/Inf 均不参与计算")

    for gains in [[math.nan, math.inf, -math.inf], [0.0, -0.0, 0.0], [2.0, -3.0, 4.0]]:
        model = Model(gain_enabled=False, gains=gains)
        model.ready([0.0, 0.03125, -0.0625])
        assert model.process([math.nan, 1.03125, -1.0625])
        model.expect([0.0, 1.0, -1.0])
    model = Model(gain_enabled=True, gains=[1.0, 2.0, -3.0])
    assert model.call("model_legacy_config")
    model.ready([0.0, 0.03125, -0.0625])
    assert model.process([math.nan, 1.03125, -1.0625])
    model.expect([0.0, 1.0, -1.0])
    passed("关闭增益修正时忽略整个数组包括 NaN/Inf/零；真实四字段聚合初始化保留原名义电流行为")

    for mask in MASKS:
        for phase in range(3):
            if not mask & (1 << phase):
                continue
            for invalid_gain in [0.0, -0.0, math.nan, math.inf, -math.inf]:
                gains = [1.0, 1.0, 1.0]
                gains[phase] = invalid_gain
                model = Model(mask=mask, gain_enabled=True, gains=gains)
                assert model.read("model_state") == UNCONFIGURED
                assert not model.start()
                assert not model.process([1.0, -0.5, -0.5])
    model = Model(gain_enabled=True, gains=[1.0, 2.0, -0.5])
    assert model.call("model_alias_config")
    model.ready()
    assert model.process([math.nan, 0.5, 2.0])
    model.expect([0.0, 1.0, -1.0])
    passed("启用后逐实测相拒绝零/NaN/Inf 系数；负系数合法，配置别名重新初始化保留修正")

    model = Model(gain_enabled=True, gains=[math.nan, 2.0, -0.5])
    model.ready([0.0, 0.03125, -0.0625])
    assert model.process([math.nan, 1.03125, 1.9375])
    model.expect([-1.0, 2.0, -1.0])
    # 输入配置与实例内配置是独立副本；改输入配置不改变已就绪实例。
    model.cpu.mem_write(symbols["model_config"], b"\0" * 16)
    model.write("model_gain_enabled", 0)
    model.cpu.mem_write(symbols["model_signed_gain"], struct.pack("<3f", math.nan, math.nan, math.nan))
    assert model.process([math.nan, 1.03125, 1.9375])
    model.expect([-1.0, 2.0, -1.0])
    model.call("model_reset")
    assert model.read("model_state") == WAITING
    model.ready([0.0, 0.0625, -0.03125])
    assert model.process([math.nan, 1.0625, 1.96875])
    model.expect([-1.0, 2.0, -1.0])
    passed("初始化复制增益且 READY 不读取外部配置；reset 保留使能和系数并要求重新校零")

    for gains, raw in [([1.0, largest, 1.0], [0.0, 2.0, 0.0]),
                       ([1.0, -largest, 1.0], [0.0, -2.0, 0.0]),
                       ([1.0, 2.0, 2.0], [0.0, largest * 0.375, largest * 0.375])]:
        model = Model(gain_enabled=True, gains=gains)
        model.ready([0.0, 0.03125, -0.0625])
        frozen = model.floats("model_offsets")
        assert not model.process(raw)
        assert model.read("model_state") == READY and model.read("model_failure") == NONE
        assert model.floats("model_offsets") == frozen and model.read("model_count") == 4
        assert model.process([math.nan, 0.03125, -0.0625])
        model.expect([0.0, 0.0, 0.0])
    passed("增益乘法或修正后缺相重构溢出只撤销当拍输出，READY/计数/冻结零偏保持且下一拍可恢复")

    for gain in [100.0, -100.0]:
        model = Model(count=2, maximum=0.0625, noise=0.125, gain_enabled=True, gains=[math.nan, gain, -gain])
        assert model.start()
        assert not model.calibrate([math.nan, 0.0, -0.125])
        assert model.calibrate([math.nan, 0.125, 0.0])
        assert model.floats("model_offsets") == [0.0, 0.0625, -0.0625]
    model = Model(count=2, maximum=0.0625, noise=0.125, gain_enabled=True,
                  gains=[math.nan, 0.001, -0.001])
    assert model.start()
    assert not model.calibrate([math.nan, 0.125, 0.0])
    assert not model.calibrate([math.nan, 0.125, 0.0])
    assert model.read("model_failure") == OFFSET
    model = Model(count=2, maximum=0.125, noise=0.03125, gain_enabled=True,
                  gains=[math.nan, 0.001, -0.001])
    assert model.start()
    assert not model.calibrate([math.nan, -0.03125, 0.0])
    assert not model.calibrate([math.nan, 0.03125, 0.0])
    assert model.read("model_failure") == NOISE
    passed("大正负增益不放大零偏窗口判限，小增益也不能掩盖名义 A 均值/噪声超限")

    randomizer = random.Random(6701431)
    for mask in MASKS:
        gains = [float32(randomizer.uniform(0.25, 3.0) * (-1 if index % 2 else 1)) for index in range(3)]
        offsets = [0.015625, -0.03125, 0.0625]
        model = Model(mask=mask, gain_enabled=True, gains=gains)
        model.ready(offsets)
        frozen = model.floats("model_offsets")
        for _ in range(200):
            raw = [float32(randomizer.uniform(-5.0, 5.0)) for _ in range(3)]
            corrected = [(raw[index] - frozen[index]) * gains[index] if mask & (1 << index) else 0.0
                         for index in range(3)]
            if mask != 7:
                missing = next(index for index in range(3) if not mask & (1 << index))
                corrected[missing] = -sum(corrected)
                raw[missing] = math.nan
            assert model.process(raw)
            model.expect(corrected)
    passed("四种相集合共 800 次非单位正负增益随机结果匹配独立双精度重构及复数 Clarke 参考")

print(f"PASS: {len(CASES)} CurrentMeasurement C behavior groups")
for name in CASES:
    print("  " + name)
