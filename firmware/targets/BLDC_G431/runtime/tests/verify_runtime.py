"""使用 HCC 替身和模拟 UART 寄存器，执行正式 G431 观察入口、Motor 与 VOFA。"""

from pathlib import Path
import math
import os
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

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
VALID_TIME, VALID_TARGET, VALID_VELOCITY, VALID_BUS = 1, 2, 4, 32
VALID_STATE, VALID_FAULTS, VALID_RESULT = 64, 128, 256
VALID_STATUS = VALID_TARGET | VALID_STATE | VALID_FAULTS | VALID_RESULT
CURRENT_WAITING, CURRENT_CALIBRATING, CURRENT_READY, CURRENT_FAILED = range(1, 5)
CURRENT_ABORTED, CURRENT_ZERO_UNCONFIRMED = 1, 2
CAPTURE_IDLE, CAPTURE_ACTIVE, CAPTURE_COMPLETE, CAPTURE_FAILED = range(4)
CAPTURE_NOT_READY, CAPTURE_ABORTED, CAPTURE_INVALID, CAPTURE_TIMING, CAPTURE_NUMERIC, CAPTURE_BUSY = range(1, 7)
STAGE_IDLE, STAGE_READY, STAGE_ACTIVE, STAGE_COMPLETE, STAGE_FAILED = range(5)
STAGE_NOT_READY, STAGE_ABORTED, STAGE_BUSY, STAGE_CLOCK = range(1, 5)
CASES = []


def passed(name: str) -> None:
    CASES.append(name)


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def read_image(path: Path) -> tuple:
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(section["p_vaddr"], section.data()) for section in elf.iter_segments()
                    if section["p_type"] == "PT_LOAD"]
        initializers = [(section["sh_addr"], section.data()) for section in elf.iter_sections()
                        if (section["sh_flags"] & 2) and section["sh_type"] == "SHT_PROGBITS"]
        symbols = {symbol.name: symbol["st_value"]
                   for symbol in elf.get_section_by_name(".symtab").iter_symbols()}
        return segments, initializers, symbols


project = ET.parse(TARGET / "MDK-ARM/BLDC_control V1.uvprojx").getroot()
observe = next(target for target in project.findall("./Targets/Target")
               if target.findtext("TargetName") == "BLDC_G431_Observe")
assert observe.findtext("./TargetOption/TargetArmAds/Cads/Optim") == "4"
assert observe.findtext("./TargetOption/TargetArmAds/Cads/oTime") == "0"
hot_groups = {"Application/User/Runtime", "Application/User/Motor", "Control"}
for group in observe.findall("./Groups/Group"):
    policy = group.findtext("./GroupOption/GroupArmAds/Cads/oTime")
    if group.findtext("GroupName") in hot_groups:
        assert policy == "1", group.findtext("GroupName")
        assert group.findtext("./GroupOption/GroupArmAds/Cads/Optim") == "0"
        assert group.findtext("./GroupOption/CommonProperty/IncludeInBuild") == "2"
    else:
        assert policy in (None, "2"), group.findtext("GroupName")
assert hot_groups <= {group.findtext("GroupName") for group in observe.findall("./Groups/Group")}
passed("Observe仅对Runtime、Motor和Control三个组显式采用速度优化，保留全局与硬件组继承策略")


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
                     REPOSITORY / "control",
                     REPOSITORY / "firmware/app/communication",
                     REPOSITORY / "firmware/platform_contract",
                     TARGET / "Drivers/CMSIS/Device/ST/STM32G4xx/Include",
                     TARGET / "Drivers/CMSIS/Include"]
    command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3", "-Otime",
               "--split_sections", "-g", "-DSTM32G431xx", "-DBLDC_G431_OBSERVE_ENTRY",
               "-DCONTROL_CURRENT_PROFILE"]
    for path in include_paths:
        command += ["-I", str(path)]
    objects = []
    for source in [TESTS / "model.c", TARGET / "runtime/target_runtime.c",
                   TARGET / "runtime/current_capture.c",
                   TARGET / "runtime/compute_probe.c",
                   TARGET / "runtime/compute_comparison.c",
                   TARGET / "runtime/irq_timing.c",
                   TARGET / "runtime/controller_profile.c",
                   TARGET / "runtime/stage_budget.c",
                   TARGET / "runtime/stage_budget_runtime.c",
                   REPOSITORY / "firmware/app/motor/motor.c",
                   REPOSITORY / "firmware/app/motor/control_runtime.c",
                   REPOSITORY / "control/position_estimator.c",
                   REPOSITORY / "control/current_measurement.c",
                   REPOSITORY / "control/electrical_angle.c",
                   REPOSITORY / "control/foc_math.c",
                   REPOSITORY / "control/current_controller.c",
                   REPOSITORY / "control/speed_controller.c",
                   REPOSITORY / "firmware/app/communication/motor_vofa.c",
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
    segments, initializers, symbols = read_image(executable)

    class Model:
        def __init__(self, settings: dict | None = None, start: bool = True,
                     image: tuple | None = None) -> None:
            image_segments, image_initializers, self.symbols = (
                (segments, initializers, symbols) if image is None else image)
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for base, size in [(0x08000000, 0x40000), (0x20000000, 0x8000),
                               (0x40001000, 0x1000), (0x40012000, 0x2000),
                               (0xE0001000, 0x1000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(base, size)
            for address, data in image_segments + image_initializers:
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
                "<5I", self.cpu.mem_read(self.symbols["model_motor_offsets"], 20))
            self.current_offsets = struct.unpack(
                "<10I", self.cpu.mem_read(self.symbols["model_current_offsets"], 40))
            sizes = struct.unpack(
                "<3I", self.cpu.mem_read(self.symbols["model_current_field_sizes"], 12))
            self.current_formats = [{1: "B", 2: "H", 4: "I"}[size] for size in sizes]
            self.angle_offsets = struct.unpack(
                "<5I", self.cpu.mem_read(self.symbols["model_angle_offsets"], 20))
            self.capture_offsets = struct.unpack(
                "<10I", self.cpu.mem_read(self.symbols["model_capture_offsets"], 40))
            capture_sizes = struct.unpack(
                "<3I", self.cpu.mem_read(self.symbols["model_capture_field_sizes"], 12))
            self.capture_size = capture_sizes[0]
            self.capture_formats = [{1: "B", 2: "H", 4: "I"}[size] for size in capture_sizes[1:]]
            self.compute_offsets = struct.unpack(
                "<13I", self.cpu.mem_read(self.symbols["model_compute_offsets"], 52))
            self.compute_timing_offsets = struct.unpack(
                "<5I", self.cpu.mem_read(self.symbols["model_compute_timing_offsets"], 20))
            self.controller_profile_layout = struct.unpack(
                "<2I", self.cpu.mem_read(self.symbols["model_controller_profile_layout"], 8))
            self.runtime_offsets = struct.unpack(
                "<8I", self.cpu.mem_read(self.symbols["model_runtime_offsets"], 32))
            self.stage_budget_layout = struct.unpack(
                "<2I", self.cpu.mem_read(self.symbols["model_stage_budget_layout"], 8))
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
            return self.memory_word(self.symbols[name])

        def control_time(self) -> int:
            return struct.unpack("<Q", self.cpu.mem_read(self.symbols["model_control_time"], 8))[0]

        def write(self, name: str, value: int | float, fmt: str = "I") -> None:
            self.cpu.mem_write(self.symbols[name], struct.pack("<" + fmt, value))

        def motor(self) -> tuple[int, int, int, int, float]:
            values = []
            for index, offset in enumerate(self.offsets):
                fmt = "f" if index == 4 else "I"
                values.append(struct.unpack("<" + fmt,
                                            self.cpu.mem_read(self.symbols["s_motor"] + offset, 4))[0])
            return tuple(values)

        def current(self, runtime_name: str = "s_control_runtime") -> dict:
            # 读取真实集成实例，而不是复制一套测试用校准实现。
            def read(index: int, fmt: str):
                address = self.symbols[runtime_name] + self.current_offsets[index]
                values = struct.unpack("<" + fmt,
                                       self.cpu.mem_read(address, struct.calcsize("<" + fmt)))
                return values[0] if len(values) == 1 else values

            return {"state": read(0, self.current_formats[0]),
                    "failure": read(1, self.current_formats[1]),
                    "count": read(2, "I"), "offset": read(3, "3f"),
                    "mean": read(4, "3f"), "phase": read(5, "3f"),
                    "alpha": read(6, "f"), "beta": read(7, "f"),
                    "valid": bool(read(8, self.current_formats[2])), "settle": read(9, "I")}

        def assert_current_unpublished(self) -> None:
            frame = self.snapshot_frame()
            assert all(math.isnan(frame[channel]) for channel in [3, 4]), frame
            assert int(frame[8]) & (8 | 16) == 0, frame
            self.assert_default_off()

        def capture_bytes(self) -> bytes:
            return bytes(self.cpu.mem_read(self.symbols["g_target_runtime_current_capture"], self.capture_size))

        def capture(self) -> dict:
            block = self.capture_bytes()

            def read(index: int, fmt: str):
                values = struct.unpack_from("<" + fmt, block, self.capture_offsets[index])
                return values[0] if len(values) == 1 else values

            return {"request": read(0, "I"), "state": read(1, self.capture_formats[0]),
                    "failure": read(2, self.capture_formats[1]), "count": read(3, "I"),
                    "first": read(4, "Q"), "last": read(5, "Q"),
                    "offset": (read(6, "f"), read(7, "f")),
                    "b": read(8, "4f"), "c": read(9, "4f"),
                    "sequence": self.word("g_target_runtime_current_sequence")}

        def request_capture(self, request: int = 1) -> dict:
            self.write("g_target_runtime_current_request", request)
            self.sample()
            capture = self.capture()
            assert capture["request"] == request and capture["sequence"] % 2 == 0, capture
            self.assert_default_off()
            return capture

        def compute(self) -> dict:
            offsets = self.compute_offsets
            data = bytes(self.cpu.mem_read(self.symbols["g_target_runtime_compute_result"], offsets[7]))
            read = lambda offset, fmt: struct.unpack_from("<" + fmt, data, offset)[0]
            timing = self.compute_timing_offsets
            def segments(base):
                return [[tuple(read(base + case * offsets[8] + timing[0] + segment * timing[1]
                                    + member, "I") for member in timing[2:])
                         for segment in range(6)] for case in range(6)]
            return {"request": read(offsets[0], "I"),
                    "state": read(offsets[1], {1: "B", 2: "H", 4: "I"}[offsets[5]]),
                    "failure": read(offsets[2], {1: "B", 2: "H", 4: "I"}[offsets[6]]),
                    "steps": read(offsets[3], "I"),
                    "counts": [read(offsets[4] + i * offsets[8], "I") for i in range(6)],
                    "hardware_counts": [read(offsets[10] + i * offsets[8], "I") for i in range(6)],
                    "sweep": read(offsets[9], "I"), "schema": read(offsets[12], "I"),
                    "errors": struct.unpack_from("<5f", data, offsets[11]),
                    "software_segments": segments(offsets[4]),
                    "hardware_segments": segments(offsets[10]),
                    "sequence": self.word("g_target_runtime_compute_sequence"), "bytes": data}

        def prepare_compute(self) -> None:
            self.memory_write(0xE000EDFC, 1 << 24)
            self.memory_write(0xE0001000, 1)
            self.write("model_bus_voltage", 2.68, "f")
            self.sample(20)  # 达到真实慢观测分频，发布合法母线快照后才许可诊断。
            # 假周期数只验证接入与并发，不能作为 Cortex-M4 的物理执行时间。
            def advance_clock(cpu, access, address, size, value, user_data):
                self.memory_write(DWT_CYCCNT, self.memory_word(DWT_CYCCNT) + 17)
            self.compute_clock_hook = self.cpu.hook_add(
                UC_HOOK_MEM_READ, advance_clock, begin=DWT_CYCCNT, end=DWT_CYCCNT + 3)

        def irq_timing(self) -> dict:
            sequence = self.word("g_target_irq_timing_sequence")
            data = bytes(self.cpu.mem_read(self.symbols["g_target_irq_timing_result"], 84))
            assert sequence % 2 == 0
            words = struct.unpack("<21I", data)
            return {"schema": words[0], "request": words[1], "state": words[2],
                    "failure": words[3], "count": words[4],
                    "rows": [words[index:index + 4] for index in range(5, 21, 4)],
                    "sequence": sequence, "bytes": data}

        def controller_profile(self) -> dict:
            size, offset = self.controller_profile_layout
            sequence = self.word("g_observe_controller_profile_sequence")
            data = bytes(self.cpu.mem_read(self.symbols["g_observe_controller_profile_result"], size))
            assert sequence % 2 == 0
            words = struct.unpack_from("<6I", data)
            timing = self.compute_timing_offsets
            rows = [tuple(struct.unpack_from("<I", data, offset + row * timing[1] + member)[0]
                          for member in timing[2:]) for row in range(7)]
            return {"request": words[0], "state": words[1], "failure": words[2],
                    "calls": words[3], "valid": words[4], "rejected": words[5],
                    "rows": rows, "sequence": sequence, "bytes": data}

        def stage_budget(self) -> dict:
            size, offset = self.stage_budget_layout
            sequence = self.word("g_target_stage_budget_sequence")
            data = bytes(self.cpu.mem_read(self.symbols["g_target_stage_budget_result"], size))
            assert sequence % 2 == 0 and sequence == self.word("g_target_stage_budget_sequence")
            words = struct.unpack_from("<21I", data)
            rows = [struct.unpack_from("<4I", data, offset + row * 16) for row in range(6)]
            return {"schema": words[0], "request": words[1], "state": words[2],
                    "failure": words[3], "count": words[4], "stage_calls": words[5],
                    "stage_accepted": words[6], "enable_attempts": words[7],
                    "rows": rows, "active": self.word("g_target_stage_budget_active"),
                    "sequence": sequence, "bytes": data}

        def runtime_status(self) -> dict:
            def read(index: int, fmt: str):
                address = self.symbols["s_control_runtime"] + self.runtime_offsets[index]
                return struct.unpack("<" + fmt, self.cpu.mem_read(address, struct.calcsize("<" + fmt)))[0]
            return {"phase": read(0, "I"), "control_enabled": bool(read(1, "B")),
                    "current_confirmed": bool(read(2, "B")), "timing_confirmed": bool(read(3, "B")),
                    "angle_confirmed": bool(read(4, "B")), "time_available": bool(read(5, "B")),
                    "last_current_time": read(6, "Q"), "observation_counter": read(7, "I")}

        def assert_stage_unpowered(self) -> None:
            self.assert_default_off()
            status = self.runtime_status()
            assert status["phase"] == 0
            assert not any(status[key] for key in ["control_enabled", "current_confirmed",
                                                  "timing_confirmed", "angle_confirmed"]), status
            result = self.stage_budget()
            assert result["stage_calls"] == result["stage_accepted"] == result["enable_attempts"] == 0
            assert result["count"] == 0 and result["rows"] == [(0, 0, 0, 0)] * 6
            self.assert_angle_invalid()

        def prepare_stage(self) -> None:
            self.memory_write(0xE000EDFC, 1 << 24)
            self.memory_write(0xE0001000, 1)
            self.write("model_bus_voltage", 2.7, "f")
            self.sample(20)

        def request_stage(self, request: int = 1) -> dict:
            self.write("g_target_runtime_stage_request", request)
            self.sample()
            result = self.stage_budget()
            self.assert_stage_unpowered()
            return result

        def angle(self, runtime_name: str = "s_control_runtime") -> dict:
            def read(index: int, fmt: str):
                address = self.symbols[runtime_name] + self.angle_offsets[index]
                return struct.unpack("<" + fmt,
                                     self.cpu.mem_read(address, struct.calcsize("<" + fmt)))[0]

            return {"turns": read(0, "f"), "sin": read(1, "f"), "cos": read(2, "f"),
                    "valid": bool(read(3, self.current_formats[2])),
                    "velocity_valid": bool(read(4, self.current_formats[2])),
                    "velocity": struct.unpack("<f", self.cpu.mem_read(
                        self.symbols[runtime_name] + self.word("model_velocity_offset"), 4))[0]}

        def assert_angle_invalid(self, runtime_name: str = "s_control_runtime") -> None:
            angle = self.angle(runtime_name)
            assert not angle["valid"] and all(angle[field] == 0.0
                                              for field in ["turns", "sin", "cos"]), angle

        def angle_sample(self, count: int = 1) -> None:
            for _ in range(count):
                self.call("model_angle_sample")
            self.assert_default_off()

        def angle_position(self, turns: float, observed_at: int | None = None) -> None:
            # 独立 Runtime 的合成 source；按其真实分频计数交付新观测。
            counter_address = self.symbols["model_angle_runtime"] + self.word("model_observation_counter_offset")
            next_read = self.control_time() + 2 * (10 - self.memory_word(counter_address))
            self.write("model_position_available", 1)
            self.write("model_position_turns", turns, "f")
            self.write("model_position_time", next_read if observed_at is None else observed_at, "Q")
            self.angle_sample((next_read - self.control_time()) // 2)

        def begin_current_window(self) -> None:
            self.write("model_position_available", 1)
            self.write("model_position_use_fixed_time", 0)
            self.write("model_current_b", 0.02, "f")
            self.write("model_current_c", -0.03, "f")
            self.sample(118)
            current = self.current()
            assert current["state"] == CURRENT_WAITING and current["settle"] == 99, current
            assert current["count"] == 0 and not current["valid"]
            self.sample()
            current = self.current()
            assert current["state"] == CURRENT_CALIBRATING and current["count"] == 1, current

        def complete_current_window(self) -> None:
            self.begin_current_window()
            self.sample(2046)
            current = self.current()
            assert current["state"] == CURRENT_CALIBRATING and current["count"] == 2047
            assert current["offset"] == (0.0, 0.0, 0.0) and not current["valid"]
            self.sample()
            current = self.current()
            assert current["state"] == CURRENT_READY and current["count"] == 2048, current
            assert current["failure"] == 0 and current["valid"], current

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
                 stack: int = 0x20007FF0, instruction_budget: int = 100000,
                 interrupt_write: int | None = None, interrupt_step_cycles: int = 1) -> None:
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

            if interrupt_clock or interrupt_write is not None:
                hooks = [self.cpu.hook_add(UC_HOOK_MEM_READ if interrupt_clock else UC_HOOK_MEM_WRITE,
                                          read_clock, begin=DWT_CYCCNT if interrupt_clock else interrupt_write,
                                          end=DWT_CYCCNT + 3 if interrupt_clock else interrupt_write),
                         self.cpu.hook_add(UC_HOOK_CODE, pause_after_clock)]
            try:
                self.cpu.emu_start(self.symbols[name] | 1, 0x0803F000, count=instruction_budget)
                for hook in hooks:
                    self.cpu.hook_del(hook)
                if interrupt_clock or interrupt_write is not None:
                    assert paused, "DWT read interruption point was not reached: " + name
                    # 暂停低优先级代码，独立栈执行真实 ADC callback，再恢复寄存器而保留内存写入。
                    context = self.cpu.context_save()
                    previous_phase = self.phase
                    previous_step = self.word("model_tick_cycles")
                    previous_count = self.word("model_callback_count")
                    self.interrupt_masks.append(self.cpu.reg_read(UC_ARM_REG_PRIMASK))
                    self.write("model_tick_cycles", interrupt_step_cycles)
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
                    interrupt_clock: bool = False, interrupt_write: int | None = None,
                    interrupt_step_cycles: int = 1) -> None:
            before_rx, before_tx = self.rx_reads, self.tx_writes
            self.memory_write(USART1 + 0x1C, status)
            self.memory_write(USART1 + 0x24, byte)
            self.phase = "service"
            self.call("model_service", interrupt_clock=interrupt_clock,
                      interrupt_write=interrupt_write, interrupt_step_cycles=interrupt_step_cycles)
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

        def position(self, turns: float, observed_at: int | None = None) -> None:
            # 仅显式调用才交付下一份采集；普通 sample() 会重复读取同一时间戳。
            now = self.control_time()
            next_read = (now // 20 + 1) * 20
            self.write("model_position_available", 1)
            self.write("model_position_turns", turns, "f")
            self.write("model_position_time", next_read if observed_at is None else observed_at, "Q")
            self.sample((next_read - now) // 2)

        def snapshot_frame(self) -> tuple[float, ...]:
            # 健康 IDLE 的 clear 只触发本拍一致发布，不改变反馈或开放功率。
            self.receive(b"clear\n")
            self.sample()
            return self.frame()

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
    for settings in [{"model_facts_rate": 10000}, {"model_facts_period": 1},
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
    model.sample(100)
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
        model.sample(100)
        frame = model.frame()
        assert math.isnan(frame[5]) and int(frame[8]) & VALID_BUS == 0
    for value in [-1.0, float("nan"), float("inf")]:
        model = Model()
        model.write("model_bus_voltage", value, "f")
        model.sample(100)
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

    for cycles in [74 * 170, 126 * 170]:
        model = Model()
        model.sample()
        model.write("model_tick_cycles", cycles)
        model.sample()
        assert model.motor()[1] & FAULT_CADENCE
    for first_sample in [False, True]:
        model = Model()
        if first_sample:
            model.sample(100)
        previous = model.motor()
        before = model.word("model_safeoff_count")
        model.guard(126)
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
    model.guard(100, interrupt_clock=True)
    assert model.interruptions == 1
    assert model.word("g_target_runtime_guard_faults") == 0, "ADC preemption invented guard fault"
    assert model.interrupt_masks == [0]
    assert model.motor()[0:2] == (IDLE, 0)
    model.sample()
    model.assert_default_off()
    model = Model()
    model.sample(100)
    model.memory_write(DWT_CYCCNT, model.memory_word(DWT_CYCCNT) + 100 * 170)
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

    for first, second, expected in [(0.99, 0.001, 660.0), (0.01, 0.999, -660.0)]:
        model = Model()
        model.position(first)
        frame = model.snapshot_frame()
        assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
        model.position(second)
        frame = model.snapshot_frame()
        assert int(frame[8]) & VALID_VELOCITY
        assert math.isclose(frame[2], expected, abs_tol=0.01), frame
        assert all(math.isnan(frame[channel]) for channel in [3, 4])
        assert model.motor()[0:2] == (IDLE, 0)
        model.assert_default_off()
    passed("independent periodic observations establish signed velocity across either zero crossing")

    model = Model()
    model.position(0.99)
    model.position(0.001)
    first_frame = model.snapshot_frame()
    # 同时间戳即使载荷变化也不能成为新采集，更不能对重复位置求出零速度。
    model.write("model_position_turns", 0.4, "f")
    model.sample(20)
    frame = model.snapshot_frame()
    assert frame[2] == first_frame[2] and int(frame[8]) & VALID_VELOCITY
    assert model.word("model_position_read_count") == 4
    # 下一份观测间隔为 60 tick（3 ms），不是 20 tick 轮询间隔。
    model.position(0.004, observed_at=100)
    frame = model.snapshot_frame()
    expected = first_frame[2] * (10.0 / 13.0) + 60.0 * (3.0 / 13.0)
    assert math.isclose(frame[2], expected, abs_tol=0.01), (frame, expected)
    passed("duplicate timestamps preserve velocity and a later observation uses its actual capture interval")

    model = Model()
    model.position(0.10)
    model.position(0.11)
    model.write("model_position_available", 0)
    model.sample(20)
    frame = model.snapshot_frame()
    assert math.isclose(frame[2], 600.0, abs_tol=0.01)
    assert int(frame[8]) & VALID_VELOCITY
    # 到 140 拍恰好为 5 ms；到142tick便应失效，无需等下一次 10 样本轮询。
    model.sample((138 - model.control_time()) // 2)
    frame = model.snapshot_frame()
    assert model.control_time() == 140
    assert math.isclose(frame[2], 600.0, abs_tol=0.01)
    assert int(frame[8]) & VALID_VELOCITY
    model.sample()
    frame = model.snapshot_frame()
    assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
    assert model.motor()[0:2] == (IDLE, 0), "missing feedback must not invent a board fault"
    model.position(0.12)
    frame = model.snapshot_frame()
    assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
    model.position(0.13)
    frame = model.snapshot_frame()
    assert math.isclose(frame[2], 600.0, abs_tol=0.01)
    assert int(frame[8]) & VALID_VELOCITY
    passed("unavailable publication retains fresh cache, expires after 5 ms and requires two new samples to recover")

    invalid_positions = ["acquisition", "reference", "future", "rollback", "stale",
                         "negative", "one_turn", "nan", "infinite"]
    for invalid in invalid_positions:
        model = Model()
        model.position(0.10)
        model.position(0.11)
        turns, timestamp = 0.12, 60
        if invalid == "acquisition":
            model.write("model_position_valid", 0)
        elif invalid == "reference":
            model.write("model_position_referenced", 0)
        elif invalid == "future":
            timestamp = 61
        elif invalid == "rollback":
            timestamp = 39
        elif invalid == "stale":
            model.sample(50)
            timestamp = 59  # 下一次读取在 160 拍，年龄为 101 tick。
        else:
            turns = {"negative": -0.1, "one_turn": 1.0,
                     "nan": float("nan"), "infinite": float("inf")}[invalid]
        model.position(turns, timestamp)
        frame = model.snapshot_frame()
        assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0, invalid
        assert model.motor()[0:2] == (IDLE, 0), invalid
        model.write("model_position_valid", 1)
        model.write("model_position_referenced", 1)
        model.position(0.20)
        frame = model.snapshot_frame()
        assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0, invalid
        model.position(0.21)
        frame = model.snapshot_frame()
        assert math.isclose(frame[2], 600.0, abs_tol=0.02), (invalid, frame)
        assert int(frame[8]) & VALID_VELOCITY, invalid
        model.assert_default_off()
    passed("invalid acquisition/reference/value and stale/future/backward timestamps discard history before rebuilding")

    model = Model()
    model.position(0.10)
    model.position(0.11)
    frame = model.snapshot_frame()
    assert int(frame[8]) & VALID_VELOCITY
    model.receive(b"start:100\n")
    model.sample()
    assert model.motor()[0:3] == (IDLE, 0, NOT_READY)
    frame = model.frame()
    assert frame[9] == float(NOT_READY) and int(frame[8]) & VALID_VELOCITY
    previous = model.motor()
    model.guard(126)
    assert model.motor() == previous
    frame = model.frame()
    assert frame[8] == 0.0
    assert all(math.isnan(value) for index, value in enumerate(frame) if index != 8)
    model.assert_default_off()
    passed("valid velocity does not permit Start or power; unincorporated guard evidence invalidates every telemetry field")

    for first, second, direction in [(0.99, 0.001, 1.0), (0.01, 0.999, -1.0)]:
        model = Model()
        model.position(first)
        model.position(second)
        frame = model.frame()
        assert math.isclose(frame[2], direction * 660.0, abs_tol=0.01)
        assert int(frame[8]) & VALID_VELOCITY
        assert model.motor()[3] == 0, "feedback publication must not depend on a command"
        model.write("model_position_available", 0)
        model.sample(50)
        assert model.control_time() == 140
        frame = model.frame()
        assert math.isclose(frame[2], direction * 660.0, abs_tol=0.01)
        assert int(frame[8]) & VALID_VELOCITY
        model.sample()
        frame = model.frame()
        assert model.control_time() == 142
        assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
        model.position(0.20)
        frame = model.frame()
        assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
        model.position(0.20 + direction * 0.01)
        frame = model.frame()
        assert math.isclose(frame[2], direction * 600.0, abs_tol=0.02)
        assert int(frame[8]) & VALID_VELOCITY
        assert model.motor()[3] == 0
        model.assert_default_off()
    passed("velocity validity changes publish naturally at acquisition, 5 ms expiry and signed recovery without commands")

    for rejected in ["model_position_valid", "model_position_referenced"]:
        model = Model()
        model.position(0.10)
        model.position(0.11)
        frame = model.frame()
        assert int(frame[8]) & VALID_VELOCITY
        model.write(rejected, 0)
        model.position(0.12)
        assert model.control_time() == 60  # 未到 100 拍周期发布，必须由失效变化触发。
        frame = model.frame()
        assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
        model.write(rejected, 1)
        model.position(0.13)
        frame = model.frame()
        assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
        model.position(0.14)
        frame = model.frame()
        assert math.isclose(frame[2], 600.0, abs_tol=0.01)
        assert int(frame[8]) & VALID_VELOCITY
        assert model.motor()[3] == 0
    passed("invalid observation immediately withdraws published velocity and two new samples restore it without commands")

    model = Model()
    model.position(0.10)
    model.position(0.11)
    model.write("model_ms", 10)
    for _ in range(5):
        model.service(TXE)
    assert len(model.output) == 5
    model.write("model_position_available", 0)
    model.sample(51)
    # 已经开始发送的旧帧保持完整；下份帧必须来自已撤销速度的新版快照。
    for _ in range(39):
        model.service(TXE)
    assert len(model.output) == 44
    old_frame = struct.unpack("<10f", model.output[:40])
    assert int(old_frame[8]) & VALID_VELOCITY
    frame = model.frame()
    assert math.isnan(frame[2]) and int(frame[8]) & VALID_VELOCITY == 0
    assert model.motor()[3] == 0
    model.assert_default_off()
    passed("an in-flight frame completes consistently and the next frame reflects feedback expiry without a command")

    model = Model()
    model.sample(10)
    frame = model.frame()
    assert frame[5] == 24.0 and int(frame[8]) & VALID_BUS
    model.write("model_bus_available", 0)
    model.sample(50)
    assert model.control_time() == 120
    frame = model.frame()
    assert frame[5] == 24.0 and int(frame[8]) & VALID_BUS
    model.sample()
    frame = model.frame()
    assert model.control_time() == 122
    assert math.isnan(frame[5]) and int(frame[8]) & VALID_BUS == 0
    model.write("model_bus_available", 1)
    model.sample(9)
    frame = model.frame()
    assert model.control_time() == 140
    assert frame[5] == 24.0 and int(frame[8]) & VALID_BUS
    model.write("model_bus_valid", 0)
    model.sample(10)
    frame = model.frame()
    assert model.control_time() == 160
    assert math.isnan(frame[5]) and int(frame[8]) & VALID_BUS == 0
    assert model.motor()[3] == 0
    model.assert_default_off()
    passed("bus validity publishes on acquisition, 5 ms expiry, recovery and invalid capture without commands")

    model = Model()
    model.sample(500)
    current = model.current()
    assert current["state"] == CURRENT_WAITING and current["settle"] == 0, current
    assert current["count"] == 0 and current["offset"] == (0.0, 0.0, 0.0)
    model.receive(b"start:100\n")
    model.sample()
    assert model.motor()[0:3] == (IDLE, 0, NOT_READY)
    model.assert_current_unpublished()
    passed("缺少有效位置速度时持续等待零偏采集，Start 拒绝且 Id/Iq 保持无效")

    model = Model()
    model.call("model_current_without_permission", instruction_budget=1000000)
    assert model.word("model_unpermitted_init_ok") == 1
    current = model.current("model_unpermitted_runtime")
    assert current["state"] == CURRENT_WAITING and current["settle"] == 0, current
    assert current["count"] == 0 and not current["valid"]
    assert current["offset"] == (0.0, 0.0, 0.0)
    model.assert_default_off()
    passed("装配方未授予无外部电流许可时，即使位置静止且功率关闭也不启动校零")

    model = Model({"model_position_available": 1, "model_position_use_fixed_time": 0})
    model.sample(70)
    assert model.current()["state"] == CURRENT_WAITING and model.current()["settle"] == 51
    model.write("model_position_valid", 0)
    model.sample(20)
    assert model.current()["state"] == CURRENT_WAITING and model.current()["settle"] == 0
    model.write("model_position_valid", 1)
    model.sample(118)
    current = model.current()
    assert current["state"] == CURRENT_WAITING and current["settle"] == 99, current
    model.sample()
    assert model.current()["state"] == CURRENT_CALIBRATING and model.current()["count"] == 1
    model.assert_current_unpublished()
    passed("等待阶段位置失效清空静止计数，恢复后重新连续等待 100 拍才开始完整窗口")

    model = Model()
    model.complete_current_window()
    current = model.current()
    assert all(math.isclose(value, expected, abs_tol=1e-6)
               for value, expected in zip(current["offset"], (0.0, 0.02, -0.03))), current
    assert current["phase"] == (0.0, 0.0, 0.0)
    frozen = current["offset"]
    model.write("model_current_b", 0.32, "f")
    model.write("model_current_c", -0.13, "f")
    model.sample()
    current = model.current()
    assert current["state"] == CURRENT_READY and current["valid"], current
    assert current["count"] == 2048 and current["offset"] == frozen
    assert all(math.isclose(value, expected, abs_tol=1e-6)
               for value, expected in zip(current["phase"], (0.2, -0.3, 0.1))), current
    assert math.isclose(current["alpha"], 0.2, abs_tol=1e-6)
    assert math.isclose(current["beta"], -0.4 / math.sqrt(3.0), abs_tol=1e-6)
    model.write("model_position_turns", 0.01, "f")
    model.sample(20)
    assert model.current()["state"] == CURRENT_READY and model.current()["offset"] == frozen
    model.sample(500)
    assert model.current()["count"] == 2048 and model.current()["offset"] == frozen
    model.receive(b"start:100\n")
    model.sample()
    assert model.motor()[0:3] == (IDLE, 0, NOT_READY)
    model.assert_current_unpublished()
    passed("完整 2048 点后冻结 BC 零偏，逐拍重构三相和 Clarke，手转不重学且仍不发布 Id/Iq")

    model = Model()
    model.begin_current_window()
    model.sample(100)
    model.write("model_position_turns", 0.02, "f")
    model.sample(20)
    current = model.current()
    assert current["state"] == CURRENT_FAILED and current["failure"] == CURRENT_ZERO_UNCONFIRMED, current
    assert current["offset"] == (0.0, 0.0, 0.0) and not current["valid"]
    stopped_count = current["count"]
    model.sample(1000)
    model.receive(b"clear\n")
    model.sample()
    assert model.current()["state"] == CURRENT_FAILED and model.current()["count"] == stopped_count
    assert model.motor()[0:2] == (IDLE, 0)
    model.assert_current_unpublished()
    passed("零偏窗口内运动锁定失败，恢复静止或 Clear 均不会自动重试和恢复零偏")

    for rejected in ["model_position_valid", "model_position_referenced", "model_position_available"]:
        model = Model()
        model.begin_current_window()
        model.write(rejected, 0)
        model.sample(101)
        current = model.current()
        assert current["state"] == CURRENT_FAILED and current["failure"] == CURRENT_ZERO_UNCONFIRMED, rejected
        assert not current["valid"] and current["offset"] == (0.0, 0.0, 0.0), rejected
        stopped_count = current["count"]
        model.write(rejected, 1)
        model.sample(500)
        assert model.current()["state"] == CURRENT_FAILED and model.current()["count"] == stopped_count
        model.assert_current_unpublished()
    passed("零偏窗口内位置失效、失去参考或过期均锁定失败，后续新位置不能静默重试")

    for completed in [False, True]:
        for failure in ["trip", "current", "cadence", "unexpected_power"]:
            model = Model()
            if completed:
                model.complete_current_window()
            else:
                model.begin_current_window()
            if failure == "trip":
                model.write("model_trip_active", 1)
            elif failure == "current":
                model.write("model_current_valid", 0)
            elif failure == "cadence":
                model.write("model_tick_cycles", 126 * 170)
            else:
                model.memory_write(TIM1 + 0x44, MOE)
            model.sample()
            current = model.current()
            assert model.motor()[0] == FAULT, (completed, failure)
            assert current["state"] == CURRENT_FAILED and current["failure"] == CURRENT_ABORTED, current
            assert current["offset"] == (0.0, 0.0, 0.0) and not current["valid"]
            model.assert_current_unpublished()
    passed("采集中或 READY 后出现硬件故障、坏电流、采样节拍故障和意外功率均撤销零偏并关断")

    for completed in [False, True]:
        for time_step in [0, 4]:
            model = Model()
            if completed:
                model.complete_current_window()
            else:
                model.begin_current_window()
            model.write("model_current_time_step", time_step)
            model.sample()
            current = model.current()
            assert current["state"] == CURRENT_FAILED and current["failure"] == CURRENT_ABORTED, current
            assert current["offset"] == (0.0, 0.0, 0.0) and not current["valid"]
            stopped_count = current["count"]
            model.write("model_current_time_step", 2)
            model.sample(500)
            assert model.current()["state"] == CURRENT_FAILED and model.current()["count"] == stopped_count
            assert model.motor()[0:2] == (IDLE, 0)
            model.assert_current_unpublished()
    passed("同步电流时间戳重复或跳拍撤销采集与冻结零偏，后续连续样本不自动恢复")

    model = Model()
    model.begin_current_window()
    # 通过模型直接构造业务越权；即使没有功率使能，非 IDLE 也不得继续校零。
    model.memory_write(symbols["s_motor"] + model.offsets[0], RUN)
    model.phase = "sample"
    model.call("model_sample")
    model.phase = ""
    current = model.current()
    assert current["state"] == CURRENT_FAILED and current["failure"] == CURRENT_ABORTED
    assert current["offset"] == (0.0, 0.0, 0.0) and not current["valid"]
    assert model.word("model_stage_count") == model.word("model_enable_count") == 0
    assert model.memory_word(TIM1 + 0x44) & MOE == 0
    passed("非 IDLE 状态不能继续零偏窗口，测试注入不产生任何功率请求")

    model = Model()
    model.position(0.10)
    model.position(0.11)
    assert model.angle()["velocity_valid"]
    model.assert_angle_invalid()
    model.receive(b"start:100\n")
    model.sample()
    assert model.motor()[0:3] == (IDLE, 0, NOT_READY)
    model.assert_angle_invalid()
    model.assert_current_unpublished()
    # 数值参数合法，也不能越过装配方的标定可信标志。
    model.call("model_angle_start")
    assert model.word("model_angle_init_ok") == 1
    model.angle_position(0.10)
    model.angle_position(0.11)
    assert model.angle("model_angle_runtime")["valid"]
    model = Model({"model_angle_calibration_confirmed": 0})
    model.call("model_angle_start")
    assert model.word("model_angle_init_ok") == 1
    model.angle_position(0.10)
    model.angle_position(0.11)
    assert model.angle("model_angle_runtime")["velocity_valid"]
    model.assert_angle_invalid("model_angle_runtime")
    model.call("model_angle_command_start")
    base = symbols["model_angle_motor"]
    assert model.memory_word(base + model.offsets[0]) == IDLE
    assert model.memory_word(base + model.offsets[2]) == NOT_READY
    assert model.word("model_angle_snapshot_id_valid") == model.word("model_angle_snapshot_iq_valid") == 0
    model.assert_default_off()
    passed("默认与未确认标定入口即使速度有效也不产生电角度，Start 和 Id/Iq 仍不开放")

    for trusted, pairs, expected_init in [(0, 0, 1), (1, 0, 0), (1, 65, 0)]:
        model = Model({"model_angle_calibration_confirmed": trusted,
                       "model_angle_pole_pairs": pairs})
        model.call("model_angle_start")
        assert model.word("model_angle_init_ok") == expected_init
        model.assert_angle_invalid("model_angle_runtime")
        model.assert_default_off()
    passed("未确认标定不使用空参数，确认后非法极对数使 Runtime 初始化失败")

    for direction in [1, -1]:
        model = Model()
        model.write("model_angle_direction", direction, "i")
        model.call("model_angle_start")
        assert model.word("model_angle_init_ok") == 1
        model.angle_position(0.10)
        model.assert_angle_invalid("model_angle_runtime")
        model.angle_position(0.11)
        assert model.control_time() == 40
        for ticks in range(21):
            if ticks:
                model.angle_sample()
            angle = model.angle("model_angle_runtime")
            expected = (7 * direction * (0.11 + 600 / 60 * (2 * ticks) / 20000) - 0.125) % 1
            assert angle["valid"] and math.isclose(angle["turns"], expected, abs_tol=2e-6), (ticks, angle)
            assert math.isclose(angle["sin"], math.sin(2 * math.pi * expected), abs_tol=1e-5)
            assert math.isclose(angle["cos"], math.cos(2 * math.pi * expected), abs_tol=1e-5)
        model.angle_sample()
        angle = model.angle("model_angle_runtime")
        assert model.control_time() == 82 and angle["velocity_valid"]
        model.assert_angle_invalid("model_angle_runtime")
    passed("合成正反方向以位置采集时刻外推，每个 fast tick 更新且预测年龄恰好 2ms 可用")

    for direction in [1, -1]:
        model = Model()
        model.write("model_angle_direction", direction, "i")
        model.call("model_angle_start")
        model.angle_position(0.10, observed_at=20)
        model.angle_position(0.11, observed_at=35)
        assert model.control_time() == 40
        first_interval_s = (35 - 20) / 20000
        first_velocity = (0.11 - 0.10) * 60 / first_interval_s
        first_age_s = (40 - 35) / 20000
        first_expected = (7 * direction * (0.11 + first_velocity / 60 * first_age_s) - 0.125) % 1
        angle = model.angle("model_angle_runtime")
        assert angle["valid"] and math.isclose(angle["velocity"], first_velocity, abs_tol=0.02), angle
        assert math.isclose(angle["turns"], first_expected, abs_tol=2e-6), (angle, first_expected)
        # 采集节拍和消费节拍分别计算；速度改变后必须使用实际 LPF 状态外推。
        model.angle_position(0.13, observed_at=55)
        assert model.control_time() == 60
        interval_s = (55 - 35) / 20000
        raw_velocity = (0.13 - 0.11) * 60 / interval_s
        alpha = interval_s / (0.01 + interval_s)
        filtered_velocity = first_velocity + alpha * (raw_velocity - first_velocity)
        age_s = (60 - 55) / 20000
        expected = (7 * direction * (0.13 + filtered_velocity / 60 * age_s) - 0.125) % 1
        angle = model.angle("model_angle_runtime")
        assert angle["valid"] and math.isclose(angle["velocity"], filtered_velocity, abs_tol=0.02), angle
        assert math.isclose(angle["turns"], expected, abs_tol=2e-6), (angle, expected)
        model.assert_default_off()
    passed("延迟消费位置按真实采集间隔估速，变速后使用 10ms 低通速度与采集年龄外推电角度")

    model = Model()
    model.call("model_angle_start")
    model.angle_position(0.10)
    model.angle_position(0.11)
    model.write("model_position_available", 0)
    model.angle_sample(21)
    assert model.angle("model_angle_runtime")["velocity_valid"]
    model.assert_angle_invalid("model_angle_runtime")
    # 外推超时只使电角度失效，未抛弃尚在 5ms 范围内的速度历史。
    model.angle_position(0.13)
    assert model.angle("model_angle_runtime")["valid"]
    model.write("model_position_available", 0)
    model.angle_sample(50)
    assert model.angle("model_angle_runtime")["velocity_valid"]
    model.assert_angle_invalid("model_angle_runtime")
    model.angle_sample()
    assert not model.angle("model_angle_runtime")["velocity_valid"]
    model.angle_position(0.14)
    model.assert_angle_invalid("model_angle_runtime")
    model.angle_position(0.15)
    assert model.angle("model_angle_runtime")["valid"]
    passed("预测 horizon 独立于 5ms 位置 freshness，单纯外推过期与丢弃位置历史有不同恢复条件")

    for invalid in ["acquisition", "reference", "future", "rollback", "stale", "nan"]:
        model = Model()
        model.call("model_angle_start")
        model.angle_position(0.10)
        model.angle_position(0.11)
        assert model.angle("model_angle_runtime")["valid"]
        turns, captured = 0.12, 60
        if invalid == "acquisition":
            model.write("model_position_valid", 0)
        elif invalid == "reference":
            model.write("model_position_referenced", 0)
        elif invalid == "future":
            captured = 61
        elif invalid == "rollback":
            captured = 39
        elif invalid == "stale":
            captured = 0
            model.angle_sample(50)
        else:
            turns = float("nan")
        model.angle_position(turns, captured)
        model.assert_angle_invalid("model_angle_runtime")
        assert not model.angle("model_angle_runtime")["velocity_valid"], invalid
        model.write("model_position_valid", 1)
        model.write("model_position_referenced", 1)
        model.angle_position(0.20)
        model.assert_angle_invalid("model_angle_runtime")
        model.angle_position(0.21)
        assert model.angle("model_angle_runtime")["valid"], invalid
        model.assert_default_off()
    passed("位置坏值、失去参考及未来回退过期时间立即撤销电角度，恢复需两份新观测")

    for step in [0, 4, 0xFFFFFFFF]:
        model = Model()
        model.call("model_angle_start")
        model.angle_position(0.10)
        model.angle_position(0.11)
        before_reads = model.word("model_position_read_count")
        if step == 0xFFFFFFFF:
            # ControlTime 为 uint64；用模型时钟回退而非无符号步长相加。
            model.write("model_control_time", 38, "Q")
            model.write("model_current_time_step", 2)
        else:
            model.write("model_current_time_step", step)
        model.angle_sample()
        model.assert_angle_invalid("model_angle_runtime")
        assert not model.angle("model_angle_runtime")["velocity_valid"]
        assert model.word("model_position_read_count") == before_reads
        model.write("model_current_time_step", 2)
        model.angle_position(0.20)
        model.assert_angle_invalid("model_angle_runtime")
        model.angle_position(0.21)
        assert model.angle("model_angle_runtime")["valid"]
        model.assert_default_off()
    passed("fast tick 重复跳拍或回退时不读取新位置并撤销预测，连续采集恢复后重新建立基线")

    model = Model()
    model.call("model_angle_start")
    model.angle_position(0.10)
    model.angle_position(0.11)
    model.call("model_angle_command_start")
    assert model.angle("model_angle_runtime")["valid"]
    base = symbols["model_angle_motor"]
    assert model.memory_word(base + model.offsets[0]) == IDLE
    assert model.memory_word(base + model.offsets[2]) == NOT_READY
    assert model.word("model_angle_snapshot_id_valid") == model.word("model_angle_snapshot_iq_valid") == 0
    model.write("model_angle_faults", FAULT_TRIP)
    model.angle_sample()
    assert model.memory_word(base + model.offsets[0]) == FAULT
    model.assert_angle_invalid("model_angle_runtime")
    model.assert_default_off()
    passed("合成可信电角度不提供功率许可或 Id/Iq，故障仍立即撤销角度且不 stage 或 enable")

    model = Model()
    assert model.capture()["state"] == CAPTURE_IDLE
    rejected = model.request_capture(1)
    assert rejected["state"] == CAPTURE_FAILED and rejected["failure"] == CAPTURE_NOT_READY
    assert rejected["count"] == 0
    model.write("model_position_available", 1)
    model.write("model_position_use_fixed_time", 0)
    model.write("model_current_b", 0.02, "f")
    model.write("model_current_c", -0.03, "f")
    model.sample((4334 - model.control_time()) // 2)
    assert model.current()["state"] == CURRENT_READY and model.current()["count"] == 2048
    before = model.capture_bytes(), model.capture()["sequence"]
    # 同一序号既不能在条件变好后重放，也不能因主机重复写入自动重试。
    model.request_capture(1)
    model.sample(100)
    assert (model.capture_bytes(), model.capture()["sequence"]) == before
    started = model.request_capture(2)
    assert started["state"] == CAPTURE_ACTIVE and started["failure"] == 0
    model.assert_current_unpublished()
    passed("诊断请求须在零偏 READY 后显式给新序号，拒绝结果和重复序号不会自动重放")

    model = Model()
    model.complete_current_window()
    frozen = model.current()["offset"]
    model.write("model_current_b", 0.12, "f")
    model.write("model_current_c", -0.23, "f")
    requested_at = model.control_time() + 2
    active = model.request_capture(1)
    assert active["state"] == CAPTURE_ACTIVE
    latched_active = model.capture_bytes(), active["sequence"]
    model.sample(1023)
    model.write("model_current_b", 0.32, "f")
    model.write("model_current_c", -0.43, "f")
    model.sample(1023)
    assert (model.capture_bytes(), model.capture()["sequence"]) == latched_active
    model.sample()
    capture = model.capture()
    assert capture["state"] == CAPTURE_COMPLETE and capture["failure"] == 0
    assert capture["count"] == 2048 and capture["first"] == requested_at
    assert capture["last"] - capture["first"] == 4094
    assert capture["sequence"] > active["sequence"] and capture["sequence"] % 2 == 0
    # 不发额外命令/采样催更；完成当拍须发布健康时间，不能仍是较早的100Hz快照。
    frame = model.frame()
    assert int(frame[8]) & VALID_TIME
    assert frame[0] * 20000 >= capture["last"] - 0.01, (frame, capture)
    assert model.current()["offset"] == frozen
    assert all(math.isclose(actual, expected, abs_tol=1e-6)
               for actual, expected in zip(capture["offset"], frozen[1:]))
    expected_variance = 2048 / 2047 * 0.1 ** 2
    for phase, expected in [("b", (0.22, 0.12, 0.32)), ("c", (-0.33, -0.43, -0.23))]:
        assert all(math.isclose(actual, value, abs_tol=3e-6)
                   for actual, value in zip(capture[phase][:3], expected)), capture
        assert math.isclose(capture[phase][3], expected_variance, abs_tol=3e-6), capture
    # 名义原始均值与冻结零偏分别留存；非零窗口不扣零也不重新学习。
    first_read = model.capture_bytes(), capture["sequence"]
    assert model.capture_bytes() == first_read[0]
    model.sample(100)
    model.assert_current_unpublished()
    assert (model.capture_bytes(), model.capture()["sequence"]) == first_read
    model.request_capture(1)
    model.sample(100)
    assert (model.capture_bytes(), model.capture()["sequence"]) == first_read
    requested_at = model.control_time() + 2
    assert model.request_capture(2)["state"] == CAPTURE_ACTIVE
    model.sample(2047)
    capture = model.capture()
    assert capture["request"] == 2 and capture["state"] == CAPTURE_COMPLETE
    assert capture["count"] == 2048 and capture["first"] == requested_at
    assert capture["last"] - capture["first"] == 4094
    assert math.isclose(capture["b"][0], 0.32, abs_tol=1e-6)
    assert math.isclose(capture["c"][0], -0.43, abs_tol=1e-6)
    assert capture["b"][3] == capture["c"][3] == 0.0
    assert model.current()["offset"] == frozen
    model.assert_current_unpublished()
    passed("单次2048点保存原始BC统计与冻结零偏，首点属于请求当拍、活动期锁存不变、完成后新请求另开窗口")

    model = Model()
    model.complete_current_window()
    assert model.request_capture(1)["state"] == CAPTURE_ACTIVE
    model.sample(10)
    busy = model.request_capture(2)
    assert busy["request"] == 2 and busy["state"] == CAPTURE_FAILED
    assert busy["failure"] == CAPTURE_BUSY and busy["count"] == 0
    before = model.capture_bytes(), busy["sequence"]
    model.sample(100)
    assert (model.capture_bytes(), model.capture()["sequence"]) == before
    assert model.request_capture(3)["state"] == CAPTURE_ACTIVE
    model.assert_current_unpublished()
    passed("新请求撞上活动窗口时丢弃旧统计并锁存新序号BUSY，不隐式启动替代窗口")

    for simultaneous_request in [False, True]:
        model = Model()
        model.complete_current_window()
        assert model.request_capture(1)["state"] == CAPTURE_ACTIVE
        model.sample(10)
        model.receive(b"stop\n")
        if simultaneous_request:
            model.write("g_target_runtime_current_request", 2)
        model.sample()
        capture = model.capture()
        assert capture["state"] == CAPTURE_FAILED
        if simultaneous_request:
            assert capture["request"] == 2 and capture["failure"] == CAPTURE_NOT_READY
            assert capture["count"] == 0
        else:
            assert capture["request"] == 1 and capture["failure"] == CAPTURE_ABORTED
            assert 0 < capture["count"] < 2048
        before = model.capture_bytes(), capture["sequence"]
        model.sample(100)
        assert (model.capture_bytes(), model.capture()["sequence"]) == before
        assert model.motor()[0:3] == (IDLE, 0, ACCEPTED)
        model.assert_current_unpublished()
    passed("Stop显式中止活动诊断窗口，同拍新请求仍拒绝且不因持续IDLE自动恢复采集")

    for failure in ["trip", "current", "time_duplicate", "time_skip", "motion", "unexpected_power", "position"]:
        model = Model()
        model.complete_current_window()
        assert model.request_capture(1)["state"] == CAPTURE_ACTIVE
        model.sample(5)
        if failure == "trip":
            model.write("model_trip_active", 1)
        elif failure == "current":
            model.write("model_current_valid", 0)
        elif failure in ["time_duplicate", "time_skip"]:
            model.write("model_current_time_step", 0 if failure == "time_duplicate" else 4)
        elif failure == "motion":
            model.write("model_position_turns", 0.01, "f")
        elif failure == "unexpected_power":
            model.memory_write(TIM1 + 0x44, MOE)
        else:
            model.write("model_position_valid", 0)
        model.sample(20 if failure in ["motion", "position"] else 1)
        capture = model.capture()
        assert capture["state"] == CAPTURE_FAILED and capture["failure"] != 0, (failure, capture)
        assert capture["request"] == 1 and 0 < capture["count"] < 2048, (failure, capture)
        assert capture["sequence"] % 2 == 0
        before = model.capture_bytes(), capture["sequence"]
        model.write("model_current_time_step", 2)
        model.sample(100)
        assert (model.capture_bytes(), model.capture()["sequence"]) == before, failure
        model.assert_current_unpublished()
    passed("活动诊断在故障、坏采样、时间异常、运动、位置失效及意外功率时失败锁存且不stage或enable")

    model = Model()
    model.write("g_target_runtime_compute_request", 1)
    model.service()
    assert model.compute()["state"] == 3 and model.compute()["failure"] == 1
    frozen = model.compute()
    model.prepare_compute()
    model.service()
    assert model.compute() == frozen
    model.assert_default_off()
    passed("计时诊断在无真实采样或时钟许可时拒绝，拒绝身份在条件改善后不自动重放")

    for request_name, failure_field in [("g_target_runtime_compute_request", "compute"),
                                        ("g_target_runtime_stage_request", "stage")]:
        model = Model()
        model.prepare_compute()
        model.write(request_name, 1)
        model.service()
        model.sample()
        result = model.compute() if failure_field == "compute" else model.stage_budget()
        assert result["state"] in (3, 4) and result["failure"] == 1, result
        assert model.word("model_stage_count") == model.word("model_enable_count") == 0
        frozen = result
        model.sample(10)
        model.service()
        assert (model.compute() if failure_field == "compute" else model.stage_budget()) == frozen
        model.assert_default_off()
    passed("100us入口明确拒绝旧compute/stage诊断并冻结NOT_READY，不执行stage或enable")

    print(f"PASS: {len(CASES)} 组 Observe Runtime ARM 集成模型；实际100us采样配置，HCC/MMIO为替身")
    for name in CASES:
        print("  " + name)
