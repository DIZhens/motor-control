"""ARMCC/Unicorn 验证正式 CORDIC 适配器；寄存器替身不提供实体精度或性能结论。"""

from pathlib import Path
import math
import os
import random
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_MEM_WRITE
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP,
                              UC_ARM_REG_PRIMASK, UC_CPU_ARM_CORTEX_M4)


TESTS = Path(__file__).resolve().parent
RUNTIME = Path(__file__).resolve().parents[3] / 'platform'
TARGET = Path(__file__).resolve().parents[3]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CSR, WDATA, RDATA = 0x40020C00, 0x40020C04, 0x40020C08
RCC_ENABLE = 0x40021048
EXPECTED, READY = 0x00180060, 0x80000000
CASES = []


def passed(name):
    CASES.append(name)


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


with tempfile.TemporaryDirectory(prefix="g431-cordic-math-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x10000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    objects = []
    for source in [TARGET / 'platform/cordic_math.c', TESTS / 'cordic_math_model.c']:
        obj = temporary / (source.stem + ".o")
        checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
                 "--split_sections", "-g", "-DSTM32G431xx", "-I", str(RUNTIME),
                 "-I", str(TARGET / 'Drivers/CMSIS/Include'), "-I",
                 str(TARGET / 'Drivers/CMSIS/Device/ST/STM32G4xx/Include'),
                 "-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=model_init", "--keep=cordic_math_model.o(*)", "-o", str(executable),
             *objects], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(p["p_vaddr"], p.data()) for p in elf.iter_segments() if p["p_type"] == "PT_LOAD"]
        initializers = [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                        if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self, configuration=0x50, clock=True, stale=False, initialize=True):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address, size in [(0x08000000, 0x40000), (0x20000000, 0x10000),
                                  (0x40020000, 0x2000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.set_register(RCC_ENABLE, 8 if clock else 0)
            self.set_register(CSR, configuration | (READY if stale else 0))
            self.writes, self.configuration_writes, self.arguments = [], [], []
            self.pending, self.reads, self.polls = stale, 0, 0
            self.ready_after = 1
            self.results = [0, 0]
            self.forced_results = None
            self.keep_ready = False
            self.corrupt_after_result = False
            self.reservation_losses = 0
            self.strex_attempts = 0
            self.reservation_pause = None
            self.reservation_resume = False
            self.busy_after_loss = False
            self.cpu.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self.peripheral_access,
                              begin=0x40020000, end=0x40021FFF)
            if initialize:
                self.call("model_init")

        def set_register(self, address, value):
            self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

        def register(self, address):
            return struct.unpack("<I", self.cpu.mem_read(address, 4))[0]

        def peripheral_access(self, cpu, access, address, size, value, _):
            assert size == 4, (address, size)
            if access == UC_MEM_WRITE:
                if address == CSR:
                    assert not self.pending and value == EXPECTED
                    self.configuration_writes.append(value)
                    return
                assert address == WDATA, f"禁止改变其他外设寄存器: {address:#x}"
                self.writes.append(value & 0xFFFFFFFF)
                self.arguments.append(value & 0xFFFFFFFF)
                assert len(self.arguments) <= 2 and not self.pending
                if len(self.arguments) == 2:
                    assert self.arguments[1] == 0x7FFFFFFF, self.arguments
                    angle = struct.unpack("<i", struct.pack("<I", self.arguments[0]))[0]
                    theta = angle / 2**31 * math.pi
                    quantize = lambda x: max(-2**31, min(2**31 - 1, round(x * (2**31 - 1))))
                    self.results = self.forced_results or [quantize(math.cos(theta)), quantize(math.sin(theta))]
                    self.pending, self.polls, self.reads = True, 0, 0
                    self.arguments = []
            elif address == CSR and self.pending:
                self.polls += 1
                if self.ready_after is not None and self.polls >= self.ready_after:
                    self.set_register(CSR, self.register(CSR) | READY)
            elif address == RDATA:
                assert self.pending and self.register(CSR) & READY, "不能提前读 RDATA，实体会阻塞总线"
                self.set_register(RDATA, self.results[self.reads])
                self.reads += 1
                if self.reads == 2:
                    self.pending = False
                    if not self.keep_ready:
                        self.set_register(CSR, self.register(CSR) & ~READY)
                    if self.corrupt_after_result:
                        self.set_register(CSR, self.register(CSR) | (1 << 8))

        def write(self, name, value):
            self.cpu.mem_write(symbols[name], struct.pack("<I", value))

        def integers(self, name, count):
            return struct.unpack("<" + "I" * count, self.cpu.mem_read(symbols[name], 4 * count))

        def call(self, name):
            before_mask = self.cpu.reg_read(UC_ARM_REG_PRIMASK)
            self.cpu.reg_write(UC_ARM_REG_SP, 0x2000FFF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                next_pc = symbols[name]
                while True:
                    self.cpu.emu_start(next_pc | 1, 0x0803F000, count=100000)
                    if self.reservation_pause is None:
                        break
                    next_pc = self.reservation_pause
                    self.reservation_pause = None
                    helper = symbols["model_clear_exclusive"] & ~1
                    # 仅执行真实CLREX，不恢复整个CPU context，以保留exclusive monitor的清除。
                    assert bytes(self.cpu.mem_read(helper, 4)) == bytes.fromhex("bf f3 2f 8f")
                    self.cpu.emu_start(helper | 1, helper + 4, count=1)
                    assert self.cpu.reg_read(UC_ARM_REG_PC) == helper + 4
                    if self.busy_after_loss:
                        self.cpu.mem_write(symbols["model_context"], struct.pack("<I", 1))
                    self.reservation_resume = True
            except UcError as error:
                raise AssertionError(f"{name}: {error}; pc={self.cpu.reg_read(UC_ARM_REG_PC):#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name
            assert self.cpu.reg_read(UC_ARM_REG_PRIMASK) == before_mask, "不允许屏蔽或开启调用方中断"
            return bool(self.integers("model_result", 1)[0])

        def inject_reservation_losses(self, losses, busy_after_loss=False):
            self.reservation_losses = losses
            self.strex_attempts = 0
            self.busy_after_loss = busy_after_loss
            def before_strex(cpu, address, size, _):
                if size != 4:
                    return
                first_half = struct.unpack("<H", cpu.mem_read(address, 2))[0]
                # ARMv7-M STREX T1的首半字为1110 1000 0100 Rn。
                if first_half & 0xFFF0 != 0xE840:
                    return
                if self.reservation_resume:
                    self.reservation_resume = False
                    return
                self.strex_attempts += 1
                if self.reservation_losses > 0:
                    self.reservation_losses -= 1
                    self.reservation_pause = address
                    cpu.emu_stop()
            return self.cpu.hook_add(UC_HOOK_CODE, before_strex)

        @property
        def words(self):
            return self.integers("model_words", 3)

        @property
        def outputs(self):
            return struct.unpack("<2f", self.cpu.mem_read(symbols["model_outputs"], 8))

        def compute(self, turns, kind=0):
            self.cpu.mem_write(symbols["model_turns"], struct.pack("<f", turns))
            self.write("model_argument_kind", kind)
            return self.call("model_compute")

    model = Model()
    assert model.words == (1, 0, 0) and model.configuration_writes == [EXPECTED]
    assert model.register(RCC_ENABLE) == 8
    assert model.call("model_init") and model.configuration_writes == [EXPECTED]
    assert not model.call("model_init_null")
    model = Model(configuration=EXPECTED)
    assert model.words == (1, 0, 0)
    passed("启动仅配置一次固定 CSR，接受复位值及既有期望值，不改变 RCC 或中断屏蔽")

    for configuration, clock, stale in [(0x50, False, False), (EXPECTED | 1, True, False),
            (EXPECTED | (1 << 8), True, False), (EXPECTED | (1 << 16), True, False),
            (EXPECTED | (1 << 17), True, False), (EXPECTED | (1 << 18), True, False),
            (EXPECTED ^ (1 << 19), True, False), (EXPECTED ^ (1 << 20), True, False),
            (EXPECTED | (1 << 21), True, False), (EXPECTED | (1 << 22), True, False),
            (EXPECTED, True, True)]:
        model = Model(configuration, clock, stale)
        assert model.words == (0, 1, 0) and not model.writes and not model.configuration_writes
        model.set_register(RCC_ENABLE, 8)
        model.set_register(CSR, EXPECTED)
        assert not model.call("model_init") and not model.compute(0.25)
        assert model.outputs == (123.0, -456.0) and not model.writes
    passed("时钟关闭、配置不符、DMA/IRQ 与残留结果均隔离，修正寄存器或重复 init 不能自动恢复")

    model = Model(initialize=False)
    assert not model.compute(0.25) and not model.call("model_compute_second")
    assert not model.writes
    model.call("model_init")
    for value in [-0.1, 1.0, 1.1, math.nan, math.inf, -math.inf]:
        assert not model.compute(value) and not model.writes
    for kind in range(1, 5):
        assert not model.compute(0.25, kind) and not model.writes
    assert model.words == (1, 0, 0) and model.outputs == (123.0, -456.0)
    assert model.compute(0.25)
    passed("未 init、空指针、输出别名、非有限或未归一化角度拒绝；坏参数不污染可用实例")

    model = Model()
    model.forced_results = [1073741824, -536870912]
    assert model.compute(0.125) and model.outputs == (-0.25, 0.5)
    assert model.writes == [0x20000000, 0x7FFFFFFF] and model.reads == 2
    assert model.compute(0.75) and model.writes[-2:] == [0xC0000000, 0x7FFFFFFF]
    assert model.configuration_writes == [EXPECTED]
    passed("每次角度加显式模长两个输入，cos 再 sin 两个结果，输出重排正确且每拍不重配")

    model = Model()
    values = [0.0, -0.0, 2**-32, 0.125, 0.25, f32(0.5 - 2**-25), 0.5,
              f32(0.5 + 2**-24), 0.75, f32(1 - 2**-24)]
    values += [f32(value / 4096) for value in range(4096)]
    generator = random.Random(431)
    values += [f32(generator.random()) for _ in range(256)]
    for value in values:
        assert model.compute(value)
        expected_angle = int((value if value < 0.5 else value - 1) * 2**32)
        assert model.writes[-2] == expected_angle & 0xFFFFFFFF, (value, model.writes[-2], expected_angle)
        sine, cosine = model.outputs
        theta = 2 * math.pi * value
        assert math.isclose(sine, math.sin(theta), abs_tol=6e-8), (value, sine)
        assert math.isclose(cosine, math.cos(theta), abs_tol=6e-8), (value, cosine)
    assert model.words == (1, 0, 0)
    passed("4362 个含半圈两侧与一圈前沿的角度，实际 ARM 浮点到 Q1.31 转换无正端溢出；精度仅验证替身转换")

    model = Model()
    assert not model.call("model_init_second") and not model.call("model_compute_second")
    model.call("model_mark_busy")
    assert not model.compute(0.2) and not model.writes and model.words == (1, 0, 1)
    assert not model.call("model_init")
    model.call("model_clear_busy")
    assert model.compute(0.2) and model.words == (1, 0, 0)
    passed("不同对象不能共享物理单元，busy 拒绝重入且不破坏外层事务，释放后原对象可继续")

    for losses in [1, 2, 3]:
        model = Model()
        hook = model.inject_reservation_losses(losses)
        assert model.compute(0.125)
        model.cpu.hook_del(hook)
        assert model.strex_attempts == losses + 1 and model.reservation_losses == 0
        assert model.words == (1, 0, 0) and len(model.writes) == 2 and model.reads == 2
        assert all(math.isclose(actual, math.sqrt(0.5), abs_tol=6e-8) for actual in model.outputs)
    passed("在真实STREX前执行CLREX，单次、两次和三次reservation丢失均在4次上界内恢复且只提交一次外设事务")

    model = Model()
    hook = model.inject_reservation_losses(10)
    assert not model.compute(0.125)
    model.cpu.hook_del(hook)
    assert model.strex_attempts == 4 and model.reservation_losses == 6
    assert model.words == (1, 0, 0) and not model.writes and model.outputs == (123.0, -456.0)
    assert model.compute(0.125) and len(model.writes) == 2
    passed("持续reservation丢失恰好4次后有界拒绝，不写外设、不改输出或隔离状态；后续显式调用仍可用")

    model = Model()
    hook = model.inject_reservation_losses(1, busy_after_loss=True)
    assert not model.compute(0.125)
    model.cpu.hook_del(hook)
    assert model.strex_attempts == 1 and model.words == (1, 0, 1)
    assert not model.writes and model.outputs == (123.0, -456.0)
    model = Model()
    model.call("model_mark_busy")
    hook = model.inject_reservation_losses(10)
    assert not model.compute(0.125)
    model.cpu.hook_del(hook)
    assert model.strex_attempts == 0 and model.reservation_losses == 10 and model.words == (1, 0, 1)
    passed("首次或重取时发现busy已占用立即拒绝，不消耗剩余STREX重试也不释放其他持有者的busy")

    model = Model()
    def interrupted_failure(cpu, access, address, size, value, _):
        # 在首次参数核对之后、取得 busy 之前，替身模拟先行事务因故障隔离。
        cpu.mem_write(symbols["model_context"] + 4, bytes([0, 1]))
    model.cpu.hook_add(UC_HOOK_MEM_READ, interrupted_failure,
                      begin=symbols["model_context"], end=symbols["model_context"] + 3)
    assert not model.compute(0.2) and model.words == (0, 1, 0) and not model.writes
    assert model.outputs == (123.0, -456.0)
    passed("初次状态核对后发生隔离，获取 busy 后仍须重新核对，不能在抢占窗口恢复已失效事务")

    for ready_after in [1, 2, 10, 64]:
        model = Model()
        model.ready_after = ready_after
        assert model.compute(0.123) and model.reads == 2 and model.words == (1, 0, 0)
    for ready_after, expected_reads in [(65, 2), (None, 0)]:
        model = Model()
        model.ready_after = ready_after
        assert not model.compute(0.123) and model.reads == expected_reads
        assert model.polls <= 66 and model.words == (0, 1, 0)
        assert model.outputs == (123.0, -456.0)
        writes = list(model.writes)
        model.ready_after = 1
        assert not model.compute(0.4) and not model.call("model_init")
        assert model.writes == writes
        assert not model.call("model_init_second")
    passed("RRDY 等待最多 64 次，迟到结果就绪才排空；从不提前阻塞读，超时后禁止重试及换实例逃逸")

    for changed in ["clock", "configuration", "stale", "repeat_init"]:
        model = Model()
        if changed == "clock":
            model.set_register(RCC_ENABLE, 0)
        elif changed in ["configuration", "repeat_init"]:
            model.set_register(CSR, EXPECTED | 1)
        else:
            model.set_register(CSR, EXPECTED | READY)
            model.pending = True
        result = model.call("model_init") if changed == "repeat_init" else model.compute(0.25)
        assert not result and model.words == (0, 1, 0) and not model.writes
        assert model.outputs == (123.0, -456.0)
    model = Model()
    model.corrupt_after_result = True
    assert not model.compute(0.25) and model.words == (0, 1, 0)
    assert model.outputs == (123.0, -456.0)
    passed("运行中时钟、配置或残留结果异常，以及读后配置改变均隔离，失败不发布半份输出")

for index, name in enumerate(CASES, 1):
    print(f"PASS {index}: {name}")
print(f"g431 cordic math: {len(CASES)} groups passed; peripheral results are synthetic, no hardware precision/performance claim")
