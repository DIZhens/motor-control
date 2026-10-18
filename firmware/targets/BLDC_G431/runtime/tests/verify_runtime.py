"""Execute actual G431 observe runtime, Motor and VOFA with fake HCC and UART registers."""

from pathlib import Path
import math
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn import UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_PRIMASK, UC_ARM_REG_SP
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4


TARGET = Path(__file__).resolve().parents[2]
REPOSITORY = TARGET.parents[2]
TESTS = Path(__file__).resolve().parent
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
TIM1 = 0x40012C00
TIM6 = 0x40001000
USART1 = 0x40013800
DWT_CYCCNT = 0xE0001004
MOE = 1 << 15
RXNE, TXE, ORE, FE = 1 << 5, 1 << 7, 1 << 3, 1 << 1
BOOT, IDLE, RUN, FAULT = range(4)
NONE, ACCEPTED, NOT_READY, BAD_TARGET, WRONG_STATE, FAULT_ACTIVE, BAD_COMMAND = range(7)
FAULT_STARTUP, FAULT_CURRENT, FAULT_CADENCE = 1, 2, 4
FAULT_TRIP, FAULT_UNEXPECTED_POWER, FAULT_CLOCK = 8, 16, 32
VALID_TIME, VALID_TARGET, VALID_BUS = 1, 2, 32
VALID_STATE, VALID_FAULTS, VALID_RESULT = 64, 128, 256
VALID_STATUS = VALID_TARGET | VALID_STATE | VALID_FAULTS | VALID_RESULT
CASES = []


def passed(name: str) -> None:
    CASES.append(name)


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


with tempfile.TemporaryDirectory(prefix="g431-observe-runtime-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00040000 {\n"
        " ER_IROM1 0x08000000 0x00040000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    include_paths = [TESTS / "stubs", TARGET / "runtime", REPOSITORY / "firmware/app/motor",
                     REPOSITORY / "firmware/app/communication",
                     REPOSITORY / "firmware/platform_contract",
                     TARGET / "Drivers/CMSIS/Device/ST/STM32G4xx/Include",
                     TARGET / "Drivers/CMSIS/Include"]
    command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
               "--split_sections", "-g", "-DSTM32G431xx"]
    for path in include_paths:
        command += ["-I", str(path)]
    objects = []
    for source in [TESTS / "model.c", TARGET / "runtime/target_runtime.c",
                   REPOSITORY / "firmware/app/motor/motor.c",
                   REPOSITORY / "firmware/app/communication/vofa.c"]:
        obj = temporary / (source.stem + ".o")
        run_checked(command + ["-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    run_checked(
        [str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
         "--entry=model_start", "--keep=model.o(*)", "-o", str(executable), *objects],
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
        def __init__(self, settings: dict | None = None, start: bool = True) -> None:
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for base, size in [(0x08000000, 0x40000), (0x20000000, 0x8000),
                               (0x40001000, 0x1000), (0x40012000, 0x2000),
                               (0xE0001000, 0x1000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(base, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.memory_write(0xE000ED88, 0x00F00000)
            self.memory_write(TIM6 + 0x28, 169)
            self.memory_write(TIM1 + 0x44, MOE)
            self.memory_write(DWT_CYCCNT, 8500)
            self.output = bytearray()
            self.fast_uart_accesses = []
            self.phase = ""
            self.rx_reads = 0
            self.tx_writes = 0
            self.interruptions = 0
            self.interrupt_masks = []
            self.cpu.hook_add(UC_HOOK_MEM_READ, self.uart_read, begin=USART1, end=USART1 + 0x2F)
            self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.uart_write, begin=USART1, end=USART1 + 0x2F)
            self.offsets = struct.unpack(
                "<5I", self.cpu.mem_read(symbols["model_motor_offsets"], 20))
            for name, value in (settings or {}).items():
                self.write(name, value)
            if start:
                self.call("model_start")
                self.assert_default_off()

        def memory_write(self, address: int, value: int) -> None:
            self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

        def memory_word(self, address: int) -> int:
            return struct.unpack("<I", self.cpu.mem_read(address, 4))[0]

        def word(self, name: str) -> int:
            return self.memory_word(symbols[name])

        def write(self, name: str, value: int | float, fmt: str = "I") -> None:
            self.cpu.mem_write(symbols[name], struct.pack("<" + fmt, value))

        def motor(self) -> tuple[int, int, int, int, float]:
            values = []
            for index, offset in enumerate(self.offsets):
                fmt = "f" if index == 4 else "I"
                values.append(struct.unpack("<" + fmt,
                                            self.cpu.mem_read(symbols["s_motor"] + offset, 4))[0])
            return tuple(values)

        def uart_read(self, cpu, access, address, size, value, user_data) -> None:
            if self.phase in ["sample", "guard"]:
                self.fast_uart_accesses.append((self.phase, "read", address))
            if address == USART1 + 0x24:
                self.rx_reads += 1
                self.memory_write(USART1 + 0x1C, self.memory_word(USART1 + 0x1C) & ~RXNE)

        def uart_write(self, cpu, access, address, size, value, user_data) -> None:
            if self.phase in ["sample", "guard"]:
                self.fast_uart_accesses.append((self.phase, "write", address))
            if address == USART1 + 0x28:
                self.output.append(value & 0xFF)
                self.tx_writes += 1
            elif address == USART1 + 0x20:
                self.memory_write(USART1 + 0x1C, self.memory_word(USART1 + 0x1C) & ~value)

        def call(self, name: str, interrupt_clock: bool = False,
                 stack: int = 0x20007FF0) -> None:
            self.cpu.reg_write(UC_ARM_REG_SP, stack)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            clock_read = False
            paused = False
            hooks = []

            def read_clock(cpu, access, address, size, value, user_data):
                nonlocal clock_read
                clock_read = True

            def pause_after_clock(cpu, address, size, user_data):
                nonlocal paused
                if clock_read and cpu.reg_read(UC_ARM_REG_PRIMASK) == 0:
                    paused = True
                    cpu.emu_stop()

            if interrupt_clock:
                hooks = [self.cpu.hook_add(UC_HOOK_MEM_READ, read_clock,
                                          begin=DWT_CYCCNT, end=DWT_CYCCNT + 3),
                         self.cpu.hook_add(UC_HOOK_CODE, pause_after_clock)]
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=100000)
                for hook in hooks:
                    self.cpu.hook_del(hook)
                if interrupt_clock:
                    assert paused, "DWT read interruption point was not reached: " + name
                    # 暂停低优先级代码，独立栈执行真实 ADC callback，再恢复寄存器而保留内存写入。
                    context = self.cpu.context_save()
                    previous_phase = self.phase
                    previous_step = self.word("model_tick_cycles")
                    previous_count = self.word("model_callback_count")
                    self.interrupt_masks.append(self.cpu.reg_read(UC_ARM_REG_PRIMASK))
                    self.write("model_tick_cycles", 1)
                    self.phase = "sample"
                    self.call("model_sample", stack=0x20006FF0)
                    assert self.word("model_callback_count") == previous_count + 1
                    self.phase = previous_phase
                    self.write("model_tick_cycles", previous_step)
                    self.cpu.context_restore(context)
                    self.interruptions += 1
                    self.cpu.emu_start(self.cpu.reg_read(UC_ARM_REG_PC) | 1,
                                       0x0803F000, count=100000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, "unbounded call: " + name

        def assert_default_off(self) -> None:
            assert self.motor()[0] != RUN
            assert self.word("model_stage_count") == self.word("model_enable_count") == 0
            assert self.word("model_rearm_count") == 0
            assert self.memory_word(TIM1 + 0x44) & MOE == 0
            assert self.fast_uart_accesses == []

        def service(self, status: int = 0, byte: int = 0,
                    interrupt_clock: bool = False) -> None:
            before_rx, before_tx = self.rx_reads, self.tx_writes
            self.memory_write(USART1 + 0x1C, status)
            self.memory_write(USART1 + 0x24, byte)
            self.phase = "service"
            self.call("model_service", interrupt_clock=interrupt_clock)
            self.phase = ""
            assert self.rx_reads - before_rx <= 1 and self.tx_writes - before_tx <= 1

        def receive(self, data: bytes, error: int = 0) -> None:
            for index, byte in enumerate(data):
                self.service(RXNE | (error if index == 0 else 0), byte)

        def sample(self, count: int = 1) -> None:
            self.phase = "sample"
            for _ in range(count):
                self.call("model_sample")
            self.phase = ""
            self.assert_default_off()

        def guard(self, elapsed_us: int = 50, update: bool = True,
                  interrupt_clock: bool = False) -> None:
            self.memory_write(DWT_CYCCNT, self.memory_word(DWT_CYCCNT) + elapsed_us * 170)
            self.memory_write(TIM6 + 0x10, 1 if update else 0)
            self.phase = "guard"
            self.call("model_guard", interrupt_clock=interrupt_clock)
            self.phase = ""
            self.assert_default_off()

        def frame(self) -> tuple[float, ...]:
            before = len(self.output)
            self.write("model_ms", self.word("model_ms") + 10)
            for _ in range(44):
                self.service(TXE)
            data = bytes(self.output[before:])
            assert len(data) == 44 and data[-4:] == b"\x00\x00\x80\x7f", data
            return struct.unpack("<10f", data[:40])

    model = Model()
    assert model.motor() == (IDLE, 0, NONE, 0, 0.0)
    assert model.word("model_bind_count") == model.word("model_arm_count") == 1
    assert model.word("model_start_count") == 1
    assert model.cpu.reg_read(UC_ARM_REG_PRIMASK) == 0
    assert model.memory_word(TIM6) & 1 and model.memory_word(TIM6 + 0x0C) & 1
    before = model.motor(), model.word("model_start_count"), model.word("model_safeoff_count")
    model.call("model_start")
    after = model.motor(), model.word("model_start_count"), model.word("model_safeoff_count")
    assert after == before
    masked = Model(start=False)
    masked.cpu.reg_write(UC_ARM_REG_PRIMASK, 1)
    masked.call("model_start")
    assert masked.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    passed("startup binds, arms and starts timing once while power remains disabled")

    failures = ["model_facts_ok", "model_bind_ok", "model_trip_bind_ok",
                "model_position_arm_ok", "model_bus_arm_ok", "model_io_arm_ok",
                "model_trip_arm_ok", "model_trip_available", "model_start_ok"]
    for name in failures:
        model = Model({name: 0})
        assert model.motor()[0:2] == (FAULT, FAULT_STARTUP), name
        assert model.memory_word(TIM6) & 1 == 0
        frame = model.frame()
        assert frame[6:9] == (float(FAULT), float(FAULT_STARTUP), float(VALID_STATUS)), name
    for settings in [{"model_facts_rate": 10000}, {"model_facts_period": 2},
                     {"model_trip_active": 1}, {"model_trip_causes": 2}]:
        model = Model(settings)
        assert model.motor()[0:2] == (FAULT, FAULT_STARTUP)
    for address, value in [(symbols["SystemCoreClock"], 160000000), (TIM6 + 0x28, 168)]:
        model = Model(start=False)
        model.memory_write(address, value)
        model.call("model_start")
        model.assert_default_off()
        assert model.motor()[0:2] == (FAULT, FAULT_CLOCK)
        assert model.word("model_start_count") == 0
    passed("each startup dependency/arm failure, bad timing fact and dirty trip fails closed")

    model = Model()
    model.receive(b"start:12")
    model.sample()
    assert model.motor()[2:4] == (NONE, 0)
    model.receive(b"0\n")
    assert model.motor()[2:4] == (NONE, 0)
    model.sample()
    assert model.motor() == (IDLE, 0, NOT_READY, 1, 0.0)
    for data, result in [(b"speed:200\n", WRONG_STATE), (b"clear\n", ACCEPTED),
                         (b"start:-12\n", NOT_READY)]:
        old_count = model.motor()[3]
        model.receive(data)
        assert model.motor()[3] == old_count
        model.sample()
        assert model.motor()[2:4] == (result, old_count + 1)
    passed("only complete requests cross to Motor at a sample boundary")

    model = Model()
    model.receive(b"start:1\nspeed:2\nstop\nclear\nstart:3\n")
    assert model.motor()[3] == 0
    before = model.word("model_safeoff_count")
    model.sample()
    assert model.motor() == (IDLE, 0, ACCEPTED, 1, 0.0)
    assert model.word("model_safeoff_count") == before + 1
    model.sample(3)
    assert model.motor()[2:4] == (ACCEPTED, 1)
    passed("Stop overtakes and retires old queued motion, overflow and later requests")

    model = Model()
    model.receive(b"start:1\nclear\n")
    model.sample()
    assert model.motor()[2:4] == (NOT_READY, 1)
    model.sample()
    assert model.motor()[2:4] == (BAD_COMMAND, 2)
    passed("one-slot overflow reports rejection on the owner without replacing a pending request")

    for data, error in [(b"start:nan\n", 0), (b"start:1\x00stop\n", 0),
                        (b"x" * 90 + b"stop\n", 0), (b"start:12\n", ORE),
                        (b"stop\n", FE)]:
        model = Model()
        model.receive(data, error)
        assert model.motor()[3] == 0
        model.sample()
        assert model.motor()[2:4] == (BAD_COMMAND, 1)
        model.receive(b"clear\n")
        model.sample()
        assert model.motor()[2:4] == (ACCEPTED, 2)
    passed("malformed, NUL, long and UART-error lines discard through LF then recover")

    for fragment, error in [(b"start:12", 0), (b"start:12", ORE), (b"start:\x00", 0)]:
        model = Model()
        model.receive(fragment, error)
        model.write("model_ms", 100)
        model.service()
        model.receive(b"stop\n")
        model.sample()
        assert model.motor()[2:4] == (BAD_COMMAND, 1)
        model.receive(b"clear\n")
        model.sample()
        assert model.motor()[2:4] == (ACCEPTED, 2)
    passed("100ms fragment timeout never promotes a tail command or releases error-line discard")

    model = Model()
    model.write("model_ms", 10)
    for _ in range(100):
        model.service()
    assert model.output == b"" and model.motor()[3] == 0
    model.sample(200)
    frame = model.frame()
    assert math.isnan(frame[0]) and frame[8] == VALID_STATUS
    frame = model.frame()
    assert math.isclose(frame[0], 0.01, rel_tol=1e-6)
    assert frame[1] == 0.0 and frame[5] == 24.0
    assert all(math.isnan(frame[channel]) for channel in [2, 3, 4])
    assert frame[6:] == (float(IDLE), 0.0, float(VALID_STATUS | VALID_TIME | VALID_BUS), 0.0)
    assert model.word("model_bus_read_count") == 10
    model.assert_default_off()
    passed("no-TXE service never waits; 44-byte telemetry keeps unimplemented channels invalid")

    for settings in [{"model_bus_available": 0}, {"model_bus_valid": 0},
                     {"model_bus_age": 101}, {"model_bus_future": 1}]:
        model = Model(settings)
        model.sample(200)
        frame = model.frame()
        assert math.isnan(frame[5]) and int(frame[8]) & VALID_BUS == 0
    for value in [-1.0, float("nan"), float("inf")]:
        model = Model()
        model.write("model_bus_voltage", value, "f")
        model.sample(200)
        frame = model.frame()
        assert math.isnan(frame[5]) and int(frame[8]) & VALID_BUS == 0
    passed("bus publication failure, stale/future timestamp and invalid voltage stay invalid")

    for settings in [{"model_current_valid": 0}, {"model_current_mask": 7}]:
        model = Model(settings)
        model.sample()
        assert model.motor()[0:2] == (FAULT, FAULT_CURRENT)
    for value in [float("inf"), float("nan")]:
        model = Model()
        model.write("model_current_b", value, "f")
        model.sample()
        assert model.motor()[0:2] == (FAULT, FAULT_CURRENT)
    for setting, value in [("model_trip_available", 0), ("model_trip_active", 1),
                           ("model_trip_causes", 2)]:
        model = Model()
        model.write(setting, value)
        model.sample()
        assert model.motor()[0:2] == (FAULT, FAULT_TRIP)
    model = Model()
    model.call("model_notify")
    model.sample()
    assert model.motor()[0:2] == (IDLE, 0)
    model.memory_write(TIM1 + 0x44, MOE)
    model.sample()
    assert model.motor()[0:2] == (FAULT, FAULT_UNEXPECTED_POWER)
    model.receive(b"stop\n")
    model.sample()
    model.receive(b"clear\n")
    model.sample()
    frame = model.frame()
    assert frame[6:8] == (float(FAULT), float(FAULT_UNEXPECTED_POWER))
    assert frame[9] == float(NOT_READY)
    passed("sample faults use authoritative evidence, latch, safe-off and survive Stop/Clear")

    for cycles in [24 * 170, 76 * 170]:
        model = Model()
        model.sample()
        model.write("model_tick_cycles", cycles)
        model.sample()
        assert model.motor()[1] & FAULT_CADENCE
    for first_sample in [False, True]:
        model = Model()
        if first_sample:
            model.sample(200)
        previous = model.motor()
        before = model.word("model_safeoff_count")
        model.guard(76)
        assert model.word("g_target_runtime_guard_faults") & FAULT_CADENCE
        assert model.word("model_safeoff_count") == before + 1
        assert model.motor() == previous
        frame = model.frame()
        assert frame[8] == 0.0
        assert all(math.isnan(value) for index, value in enumerate(frame) if index != 8)
        model.sample()
        assert model.motor()[0] == FAULT and model.motor()[1] & FAULT_CADENCE
    model = Model()
    before = model.word("model_safeoff_count")
    model.guard(0, update=False)
    assert model.word("model_safeoff_count") == before
    model.guard(0)
    assert model.word("g_target_runtime_guard_faults") & FAULT_CLOCK
    assert model.word("model_safeoff_count") == before + 1
    passed("TIM6 safe-offs without ADC, hides old telemetry and ADC later incorporates fault")

    model = Model()
    model.sample()
    model.guard(50, interrupt_clock=True)
    assert model.interruptions == 1
    assert model.word("g_target_runtime_guard_faults") == 0, "ADC preemption invented guard fault"
    assert model.interrupt_masks == [0]
    assert model.motor()[0:2] == (IDLE, 0)
    model.sample()
    model.assert_default_off()
    model = Model()
    model.sample(200)
    model.memory_write(DWT_CYCCNT, model.memory_word(DWT_CYCCNT) + 50 * 170)
    model.write("model_ms", 10)
    model.service(TXE, interrupt_clock=True)
    for _ in range(43):
        model.service(TXE)
    assert len(model.output) == 44
    frame = struct.unpack("<10f", model.output[:40])
    assert model.interruptions == 1
    assert model.interrupt_masks == [0]
    assert frame[8] == VALID_STATUS | VALID_TIME | VALID_BUS, "ADC preemption invalidated telemetry"
    assert model.motor()[0:2] == (IDLE, 0)
    model.assert_default_off()
    passed("ADC preemption after DWT reads preserves cadence and healthy telemetry validity")

print(f"PASS: {len(CASES)} G431 runtime C behavior groups")
for name in CASES:
    print("  " + name)
