"""Execute G431 validation's actual ARM code with fake HCC/time; no ST-Link access."""
from pathlib import Path
import math
import os
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_PRIMASK
from elftools.elf.elffile import ELFFile

root = Path(__file__).resolve().parents[2]
compiler = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
names = [
    "sequence", "started", "state", "result", "command_sequence", "active_request_sequence",
    "fast_tick_count", "invalid_current_count", "stage_attempts", "stage_successes",
    "enable_attempts", "enable_successes", "capture_samples", "guard_irq_count",
    "capture_sequence", "capture_data", "capture_request", "capture_result", "capture_count",
    "capture_min_cycles", "capture_max_cycles", "capture_first", "capture_last",
    "capture_b", "capture_c", "mailbox_size", "legacy_size",
]
KEY = 0x4310A11E
TIM1 = 0x40012C00
TIM6 = 0x40001000
DWT_CYCCNT = 0xE0001004
COMP = 0x40010200
DAC1 = 0x50000800
DAC3 = 0x50001000
BREAKS = (1 << 7) | (1 << 8)
probe_names = ["sequence", "request", "source", "outcome", "held", "original_code",
               "expected_comp", "expected_flag", "bdtr_before", "sr_before", "comp_before",
               "bdtr_after", "sr_after", "comp_after", "start", "elapsed", "poll_count"]
cases = []

def passed(name):
    cases.append(name)

with tempfile.TemporaryDirectory(prefix="g431-validation-model-") as directory:
    temp = Path(directory)
    scatter = temp / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00020000 {\n"
        " ER_IROM1 0x08000000 0x00020000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    settings = ET.parse(root / "MDK-ARM/BLDC_control V1.uvprojx").find(".//Cads/VariousControls")
    command = [str(compiler / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
               "--split_sections", "-g"]
    for include in settings.findtext("IncludePath").split(";"):
        command += ["-I", str((root / "MDK-ARM" / include.replace("\\", "/")).resolve())]
    command += ["-D" + item for item in settings.findtext("Define").split(",")]
    objects = []
    for source in [root / "validation/tests/model.c", root / "validation/target_validation.c",
                   root / "validation/protection_probe.c"]:
        obj = temp / (source.stem + ".o")
        result = subprocess.run(command + ["-c", str(source), "-o", str(obj)],
                                capture_output=True, text=True)
        assert result.returncode == 0 and "warning" not in result.stderr.lower(), result.stderr
        objects.append(str(obj))
    result = subprocess.run([str(compiler / "armlink.exe"), "--cpu=Cortex-M4.fp",
                             "--scatter", str(scatter), "--entry=model_init", "--keep=model.o(*)",
                             "-o", str(temp / "model.axf"), *objects],
                            cwd=temp, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with (temp / "model.axf").open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(s["p_vaddr"], s.data()) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        initializers = [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                        if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self, failure=None, pending=None):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for base, size in [(0x08000000, 0x20000), (0x20000000, 0x8000),
                               (0x40012000, 0x1000), (TIM6, 0x1000), (0x40010000, 0x1000),
                               (0x50000000, 0x2000),
                               (0xE0001000, 0x1000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(base, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.memory_write(0xE000ED88, 0x00F00000)
            data = self.cpu.mem_read(symbols["model_offsets"], len(names) * 4)
            self.offsets = dict(zip(names, struct.unpack("<" + "I" * len(names), data)))
            self.sequence = 0
            self.inject_fault = None
            self.inject_events = []
            self.probe_clock_step = 170
            self.in_poll = False
            self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.dac_write, begin=DAC1, end=DAC3 + 0x60)
            self.cpu.hook_add(UC_HOOK_MEM_READ, self.clock_read, begin=DWT_CYCCNT, end=DWT_CYCCNT + 3)
            if failure:
                self.write(failure, 0)
            if pending:
                self.submit(pending, key=KEY)
            self.call("model_init")

        def dac_write(self, cpu, access, address, size, value, user_data):
            mapping = {DAC1 + 8: (4, 1 << 7), DAC3 + 8: (1, 1 << 8), DAC3 + 20: (10, 1 << 8)}
            if address not in mapping:
                return
            mask, flag = mapping[address]
            if value == 0:
                self.inject_events.append((address, self.moe(), cpu.reg_read(UC_ARM_REG_PRIMASK),
                                           self.word("model_off_calls")))
                self.in_poll = True
                if self.inject_fault != "no_comp":
                    active_mask = 2 if self.inject_fault == "wrong_comp" else mask
                    for index in range(4):
                        if active_mask & (1 << index):
                            self.memory_write(COMP + 4 * index,
                                              self.memory_word(COMP + 4 * index) | (1 << 30))
                if self.inject_fault != "no_flag":
                    self.memory_write(TIM1 + 0x10, self.memory_word(TIM1 + 0x10) | flag)
                if self.inject_fault != "no_moe_clear":
                    self.memory_write(TIM1 + 0x44, self.memory_word(TIM1 + 0x44) & ~(1 << 15))
            else:
                self.in_poll = False
                for index in range(4):
                    if mask & (1 << index):
                        self.memory_write(COMP + 4 * index,
                                          self.memory_word(COMP + 4 * index) & ~(1 << 30))

        def clock_read(self, cpu, access, address, size, value, user_data):
            if self.in_poll:
                self.memory_write(DWT_CYCCNT, self.memory_word(DWT_CYCCNT) + self.probe_clock_step)

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            self.cpu.emu_start(symbols[name] | 1, 0x0801F000, count=1000000)
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000, name

        def memory_write(self, address, value):
            self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

        def memory_word(self, address):
            return struct.unpack("<I", self.cpu.mem_read(address, 4))[0]

        def write(self, name, value, fmt="I"):
            self.cpu.mem_write(symbols[name], struct.pack("<" + fmt, value))

        def word(self, name):
            return self.memory_word(symbols[name])

        def status(self, name):
            return self.memory_word(symbols["g_target_validation"] + self.offsets[name])

        def submit(self, command, key=0, duration=2000, duty=0.5):
            self.sequence += 1
            self.cpu.mem_write(symbols["g_target_validation_request"],
                               struct.pack("<IIIIf", self.sequence, command, key, duration, duty))

        def tick(self, count=1):
            for _ in range(count):
                self.call("model_tick")

        def advance_guard(self, us):
            self.memory_write(DWT_CYCCNT, self.memory_word(DWT_CYCCNT) + us * 170)
            self.call("model_guard")

        def moe(self):
            return bool(self.memory_word(TIM1 + 0x44) & (1 << 15))

        def capture(self):
            base = symbols["g_target_validation_capture"] + self.offsets["capture_data"]
            result = {}
            for name, fmt in [("request", "I"), ("result", "I"), ("count", "I"),
                              ("min_cycles", "I"), ("max_cycles", "I"),
                              ("first", "Q"), ("last", "Q"), ("b", "ffff"), ("c", "ffff")]:
                values = struct.unpack("<" + fmt, self.cpu.mem_read(
                    base + self.offsets["capture_" + name], struct.calcsize("<" + fmt)))
                result[name] = values[0] if len(values) == 1 else values
            return result

        def probe(self):
            assert self.word("model_probe_size") == len(probe_names) * 4
            data = self.cpu.mem_read(symbols["g_target_validation_probe"], len(probe_names) * 4)
            return dict(zip(probe_names, struct.unpack("<" + "I" * len(probe_names), data)))

    def powered(duration=2000, duty=0.5):
        m = Model()
        m.tick(200)
        m.submit(3, key=KEY, duration=duration, duty=duty)
        m.tick()
        assert not m.moe() and m.word("model_stage_calls") == 1
        m.tick()
        assert m.moe() and m.word("model_enable_calls") == 1
        return m

    for pending in [3, 4]:
        m = Model(pending=pending); m.tick(200)
        assert m.status("started") == 1 and m.status("state") == 0 and not m.moe()
        assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0
        assert m.status("sequence") % 2 == 0
        assert m.offsets["legacy_size"] == 76 and m.offsets["mailbox_size"] == 20
        passed("startup discards previous request " + str(pending))
    for failure in ["model_clock_ok", "model_facts_ok", "model_bind_ok", "model_arm_ok",
                    "model_trip_arm_ok", "model_start_ok", "model_trip_valid"]:
        m = Model(failure=failure)
        assert m.status("started") == 0 and not m.moe() and m.word("model_enable_calls") == 0
        passed("startup failure preserves disabled power: " + failure)

    m = Model(); m.submit(2); m.tick(2)
    assert m.word("model_stage_calls") == 2 and m.word("model_enable_calls") == 0
    assert m.status("result") == 1 and m.status("state") == 0 and not m.moe()
    m.tick(100)
    assert m.word("model_stage_calls") == 2
    passed("stage-only crosses fresh boundaries, never enables and does not replay")
    for duty in [-1.0, 0.099, 0.901, 1.0, float("nan"), float("inf")]:
        m = Model(); m.submit(3, key=KEY, duty=duty); m.tick(3)
        assert m.status("result") == 4 and m.word("model_enable_calls") == 0 and not m.moe()
        passed("invalid duty " + str(duty))
    for duration in [0, 999, 10001, 0xFFFFFFFF]:
        m = Model(); m.submit(3, key=KEY, duration=duration); m.tick(3)
        assert m.status("result") == 4 and not m.moe()
        passed("invalid duration " + str(duration))
    for key in [0, 1, KEY + 1]:
        m = Model(); m.submit(3, key=key); m.tick(3)
        assert m.status("result") == 3 and not m.moe()
        passed("absent/wrong power key")
    for voltage in [0.0, 9.99, 14.01, float("nan"), float("inf")]:
        m = Model(); m.write("model_bus_voltage", voltage, "f"); m.submit(3, key=KEY); m.tick()
        assert m.status("result") == 7 and not m.moe()
        passed("invalid burst bus " + str(voltage))
    for age, future, accepted in [(100, 0, True), (101, 0, False), (0, 1, False)]:
        m = Model(); m.tick(200)
        m.write("model_bus_age_ticks", age); m.write("model_bus_future_ticks", future)
        m.submit(3, key=KEY); m.tick(2)
        assert m.moe() == accepted
        passed("G431 bus freshness/future time boundary")
    for duty in [0.1, 0.5, 0.9]:
        m = powered(duty=duty); m.tick(60)
        assert not m.moe() and m.status("result") == 1 and m.word("model_enable_calls") == 1
        assert m.word("model_common_duty_failures") == 0
        passed("bounded single burst, common duty " + str(duty) + "; no automatic replay")
    m = Model(); m.submit(3, key=KEY, duty=0.1); m.tick(2); assert m.moe()
    m = Model(); m.submit(3, key=KEY, duty=0.9); m.tick(2); assert m.moe()
    passed("accept duty envelope edges")

    for duration in [1000, 10000]:
        m = powered(duration=duration); m.tick(duration // 50 + 2)
        assert m.status("result") == 1 and not m.moe() and m.word("model_enable_calls") == 1
        passed("accepted duration boundary " + str(duration))
    m = powered(); m.tick(38)
    control_time = m.word("model_time")
    m.advance_guard(50)
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 1
    assert m.word("model_time") == control_time
    m.tick(10)
    assert m.status("result") == 1 and m.word("model_enable_calls") == 1
    passed("TIM6 enforces total burst deadline without an ADC callback at that boundary")

    m = powered(); control_time = m.word("model_time")
    m.advance_guard(100)
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 6
    assert m.word("model_time") == control_time and m.memory_word(TIM1) & 1
    assert not (m.memory_word(TIM6) & 1)
    m.tick(100)
    assert m.status("state") == 0 and m.status("result") == 6 and m.word("model_enable_calls") == 1
    passed("independent TIM6/DWT guard stops with ADC and ControlTime stalled")
    m = powered(); m.advance_guard(0); m.advance_guard(0)
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 12
    passed("stalled DWT clock fails closed")
    m = powered(); m.call("model_trip_event")
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 11
    m.write("model_trip_active", 0); m.tick(3)
    assert m.status("result") == 11 and m.word("model_enable_calls") == 1
    passed("urgent Trip stops even before next ADC; sticky episode does not restart")
    for fault, reason in [("model_current_valid", 6), ("model_bus_valid", 7),
                          ("model_trip_valid", 11), ("model_stage_ok", 8)]:
        m = powered(); m.write(fault, 0); m.tick()
        assert not m.moe() and m.status("result") == reason
        m.write(fault, 1); m.tick(60)
        assert m.word("model_enable_calls") == 1 and not m.moe()
        passed("fault stops and recovery never repeats enable: " + fault)
    m = Model(); m.write("model_enable_ok", 0); m.submit(3, key=KEY); m.tick(3)
    assert m.status("result") == 9 and not m.moe()
    passed("HCC enable rejection fails closed")
    m = powered(); m.memory_write(TIM1 + 0x44, 0); m.tick()
    assert m.status("result") == 10 and m.word("model_enable_calls") == 1
    passed("external Platform off cannot be undone within the same action")
    m = Model(); m.submit(2); m.tick(); m.memory_write(TIM1 + 0x44, 1 << 15); m.tick()
    assert m.status("result") == 10 and not m.moe()
    passed("unexpected MOE during staging is shut down")

    m = powered(); m.submit(1); m.tick()
    assert m.status("result") == 2 and not m.moe()
    m.submit(3); m.tick(3)
    assert m.status("result") == 3 and m.word("model_enable_calls") == 1
    passed("operator stop and one-use power key")
    m = powered(); active = m.status("active_request_sequence")
    m.submit(4); m.tick()
    assert m.status("result") == 5 and m.status("active_request_sequence") == active
    assert m.moe() and m.capture()["count"] == 0
    passed("busy capture cannot replace an active PWM transaction")

    m = Model(); m.write("model_current_b_a", -1.5, "f"); m.write("model_current_c_a", 0.75, "f")
    m.submit(4, duration=0, duty=float("nan")); m.tick(4096)
    data = m.capture()
    assert data["result"] == 1 and data["count"] == 4096 and data["last"] - data["first"] == 4095
    assert data["min_cycles"] == data["max_cycles"] == 8500
    assert data["b"] == (-1.5, -1.5, -1.5, 0.0) and data["c"] == (0.75, 0.75, 0.75, 0.0)
    assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0 and not m.moe()
    m.tick(100)
    assert m.capture() == data
    assert m.memory_word(symbols["g_target_validation_capture"]) % 2 == 0
    passed("4096 contiguous samples, G431 time domain, frozen statistics and no staging/power")
    m = Model(); m.submit(4)
    for index in range(4096):
        sign = 1 if index % 2 else -1
        m.write("model_current_b_a", -1.5 + sign * 0.25, "f")
        m.write("model_current_c_a", 0.75 + sign * 0.5, "f")
        m.tick()
    data = m.capture()
    assert abs(data["b"][0] + 1.5) < 1e-5 and abs(data["c"][0] - 0.75) < 1e-5
    assert data["b"][1:3] == (-1.75, -1.25) and data["c"][1:3] == (0.25, 1.25)
    assert math.isclose(data["b"][3], 0.25**2 * 4096 / 4095, abs_tol=1e-5)
    assert math.isclose(data["c"][3], 0.5**2 * 4096 / 4095, abs_tol=1e-5)
    passed("Welford mean/range/variance match independent known signals")
    for name, value, fmt in [("model_current_valid", 0, "I"), ("model_current_mask", 4, "I"),
                             ("model_current_b_a", float("nan"), "f"),
                             ("model_current_c_a", float("inf"), "f"),
                             ("model_step_ticks", 2, "I"), ("model_step_us", 100, "I"),
                             ("model_step_us", 20, "I")]:
        m = Model(); m.submit(4); m.tick(10); m.write(name, value, fmt); m.tick()
        assert m.capture()["result"] == 6 and m.capture()["count"] == 10 and not m.moe()
        passed("incomplete window fails: " + name + "=" + str(value))
    m = Model(); m.submit(4); m.tick(10); request = m.sequence
    for command in [2, 3, 4]:
        m.submit(command, key=KEY); m.tick()
        assert m.status("result") == 5 and m.status("active_request_sequence") == request
    assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0
    m.submit(1); m.tick()
    assert m.capture()["result"] == 2 and m.capture()["count"] == 13
    m.submit(4); m.tick(4096)
    assert m.capture()["result"] == 1 and m.capture()["request"] == m.sequence
    passed("busy requests do not replace capture; cancellation and a new window are isolated")
    m = Model(); m.submit(4); m.tick(10); m.advance_guard(100)
    assert m.word("g_target_validation_stop_reason") == 6 and not m.moe()
    m.tick()
    assert m.capture()["result"] == 6 and m.capture()["count"] == 10
    passed("ADC absent capture leaves independent failure reason and publishes partial window on recovery")
    m = Model(); m.memory_write(DWT_CYCCNT, 0xFFFFFF00); m.submit(4); m.tick(4096)
    assert m.capture()["result"] == 1 and m.capture()["count"] == 4096
    passed("DWT wrap does not break capture deadline or intervals")
    m = Model(); m.submit(4); m.tick(); m.write("model_current_b_a", 1e30, "f"); m.tick()
    m.write("model_current_b_a", -1e30, "f"); m.tick()
    assert m.capture()["result"] == 6
    passed("finite input causing accumulator overflow is not reported COMPLETE")

    # DAC/COMP/TIM effects below are injected MMIO behavior, not an analog model.
    # Actual ARM probe code must see hardware evidence before ANY software off.
    def probe_powered(command=5):
        m = Model()
        m.tick(20)
        m.submit(command, key=KEY)
        m.tick(2)
        assert m.moe() and m.probe()["outcome"] == 1 and not m.inject_events
        return m

    for command in [5, 6, 7, 8, 9]:
        m = Model(pending=command); m.tick(20)
        assert not m.inject_events and m.word("model_enable_calls") == m.word("model_rearm_calls") == 0
        passed("startup discards protection request " + str(command))
    for command in [5, 6, 7]:
        for key, duration, duty, reason in [(0, 2000, 0.5, 3), (KEY, 1000, 0.5, 4),
                                           (KEY, 2000, 0.3, 4)]:
            m = Model(); m.submit(command, key=key, duration=duration, duty=duty); m.tick(10)
            assert m.status("result") == reason and not m.inject_events and not m.moe()
            assert m.word("model_enable_calls") == 0
            passed("protection request requires key and fixed payload " + str((command, reason, duration)))

    for command, comp, flag, address, original, cause in [(5, 4, 128, DAC1 + 8, 2065, 4),
                                                        (6, 1, 256, DAC3 + 8, 345, 2),
                                                        (7, 10, 256, DAC3 + 20, 345, 2)]:
        m = probe_powered(command)
        m.tick(3); assert m.moe() and not m.inject_events
        off_before = m.word("model_off_calls")
        m.tick()
        data = m.probe()
        assert len(m.inject_events) == 1 and m.inject_events[0] == (address, True, 1, off_before)
        assert data["outcome"] == 2 and data["sequence"] % 2 == 0 and data["held"] == 1
        assert data["source"] == command - 4 and data["request"] == m.sequence
        assert data["bdtr_before"] & (1 << 15) and not data["bdtr_after"] & (1 << 15)
        assert data["comp_before"] == 0 and data["comp_after"] == comp
        assert data["sr_before"] & BREAKS == 0 and data["sr_after"] & BREAKS == flag
        assert data["original_code"] == original and m.memory_word(address) == 0
        assert not m.moe() and m.status("result") == 11 and m.status("state") == 0
        assert m.word("model_enable_calls") == 1 and not m.memory_word(TIM6) & 1
        m.tick(30); assert m.probe() == data and len(m.inject_events) == 1
        m.submit(9); m.tick()
        assert m.status("result") == 15 and m.word("model_rearm_calls") == 1
        assert m.memory_word(address) == 0 and m.probe()["held"] == 1 and not m.moe()
        m.submit(3, key=KEY); m.tick(2)
        assert m.status("result") == 11 and m.word("model_enable_calls") == 1
        m.submit(8); m.tick()
        assert m.status("result") == 1 and m.memory_word(address) == original and m.probe()["held"] == 0
        assert m.word("model_trip_causes") == cause and m.word("model_rearm_calls") == 1
        m.submit(3, key=KEY); m.tick(2)
        assert m.status("result") == 11 and m.word("model_enable_calls") == 1
        m.submit(9); m.tick()
        assert m.status("result") == 1 and m.word("model_trip_causes") == 0 and not m.moe()
        m.tick(40); assert m.word("model_enable_calls") == 1
        m.submit(3); m.tick(); assert m.status("result") == 3
        m.submit(3, key=KEY); m.tick(60)
        assert m.status("result") == 1 and m.word("model_enable_calls") == 2
        assert len(m.inject_events) == 1
        passed("observed hardware Break / held fault / separate clear and rearm / no restart, source " + str(command))

    for fault in ["no_moe_clear", "no_flag", "no_comp", "wrong_comp"]:
        m = probe_powered(); m.inject_fault = fault; m.tick(4)
        assert m.probe()["outcome"] == 4 and m.status("result") == 14 and not m.moe()
        assert m.probe()["held"] == 1 and m.probe()["elapsed"] >= 1700
        assert m.word("model_enable_calls") == 1
        m.tick(30); assert len(m.inject_events) == 1
        if fault == "no_moe_clear":
            assert m.probe()["bdtr_after"] & (1 << 15) # Not laundered by subsequent safe-off.
        passed("missing hardware evidence fails closed: " + fault)
    m = probe_powered(); m.inject_fault = "no_moe_clear"; m.probe_clock_step = 0; m.tick(4)
    assert m.status("result") == 14 and m.probe()["poll_count"] == 128 and not m.moe()
    passed("stalled DWT during failed Break has independent finite poll bound")
    m = probe_powered(7); m.inject_fault = "wrong_comp"; m.tick(4)
    assert m.status("result") == 14 and m.probe()["comp_after"] == 2 and not m.moe()
    passed("shared A/B threshold requires both comparator outputs, not just B")
    m = Model(); m.memory_write(DWT_CYCCNT, 0x100000000 - 6 * 8500 - 50)
    m.submit(5, key=KEY); m.tick(6)
    assert m.probe()["outcome"] == 2 and not m.moe()
    passed("injection observation works across DWT wrap")

    for address, bit in [(TIM1 + 0x44, 1 << 12), (TIM1 + 0x44, 1 << 24),
                         (TIM1 + 0x44, 1 << 14), (TIM1 + 0x60, 1 << 3),
                         (TIM1 + 0x64, 1 << 1), (COMP + 8, 1), (DAC1, 1), (DAC1 + 0x3C, 1)]:
        m = Model(); m.memory_write(address, m.memory_word(address) ^ bit)
        m.submit(5, key=KEY); m.tick(10)
        assert m.status("result") == 13 and not m.inject_events and m.word("model_enable_calls") == 0
        passed("bad protection configuration rejected before enable: " + hex(address) + "/" + hex(bit))
    m = probe_powered(); m.memory_write(DAC1 + 8, 2000); m.tick(4)
    assert m.status("result") == 13 and not m.inject_events and not m.moe()
    passed("threshold changed after prepare is rejected at injection boundary")
    for command in [1, 8, 9]:
        m = probe_powered(); m.submit(command); m.tick()
        if command == 1:
            assert m.status("result") == 2 and not m.moe() and not m.inject_events
        else:
            assert m.status("result") == 5 and m.moe() and not m.inject_events
            m.tick(3); assert m.probe()["outcome"] == 2
        passed("active probe control handling " + str(command))
    for fault in ["model_current_valid", "model_bus_valid", "model_trip_valid", "model_stage_ok"]:
        m = probe_powered(); m.write(fault, 0); m.tick(6)
        assert not m.inject_events and not m.moe() and m.word("model_enable_calls") == 1
        passed("loss of prerequisite before injection cancels probe: " + fault)
    m = probe_powered(); m.advance_guard(100); m.tick(5)
    assert not m.inject_events and not m.moe() and m.status("result") == 6
    passed("ADC loss guard cancels probe before DAC injection")
    m = probe_powered(); m.tick(4); m.memory_write(DAC1 + 8, 1800)
    m.submit(8); m.tick()
    assert m.status("result") == 13 and m.probe()["held"] == 1 and m.memory_word(DAC1 + 8) == 1800
    assert not m.moe()
    passed("clear does not overwrite externally changed threshold")
    m = probe_powered(); m.tick(4); m.submit(8); m.tick()
    m.write("model_rearm_ok", 0); m.submit(9); m.tick()
    assert m.status("result") == 15 and m.word("model_trip_causes") == 4 and not m.moe()
    passed("HCC rearm rejection preserves sticky fault and disabled output")

print(f"PASS: {len(cases)} ARM-executed G431 validation cases.")
print("Fake HCC/time only; does not prove electrical behavior, interrupt latency or hardware L1/L2.")
