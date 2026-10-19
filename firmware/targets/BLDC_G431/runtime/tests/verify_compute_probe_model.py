"""用 ARMCC/Unicorn 验证正式无功率计算模块；假 cycle 仅验证计时语义，不是实测性能。"""

from pathlib import Path
import math
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_CPU_ARM_CORTEX_M4


TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parents[4]
RUNTIME = TESTS.parent
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
IDLE, ACTIVE, COMPLETE, FAILED = range(4)
NONE, NOT_READY, ABORTED, CLOCK, ALGORITHM, BUSY = range(6)


def passed(name):
    CASES.append(name)


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


with tempfile.TemporaryDirectory(prefix="observe-compute-probe-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x10000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    sources = [RUNTIME / "compute_probe.c", TESTS / "compute_probe_model.c"]
    sources += [ROOT / "control" / name for name in ["current_measurement.c", "electrical_angle.c",
                "current_controller.c", "speed_controller.c", "foc_math.c"]]
    objects = []
    for source in sources:
        obj = temporary / (source.stem + ".o")
        checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
                 "--split_sections", "-g", "-I", str(RUNTIME), "-I", str(ROOT / "control"),
                 "-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=model_init", "--keep=compute_probe_model.o(*)", "-o", str(executable),
             *objects], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(p["p_vaddr"], p.data()) for p in elf.iter_segments() if p["p_type"] == "PT_LOAD"]
        initializers = [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                        if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
        sizes = {s.name: s["st_size"] for s in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address, size in [(0x08000000, 0x40000), (0x20000000, 0x10000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.call("model_init")

        def write(self, name, value):
            self.cpu.mem_write(symbols[name], struct.pack("<I", value))

        def integers(self, name, count):
            return struct.unpack("<" + "I" * count, self.cpu.mem_read(symbols[name], 4 * count))

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x2000FFF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=1000000)
            except UcError as error:
                raise AssertionError(f"{name}: {error}; pc={self.cpu.reg_read(UC_ARM_REG_PC):#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name
            return bool(self.integers("model_result", 1)[0])

        @property
        def words(self):
            return self.integers("model_words", 8)

        @property
        def statistics(self):
            words = self.integers("model_case_words", 36)
            return [words[index:index + 6] for index in range(0, 36, 6)]

        @property
        def sums(self):
            return struct.unpack("<6Q", self.cpu.mem_read(symbols["model_sums"], 48))

        @property
        def duties(self):
            words = struct.unpack("<18f", self.cpu.mem_read(symbols["model_duties"], 72))
            return [words[index:index + 3] for index in range(0, 18, 3)]

        @property
        def phasors(self):
            return struct.unpack("<4f", self.cpu.mem_read(symbols["model_phasors"], 16))

        @property
        def integrals(self):
            return struct.unpack("<3f", self.cpu.mem_read(symbols["model_integrals"], 12))

        @property
        def snapshot(self):
            return bytes(self.cpu.mem_read(symbols["model_probe"], sizes["model_probe"]))

        def start(self, request=1, clock_hz=170000000, maximum=20000, permitted=True):
            for name, value in [("model_request", request), ("model_clock_hz", clock_hz),
                                ("model_max_elapsed", maximum), ("model_permitted", int(permitted))]:
                self.write(name, value)
            return self.call("model_start")

        def step(self, begin=100, end=300, permitted=True, fail_read=0):
            for name, value in [("model_clock_begin", begin), ("model_clock_end", end),
                                ("model_permitted", int(permitted)), ("model_clock_fail_read", fail_read)]:
                self.write(name, value)
            return self.call("model_step")

        def abort(self, reason=ABORTED):
            self.write("model_reason", reason)
            return self.call("model_abort")

        def expect(self, request=1, state=ACTIVE, failure=NONE, count=0, next_case=0):
            words = self.words
            assert (words[0], words[1], words[2], words[5], words[6]) == (
                request, state, failure, count, next_case), words

    model = Model()
    model.expect(0, IDLE)
    assert model.statistics == [(0,) * 6] * 6 and model.sums == (0,) * 6
    before = model.snapshot
    assert not model.step(permitted=False, fail_read=1)
    assert not model.abort() and not model.call("model_null_arguments")
    assert model.snapshot == before and model.words[7] == 0
    assert not model.call("model_revoke") and model.snapshot == before
    passed("初始化、非活动 step/abort 和空指针无副作用，不读取假时钟")

    for request, clock_hz, maximum, permitted in [(0, 1, 1, True), (1, 0, 1, True),
            (1, 1, 0, True), (1, 1, 0x80000000, True), (1, 1, 0xFFFFFFFF, True),
            (1, 1, 1, False)]:
        model = Model()
        assert not model.start(request, clock_hz, maximum, permitted)
        model.expect(request, FAILED, NOT_READY)
        assert not model.step() and model.words[7] == 0
    passed("零身份、零频率、零或半圈以上耗时上界、无许可均拒绝且不自行重试")

    model = Model()
    assert model.start(11)
    model.expect(11)
    assert model.words[3:5] == (170000000, 20000)
    expected = [[] for _ in range(6)]
    for index in range(96):
        case = index % 6
        elapsed = 20 + case * 11 + ((index // 6) * 7) % 29
        expected[case].append(elapsed)
        assert model.step(1000, 1000 + elapsed) == (index == 95)
        model.expect(11, COMPLETE if index == 95 else ACTIVE, NONE, index + 1, (case + 1) % 6)
        assert model.words[7] == 2
        assert model.statistics[case][:3] == (len(expected[case]), min(expected[case]), max(expected[case]))
        assert model.sums[case] == sum(expected[case])
    assert [item[3] for item in model.statistics] == [1, 1, 1, 1, 0, 0]
    assert model.statistics[3][4:] == (1, 1)
    assert all(math.isfinite(duty) and 0 <= duty <= 1 for row in model.duties for duty in row)
    assert model.duties[4:] == [(0.5, 0.5, 0.5)] * 2
    before = model.snapshot
    assert not model.step(1, 1, False, 1) and not model.abort(CLOCK)
    assert model.snapshot == before
    passed("六例轮转各16次后冻结；独立 min/max/64位sum正确，普通及双角、限幅和拒绝路径执行")

    before_stats, before_sums, before_duties = model.statistics, model.sums, model.duties
    assert model.call("model_revoke")
    model.expect(11, FAILED, NOT_READY, 96)
    assert (model.statistics, model.sums, model.duties) == (before_stats, before_sums, before_duties)
    before = model.snapshot
    assert not model.call("model_revoke") and not model.abort(CLOCK) and model.snapshot == before
    model = Model()
    assert model.start(12)
    assert not model.step()
    assert model.call("model_revoke")
    model.expect(12, FAILED, NOT_READY, 1, 1)
    model = Model()
    assert model.start(13) and model.step(fail_read=1)
    before = model.snapshot
    assert not model.call("model_revoke") and model.snapshot == before
    model.expect(13, FAILED, CLOCK)
    passed("发布前复核可撤销ACTIVE或COMPLETE并保留统计，IDLE无影响且不覆盖已有失败首因")

    model = Model()
    assert model.start()
    assert not model.step()
    theta = 2 * math.pi * ((7 * (0.04 + 60 / 60 * 0.00002) - 0.125) % 1)
    sine, cosine = math.sin(theta), math.cos(theta)
    assert math.isclose(model.phasors[0], sine, abs_tol=5e-7)
    assert math.isclose(model.phasors[1], cosine, abs_tol=5e-7)
    # 独立双精度参考：名义 B/C 经冻结 offset、signed gain，再重构与幅值不变 Clarke。
    alpha, beta = 0.02, 0.22 / math.sqrt(3)
    direct, quadrature = alpha * cosine + beta * sine, -alpha * sine + beta * cosine
    vd, vq = -2 * direct, 2 * (0.1812 - quadrature)
    va, vb = vd * cosine - vq * sine, vd * sine + vq * cosine
    phases = [va, -0.5 * va + math.sqrt(3) / 2 * vb, -0.5 * va - math.sqrt(3) / 2 * vb]
    common = -(max(phases) + min(phases)) / 2
    expected_duties = [0.5 + (phase + common) / 12 for phase in phases]
    for actual, expected_duty in zip(model.duties[0], expected_duties):
        assert math.isclose(actual, expected_duty, abs_tol=2e-7), (actual, expected_duty)
    speed_integral = model.integrals[0]
    assert not model.step()  # 速度保持例不能产生新的速度 PI 更新。
    assert model.integrals[0] == speed_integral
    assert not model.step()  # 施加角和测量角确实不同，且均由正式 angle 模块计算。
    assert model.phasors[:2] != model.phasors[2:]
    predicted = 2 * math.pi * ((7 * (0.04 + 60 / 60 * 0.00007) - 0.125) % 1)
    assert math.isclose(model.phasors[2], math.sin(predicted), abs_tol=5e-7)
    assert math.isclose(model.phasors[3], math.cos(predicted), abs_tol=5e-7)
    passed("真实算法首例与独立双精度 duty参考一致，双角例确实采用不同预测时间")

    assert not model.step()  # 限幅例依然按正式控制算法提交独立积分历史。
    before_integrals, before_phasors = model.integrals, model.phasors
    assert before_integrals[1:] != (0.0, 0.0)
    assert not model.step()  # 坏测量在前段拒绝，不触碰下游控制历史。
    assert model.integrals == before_integrals and model.phasors == before_phasors
    assert model.duties[4] == (0.5, 0.5, 0.5) and model.statistics[4][3:] == (0, 0, 0)
    assert not model.step()  # 坏控制输入由正式 current_controller 清除其独立积分。
    assert model.integrals[1:] == (0.0, 0.0)
    assert model.duties[5] == (0.5, 0.5, 0.5) and model.statistics[5][3:] == (0, 0, 0)
    model.expect(count=6)
    passed("速度保持不更新积分；坏测量不触碰下游历史，坏控制输入清历史且仅记录预期拒绝")

    for begin, end in [(0, 1), (0xFFFFFFFF, 0), (0xFFFFFF00, 0x100), (500, 20500)]:
        model = Model()
        assert model.start()
        assert not model.step(begin, end)
        elapsed = (end - begin) & 0xFFFFFFFF
        model.expect(count=1, next_case=1)
        assert model.statistics[0][:3] == (1, elapsed, elapsed) and model.sums[0] == elapsed
    passed("cycle零起点、最小正差值、单次32位回绕与上界等值均合法")

    model = Model()
    assert model.start(maximum=0x7FFFFFFF)
    for index in range(96):
        assert model.step(1, 0x80000000) == (index == 95)
    assert model.sums == (16 * 0x7FFFFFFF,) * 6
    passed("统计总和正确超过32位；每例固定次数下64位加总不溢出")

    for begin, end, fail_read, calls in [(100, 100, 0, 2), (101, 100, 0, 2),
            (0, 20001, 0, 2), (0, 0x80000000, 0, 2), (0, 100, 1, 1), (0, 100, 2, 2)]:
        model = Model()
        assert model.start()
        assert not model.step()
        before_stats, before_sums = model.statistics, model.sums
        assert model.step(begin, end, fail_read=fail_read)
        model.expect(state=FAILED, failure=CLOCK, count=1, next_case=1)
        assert model.words[7] == calls and model.statistics == before_stats and model.sums == before_sums
        before = model.snapshot
        assert not model.step() and not model.abort(ALGORITHM) and model.snapshot == before
    model = Model()
    assert model.start() and model.call("model_step_null_clock")
    model.expect(state=FAILED, failure=CLOCK)
    assert model.words[7] == 0
    passed("停滞、倒退、越界、任一读时钟失败与空时钟回调锁存CLOCK，失败计时不进入统计")

    model = Model()
    assert model.start()
    assert not model.step()
    before_stats = model.statistics
    assert model.step(permitted=False)
    model.expect(state=FAILED, failure=NOT_READY, count=1, next_case=1)
    assert model.words[7] == 0 and model.statistics == before_stats
    passed("当拍许可撤销在读时钟之前终止，保留已有统计")

    for reason, expected_reason in [(NONE, ABORTED), (ABORTED, ABORTED), (CLOCK, CLOCK),
                                    (NOT_READY, NOT_READY), (ALGORITHM, ALGORITHM), (99, ABORTED)]:
        model = Model()
        assert model.start()
        assert not model.step()
        assert model.abort(reason)
        model.expect(state=FAILED, failure=expected_reason, count=1, next_case=1)
        before = model.snapshot
        assert not model.abort(BUSY) and not model.step() and model.snapshot == before
    passed("显式abort保留已完成计时及首因，无效原因统一ABORTED，结束后不覆盖结果")

    model = Model()
    assert model.start(31)
    assert not model.step()
    before = model.snapshot
    assert not model.start(31, 0, 0, False) and model.snapshot == before
    assert not model.start(32)
    model.expect(32, FAILED, BUSY)
    assert model.statistics == [(0,) * 6] * 6
    before = model.snapshot
    assert not model.start(32) and model.snapshot == before
    assert not model.step()
    assert model.start(33)
    model.expect(33)
    assert model.abort()
    before = model.snapshot
    assert not model.start(33) and model.snapshot == before
    passed("同身份无副作用；活动中不同请求仅记BUSY，新身份显式重试才能重新开始")

    model = Model()
    assert model.start()
    assert not model.step()
    before_stats = model.statistics
    model.call("model_corrupt_current")
    assert model.step()
    model.expect(state=FAILED, failure=ALGORITHM, count=1, next_case=1)
    assert model.statistics == before_stats and model.words[7] == 2
    passed("实际算法异常返回锁存ALGORITHM，已完成统计保留，不把无效duty记为有效结果")

    model = Model()
    assert model.start(41)
    assert not model.step()
    before = model.snapshot
    assert model.call("model_second_instance") and model.snapshot == before
    model.call("model_init")
    model.expect(0, IDLE)
    assert model.statistics == [(0,) * 6] * 6 and model.sums == (0,) * 6
    passed("独立实例不共享算法历史或结果，显式init完整清空")

for index, name in enumerate(CASES, 1):
    print(f"PASS {index}: {name}")
print(f"observe compute probe: {len(CASES)} groups passed; clock values are synthetic, no performance claim")
