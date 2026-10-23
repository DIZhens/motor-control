"""使用 ARMCC 和 Unicorn 编译并执行正式 Motor C，验证默认关断行为，不访问硬件。"""

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
BOOT, IDLE, RUN, FAULT = range(4)
START, UPDATE, STOP, CLEAR = range(4)
NONE, ACCEPTED, NOT_READY, BAD_TARGET, WRONG_STATE, FAULT_ACTIVE, BAD_COMMAND = range(7)


def passed(name: str) -> None:
    CASES.append(name)


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


with tempfile.TemporaryDirectory(prefix="motor-default-off-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / 'motor.c', ROOT / 'tests/model.c']:
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
        def __init__(self) -> None:
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.call("model_init")

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

        def snapshot(self) -> tuple[int, int, int, int, float]:
            return (self.read("model_state"), self.read("model_faults"),
                    self.read("model_result"), self.read("model_count"),
                    self.read("model_target", "f"))

        def submit(self, kind: int, target: float = 0.0) -> None:
            self.cpu.mem_write(symbols["model_command"], struct.pack("<If", kind, target))

        def handle(self, kind: int, target: float = 0.0) -> int:
            self.submit(kind, target)
            self.call("model_handle_command")
            result = self.read("model_returned_result")
            assert result == self.read("model_result")
            return result

        def fault(self, flags: int) -> None:
            self.write("model_fault_flags", flags)
            self.call("model_latch_fault")

        def inject_run(self, target: float) -> None:
            self.write("model_injected_target", target, "f")
            self.call("model_inject_run")

    model = Model()
    assert model.snapshot() == (BOOT, 0, NONE, 0, 0.0)
    model.fault(0)
    assert model.snapshot() == (BOOT, 0, NONE, 0, 0.0)
    model.call("model_finish_boot")
    assert model.snapshot() == (IDLE, 0, NONE, 0, 0.0)
    model.call("model_finish_boot")
    assert model.snapshot() == (IDLE, 0, NONE, 0, 0.0)
    passed("explicit BOOT completion to IDLE and zero fault evidence is inert")

    model = Model()
    for kind, expected in [(START, WRONG_STATE), (UPDATE, WRONG_STATE),
                           (CLEAR, WRONG_STATE), (STOP, ACCEPTED)]:
        assert model.handle(kind, 100.0) == expected
        assert model.read("model_state") == BOOT and model.read("model_target", "f") == 0.0
    assert model.read("model_count") == 4
    passed("BOOT refuses motion and Clear; Stop preserves BOOT")

    model = Model()
    model.call("model_finish_boot")
    for target in [0.0, -0.0, 100.0, -100.0, 1e30, -1e30, 3.4028234663852886e38]:
        assert model.handle(START, target) == NOT_READY
        assert model.snapshot()[0:2] == (IDLE, 0)
        assert model.read("model_target", "f") == 0.0
    assert model.handle(UPDATE, 100.0) == WRONG_STATE
    assert model.handle(STOP) == ACCEPTED
    assert model.handle(CLEAR) == ACCEPTED
    assert model.handle(CLEAR) == ACCEPTED
    assert model.snapshot()[0:2] == (IDLE, 0)
    passed("finite IDLE Start stays NOT_READY without invented speed limits or fake RUN")

    for state in [BOOT, IDLE, FAULT]:
        model = Model()
        if state == IDLE:
            model.call("model_finish_boot")
        elif state == FAULT:
            model.fault(0x20)
        for kind in [START, UPDATE]:
            for target in [float("nan"), float("inf"), float("-inf")]:
                assert model.handle(kind, target) == BAD_TARGET
                assert model.read("model_state") == state
                assert model.read("model_target", "f") == 0.0
    passed("NaN and both infinities reject before motion state validation")

    model = Model()
    model.fault(1)
    model.fault(4)
    assert model.snapshot() == (FAULT, 5, NONE, 0, 0.0)
    for kind, expected in [(STOP, ACCEPTED), (CLEAR, NOT_READY),
                           (START, FAULT_ACTIVE), (UPDATE, FAULT_ACTIVE)]:
        assert model.handle(kind, 100.0) == expected
        assert model.read("model_state") == FAULT and model.read("model_faults") == 5
        assert model.read("model_target", "f") == 0.0
    before = model.snapshot()
    model.call("model_finish_boot")
    model.fault(0)
    assert model.snapshot() == before
    passed("fault evidence OR-latches; Stop, Clear and boot completion never clear it")

    model = Model()
    model.call("model_finish_boot")
    before = model.snapshot()
    model.submit(START, 120.0)
    assert model.snapshot() == before
    model.call("model_handle_command")
    assert model.snapshot() == (IDLE, 0, NOT_READY, 1, 0.0)
    assert model.handle(0xFFFFFFFF, 120.0) == BAD_COMMAND
    model.call("model_null_command")
    assert model.read("model_returned_result") == BAD_COMMAND
    assert model.snapshot() == (IDLE, 0, BAD_COMMAND, 3, 0.0)
    model.call("model_null_arguments")
    assert model.read("model_null_result") == BAD_COMMAND
    assert model.snapshot() == (IDLE, 0, BAD_COMMAND, 3, 0.0)
    passed("requests change business state only when owner calls handler; count includes rejects")

    model = Model()
    model.inject_run(55.0)
    assert model.handle(UPDATE, 66.0) == NOT_READY
    assert model.read("model_target", "f") == 55.0
    assert model.handle(START, 77.0) == WRONG_STATE
    assert model.handle(CLEAR) == WRONG_STATE
    assert model.handle(STOP) == ACCEPTED
    assert model.snapshot() == (IDLE, 0, ACCEPTED, 4, 0.0)
    model.inject_run(55.0)
    model.fault(0x80)
    assert model.read("model_state") == FAULT and model.read("model_target", "f") == 0.0
    passed("reserved RUN injection cannot accept control targets; Stop and fault clear its target")

    model = Model()
    randomizer = random.Random(431)
    targets = [0.0, 10.0, -10.0, 1e30, float("inf"), float("nan")]
    for index in range(512):
        if index % 17 == 0:
            model.call("model_finish_boot")
        if index >= 256 and index % 31 == 0:
            model.fault(1 << ((index // 31) % 8))
        model.handle(randomizer.choice([START, UPDATE, STOP, CLEAR, 1234]),
                     randomizer.choice(targets))
        state, faults, _, count, target = model.snapshot()
        assert state in [BOOT, IDLE, FAULT] and count == index + 1
        assert math.isfinite(target) and target == 0.0
        assert state == (FAULT if faults != 0 else IDLE)
    passed("512 mixed requests before/after fault never enter RUN or escape latched FAULT")

print(f"PASS: {len(CASES)} Motor C behavior groups")
for name in CASES:
    print("  " + name)
