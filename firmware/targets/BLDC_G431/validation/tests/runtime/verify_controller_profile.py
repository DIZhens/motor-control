"""ARMCC/Unicorn 验证有限电流环探针；假 cycle 不作性能声明，正式 C 双编译逐位对照。"""

from pathlib import Path
import math
import os
import random
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_CPU_ARM_CORTEX_M4


TESTS = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[6]
RUNTIME = ROOT / 'firmware/targets/BLDC_G431/validation/timing'
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
IDLE, ACTIVE, COMPLETE, FAILED = range(4)
NONE, NOT_READY, BUSY, CLOCK, ORDER, INCOMPLETE, ABORTED, LIMIT = range(8)
DEFAULT_CONFIG = [2.0, 2.0, 1000.0, 1000.0, 200.0, 4.0, 0.6, 0.45, 0.0001]
DEFAULT_INPUT = [0.02, 0.127017, 0.6, 0.8, 12.0, 0.0, 0.18, 0.00005, 0.0, 1.0]


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def passed(name):
    CASES.append(name)


def expected_timing(samples):
    elapsed = [item[0] for item in samples]
    clean = [item[0] for item in samples if item[1] == 0]
    return ((len(elapsed), min(elapsed, default=0), max(elapsed, default=0),
             len(clean), min(clean, default=0), max(clean, default=0),
             sum(item[1] for item in samples) & 0xFFFFFFFF), (sum(elapsed), sum(clean)))


with tempfile.TemporaryDirectory(prefix="controller-profile-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x10000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    sources = [(Path(__file__).resolve().parents[2] / 'timing/controller_profile.c', []), (TESTS / 'controller_profile_model.c', []),
               (ROOT / 'firmware/control/current_controller.c', ["-DCONTROL_CURRENT_PROFILE=1"]),
               (ROOT / 'firmware/control/current_controller.c', [
                   "-Dcurrent_controller_init=unmarked_current_controller_init",
                   "-Dcurrent_controller_reset=unmarked_current_controller_reset",
                   "-Dcurrent_controller_update=unmarked_current_controller_update"]),
               (ROOT / 'firmware/control/foc_math.c', [])]
    objects = []
    for index, (source, definitions) in enumerate(sources):
        obj = temporary / f"source_{index}.o"
        checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
                 "--split_sections", "-g", "-I", str(RUNTIME), "-I", str(ROOT / 'firmware/control'),
                 *definitions, "-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=model_init", "--keep=source_1.o(*)", "-o", str(executable), *objects], temporary)
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
            self.publication = []
            # 只观察明确的发布字段；不为无关 C 库或算法栈写建立全地址钩子。
            result_address = symbols["g_observe_controller_profile_result"]
            self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.watch_publication,
                              begin=result_address,
                              end=result_address + sizes["g_observe_controller_profile_result"] - 1)
            self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.watch_publication,
                              begin=symbols["g_observe_controller_profile_sequence"],
                              end=symbols["g_observe_controller_profile_sequence"] + 3)
            self.call("model_init")
            self.configure()

        def watch_publication(self, cpu, access, address, size, value, context):
            if address == symbols["g_observe_controller_profile_sequence"]:
                self.publication.append(("sequence", value))
            begin = symbols["g_observe_controller_profile_result"]
            if begin <= address < begin + sizes["g_observe_controller_profile_result"]:
                assert self.integer("g_observe_controller_profile_sequence") & 1
                self.publication.append(("byte", address - begin))

        def write(self, name, value):
            self.cpu.mem_write(symbols[name], struct.pack("<I", value))

        def integer(self, name):
            return self.integers(name, 1)[0]

        def integers(self, name, count):
            return struct.unpack("<" + "I" * count, self.cpu.mem_read(symbols[name], 4 * count))

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x2000FFF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=1000000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                located = [symbol for symbol, address in symbols.items()
                           if address & ~1 <= pc < (address & ~1) + sizes.get(symbol, 0)]
                raise AssertionError(f"{name}: {error}; pc={pc:#x}; {located}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name
            assert self.integer("g_observe_controller_profile_sequence") % 2 == 0
            assert self.integer("model_public_changed_during_mark") == 0
            return bool(self.integer("model_result"))

        @property
        def words(self):
            return self.integers("model_words", 6)

        @property
        def timing(self):
            words = self.integers("model_timing_words", 49)
            return [words[index:index + 7] for index in range(0, 49, 7)]

        @property
        def sums(self):
            words = struct.unpack("<14Q", self.cpu.mem_read(symbols["model_sums"], 112))
            return [words[index:index + 2] for index in range(0, 14, 2)]

        @property
        def public(self):
            return bytes(self.cpu.mem_read(symbols["g_observe_controller_profile_result"],
                                           sizes["g_observe_controller_profile_result"]))

        def configure(self, config=DEFAULT_CONFIG):
            self.cpu.mem_write(symbols["model_config"], struct.pack("<9f", *config))
            assert self.call("model_configure")

        def start(self, request=1, null=False):
            self.write("model_request", request)
            self.write("model_start_null", int(null))
            return self.call("model_start")

        def prepare(self, cycles=None, irqs=None, fail_read=0):
            self.write("model_fail_read", fail_read)
            self.cpu.mem_write(symbols["model_cycles"], struct.pack("<7I", *(cycles or
                                     [100 + 10 * index for index in range(7)])))
            self.cpu.mem_write(symbols["model_irqs"], struct.pack("<7I", *(irqs or [0] * 7)))
            self.call("model_prepare")

        def mark(self, mark):
            self.write("model_mark", mark)
            self.call("model_record_mark")

        def finish(self, valid=True):
            self.write("model_valid", int(valid))
            self.call("model_finish")

        def freeze(self, success=True):
            self.write("model_success", int(success))
            self.call("model_freeze")

        def manual(self, cycles=None, irqs=None, fail_read=0, marks=range(7), valid=True):
            self.prepare(cycles, irqs, fail_read)
            for mark in marks:
                self.mark(mark)
            self.finish(valid)

        def controller(self, inputs=DEFAULT_INPUT, flags=(1, 1, 1, 1, 0), mode=0,
                       bad_value=math.nan):
            self.cpu.mem_write(symbols["model_inputs"], struct.pack("<10f", *inputs))
            self.cpu.mem_write(symbols["model_flags"], struct.pack("<5I", *flags))
            self.write("model_mode", mode)
            self.cpu.mem_write(symbols["model_bad_value"], struct.pack("<f", bad_value))
            self.call("model_controller")
            values = bytes(self.cpu.mem_read(symbols["model_outputs"], 88))
            status = self.integers("model_status", 8)
            assert values[:44] == values[44:]
            assert status[:4] == status[4:]
            return bool(status[1])

        def expect(self, state, failure=NONE, count=0, valid=0, rejected=0, request=1):
            assert self.words == (request, state, failure, count, valid, rejected), self.words

    model = Model()
    model.expect(IDLE, request=0)
    before = model.public
    for mark in [0, 1, 6, 7, 0xFFFFFFFF]:
        model.mark(mark)
    model.finish()
    model.freeze()
    assert model.public == before and model.integer("model_read_calls") == 0
    assert model.integers("model_sizes", 2) == (56, 416)
    published_size = sizes["g_observe_controller_profile_result"]
    assert len(model.publication) == published_size + 2
    assert model.publication[0][0] == "sequence" and model.publication[0][1] & 1
    assert model.publication[-1][0] == "sequence" and model.publication[-1][1] % 2 == 0
    assert sorted(item[1] for item in model.publication if item[0] == "byte") == list(range(published_size))
    passed("初始化与非活动钩子无读钟副作用；ARM布局56B/416B，发布严格奇数/全部字节/偶数")

    model = Model()
    assert model.start(11)
    active_snapshot = model.public
    active_sequence = model.integer("g_observe_controller_profile_sequence")
    expected = [[] for _ in range(7)]
    for index in range(160):
        accepted = index % 5 != 4
        stride = 9 + index % 9
        cycles = [1000 + item * stride for item in range(7)]
        irqs = [10 + (item // 2 if index % 2 else 0) for item in range(7)]
        model.prepare(cycles, irqs)
        inputs = list(DEFAULT_INPUT)
        inputs[4] = 0.1 if index % 5 == 3 else 12.0
        inputs[6] = 0.1 + (index % 4) * 0.1
        valid = model.controller(inputs, (1, 1, int(accepted), 1, index % 2))
        assert valid == accepted
        assert model.integer("model_read_calls") == (7 if accepted else 1)
        model.finish(valid)
        assert model.public == active_snapshot
        assert model.integer("g_observe_controller_profile_sequence") == active_sequence
        if accepted:
            for row in range(7):
                begin, end = (0, 6) if row == 0 else (row - 1, row)
                expected[row].append((cycles[end] - cycles[begin], irqs[end] - irqs[begin]))
    model.freeze()
    model.expect(COMPLETE, count=160, valid=128, rejected=32, request=11)
    for row in range(7):
        words, sums = expected_timing(expected[row])
        assert model.timing[row] == words and model.sums[row] == sums
    before = model.public
    model.manual()
    model.freeze(False)
    assert model.public == before
    passed("真实marked/unmarked算法160次逐位同输出/历史；128有效与32拒绝完整，ACTIVE不发布且拒绝不混入分段")

    for fail_read in range(1, 8):
        model = Model()
        assert model.start()
        model.manual()
        model.prepare(fail_read=fail_read)
        assert model.controller()
        assert model.integer("model_read_calls") == fail_read
        model.finish()
        model.expect(FAILED, CLOCK, count=1, valid=1)
        assert all(item[0] == 1 for item in model.timing)
        before = model.public
        model.freeze(False)
        assert model.public == before
    for end in range(1, 7):
        for delta in [0, -1, 0x80000000]:
            model = Model()
            assert model.start()
            model.manual()
            cycles = [100 + index * 10 for index in range(7)]
            cycles[end] = (cycles[end - 1] + delta) & 0xFFFFFFFF
            model.manual(cycles)
            model.expect(FAILED, CLOCK, count=1, valid=1)
            assert all(item == (1, 60 if row == 0 else 10, 60 if row == 0 else 10,
                                1, 60 if row == 0 else 10, 60 if row == 0 else 10, 0)
                       for row, item in enumerate(model.timing))
    passed("七个stamp读取逐一失败与各区间零/倒退/半圈差锁存CLOCK，不提交半份统计且保留首因")

    for marks in [[1], [0, 2], [0, 0], [0, 1, 1], list(range(7)) + [7], []]:
        model = Model()
        assert model.start()
        model.manual(marks=marks)
        model.expect(FAILED, ORDER)
    for mark_count in range(1, 7):
        model = Model()
        assert model.start()
        model.manual(marks=range(mark_count))
        model.expect(FAILED, INCOMPLETE)
        assert all(item == (0,) * 7 for item in model.timing)
        model = Model()
        assert model.start()
        model.manual(marks=range(mark_count), valid=False)
        model.freeze(False)
        model.expect(FAILED, ABORTED, count=1, rejected=1)
        assert all(item == (0,) * 7 for item in model.timing)
    model = Model()
    assert model.start()
    model.manual(valid=False)
    model.expect(FAILED, ORDER)
    model = Model()
    assert model.start()
    model.prepare(fail_read=1)
    model.mark(0)
    model.mark(7)
    model.finish()
    model.expect(FAILED, CLOCK)
    model = Model()
    assert model.start()
    model.prepare(fail_read=1)
    model.mark(0)
    assert not model.start(2)
    model.expect(FAILED, CLOCK)
    passed("mark跳号/重复/重入和无调用finish拒绝；半份有效不提交、预期早退只计拒绝，首因不覆盖")

    for count, valid in [(129, True), (33, False)]:
        model = Model()
        assert model.start()
        for _ in range(count):
            model.manual(marks=range(7) if valid else [0], valid=valid)
        accepted = count - 1
        model.expect(FAILED, LIMIT, accepted, accepted if valid else 0, 0 if valid else accepted)
        assert all(item[0] == (accepted if valid else 0) for item in model.timing)
    model = Model()
    assert model.start()
    for _ in range(128):
        model.manual()
    for _ in range(32):
        model.manual(marks=[0], valid=False)
    model.prepare()
    model.mark(0)
    assert model.integer("model_read_calls") == 0
    model.finish()
    model.expect(FAILED, LIMIT, 160, 128, 32)
    model = Model()
    assert model.start()
    model.freeze()
    model.expect(FAILED, INCOMPLETE)
    passed("有效/拒绝和总调用均有固定上界；额外调用不读钟，提前成功声明不得变COMPLETE")

    for request, null in [(0, False), (1, True)]:
        model = Model()
        assert not model.start(request, null)
        model.expect(FAILED, NOT_READY, request=request)
        assert model.integer("model_read_calls") == 0
    model = Model()
    assert model.start(21)
    model.manual()
    before = model.public
    assert not model.start(21, True) and model.public == before
    assert not model.start(22)
    model.expect(FAILED, BUSY, count=1, valid=1, request=21)
    assert not model.start(21) and model.words[2] == BUSY
    assert model.start(23)
    model.prepare()
    model.mark(0)
    model.freeze()
    model.expect(FAILED, INCOMPLETE, request=23)
    assert model.start(24)
    model.freeze(False)
    model.expect(FAILED, ABORTED, request=24)
    model.write("g_observe_controller_profile_sequence", 0xFFFFFFFE)
    assert model.start(25)
    assert model.integer("g_observe_controller_profile_sequence") == 0
    model.freeze(False)
    assert model.integer("g_observe_controller_profile_sequence") == 2
    passed("非法启动、同身份忽略、忙状态保留原身份/已接受统计，新身份重试及seqlock32位回绕确定")

    model = Model()
    assert model.start()
    cycles = [(0xFFFFFFF0 + index * 10) & 0xFFFFFFFF for index in range(7)]
    irqs = [0xFFFFFFFF] + [0] * 6
    model.manual(cycles, irqs)
    model.freeze(False)
    model.expect(FAILED, ABORTED, count=1, valid=1)
    assert model.timing[0] == (1, 60, 60, 0, 0, 0, 1)
    assert model.timing[1] == (1, 10, 10, 0, 0, 0, 1)
    assert all(model.timing[row] == (1, 10, 10, 1, 10, 10, 0) for row in range(2, 7))
    model = Model()
    assert model.start()
    cycles = [100 + index * 340000000 for index in range(7)]
    for _ in range(128):
        model.manual(cycles)
    for _ in range(32):
        model.manual(marks=[0], valid=False)
    model.freeze()
    model.expect(COMPLETE, count=160, valid=128, rejected=32)
    assert model.sums[0] == (128 * 2040000000,) * 2
    assert all(model.sums[row] == (128 * 340000000,) * 2 for row in range(1, 7))
    passed("cycle与IRQ独立32位回绕、相邻clean归属正确，全部总和/clean总和保留超过32位结果")

    generator = random.Random(0xC4317)
    model = Model()
    for index in range(320):
        inputs = list(DEFAULT_INPUT)
        theta = generator.uniform(-math.pi, math.pi)
        inputs[:4] = [generator.uniform(-0.5, 0.5), generator.uniform(-0.5, 0.5),
                      math.sin(theta), math.cos(theta)]
        inputs[4:7] = [generator.uniform(0.1, 24.0), generator.uniform(-0.3, 0.3),
                       generator.uniform(-0.3, 0.3)]
        inputs[8:] = [math.sin(theta + 0.03), math.cos(theta + 0.03)]
        flags = [1, 1, 1, 1, index % 2]
        if index % 11 == 0:
            inputs[index % 8] = math.nan
        elif index % 13 == 0:
            flags[index % 4] = 0
        model.prepare()
        model.controller(inputs, flags)
    for mode in range(1, 6):
        model.configure()
        model.prepare()
        model.controller(mode=mode)
    passed("320组随机双角/限幅/坏输入及空指针、配置/历史损坏，带与不带mark正式算法输出/积分逐位一致")

for index, name in enumerate(CASES, 1):
    print(f"PASS {index}: {name}")
print(f"controller profile: {len(CASES)} groups passed; synthetic stamps, no performance claim")
