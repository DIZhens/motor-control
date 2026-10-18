"""Execute the real G431 Platform ARM object; no ST-Link or board access.

The timer model implements CCR preloads, update transfers, UDIS, CNT/DIR and
status-flag clearing. Time advances only at explicit test injection points: this
is an ordering/deadline regression, not a cycle-accurate peripheral or WCET test.
"""
from pathlib import Path
import os
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_PRIMASK

ROOT = Path(__file__).resolve().parents[2]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
TIM1 = 0x40012C00
CR1, SR, EGR, CNT, ARR = TIM1, TIM1 + 0x10, TIM1 + 0x14, TIM1 + 0x24, TIM1 + 0x2C
CCRS = (TIM1 + 0x34, TIM1 + 0x38, TIM1 + 0x3C)
BDTR = TIM1 + 0x44
DWT_CTRL, DWT_CYCCNT = 0xE0001000, 0xE0001004
ADC2_ISR = 0x50000100
CEN, UDIS, DIR, MOE = 1, 2, 16, 1 << 15
BREAKS = (1 << 7) | (1 << 8)
STOP = 0x0801F000
NEUTRAL = (2125, 2125, 2125)
REQUEST = (850, 2125, 3400)
STAGE, NULL_REQUEST, DUPLICATE, OFF, ENABLE = 2, 4, 8, 16, 32


def compile_image(directory, extra_sources=(), definitions=()):
    scatter = directory / "platform.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    settings = ET.parse(ROOT / "MDK-ARM/BLDC_control V1.uvprojx").find(".//Cads/VariousControls")
    command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
               "--split_sections", "-g", "-I", str(ROOT / "platform")]
    for include in settings.findtext("IncludePath").split(";"):
        command += ["-I", str((ROOT / "MDK-ARM" / include.replace("\\", "/")).resolve())]
    command += ["-D" + item for item in settings.findtext("Define").split(",")]
    command += ["-D" + item for item in definitions]
    objects = []
    # Compile the production translation unit unchanged; no stage/enable substitutes.
    for source in [ROOT / "validation/tests/platform_model.c", ROOT / "platform/realtime_motor_io.c", *extra_sources]:
        obj = directory / (source.stem + ".o")
        result = subprocess.run(command + ["-c", str(source), "-o", str(obj)],
                                capture_output=True, text=True)
        assert result.returncode == 0 and "warning" not in result.stderr.lower(), result.stderr
        objects.append(str(obj))
    result = subprocess.run([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp",
                             "--scatter", str(scatter), "--entry=platform_model_init",
                             "--keep=platform_model.o(*)", "-o", str(directory / "platform.axf"),
                             *objects], cwd=directory, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with (directory / "platform.axf").open("rb") as stream:
        elf = ELFFile(stream)
        contents = [(s["p_vaddr"], s.data()) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        contents += [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                     if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
    return contents, symbols


class Model:
    def __init__(self, image, hal_ok=True, cycle_counter=123456, auto_start=True):
        contents, self.symbols = image
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        for base, size in [(0x08000000, 0x20000), (0x20000000, 0x8000),
                           (0x40012000, 0x1000), (0x40021000, 0x1000), (0x50000000, 0x2000),
                           (0xE0001000, 0x1000), (0xE000E000, 0x1000)]:
            self.cpu.mem_map(base, size)
        for address, data in contents:
            self.cpu.mem_write(address, data)
        self.put(0xE000ED88, 0x00F00000)
        self.put(DWT_CYCCNT, cycle_counter)
        self.shadow = [0, 0, 0]
        self.active = (0, 0, 0)
        self.transfers = []
        self.writes = []
        self.pending_fixes = {}
        self.pending_delay = 0
        self.pending_irq = False
        self.interrupted = False
        self.in_irq = False
        self.on_marker = None
        self.on_ccr = None
        self.on_read = None
        self.on_write = None
        self.on_instruction = None
        self.irq_calls = 0
        self.instruction_budget = 200000
        # Unicorn 2.1.4's global RAM-write hook misexecutes ARMCC memclr's IT tail.
        # Observe only modeled MMIO and the explicit marker, leaving bulk RAM alone.
        for start, end in [(TIM1, TIM1 + 0x7F), (ADC2_ISR, ADC2_ISR + 3),
                           (DWT_CTRL, DWT_CYCCNT + 3),
                           (self.symbols["model_marker"], self.symbols["model_marker"] + 3)]:
            self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.memory_write, begin=start, end=end)
        # Time/fault injections read MMIO only; avoid instrumenting conditional RAM loads.
        for start, end in [(TIM1, TIM1 + 0x7F), (DWT_CTRL, DWT_CYCCNT + 3)]:
            self.cpu.hook_add(UC_HOOK_MEM_READ, self.memory_read, begin=start, end=end)
        self.cpu.hook_add(UC_HOOK_CODE, self.instruction)
        self.write("model_hal_ok", int(hal_ok))
        self.write("model_auto_start", int(auto_start))
        self.call("platform_model_init")

    def put(self, address, value):
        self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def get(self, address):
        return struct.unpack("<I", self.cpu.mem_read(address, 4))[0]

    def write(self, symbol, value, fmt="I"):
        self.cpu.mem_write(self.symbols[symbol], struct.pack("<" + fmt, value))

    def word(self, symbol):
        return self.get(self.symbols[symbol])

    def moe(self):
        return bool(self.get(BDTR) & MOE)

    def transfer(self, cause):
        suppressed = bool(self.get(CR1) & UDIS)
        if not suppressed:
            self.active = tuple(self.shadow)
            self.put(SR, self.get(SR) | 1)
        self.transfers.append((cause, suppressed, self.active, tuple(self.shadow)))

    def advance(self, cycles):
        if self.get(DWT_CTRL) & 1:
            self.put(DWT_CYCCNT, self.get(DWT_CYCCNT) + cycles)
        while cycles and self.get(CR1) & CEN:
            down = bool(self.get(CR1) & DIR)
            count, top = self.get(CNT), self.get(ARR)
            distance = count if down else top - count
            assert distance >= 0 and top > 0
            if cycles < distance:
                self.put(CNT, count - cycles if down else count + cycles)
                break
            cycles -= distance
            self.put(CNT, 0 if down else top)
            self.put(CR1, self.get(CR1) ^ DIR)
            self.transfer("bottom" if down else "top")

    def position(self, count=4000, down=True):
        self.put(CNT, count)
        self.put(CR1, (self.get(CR1) | DIR) if down else (self.get(CR1) & ~DIR))

    def memory_write(self, cpu, access, address, size, value, user_data):
        if self.on_write:
            self.on_write(self, address, value)
        if address == SR:
            self.pending_fixes[address] = self.get(SR) & value
        elif address == ADC2_ISR:
            self.pending_fixes[address] = self.get(ADC2_ISR) & ~value
        elif address == EGR and value & 1:
            self.transfer("UG")
        elif address in CCRS:
            self.shadow[CCRS.index(address)] = value
            self.writes.append((address, value, self.get(CR1), cpu.reg_read(UC_ARM_REG_PRIMASK)))
            if self.on_ccr:
                self.on_ccr(self, address, value)
        elif address == self.symbols["model_marker"] and self.on_marker:
            self.on_marker(self, value)

    def memory_read(self, cpu, access, address, size, value, user_data):
        if self.on_read:
            self.on_read(self, address)

    def instruction(self, cpu, address, size, user_data):
        for target, value in self.pending_fixes.items():
            self.put(target, value)
        self.pending_fixes.clear()
        if self.pending_delay:
            delay, self.pending_delay = self.pending_delay, 0
            self.advance(delay)
        if self.on_instruction and not self.in_irq:
            self.on_instruction(self, address)
        if self.pending_irq and not self.in_irq and not cpu.reg_read(UC_ARM_REG_PRIMASK):
            self.interrupted = True
            cpu.emu_stop()

    def call(self, name, stack=0x20007FF0):
        self.cpu.reg_write(UC_ARM_REG_SP, stack)
        self.cpu.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.cpu.reg_write(UC_ARM_REG_PC, self.symbols[name] | 1)
        for _ in range(20):
            self.interrupted = False
            self.cpu.emu_start(self.cpu.reg_read(UC_ARM_REG_PC) | 1, STOP, count=self.instruction_budget)
            if self.interrupted:
                context = self.cpu.context_save()
                self.in_irq, self.pending_irq = True, False
                self.call("realtime_motor_io_safe_off", stack=0x20007000)
                self.irq_calls += 1
                self.in_irq = False
                self.cpu.context_restore(context)
            else:
                assert self.cpu.reg_read(UC_ARM_REG_PC) == STOP, name
                return
        raise AssertionError("interrupt injection did not settle")

    def tick(self, actions=STAGE, count=4000, down=True, advance=8500):
        self.advance(advance)
        self.position(count, down)
        self.put(ADC2_ISR, 1 << 6)
        self.write("model_actions", actions)
        for name in ["model_stage_result", "model_second_stage_result", "model_enable_result",
                     "model_early_enable_result"]:
            self.write(name, 0xFFFFFFFF)
        self.call("platform_model_tick")

    def powered(self):
        self.tick(STAGE)
        assert self.word("model_stage_result") == 1 and not self.moe()
        self.tick(STAGE | ENABLE)
        assert self.word("model_enable_result") == 1 and self.moe()
        return self


def run(image):
    cases = []

    def passed(name):
        cases.append(name)

    m = Model(image)
    assert m.word("model_bind_result") == m.word("model_arm_result") == m.word("model_start_result") == 1
    assert not m.moe() and m.active == NEUTRAL and m.get(CR1) & CEN
    assert m.get(DWT_CTRL) & 1 and m.get(DWT_CYCCNT) == 123456
    passed("startup seeds neutral with power off and preserves the shared DWT epoch")

    m.call("platform_model_stage_outside"); m.call("platform_model_enable_outside")
    assert m.word("model_stage_result") == m.word("model_enable_result") == 0 and not m.moe()
    passed("background stage and enable are rejected")

    m = Model(image, hal_ok=False)
    assert m.word("model_arm_result") == m.word("model_start_result") == 0 and not m.moe()
    passed("arm failure cannot start or enable")

    m = Model(image); m.call("platform_model_wrong_adc_tick")
    assert m.word("model_callbacks") == 0
    passed("ADC2 cannot create an extra control callback")

    def measured_current(model, phase):
        return struct.unpack("<f", model.cpu.mem_read(model.symbols["model_observation_" + phase + "_a"], 4))[0]

    for raw_b, raw_c in [(0, 4095), (4095, 0), (2048, 2014)]:
        m = Model(image)
        m.put(0x50000180, raw_b); m.put(0x50000080, raw_c)
        m.tick(0)
        assert m.word("model_observation_valid") == 1 and m.word("model_observation_mask") == 6
        for phase, raw in [("b", raw_b), ("c", raw_c)]:
            expected = (raw * 3.3 / 4095.0 - 1.623) / 0.7049
            assert abs(measured_current(m, phase) - expected) < 0.000002
        passed("fixed injected ranks preserve B/C channel mapping and nominal conversion: " + str((raw_b, raw_c)))

    m = Model(image); m.position(4000, True); m.put(ADC2_ISR, 0)
    m.put(0x50000180, 4095); m.put(0x50000080, 0)
    m.call("platform_model_tick")
    assert m.word("model_observation_valid") == 0 and m.word("model_observation_mask") == 4
    assert measured_current(m, "b") == 0.0 and measured_current(m, "c") < -2.0
    m.tick(0)
    assert m.word("model_observation_valid") == 0 and m.word("model_observation_mask") == 4
    passed("missing ADC2 completion invalidates pair and cannot reuse stale B data")

    invalid_configurations = [
        ("prescaler", TIM1 + 0x28, 1), ("period", ARR, 4249),
        ("repetition counter", TIM1 + 0x30, 1),
        ("counting mode", CR1, 0x80), ("clock division", CR1, 0x120),
        ("one pulse mode", CR1, 0xA8), ("update disabled", CR1, 0xA2),
        ("CCR1 preload", TIM1 + 0x18, 1 << 11),
        ("CCR2 preload", TIM1 + 0x18, 1 << 3),
        ("CCR3 preload", TIM1 + 0x1C, 1 << 11),
        ("update IRQ ownership", TIM1 + 0x0C, 1),
        ("APB2 prescaler", 0x40021008, 4 << 11),
    ]
    for name, register, value in invalid_configurations:
        m = Model(image, auto_start=False); m.put(register, value)
        m.call("platform_model_start")
        assert m.word("model_start_result") == 0 and not m.moe() and not m.get(CR1) & CEN
        passed("unsupported " + name + " cannot start Drive Timing")
    m = Model(image, auto_start=False); m.write("SystemCoreClock", 160000000)
    m.call("platform_model_start")
    assert m.word("model_start_result") == 0 and not m.moe()
    passed("unsupported core clock cannot start Drive Timing")

    m = Model(image); m.tick(STAGE | DUPLICATE)
    assert m.word("model_stage_result") == 1 and m.word("model_second_stage_result") == 0
    assert m.active == NEUTRAL and tuple(m.shadow) == REQUEST
    writes = m.writes[-3:]
    assert [item[0] for item in writes] == list(CCRS)
    assert all(item[2] & UDIS and item[3] == 1 for item in writes)
    m.advance(m.get(CNT))
    assert m.active == REQUEST and not m.get(CR1) & UDIS
    passed("one whole unequal three-phase request transfers at the next boundary")

    m = Model(image); m.tick(STAGE | ENABLE)
    assert m.word("model_stage_result") == 1 and m.word("model_enable_result") == 0
    assert not m.moe() and tuple(m.shadow) == NEUTRAL
    m.tick(STAGE); m.tick(STAGE | ENABLE)
    assert m.moe()
    passed("premature enable fails safe and requires a fresh stage-boundary-stage sequence")

    m = Model(image); m.tick(1 | STAGE)
    assert m.word("model_early_enable_result") == m.word("model_stage_result") == 0
    assert not m.moe() and tuple(m.shadow) == NEUTRAL
    passed("enable before current submission closes the stale callback window")

    m = Model(image).powered()
    assert m.active == REQUEST and m.word("model_position_ticks") == m.word("model_bus_ticks") == 2
    passed("fresh stage, boundary, and current callback stage allow enable")

    for elapsed, fake_uif in [(0, False), (0, True), (3999, True)]:
        m = Model(image); m.tick()
        if fake_uif:
            m.put(SR, m.get(SR) | 1)
        m.tick(STAGE | ENABLE, advance=elapsed)
        assert m.word("model_enable_result") == 0 and not m.moe()
        passed(f"duplicate callback or stale UIF cannot prove a commit ({elapsed}/{fake_uif})")

    for register, bit in [(DWT_CTRL, 1), (0xE000EDFC, 1 << 24)]:
        m = Model(image); m.put(register, m.get(register) & ~bit); m.tick()
        assert m.word("model_stage_result") == 0 and not m.moe()
        passed(f"missing cycle-clock prerequisite 0x{register:X} rejects stage")

    for count, down, accepted in [(4250, True, True), (1701, True, True),
                                   (1700, True, False), (1699, True, False),
                                   (1, True, False), (4000, False, False), (0, False, False)]:
        m = Model(image); before = tuple(m.shadow); m.tick(count=count, down=down)
        assert m.word("model_stage_result") == int(accepted), (count, down)
        if not accepted:
            assert tuple(m.shadow) == before and not m.moe()
        passed(f"counter/direction deadline {count}/{down}: {'accept' if accepted else 'reject'}")

    for delay in [2300, 2301, 4000, 8500, 17000]:
        m = Model(image)
        m.on_marker = lambda model, marker, delay=delay: model.advance(delay) if marker == 1 else None
        m.tick()
        assert m.word("model_stage_result") == 0 and not m.moe() and tuple(m.shadow) == NEUTRAL
        passed(f"callback delay {delay} cycles cannot masquerade as a fresh submission")

    m = Model(image, cycle_counter=0xFFFFFF00)
    m.on_marker = lambda model, marker: model.advance(500) if marker == 1 else None
    m.tick(advance=0)
    assert m.word("model_stage_result") == 1
    passed("short bounded callback remains valid across DWT wrap")

    m = Model(image)
    def change_direction(model, marker):
        if marker == 1:
            def read_count(model, address):
                if address == CNT:
                    model.put(CR1, model.get(CR1) & ~DIR)
                    model.on_read = None
            model.on_read = read_count
    m.on_marker = change_direction
    m.tick()
    assert m.word("model_stage_result") == 0 and tuple(m.shadow) == NEUTRAL
    passed("direction change between CR1/CNT reads cannot validate an inconsistent snapshot")

    for phase in range(3):
        for invalid in [-0.1, 0.099, 0.901, 1.1, float("nan"), float("inf"), -float("inf")]:
            m = Model(image)
            request = [0.2, 0.5, 0.8]; request[phase] = invalid
            m.cpu.mem_write(m.symbols["model_request"], struct.pack("<fff", *request))
            m.tick(STAGE | ENABLE)
            assert m.word("model_stage_result") == m.word("model_enable_result") == 0
            assert tuple(m.shadow) == NEUTRAL and not m.moe()
            passed(f"invalid duty phase {phase} value {invalid}")
    m = Model(image); m.tick(STAGE | NULL_REQUEST)
    assert m.word("model_stage_result") == 0 and tuple(m.shadow) == NEUTRAL
    passed("null request does not touch CCRs")

    for duty in [0.1, 0.9]:
        m = Model(image)
        m.cpu.mem_write(m.symbols["model_request"], struct.pack("<fff", duty, duty, duty))
        m.tick()
        assert m.word("model_stage_result") == 1
        passed(f"inclusive supported duty envelope {duty}")

    m = Model(image).powered(); m.tick(0)
    assert not m.moe() and tuple(m.shadow) == NEUTRAL and m.get(CR1) & CEN
    passed("missing running submission turns power off without stopping timing")

    m = Model(image).powered()
    m.on_marker = lambda model, marker: model.advance(4250) if marker == 3 else None
    m.tick()
    assert m.word("model_stage_result") == 1 and not m.moe() and tuple(m.shadow) == NEUTRAL
    passed("consumer overrun after a successful stage also fails safe before callback return")

    for flag in [1 << 7, 1 << 8, BREAKS]:
        m = Model(image); m.tick(); m.put(SR, m.get(SR) | flag); m.tick(STAGE | ENABLE)
        assert m.word("model_enable_result") == 0 and not m.moe() and m.get(SR) & flag == flag
        passed(f"latched hardware break {flag} cannot be cleared by stage or enable")

    m = Model(image).powered(); m.tick(STAGE | OFF | ENABLE | 64)
    assert not m.moe() and m.word("model_enable_result") == m.word("model_second_stage_result") == 0
    assert tuple(m.shadow) == NEUTRAL
    m.tick(STAGE | ENABLE); assert not m.moe() and m.word("model_enable_result") == 0
    m.tick(STAGE); assert not m.moe()
    m.tick(STAGE | ENABLE); assert m.moe() and m.word("model_enable_result") == 1
    passed("safe-off invalidates the current callback and requires a new complete enable sequence")

    m = Model(image); m.tick(); m.call("realtime_motor_io_safe_off")
    m.call("realtime_motor_io_safe_off"); m.tick(STAGE | ENABLE)
    assert m.word("model_enable_result") == 0 and not m.moe()
    passed("repeated safe-off cannot resurrect pending duty at a later boundary")

    for crossing_after in [CCRS[0], CCRS[1], CCRS[2]]:
        m = Model(image).powered(); old = m.active
        m.cpu.mem_write(m.symbols["model_request"], struct.pack("<fff", 0.3, 0.4, 0.7))
        def crossing(model, address, value, target=crossing_after):
            if address == target:
                model.on_ccr = None
                model.pending_delay = 4500
        m.on_ccr = crossing
        transfers = len(m.transfers)
        m.tick(STAGE | ENABLE)
        assert m.word("model_stage_result") == 0 and not m.moe() and tuple(m.shadow) == NEUTRAL
        assert not m.get(CR1) & UDIS
        assert all(event[2] in [old, NEUTRAL] for event in m.transfers[transfers:])
        assert any(event[1] for event in m.transfers[transfers:])
        passed(f"update during CCR{CCRS.index(crossing_after) + 1} write cannot expose a partial group")

    for delay in [2300, 4500]:
        m = Model(image).powered()
        def delay_release(model, address, value, delay=delay):
            if address == CR1 and model.get(CR1) & UDIS and not value & UDIS:
                model.pending_delay = delay
                model.on_write = None
        m.on_write = delay_release
        m.tick(STAGE | ENABLE)
        assert m.word("model_stage_result") == 0 and not m.moe() and tuple(m.shadow) == NEUTRAL
        assert not m.get(CR1) & UDIS
        passed(f"deadline loss immediately after UDIS release ({delay}) returns failure and disables power")

    for point in ["CCR group", "UDIS release", "MOE set"]:
        m = Model(image).powered()
        def break_at_write(model, address, value, point=point):
            selected = (point == "CCR group" and address == CCRS[1])
            selected |= (point == "UDIS release" and address == CR1
                         and model.get(CR1) & UDIS and not value & UDIS)
            selected |= (point == "MOE set" and address == BDTR and value & MOE)
            if selected:
                model.put(SR, model.get(SR) | BREAKS)
                model.put(BDTR, model.get(BDTR) & ~MOE)
                # The hardware break holds MOE low even when software writes it high.
                if address == BDTR:
                    model.pending_fixes[BDTR] = value & ~MOE
                model.on_write = None
        m.on_write = break_at_write
        m.tick(STAGE | ENABLE)
        assert m.word("model_stage_result") == int(point == "MOE set"), point
        assert not m.moe() and m.word("model_enable_result") == 0
        assert m.get(SR) & BREAKS == BREAKS and tuple(m.shadow) == NEUTRAL
        passed("asynchronous hardware break at " + point + " cannot be reported as successful enable")

    for point in ["before stage", "between CCR writes", "before enable"]:
        m = Model(image).powered()
        if point == "before stage":
            def interrupt(model, marker):
                if marker == 1:
                    model.pending_irq = True; model.on_marker = None
            m.on_marker = interrupt
        elif point == "between CCR writes":
            def interrupt(model, address, value):
                if address == CCRS[0]:
                    model.pending_irq = True; model.on_ccr = None
            m.on_ccr = interrupt
        else:
            def interrupt(model, marker):
                if marker == 2:
                    model.pending_irq = True; model.on_marker = None
            m.on_marker = interrupt
        m.tick(STAGE | ENABLE)
        assert m.irq_calls == 1 and not m.moe() and m.word("model_enable_result") == 0
        assert tuple(m.shadow) == NEUTRAL and not m.get(CR1) & UDIS
        m.tick(STAGE | ENABLE); assert not m.moe()
        passed(f"safe-off IRQ {point} blocks stale enable and requires fresh actuation")

    m = Model(image)
    m.cpu.reg_write(UC_ARM_REG_PRIMASK, 1); m.tick()
    assert m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    m.call("realtime_motor_io_safe_off")
    assert m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    passed("nested critical sections preserve the caller's PRIMASK")

    print(f"G431 actual Platform ARM regression: {len(cases)} cases PASS")
    print("Scope: source execution and modeled register ordering; no target build, WCET or electrical proof.")
    for name in cases:
        print("PASS " + name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="g431-platform-model-") as temp:
        run(compile_image(Path(temp)))
