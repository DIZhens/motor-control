"""用 ARMCC/Unicorn 执行 Observe 正式电流统计 C；不连接或操作目标板。"""

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


ROOT = Path(__file__).resolve().parents[2] / 'timing'
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
IDLE, ACTIVE, COMPLETE, FAILED = range(4)
NONE, NOT_READY, ABORTED, INVALID_SAMPLE, TIMING, NUMERIC, BUSY = range(7)


def passed(name):
    CASES.append(name)


def run_checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def reference(samples):
    mean = math.fsum(samples) / len(samples)
    variance = math.fsum((value - mean) ** 2 for value in samples) / (len(samples) - 1)
    return mean, min(samples), max(samples), variance


with tempfile.TemporaryDirectory(prefix="observe-current-capture-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    objects = []
    for source in [Path(__file__).resolve().parents[2] / 'timing/current_capture.c', Path(__file__).resolve().parent / 'current_capture_model.c']:
        obj = temporary / (source.stem + ".o")
        run_checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
                     "--split_sections", "-g", "-I", str(ROOT), "-c", str(source),
                     "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    run_checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
                 "--entry=model_init", "--keep=current_capture_model.o(*)", "-o", str(executable),
                 *objects], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(segment["p_vaddr"], segment.data()) for segment in elf.iter_segments()
                    if segment["p_type"] == "PT_LOAD"]
        initializers = [(section["sh_addr"], section.data()) for section in elf.iter_sections()
                        if (section["sh_flags"] & 2) and section["sh_type"] == "SHT_PROGBITS"]
        symbols = {symbol.name: symbol["st_value"]
                   for symbol in elf.get_section_by_name(".symtab").iter_symbols()}
        sizes = {symbol.name: symbol["st_size"]
                 for symbol in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.call("model_init")

        def write(self, name, value, fmt="I"):
            self.cpu.mem_write(symbols[name], struct.pack("<" + fmt, value))

        def integer(self, name):
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=1000000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, name
            return bool(self.integer("model_result"))

        @property
        def words(self):
            return struct.unpack("<4I", self.cpu.mem_read(symbols["model_words"], 16))

        @property
        def output(self):
            return struct.unpack("<12f", self.cpu.mem_read(symbols["model_output"], 48))

        @property
        def times(self):
            return struct.unpack("<2Q", self.cpu.mem_read(symbols["model_times"], 16))

        @property
        def snapshot(self):
            return bytes(self.cpu.mem_read(symbols["model_capture"], sizes["model_capture"]))

        def start(self, request=1, period=1, offsets=(-0.0124, -0.0003), permitted=True):
            self.write("model_request", request)
            self.write("model_period", period, "Q")
            self.write("model_offset_b", offsets[0], "f")
            self.write("model_offset_c", offsets[1], "f")
            self.write("model_permitted", int(permitted))
            return self.call("model_start")

        def update(self, time, b, c, valid=True):
            self.write("model_time", time, "Q")
            self.write("model_raw_b", b, "f")
            self.write("model_raw_c", c, "f")
            self.write("model_valid", int(valid))
            return self.call("model_update")

        def abort(self, reason=ABORTED):
            self.write("model_reason", reason)
            return self.call("model_abort")

        def batch(self, samples, first_time, period=1):
            self.write("model_time", first_time, "Q")
            self.write("model_period", period, "Q")
            transitions = 0
            for index in range(0, len(samples), 512):
                batch = samples[index:index + 512]
                raw = struct.pack("<" + "ff" * len(batch),
                                  *(value for pair in batch for value in pair))
                self.cpu.mem_write(symbols["model_batch_samples"], raw)
                self.write("model_batch_count", len(batch))
                self.call("model_batch")
                transitions += self.integer("model_batch_transitions")
            return transitions

        def expect(self, request=1, state=ACTIVE, failure=NONE, count=0):
            assert self.words == (request, state, failure, count), self.words

        def expect_reference(self, samples, offsets):
            for phase, base in [(0, 2), (1, 6)]:
                expected = reference([f32(pair[phase]) for pair in samples])
                for actual, ref in zip(self.output[base:base + 4], expected):
                    assert math.isclose(actual, ref, rel_tol=1e-5, abs_tol=2e-7), (
                        phase, actual, ref)
                # 校正统计由主机扣冻结零偏；证明本模块未把注入电流重新学为零。
                assert math.isclose(self.output[base] - self.output[phase],
                                    expected[0] - f32(offsets[phase]), abs_tol=2e-7)
            assert self.output[:2] == tuple(map(f32, offsets))

    model = Model()
    model.expect(0, IDLE)
    assert model.output == (0.0,) * 12 and model.times == (0, 0)
    before = model.snapshot
    assert not model.update(0, math.nan, math.inf, False)
    assert not model.abort() and not model.call("model_null_arguments")
    assert model.snapshot == before
    passed("初始化 IDLE；非活动 update/abort 与空指针没有副作用")

    for request, period, offsets, permitted in [
            (0, 1, (0.0, 0.0), True), (1, 0, (0.0, 0.0), True),
            (1, 1, (0.0, 0.0), False),
            *[(1, 1, (bad, 0.0), True) for bad in [math.nan, math.inf, -math.inf]],
            *[(1, 1, (0.0, bad), True) for bad in [math.nan, math.inf, -math.inf]]]:
        assert not model.start(request, period, offsets, permitted)
        model.expect(request, FAILED, NOT_READY)
        assert model.output == (0.0,) * 12 and model.times == (0, 0)
        assert model.start()
        assert model.abort()
    passed("无许可、零请求、零间隔与非有限冻结偏置均明确 NOT_READY")

    model = Model()
    offsets = (-0.0124, 0.0003)
    assert model.start(17, 2, offsets)
    model.expect(17)
    samples = [(offsets[0] + 0.14, offsets[1] - 0.07)] * 2048
    assert model.batch(samples[:2047], 100, 2) == 0
    model.expect(17, ACTIVE, NONE, 2047)
    assert model.update(100 + 2047 * 2, *samples[-1])
    model.expect(17, COMPLETE, NONE, 2048)
    assert model.times == (100, 4194)
    model.expect_reference(samples, offsets)
    before = model.snapshot
    assert not model.update(9000, math.nan, math.inf, False)
    assert not model.abort(TIMING) and model.snapshot == before
    passed("仅第2048个连续样本完成；正负注入、冻结偏置、首末时间及结束后冻结正确")

    generator = random.Random(0x431C)
    for signs, request, period in [((1, -1), 23, 1), ((-1, 1), 24, 7)]:
        offsets = (-0.01242, -0.00034)
        samples = [(f32(offsets[0] + signs[0] * 0.128 + generator.uniform(-0.004, 0.004)),
                    f32(offsets[1] + signs[1] * 0.064 + generator.uniform(-0.003, 0.003)))
                   for _ in range(2048)]
        assert model.start(request, period, offsets)
        assert model.batch(samples, 0, period) == 1
        model.expect(request, COMPLETE, NONE, 2048)
        assert model.times == (0, 2047 * period)
        model.expect_reference(samples, offsets)
    passed("双方向带噪声2048点窗口与独立双精度两遍方差参考一致")

    for second_time in [100, 99, 102, 103]:
        model = Model()
        assert model.start()
        assert not model.update(100, 0.2, -0.1)
        before = model.output
        assert model.update(second_time, 0.3, -0.2)
        model.expect(1, FAILED, TIMING, 1)
        assert model.times == (100, 100) and model.output == before
        assert not model.abort(ABORTED)
        assert not model.update(101, 0.0, 0.0)
        model.expect(1, FAILED, TIMING, 1)
    model = Model()
    assert model.start(period=2)
    assert not model.update((1 << 64) - 2, 0.0, 0.0)
    assert model.update(0, 0.0, 0.0)
    model.expect(1, FAILED, TIMING, 1)
    passed("重复、回退、跳拍及64位时间回绕拒绝，失败不消费样本且锁存首因")

    for b, c, valid in [(math.nan, 0.0, True), (math.inf, 0.0, True),
                         (-math.inf, 0.0, True), (0.0, math.nan, True),
                         (0.0, math.inf, True), (0.0, -math.inf, True),
                         (0.1, -0.1, False)]:
        model = Model()
        assert model.start()
        assert not model.update(10, 0.12, -0.11)
        before = model.output
        assert model.update(11, b, c, valid)
        model.expect(1, FAILED, INVALID_SAMPLE, 1)
        assert model.output == before and model.times == (10, 10)
    model = Model()
    assert model.start()
    assert model.update(0, 0.0, 0.0, False)
    model.expect(1, FAILED, INVALID_SAMPLE, 0)
    passed("坏样本标志和任一相NaN/Inf明确拒绝，已接受的另一相统计不会半拍更新")

    maximum_float = f32(3.402823466e38)
    for first, second in [((maximum_float, 0.0), (-maximum_float, 0.0)),
                           ((0.0, maximum_float), (0.1, -maximum_float)),
                           ((1e20, 0.0), (-1e20, 0.0))]:
        model = Model()
        assert model.start()
        assert not model.update(1, *first)
        before = model.output
        assert model.update(2, *second)
        model.expect(1, FAILED, NUMERIC, 1)
        assert model.output == before and model.times == (1, 1)
    model = Model()
    assert model.start()
    assert not model.update(1, maximum_float, maximum_float)
    assert not model.update(2, maximum_float, maximum_float)
    model.expect(count=2)
    assert model.output[5] == model.output[9] == 0.0
    passed("差值或平方差溢出以NUMERIC失败；两相事务提交，合法大常量不误拒绝")

    for bad_m2 in [-1.0, math.nan, math.inf]:
        model = Model()
        assert model.start()
        assert not model.update(1, 0.1, -0.1)
        model.write("model_raw_b", bad_m2, "f")
        model.write("model_raw_c", 0.0, "f")
        model.call("model_corrupt_m2")
        assert model.update(2, 0.1, -0.1)
        model.expect(1, FAILED, NUMERIC, 1)
    passed("负或非有限M2不被静默钳零，原始统计状态异常明确锁定")

    for reason, expected in [(NONE, ABORTED), (ABORTED, ABORTED),
                              (TIMING, TIMING), (NUMERIC, NUMERIC), (99, ABORTED)]:
        model = Model()
        assert model.start(31)
        assert not model.update(10, 0.1, -0.2)
        before = model.output
        assert model.abort(reason)
        model.expect(31, FAILED, expected, 1)
        assert model.output == before and model.times == (10, 10)
        assert not model.abort(INVALID_SAMPLE)
        model.expect(31, FAILED, expected, 1)
    passed("显式撤销保留已接收诊断与首因；无效reason统一ABORTED")

    model = Model()
    assert model.start(41)
    assert not model.update(1, 0.2, -0.2)
    assert not model.start(42)
    model.expect(42, FAILED, BUSY, 0)
    assert model.output == (0.0,) * 12 and model.times == (0, 0)
    assert not model.update(2, 0.2, -0.2)
    assert model.start(43)
    model.expect(43)
    assert not model.update(0, 0.0, 0.0)
    assert not model.start(0, 0, (math.nan, math.inf), False)
    model.expect(0, FAILED, BUSY)
    passed("活动时新请求只记录新身份BUSY并清旧窗，不隐式替换或拼接样本")

    model = Model()
    assert model.start(51, 3, (0.02, -0.01))
    assert not model.update(5, 0.1, -0.2)
    before = model.snapshot
    assert model.call("model_second_instance") and model.snapshot == before
    model.write("model_offset_b", 100.0, "f")
    model.write("model_offset_c", 200.0, "f")
    assert not model.update(8, 0.12, -0.18)
    assert model.output[:2] == (f32(0.02), f32(-0.01))
    model.call("model_init")
    model.expect(0, IDLE)
    assert model.output == (0.0,) * 12 and model.times == (0, 0)
    passed("多实例互不干扰，外部参数改变不修改冻结offset，显式init彻底清除旧窗")

for index, name in enumerate(CASES, 1):
    print(f"PASS {index}: {name}")
print(f"observe current capture: {len(CASES)} groups passed")
