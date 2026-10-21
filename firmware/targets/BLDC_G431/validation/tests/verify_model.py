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
    "interval_min_cycles", "interval_max_cycles",
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
                   root / "validation/protection_probe.c", root / "validation/timing_probe.c",
                   root / "validation/current_hold.c",
                   root.parents[1] / "app/motor/voltage_open_loop.c",
                   root.parents[2] / "control/current_controller.c",
                   root.parents[2] / "control/electrical_angle.c",
                   root.parents[2] / "control/position_estimator.c",
                   root.parents[2] / "control/speed_controller.c",
                   root.parents[2] / "control/foc_math.c"]:
        obj = temp / (source.stem + ".o")
        options = ["-Otime"] if source.parent.name == "control" else []
        result = subprocess.run(command + options + ["-c", str(source), "-o", str(obj)],
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

        def timing(self):
            fields = ["sequence", "request", "case", "outcome", "iterations", "elapsed",
                      "stage_called", "stage_accepted", "second_called", "second_accepted"]
            data = self.cpu.mem_read(symbols["g_target_validation_timing"], 40)
            return dict(zip(fields, struct.unpack("<10I", data)))

        def open_loop(self):
            fields = ["sequence", "request", "active", "result", "module_state", "module_reason",
                      "samples", "enabled_samples", "voltage", "frequency", "offset_b", "offset_c",
                      "peak_current", "start_position", "end_position", "displacement", "min_duty", "max_duty"]
            data = self.cpu.mem_read(symbols["g_target_validation_open_loop"], 72)
            return dict(zip(fields, struct.unpack("<8I10f", data)))

        def open_loop_submit(self, voltage=0.3, frequency=2.0, key=KEY, duration=3500000, duty=0.5):
            self.cpu.mem_write(symbols["g_target_validation_open_loop_config"],
                               struct.pack("<ff", voltage, frequency))
            self.submit(15, key=key, duration=duration, duty=duty)

        def survey_submit(self, voltage=0.3, excursion=0.25, key=KEY, duration=5500000, duty=0.5):
            self.cpu.mem_write(symbols["g_target_validation_angle_survey_config"],
                               struct.pack("<ff", voltage, excursion))
            self.submit(16, key=key, duration=duration, duty=duty)

        def current_survey_submit(self, current=0.06, excursion=1.0, key=KEY, duration=5500000, duty=0.5):
            self.cpu.mem_write(symbols["g_target_validation_current_survey_config"], struct.pack("<2f",current,excursion))
            self.submit(18, key=key, duration=duration, duty=duty)

        def hold_submit(self, alpha=0.06, beta=0.0, key=KEY, duration=3500000, duty=0.5):
            self.cpu.mem_write(symbols["g_target_validation_current_hold_config"], struct.pack("<2f", alpha, beta))
            self.submit(17, key=key, duration=duration, duty=duty)

        def rotor_submit(self, iq=0.1, offset=0.125, key=KEY, duration=1500000, duty=0.5):
            self.cpu.mem_write(symbols["g_target_validation_rotor_current_config"], struct.pack("<2f",iq,offset))
            self.submit(19,key=key,duration=duration,duty=duty)

        def rotor(self):
            keys=["sequence","request","active","result","samples","enabled","window_samples","limited_samples",
                "iq_ref","offset","mean_id","mean_iq","peak_error","max_voltage","peak_current","min_duty","max_duty",
                "start_position","end_position","displacement","max_rpm","last_rpm","mean_rpm","max_age"]
            return dict(zip(keys,struct.unpack("<8I16f",self.cpu.mem_read(symbols["g_target_validation_rotor_current"],96))))

        def speed_submit(self, rpm=60.0, offset=0.125, key=KEY, duration=2000000, duty=0.5):
            self.cpu.mem_write(symbols["g_target_validation_rotor_speed_config"],struct.pack("<2f",rpm,offset))
            self.submit(20,key=key,duration=duration,duty=duty)

        def speed(self):
            keys=['sequence','request','active','result','samples','enabled','window_samples','voltage_limited',
                'target_rpm','offset','mean_id','mean_iq','peak_current_error','max_voltage','peak_current','min_duty','max_duty',
                'start_position','end_position','displacement','max_rpm','last_rpm','mean_rpm','max_age',
                'mean_iq_ref','mean_speed_error','peak_speed_error','last_iq_ref',
                'speed_updates','speed_limited','speed_window_updates','integral_limited']
            return dict(zip(keys,struct.unpack('<8I20f4I',self.cpu.mem_read(symbols['g_target_validation_rotor_speed'],128))))

        def hold(self):
            keys = ["sequence", "request", "active", "result", "samples", "enabled_samples", "window_samples",
                "limited_samples", "alpha_ref", "beta_ref", "offset_b", "offset_c", "mean_alpha", "mean_beta",
                "peak_current", "max_voltage", "min_duty", "max_duty", "mean_valpha", "mean_vbeta", "peak_error"]
            return dict(zip(keys, struct.unpack("<8I13f", self.cpu.mem_read(symbols["g_target_validation_current_hold"], 84))))

        def bus_failure(self):
            raw=self.cpu.mem_read(symbols['g_target_validation_bus_failure'],96)
            return struct.unpack('<4I2Qf3IQ10I',raw)

        def survey(self, current=False):
            raw = self.cpu.mem_read(symbols["g_target_validation_current_survey" if current else "g_target_validation_angle_survey"], 168)
            header = dict(zip(["sequence", "request", "result", "stable_mask", "excursion", "voltage"],
                              struct.unpack_from("<4I2f", raw)))
            keys = ["count", "first_time", "last_time", "mean", "span", "first", "last", "mean_b", "mean_c"]
            header["windows"] = [dict(zip(keys, struct.unpack_from("<I4xQQ6f", raw, 24 + 48*i))) for i in range(3)]
            return header

    def powered(duration=2000, duty=0.5):
        m = Model()
        m.tick(200)
        m.submit(3, key=KEY, duration=duration, duty=duty)
        m.tick()
        assert not m.moe() and m.word("model_stage_calls") == 0
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

    m = Model(); m.submit(2); m.tick(3)
    assert m.word("model_stage_calls") == 2 and m.word("model_enable_calls") == 0
    assert m.status("result") == 1 and m.status("state") == 0 and not m.moe()
    m.tick(100)
    assert m.word("model_stage_calls") == 2
    passed("stage-only crosses fresh boundaries, never enables and does not replay")
    for command in [2, 3, 5, 6, 7]:
        m = Model(); m.tick(20); m.submit(command, key=KEY); m.tick()
        assert m.status("state") == 1 and m.status("active_request_sequence") == m.sequence
        assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0 and not m.moe()
        m.submit(1); m.tick(20)
        assert m.status("result") == 2 and m.status("state") == 0
        assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0 and not m.moe()
        passed("new actuation queues without stage; stop cancels before execution: " + str(command))

    for stage_ticks in [0, 1]:
        for fault, value, fmt, reason in [("model_current_valid", 0, "I", 6),
                                          ("model_trip_valid", 0, "I", 11),
                                          ("model_trip_active", 1, "I", 11),
                                          ("model_bus_valid", 0, "I", 7),
                                          ("model_bus_voltage", 9.0, "f", 7)]:
            m = Model(); m.tick(20); m.submit(3, key=KEY); m.tick(1 + stage_ticks)
            assert m.word("model_stage_calls") == stage_ticks and not m.moe()
            m.write(fault, value, fmt); m.tick()
            assert m.status("result") == reason and m.status("state") == 0
            assert m.word("model_stage_calls") == stage_ticks and m.word("model_enable_calls") == 0
            assert not m.moe()
            passed("queued actuation rechecks fresh prerequisites before stage/enable: "
                   + str((stage_ticks, fault)))
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
        m.submit(3, key=KEY); m.tick(3)
        assert m.moe() == accepted
        passed("G431 bus freshness/future time boundary")
    for duty in [0.1, 0.3, 0.5, 0.7, 0.9]:
        m = powered(duty=duty); m.tick(60)
        assert not m.moe() and m.status("result") == 1 and m.word("model_enable_calls") == 1
        assert m.word("model_common_duty_failures") == 0
        assert bytes(m.cpu.mem_read(symbols["model_last_duty"], 12)) == struct.pack("<fff", duty, duty, duty)
        passed("bounded single burst, common duty " + str(duty) + "; no automatic replay")
    m = Model(); m.submit(3, key=KEY, duty=0.1); m.tick(3); assert m.moe()
    m = Model(); m.submit(3, key=KEY, duty=0.9); m.tick(3); assert m.moe()
    passed("accept duty envelope edges")

    for duration in [1000, 10000]:
        m = powered(duration=duration); m.tick(duration // 100 + 2)
        assert m.status("result") == 1 and not m.moe() and m.word("model_enable_calls") == 1
        passed("accepted duration boundary " + str(duration))
    m = powered(); m.tick(17)
    control_time = m.word("model_time")
    m.advance_guard(100)
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 1
    assert m.word("model_time") == control_time
    m.tick(10)
    assert m.status("result") == 1 and m.word("model_enable_calls") == 1
    passed("TIM6 enforces total burst deadline without an ADC callback at that boundary")

    m = powered(); control_time = m.word("model_time")
    m.advance_guard(150)
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
    stage_before = m.word("model_stage_calls")
    m.submit(4); m.tick()
    assert m.status("result") == 5 and m.status("active_request_sequence") == active
    assert m.moe() and m.capture()["count"] == 0
    assert m.word("model_stage_calls") == stage_before + 1
    passed("busy capture cannot replace an active PWM transaction")

    m = Model(); m.write("model_current_b_a", -1.5, "f"); m.write("model_current_c_a", 0.75, "f")
    m.submit(4, duration=0, duty=float("nan")); m.tick(4096)
    data = m.capture()
    assert data["result"] == 1 and data["count"] == 4096 and data["last"] - data["first"] == 8190
    assert data["min_cycles"] == data["max_cycles"] == 17000
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
                             ("model_step_ticks", 4, "I"), ("model_step_us", 150, "I"),
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
    m = Model(); m.submit(4); m.tick(10); m.advance_guard(150)
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
        m.tick(3)
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
    m = Model(); m.memory_write(DWT_CYCCNT, 0x100000000 - 7 * 17000 - 50)
    m.submit(5, key=KEY); m.tick(7)
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
    m = probe_powered(); m.advance_guard(150); m.tick(5)
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

    # 新 slot 配置明确拒绝旧的半周期诊断，不能把固定 1700 count 作为新 deadline。
    for command in range(10, 15):
        m = Model(pending=command); m.tick(5)
        assert m.timing()["sequence"] == 0 and m.status("state") == 0
        assert not m.moe() and m.word("model_stage_calls") == m.word("model_enable_calls") == 0
        passed("startup discards legacy timing command " + str(command))
        m = Model(); m.write("model_bus_voltage", 1.0, "f"); m.tick(3)
        m.submit(command, key=0, duration=0, duty=0.0); m.tick(5)
        assert m.status("result") == 16 and m.status("state") == 0
        assert m.timing()["sequence"] == 0
        assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0 and not m.moe()
        passed("100 us sample slot refuses legacy half-cycle timing command " + str(command))
    m = Model(); m.submit(4); m.tick(); active = m.status("active_request_sequence")
    for command in range(10, 15):
        m.submit(command, key=0, duration=0, duty=0.0); m.tick()
        assert m.status("result") == 5 and m.status("state") == 3
        assert m.status("active_request_sequence") == active and m.timing()["sequence"] == 0
    assert not m.moe() and m.word("model_stage_calls") == m.word("model_enable_calls") == 0
    passed("legacy timing command cannot displace active sample capture")
    m = Model(); m.tick(3)
    assert m.word("g_target_validation_timing_recovery_ticks") == 2
    m.write("model_step_us", 150); m.tick()
    assert m.word("g_target_validation_timing_recovery_ticks") == 0
    m.write("model_step_us", 100); m.tick(2)
    assert m.word("g_target_validation_timing_recovery_ticks") == 2
    assert m.status("interval_max_cycles") == 25500
    passed("100 us cadence recovery retains out-of-window interval evidence")

    # 开环运行真实纯电压模块和台架执行器；HCC、SSI、电流与时间仍为明确替身。
    # 完整 4096 点统计由正式命令 4 产生，不向内部零偏或状态直接写入成功值。
    def zero_ready(b=-0.012, c=-0.001):
        m = Model()
        m.write("model_current_b_a", b, "f"); m.write("model_current_c_a", c, "f")
        m.submit(4); m.tick(4096)
        assert m.capture()["result"] == 1 and m.capture()["count"] == 4096
        assert not m.moe() and m.word("model_enable_calls") == 0
        return m

    def open_powered(**payload):
        m = zero_ready()
        m.open_loop_submit(**payload); m.tick()
        assert m.open_loop()["active"] == 1 and m.open_loop()["samples"] == 0
        assert m.status("state") == 1 and not m.moe() and m.word("model_stage_calls") == 0
        m.tick()
        assert m.word("model_stage_calls") == 1 and not m.moe()
        assert bytes(m.cpu.mem_read(symbols["model_last_duty"], 12)) == struct.pack("<3f", 0.5, 0.5, 0.5)
        m.tick()
        assert m.moe() and m.word("model_enable_calls") == 1
        assert bytes(m.cpu.mem_read(symbols["model_last_duty"], 12)) == struct.pack("<3f", 0.5, 0.5, 0.5)
        assert m.open_loop()["module_state"] == 1
        return m

    m = Model(pending=15); m.tick(20)
    assert m.status("state") == 0 and not m.moe() and m.open_loop()["request"] == 0
    assert struct.unpack("<3I", m.cpu.mem_read(symbols["model_open_loop_sizes"], 12)) == (8, 72, 184)
    assert m.offsets["mailbox_size"] == 20 and m.offsets["legacy_size"] == 76
    passed("open-loop extension preserves old mailbox/snapshot ABI and discards reset-time requests")
    m = Model(); m.open_loop_submit(); m.tick(3)
    assert m.status("result") == 19 and not m.moe() and m.word("model_enable_calls") == 0
    passed("open loop requires a real completed zero-current capture in this startup")
    m = Model(); m.submit(4); m.tick(10); m.submit(1); m.tick(); m.open_loop_submit(); m.tick()
    assert m.status("result") == 19 and not m.moe()
    passed("partial or cancelled zero window cannot authorize open loop")
    for b, c in [(0.101, 0.0), (-0.101, 0.0), (0.0, 0.101)]:
        m = zero_ready(b, c); m.open_loop_submit(); m.tick()
        assert m.status("result") == 19 and m.word("model_enable_calls") == 0
        passed("zero-current mean outside hard envelope is rejected: " + str((b, c)))
    m = Model(); m.submit(4); m.tick(); m.write("model_current_b_a", 0.051, "f"); m.tick(4095)
    assert m.capture()["result"] == 1
    m.open_loop_submit(); m.tick(); assert m.status("result") == 19 and not m.moe()
    passed("complete but noisy zero-current capture cannot authorize open loop")
    for payload, reason in [({"voltage": 0.0}, 4), ({"voltage": -0.1}, 4),
                            ({"voltage": 0.501}, 4), ({"voltage": float("nan")}, 4),
                            ({"voltage": float("inf")}, 4), ({"frequency": 0.0}, 4),
                            ({"frequency": 5.001}, 4), ({"frequency": -5.001}, 4),
                            ({"frequency": float("nan")}, 4), ({"key": 0}, 3),
                            ({"duration": 3499999}, 4), ({"duration": 3500001}, 4),
                            ({"duty": 0.3}, 4)]:
        m = zero_ready(); m.open_loop_submit(**payload); m.tick(3)
        assert m.status("result") == reason and m.open_loop()["result"] == reason
        assert not m.moe() and m.word("model_stage_calls") == m.word("model_enable_calls") == 0
        assert m.word("g_target_validation_request") == m.sequence
        assert m.memory_word(symbols["g_target_validation_request"] + 8) == 0
        passed("open-loop payload/key rejects before staging: " + str(payload))
    for frequency in [-5.0, 5.0]:
        m = open_powered(voltage=0.5, frequency=frequency)
        m.submit(1); m.tick()
        assert not m.moe() and m.open_loop()["result"] == 2
        passed("open-loop accepts hard envelope edge without changing old duty limits: " + str(frequency))

    m = open_powered()
    capture_before = m.capture()
    assert math.isclose(m.open_loop()["offset_b"], -0.012, abs_tol=1e-7)
    assert math.isclose(m.open_loop()["offset_c"], -0.001, abs_tol=1e-7)
    # 主机修改 config 或提交 BUSY 请求不能修改已接受的电压、频率和输出所有者。
    m.cpu.mem_write(symbols["g_target_validation_open_loop_config"], struct.pack("<ff", 20.0, 200.0))
    active = m.status("active_request_sequence")
    m.submit(3, key=KEY); m.tick()
    assert m.status("result") == 5 and m.status("active_request_sequence") == active and m.moe()
    assert math.isclose(m.open_loop()["voltage"], 0.3, abs_tol=1e-7)
    assert m.open_loop()["frequency"] == 2.0
    for remaining, expected_state in [(4998, 1), (1, 2), (5000, 3), (5000, 4), (10000, 5), (4999, 5)]:
        m.tick(remaining)
        assert m.moe() and m.open_loop()["module_state"] == expected_state
    m.tick()
    result = m.open_loop()
    assert result["active"] == 0 and result["result"] == 1
    assert result["module_state"] == 6 and result["module_reason"] == 0
    assert result["samples"] == 30002 and result["enabled_samples"] == 30001
    assert m.status("result") == m.word("g_target_validation_stop_reason") == 1 and not m.moe()
    assert m.word("model_enable_calls") == 1 and m.word("model_stage_calls") == 30001
    assert m.word("model_common_duty_failures") > 0
    assert 0.45 < result["min_duty"] < 0.5 < result["max_duty"] < 0.55
    assert result["peak_current"] < 1e-7 and m.capture() == capture_before
    assert result["sequence"] % 2 == 0 and not (m.memory_word(TIM6) & 1)
    m.tick(100)
    assert m.open_loop() == result and m.word("model_enable_calls") == 1 and not m.moe()
    passed("all five open-loop segments complete by module, freeze diagnostics, preserve zero, and never replay")

    m = open_powered(frequency=-2.0); m.tick(11000)
    assert m.open_loop()["module_state"] == 3 and m.moe()
    m.submit(1); m.tick()
    assert m.open_loop()["module_state"] == 8 and m.open_loop()["module_reason"] == 9
    assert m.open_loop()["result"] == 2 and not m.moe()
    passed("negative electrical frequency is executable and operator Stop cancels ongoing rotation")
    m = zero_ready(); m.open_loop_submit(); m.tick(); m.submit(1); m.tick(4)
    assert m.open_loop()["result"] == 2 and m.word("model_enable_calls") == 0
    assert m.word("model_stage_calls") == 0 and not m.moe()
    passed("open-loop Stop before first neutral stage prevents any power attempt")

    for field, value, fmt, reason in [
            ("model_position_valid", 0, "I", 20), ("model_position_referenced", 0, "I", 20),
            ("model_position_age_ticks", 101, "I", 20), ("model_position_future_ticks", 1, "I", 20),
            ("model_position_turns", float("nan"), "f", 20), ("model_position_turns", 1.0, "f", 20),
            ("model_bus_valid", 0, "I", 7), ("model_bus_age_ticks", 101, "I", 7),
            ("model_bus_future_ticks", 1, "I", 7), ("model_bus_voltage", 9.9, "f", 7),
            ("model_current_valid", 0, "I", 6), ("model_current_mask", 4, "I", 6),
            ("model_current_b_a", float("nan"), "f", 6), ("model_step_ticks", 4, "I", 6),
            ("model_step_us", 20, "I", 6), ("model_stage_ok", 0, "I", 8)]:
        m = open_powered(); m.write(field, value, fmt); m.tick()
        assert not m.moe() and m.open_loop()["result"] == reason
        assert m.word("model_enable_calls") == 1
        m.tick(5); assert m.word("model_enable_calls") == 1
        passed("open-loop loss of fresh prerequisite fails off without re-enable: " + field)
    m = zero_ready(); m.write("model_position_valid", 0); m.open_loop_submit(); m.tick(3)
    assert m.status("result") == 20 and m.word("model_enable_calls") == 0
    passed("open loop requires a fresh valid encoder before neutral staging")
    m = zero_ready(); m.open_loop_submit(); m.tick(2); m.write("model_enable_ok", 0); m.tick()
    assert m.open_loop()["result"] == 9 and not m.moe() and m.word("model_enable_calls") == 1
    passed("open-loop failed HCC enable is terminal")
    for b, c in [(0.25, -0.10), (0.14, 0.14)]:
        m = open_powered(); m.write("model_current_b_a", b - 0.012, "f")
        m.write("model_current_c_a", c - 0.001, "f"); m.tick()
        assert m.open_loop()["result"] == 22 and not m.moe()
        assert m.open_loop()["peak_current"] >= 0.25
        passed("nominal current auxiliary monitor checks measured B/C and reconstructed A: " + str((b, c)))
    m = open_powered(); m.advance_guard(150)
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 6
    m.tick(); assert m.open_loop()["result"] == 6 and m.word("model_enable_calls") == 1
    passed("independent TIM6 shuts open loop off when ADC stops; next ADC freezes failure")
    m = open_powered()
    deadline = m.word("s_deadline_cycles")
    m.memory_write(DWT_CYCCNT, deadline); m.write("s_last_sample_cycles", deadline)
    m.call("model_guard")
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 18
    m.tick(); assert m.open_loop()["result"] == 18 and m.open_loop()["module_state"] == 8
    assert m.word("model_enable_calls") == 1
    passed("independent total deadline is OPEN_LOOP_TIMEOUT, never false completion")
    m = open_powered(); m.call("model_trip_event")
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 11
    m.tick(); assert m.open_loop()["result"] == 11 and m.word("model_enable_calls") == 1
    passed("Trip asynchronously revokes open-loop power and cannot be undone by later stage")
    m = open_powered(); m.memory_write(TIM1 + 0x44, 0); m.tick()
    assert not m.moe() and m.open_loop()["result"] == 10 and m.word("model_enable_calls") == 1
    passed("unexpected external open-loop power off is terminal")

    m = zero_ready(); m.write("model_position_turns", 0.98, "f"); m.open_loop_submit(); m.tick(3)
    m.write("model_position_turns", 0.01, "f"); m.tick()
    assert math.isclose(m.open_loop()["displacement"], 0.03, abs_tol=1e-6)
    m.write("model_position_turns", 0.99, "f"); m.tick()
    assert math.isclose(m.open_loop()["displacement"], 0.01, abs_tol=1e-6)
    assert math.isclose(m.open_loop()["start_position"], 0.98, abs_tol=1e-6)
    assert math.isclose(m.open_loop()["end_position"], 0.99, abs_tol=1e-6)
    m.submit(1); m.tick()
    passed("encoder observation unwraps new timestamps across both periodic boundaries without controlling voltage")
    m = open_powered(); m.write("model_position_age_ticks", 2); m.tick()
    assert m.moe() and m.open_loop()["displacement"] == 0.0
    m.write("model_position_age_ticks", 4); m.write("model_position_turns", 0.30, "f"); m.tick()
    assert m.open_loop()["result"] == 20 and not m.moe()
    passed("duplicate encoder timestamps do not integrate twice and changed payload at same timestamp fails")
    m = open_powered(); m.write("model_position_turns", 0.75, "f"); m.tick()
    assert m.open_loop()["result"] == 20 and not m.moe()
    passed("exact half-turn encoder ambiguity fails instead of inventing an unwrap direction")
    m = zero_ready(); m.memory_write(DWT_CYCCNT, 0xFFFFFF00)
    m.open_loop_submit(); m.tick(3)
    assert m.moe() and m.word("model_enable_calls") == 1
    deadline = m.word("s_deadline_cycles")
    m.memory_write(DWT_CYCCNT, deadline); m.write("s_last_sample_cycles", deadline)
    m.call("model_guard")
    assert m.word("g_target_validation_stop_reason") == 18 and not m.moe()
    m.tick(); assert m.open_loop()["result"] == 18
    passed("open-loop independent total deadline remains terminal across DWT wrap")

    m = Model(pending=16); m.tick(10)
    assert not m.moe() and m.open_loop()["request"] == 0
    m.survey_submit(); m.tick()
    assert m.status("result") == 19 and m.word("model_enable_calls") == 0
    passed("static survey discards reset-time request and requires this-boot full zero capture")
    for payload, reason in [({"voltage": 0.501}, 4), ({"excursion": 0.0}, 4),
                            ({"excursion": 1.001}, 4), ({"excursion": float("nan")}, 4),
                            ({"key": 0}, 3), ({"duration": 3500000}, 4), ({"duty": 0.3}, 4)]:
        m = zero_ready(); m.survey_submit(**payload); m.tick(3)
        assert m.status("result") == reason and not m.moe()
        assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0
        passed("static survey rejects invalid payload before staging: " + str(payload))

    def survey_powered(**payload):
        m = zero_ready(); m.write("model_position_period_ticks", 20)
        m.survey_submit(**payload); m.tick(3)
        assert m.moe() and m.word("model_enable_calls") == 1
        assert m.survey()["request"] == m.sequence and m.survey()["result"] == 0
        return m

    m = survey_powered()
    capture_before = m.capture()
    m.write("model_position_turns", 0.99, "f"); m.tick(15000)
    m.write("model_position_turns", 0.954285714, "f"); m.tick(15000)
    m.write("model_position_turns", 0.99, "f"); m.tick(20000)
    r = m.survey()
    assert r["sequence"] % 2 == 0 and r["result"] == 1 and r["stable_mask"] == 7, r
    assert not m.moe() and m.word("model_enable_calls") == 1 and m.word("model_stage_calls") == 50001
    assert m.open_loop()["samples"] == 50002 and m.open_loop()["frequency"] == 0
    assert [w["count"] for w in r["windows"]] == [250, 250, 250], r
    for w, position in zip(r["windows"], [0.99, 0.954285714, 0.99]):
        assert math.isclose(w["mean"], position, abs_tol=1e-6) and w["span"] == 0
        assert w["last_time"]-w["first_time"] == 4980
        assert abs(w["mean_b"]) < 1e-7 and abs(w["mean_c"]) < 1e-7
    assert m.capture() == capture_before
    m.tick(20); assert m.survey() == r and m.word("model_enable_calls") == 1
    passed("one five-second survey records only 250 unique SSI observations per hold and powers off without retry")

    m = survey_powered(); m.tick(12500)
    # 窗口内跨机械零点，圆差统计应保持小跨度；不能按普通均值得到约半圈。
    for i in range(2500):
        m.write("model_position_turns", 0.9998 if (i//10)%2 else 0.0002, "f"); m.tick()
    m.write("model_position_turns", 0.3, "f"); m.tick(12500)
    for i in range(2500):
        m.write("model_position_turns", 0.30 if (i//10)%2 else 0.31, "f"); m.tick()
    m.write("model_position_turns", 0.0001, "f"); m.tick(20000)
    r = m.survey()
    assert r["result"] == 1 and r["stable_mask"] == 5, r
    assert min(r["windows"][0]["mean"], 1-r["windows"][0]["mean"]) < 0.0001
    assert math.isclose(r["windows"][0]["span"], 0.0004, abs_tol=1e-6)
    assert r["windows"][1]["span"] > 0.009
    assert not m.moe()
    passed("survey uses circular mean across zero; unstable hold remains unqualified even after normal power completion")

    for field, value, fmt, expected in [("model_position_valid", 0, "I", 20),
                                       ("model_current_b_a", 0.3, "f", 22),
                                       ("model_stage_ok", 0, "I", 8)]:
        m = survey_powered(); m.write(field, value, fmt); m.tick()
        assert not m.moe() and m.survey()["result"] == expected and m.survey()["stable_mask"] == 0
        passed("static survey revokes output on fault and invalidates candidate: " + field)
    m = survey_powered(); m.submit(1); m.tick()
    assert m.survey()["result"] == 2 and m.survey()["stable_mask"] == 0 and not m.moe()
    passed("static survey Stop freezes a failed observation without installing calibration")
    m = survey_powered(); deadline = m.word("s_deadline_cycles")
    m.memory_write(DWT_CYCCNT, deadline); m.write("s_last_sample_cycles", deadline); m.call("model_guard")
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 18
    m.tick(); assert m.survey()["result"] == 18 and m.survey()["stable_mask"] == 0
    passed("static survey independent deadline is failure rather than false completion")

    m = survey_powered(excursion=1.0)
    m.write("model_position_turns",0.98,"f"); m.tick(15000)
    m.write("model_position_turns",(0.98+1/7)%1,"f"); m.tick(15000)
    m.write("model_position_turns",0.98,"f"); m.tick(20000)
    r=m.survey()
    assert r["result"]==1 and r["stable_mask"]==7 and r["excursion"]==1.0 and not m.moe(),r
    assert [w["count"] for w in r["windows"]]==[250,250,250]
    assert m.word("model_stage_calls")==50001 and m.word("model_enable_calls")==1
    passed("one full electrical turn preserves five-second trajectory, three SSI windows and unique enable")

    # 使用独立R/L电气替身闭合真实ARM PI和stage；电流符号按桥→电机定义，
    # 再经低边Shunt物理方向生成采样值，不能直接把目标填回输入。
    def hold_powered(**payload):
        m = zero_ready()
        m.hold_submit(**payload); m.tick()
        assert not m.moe() and m.word("model_stage_calls") == 0
        m.tick(); assert not m.moe() and m.word("model_stage_calls") == 1
        m.tick(); assert m.moe() and m.word("model_enable_calls") == 1
        return m

    for alpha, beta in [(0.06, 0.0), (0.0, -0.06)]:
        m = hold_powered(alpha=alpha, beta=beta)
        ia, ibeta = 0.0, 0.0
        decay = math.exp(-3.1 * 0.0001 / 0.002)
        pending = [(0.5, 0.5, 0.5)] * 2
        capture = m.capture()
        for _ in range(30000):
            applied = pending.pop(0)
            va, vb, vc = [12.0*(v-sum(applied)/3) for v in applied]
            ia = decay*ia + (1-decay)*((2*va-vb-vc)/3)/3.1
            ibeta = decay*ibeta + (1-decay)*(vb-vc)/math.sqrt(3)/3.1
            b = (-ia+math.sqrt(3)*ibeta)/2
            c = (-ia-math.sqrt(3)*ibeta)/2
            m.write("model_current_b_a", -0.012-b, "f")
            m.write("model_current_c_a", -0.001-c, "f")
            m.tick()
            pending.append(struct.unpack("<3f", m.cpu.mem_read(symbols["model_last_duty"], 12)))
        r = m.hold()
        assert r["sequence"] % 2 == 0 and r["result"] == 1 and r["active"] == 0, r
        assert not m.moe() and m.word("model_enable_calls") == 1 and m.word("model_stage_calls") == 30001
        assert r["samples"] == 30002 and r["enabled_samples"] == 30001 and r["window_samples"] == 5000, r
        assert abs(r["mean_alpha"]-alpha) < 0.001 and abs(r["mean_beta"]-beta) < 0.001, r
        assert r["max_voltage"] <= 0.40001 and r["peak_error"] < 0.002, r
        assert m.capture() == capture
        m.tick(20); assert m.hold() == r and m.word("model_enable_calls") == 1
        passed("finite stationary PI tracks independent delayed RL plant with physical Shunt polarity: "+str((alpha,beta)))

    for payload, reason in [({"alpha":0},4), ({"alpha":0.081},4), ({"alpha":math.nan},4),
        ({"alpha":0.07,"beta":0.07},4), ({"key":0},3), ({"duration":3500001},4), ({"duty":0.6},4)]:
        m = zero_ready(); m.hold_submit(**payload); m.tick(3)
        assert m.status("result") == reason and not m.moe()
        assert m.hold()["result"] == reason and m.hold()["active"] == 0
        assert m.word("model_stage_calls") == m.word("model_enable_calls") == 0
        passed("current hold rejects bad payload before power: "+str(payload))
    m = Model(); m.hold_submit(); m.tick(3)
    assert m.status("result") == 19 and not m.moe()
    passed("current hold cannot invent a zero calibration")
    for field,value,fmt,reason in [("model_current_valid",0,"I",6), ("model_position_valid",0,"I",20),
        ("model_bus_voltage",9.,"f",7), ("model_current_b_a",0.3,"f",22),
        ("model_stage_ok",0,"I",8), ("model_step_ticks",3,"I",6)]:
        m = hold_powered(); m.write(field,value,fmt); m.tick()
        assert not m.moe() and m.hold()["result"] == reason
        m.tick(5); assert m.word("model_enable_calls") == 1
        passed("current hold loses prerequisite and never re-enables: "+field)
    m = hold_powered(); m.submit(1); m.tick()
    assert not m.moe() and m.hold()["result"] == 2
    passed("Stop terminates current hold")
    m = hold_powered(); m.call("model_trip_event"); m.tick()
    assert not m.moe() and m.hold()["result"] == 11
    passed("asynchronous Trip terminates current hold")
    m = hold_powered(); m.advance_guard(150)
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 6
    m.tick(); assert m.hold()["result"] == 6
    passed("independent guard removes current-hold power when ADC stops")
    m = hold_powered(); deadline = m.word("s_deadline_cycles")
    m.memory_write(DWT_CYCCNT,deadline); m.write("s_last_sample_cycles",deadline); m.call("model_guard")
    assert not m.moe() and m.word("g_target_validation_stop_reason") == 18
    m.tick(); assert m.hold()["result"] == 18
    passed("independent current-hold total deadline cannot masquerade as completion")

    def current_survey_powered(**payload):
        m = zero_ready(); m.write("model_position_period_ticks",20)
        m.current_survey_submit(**payload); m.tick(3)
        assert m.moe() and m.word("model_enable_calls")==1
        assert m.survey(current=True)["request"]==m.sequence
        return m

    for payload,reason in [({"current":0.0},4),({"current":-0.06},4),({"current":0.501},4),
        ({"current":math.nan},4),({"excursion":0.25},4),({"excursion":math.nan},4),
        ({"key":0},3),({"duration":3500000},4),({"duty":0.4},4)]:
        m=zero_ready();m.current_survey_submit(**payload);m.tick(3)
        assert m.status("result")==reason and not m.moe()
        assert m.word("model_enable_calls")==m.word("model_stage_calls")==0
        assert m.survey(current=True)["stable_mask"]==0
        passed("current survey rejects payload before enabling: "+str(payload))
    m=Model();m.current_survey_submit();m.tick(3)
    assert m.status("result")==19 and not m.moe()
    passed("current survey needs actual same-boot zero")

    for amplitude in [0.06,0.4]:
        m=current_survey_powered(current=amplitude);ia=ibeta=0.0
        decay=math.exp(-3.1*0.0001/0.002);pending=[(0.5,0.5,0.5)]*2
        capture=m.capture();moving_error=0.0
        for i in range(1,50001):
            applied=pending.pop(0);va,vb,vc=[12.0*(v-sum(applied)/3) for v in applied]
            ia=decay*ia+(1-decay)*((2*va-vb-vc)/3)/3.1
            ibeta=decay*ibeta+(1-decay)*(vb-vc)/math.sqrt(3)/3.1
            b=(-ia+math.sqrt(3)*ibeta)/2;c=(-ia-math.sqrt(3)*ibeta)/2
            elapsed=2*i
            phase=(elapsed-30000)/10000 if 30000<=elapsed<40000 else (70000-elapsed)/10000 if 60000<=elapsed<70000 else 1.0 if 40000<=elapsed<60000 else 0.0
            scale=elapsed/10000 if elapsed<10000 else (100000-elapsed)/10000 if elapsed>=90000 else 1.0
            m.write("model_current_b_a",-0.012-b,"f");m.write("model_current_c_a",-0.001-c,"f")
            m.write("model_position_turns",(0.98+phase/7)%1,"f");m.tick()
            pending.append(struct.unpack("<3f",m.cpu.mem_read(symbols["model_last_duty"],12)))
            if 30000<=elapsed<40000 or 60000<=elapsed<70000:
                moving_error=max(moving_error,math.hypot(ia-amplitude*math.cos(math.tau*phase),ibeta-amplitude*math.sin(math.tau*phase)))
        r=m.hold();sr=m.survey(current=True)
        assert r["result"]==1 and r["samples"]==50002 and r["enabled_samples"]==50001 and r["window_samples"]==7500,r
        assert abs(r["mean_alpha"]-amplitude)<0.01 and abs(r["mean_beta"])<0.001 and r["peak_error"]<0.03,r
        assert moving_error<amplitude/12 and r["max_voltage"]<=1.80001 and r["limited_samples"]==0,(moving_error,r)
        assert sr["result"]==1 and sr["stable_mask"]==7 and [w["count"] for w in sr["windows"]]==[250]*3,sr
        assert abs(((sr["windows"][1]["mean"]-sr["windows"][0]["mean"]+0.5)%1)-0.5-1/7)<1e-5,sr
        assert m.word("model_stage_calls")==50001 and m.word("model_enable_calls")==1 and not m.moe()
        assert m.capture()==capture
        m.tick(20);assert m.survey(current=True)==sr and m.hold()==r
        passed("current survey drives independent delayed RL plant through full stator-frame rotation and three unique SSI windows: "+str(amplitude))
        print("MODEL_METRICS",amplitude,r["mean_alpha"],r["mean_beta"],r["peak_error"],moving_error,r["max_voltage"])

    for field,value,fmt,reason in [("model_position_valid",0,"I",20),("model_current_b_a",0.7,"f",22),
        ("model_bus_voltage",9.0,"f",7),("model_step_ticks",3,"I",6),("model_stage_ok",0,"I",8)]:
        m=current_survey_powered();m.write(field,value,fmt);m.tick()
        assert not m.moe() and m.survey(current=True)["result"]==reason and m.survey(current=True)["stable_mask"]==0
        m.tick(5);assert m.word("model_enable_calls")==1
        passed("current survey withdraws on invalid prerequisite: "+field)
    m=current_survey_powered();m.submit(1);m.tick()
    assert not m.moe() and m.survey(current=True)["result"]==2 and m.survey(current=True)["stable_mask"]==0
    passed("current survey Stop clears qualification")
    m=current_survey_powered();m.call("model_trip_event");m.tick()
    assert not m.moe() and m.survey(current=True)["result"]==11 and m.survey(current=True)["stable_mask"]==0
    passed("current survey Trip clears qualification")
    m=current_survey_powered();m.advance_guard(150)
    assert not m.moe() and m.word("g_target_validation_stop_reason")==6
    m.tick();assert m.survey(current=True)["stable_mask"]==0
    passed("current survey independent heartbeat withdraws power")
    m=current_survey_powered();deadline=m.word("s_deadline_cycles")
    m.memory_write(DWT_CYCCNT,deadline);m.write("s_last_sample_cycles",deadline);m.call("model_guard")
    assert not m.moe() and m.word("g_target_validation_stop_reason")==18
    m.tick();assert m.survey(current=True)["stable_mask"]==0
    passed("current survey independent total deadline is failure")

    for bus in [10.0,14.0]:
        m=current_survey_powered(current=0.5);m.write("model_bus_voltage",bus,"f");m.tick(10000)
        m.submit(1);m.tick();r=m.hold()
        assert not m.moe() and r["result"]==2 and 1.2<r["max_voltage"]<=1.8*bus/14+0.00001,r
        assert r["limited_samples"]>0 if bus==10.0 else r["max_voltage"]>1.7,r
        passed("current-survey voltage/integral saturation remains bounded and Stop withdraws output: "+str(bus))
    m=current_survey_powered(current=0.4);m.write("model_current_b_a",0.6,"f");m.tick()
    assert m.moe() and m.word("g_target_validation_stop_reason")==0
    m.write("model_current_b_a",0.7,"f");m.tick()
    assert not m.moe() and m.survey(current=True)["result"]==22
    passed("current survey uses its 0.65A threshold while old hold retains 0.25A")

    def rotor_powered(iq=0.1):
        m=zero_ready();m.write("model_position_period_ticks",20)
        m.rotor_submit(iq=iq)
        for _ in range(30):
            m.tick()
            if m.moe():break
        assert m.moe() and m.word("model_enable_calls")==1,m.rotor()
        return m

    for payload,reason in [({"iq":0},4),({"iq":0.201},4),({"iq":-0.201},4),({"iq":math.nan},4),
        ({"offset":-0.1},4),({"offset":1.0},4),({"offset":math.nan},4),
        ({"key":0},3),({"duration":3500000},4),({"duty":0.4},4)]:
        m=zero_ready();m.rotor_submit(**payload);m.tick(30)
        assert not m.moe() and m.rotor()["result"]==reason,m.rotor()
        assert m.word("model_enable_calls")==m.word("model_stage_calls")==0
        passed("rotor DQ rejects invalid request before any stage: "+str(payload))
    m=Model();m.rotor_submit();m.tick(30)
    assert not m.moe() and m.rotor()["result"]==19
    passed("rotor DQ requires captured same-boot zero")
    m=zero_ready();m.write("model_position_period_ticks",10000);m.rotor_submit();m.tick(17)
    assert not m.moe() and m.word("model_enable_calls")==0 and m.rotor()["result"]==20
    passed("rotor DQ waits for two fresh SSI frames without enabling and rejects stale baseline")

    for iq in [0.1,-0.1]:
        m=rotor_powered(iq);decay=math.exp(-3.1*0.0001/0.002)
        ia=ibeta=0.0;pending=[(0.5,0.5,0.5)]*2
        initial=m.word("model_time");position=m.cpu.mem_read(symbols["model_position_turns"],4)
        origin=struct.unpack("<f",position)[0];speed=0.5 if iq>0 else -0.5
        # 真实R/L电气积分独立于固件；机械速度为外部给定，不冒充转矩/惯量模型。
        for i in range(1,10001):
            t=m.word("model_time")+2
            theta=math.tau*((7*(origin+speed*(t-initial)/20000)-0.125)%1)
            emf=0.15 if iq>0 else -0.15
            duty=pending.pop(0);va,vb,vc=[12*(v-sum(duty)/3) for v in duty]
            ea=-emf*math.sin(theta);eb=emf*math.cos(theta)
            ia=decay*ia+(1-decay)*((2*va-vb-vc)/3-ea)/3.1
            ibeta=decay*ibeta+(1-decay)*((vb-vc)/math.sqrt(3)-eb)/3.1
            b=(-ia+math.sqrt(3)*ibeta)/2;c=(-ia-math.sqrt(3)*ibeta)/2
            m.write("model_current_b_a",-0.012-b,"f");m.write("model_current_c_a",-0.001-c,"f")
            if t%20==0:m.write("model_position_turns",(origin+speed*(t-initial)/20000)%1,"f")
            m.tick();pending.append(struct.unpack("<3f",m.cpu.mem_read(symbols["model_last_duty"],12)))
        r=m.rotor()
        assert not m.moe() and r["result"]==1 and r["samples"]==10002 and r["enabled"]==10001,r
        assert r["window_samples"]==2000 and abs(r["mean_id"])<0.02 and abs(r["mean_iq"]-iq)<0.02,r
        assert r["peak_error"]<0.04 and r["max_voltage"]<1.8 and r["limited_samples"]==0,r
        assert 29.9<abs(r["mean_rpm"])<30.1 and 0.49<abs(r["displacement"])<0.51,r
        assert m.word("model_enable_calls")==1 and m.word("model_stage_calls")==10001
        m.tick(5);assert m.rotor()==r
        passed("rotor DQ with independent delayed RL and back-EMF, prescribed motion: "+str(iq))
        print("ROTOR_MODEL_METRICS",iq,r)

    for field,value,fmt,reason in [("model_position_valid",0,"I",20),("model_position_age_ticks",31,"I",20),
        ("model_current_b_a",0.7,"f",22),("model_bus_voltage",9.0,"f",7),
        ("model_stage_ok",0,"I",8),("model_step_ticks",3,"I",6)]:
        m=rotor_powered();m.write(field,value,fmt);m.tick()
        assert not m.moe() and m.rotor()["result"]==reason,m.rotor()
        m.tick(5);assert m.word("model_enable_calls")==1
        passed("rotor DQ withdraws without retry: "+field)
    m=rotor_powered();m.submit(1);m.tick()
    assert not m.moe() and m.rotor()["result"]==2
    passed("rotor DQ Stop terminates")
    m=rotor_powered();m.call("model_trip_event");m.tick()
    assert not m.moe() and m.rotor()["result"]==11
    passed("rotor DQ asynchronous Trip terminates")
    m=rotor_powered();m.advance_guard(150);m.tick()
    assert not m.moe() and m.rotor()["result"]==6
    passed("rotor DQ independent heartbeat terminates")
    m=rotor_powered();deadline=m.word("s_deadline_cycles")
    m.memory_write(DWT_CYCCNT,deadline);m.write("s_last_sample_cycles",deadline);m.call("model_guard");m.tick()
    assert not m.moe() and m.rotor()["result"]==18
    passed("rotor DQ independent finite deadline remains a failure")
    m=rotor_powered();m.write("model_position_turns",0.30,"f");m.tick()
    assert not m.moe() and m.rotor()["result"]==20
    passed("rotor DQ rejects changed position under duplicate timestamp")
    m=rotor_powered()
    while (m.word("model_time")+2)%20:m.tick()
    m.write("model_position_turns",0.27,"f");m.tick()
    assert not m.moe() and m.rotor()["result"]==23,m.rotor()
    passed("rotor DQ speed bound withdraws before another request")
    m=rotor_powered();position=0.25
    for _ in range(9000):
        if (m.word("model_time")+2)%20==0:
            position=(position+0.0025)%1
            m.write("model_position_turns",(round(position*16384)%16384)/16384,"f")
        m.tick()
        if not m.moe():break
    assert m.rotor()["result"]==23 and 2.0<=abs(m.rotor()["displacement"])<2.01,m.rotor()
    passed("rotor DQ accumulated displacement bound spans encoder wrap and terminates")

    def speed_powered(rpm=60.0):
        m=zero_ready();m.write('model_position_period_ticks',20);m.speed_submit(rpm=rpm)
        for _ in range(30):
            m.tick()
            if m.moe():break
        assert m.moe() and m.word('model_enable_calls')==1,m.speed()
        return m

    for payload,reason in [({'rpm':0},4),({'rpm':9.9},4),({'rpm':80.1},4),({'rpm':-80.1},4),
        ({'rpm':math.nan},4),({'offset':1.0},4),({'offset':math.nan},4),({'key':0},3),
        ({'duration':1500000},4),({'duty':0.4},4)]:
        m=zero_ready();m.speed_submit(**payload);m.tick(30)
        assert not m.moe() and m.speed()['result']==reason,m.speed()
        assert m.word('model_stage_calls')==m.word('model_enable_calls')==0
        passed('speed request rejected before any stage: '+str(payload))
    m=Model();m.speed_submit();m.tick(30)
    assert not m.moe() and m.speed()['result']==19
    passed('speed mode requires captured same-boot zero')

    for target,inertia,inductance,friction in [(60.0,1e-5,0.002,0.0008),(-60.0,1e-5,0.002,0.0008),
        (80.0,2e-5,0.00086,0.0012)]:
        m=speed_powered(target);position=0.25;omega=ia=ibeta=0.0
        pending=[(0.5,0.5,0.5)]*2;ke=0.04/1.5;decay=math.exp(-3.1*0.0001/inductance)
        for i in range(1,15001):
            t=m.word('model_time')+2;theta=math.tau*((7*position-0.125)%1)
            applied=pending.pop(0);va,vb,vc=[12*(d-sum(applied)/3) for d in applied]
            ea=-ke*omega*math.sin(theta);eb=ke*omega*math.cos(theta)
            ia=decay*ia+(1-decay)*((2*va-vb-vc)/3-ea)/3.1
            ibeta=decay*ibeta+(1-decay)*((vb-vc)/math.sqrt(3)-eb)/3.1
            iq=-ia*math.sin(theta)+ibeta*math.cos(theta)
            torque=1.5*ke*iq;load=0.0002*(1 if target>0 else -1) if i>=7000 else 0.0
            drive=torque-load
            if abs(omega)<0.01 and abs(drive)<=friction:omega=0.0
            else:
                sign=1 if (omega if abs(omega)>=0.01 else drive)>0 else -1
                omega+=0.0001*(drive-0.00002*omega-friction*sign)/inertia
            position=(position+omega/math.tau*0.0001)%1
            b=(-ia+math.sqrt(3)*ibeta)/2;c=(-ia-math.sqrt(3)*ibeta)/2
            m.write('model_current_b_a',-0.012-b,'f');m.write('model_current_c_a',-0.001-c,'f')
            if t%20==0:m.write('model_position_turns',(round(position*16384)%16384)/16384,'f')
            m.tick();pending.append(struct.unpack('<3f',m.cpu.mem_read(symbols['model_last_duty'],12)))
            if not m.moe() and i<15000:raise AssertionError(m.speed())
        r=m.speed();print('SPEED_MODEL_METRICS',target,inertia,inductance,r,flush=True)
        assert not m.moe() and r['result']==1 and (r['samples'],r['enabled'],r['window_samples'])==(15002,15001,4000),r
        assert r['speed_updates']==1500 and r['speed_window_updates']==400,r
        assert abs(r['mean_speed_error'])<=max(3,abs(target)*0.1) and r['peak_speed_error']<=max(12,abs(target)*0.25),r
        assert math.hypot(r['mean_id'],r['mean_iq']-r['mean_iq_ref'])<0.01 and r['peak_current_error']<0.04,r
        assert abs(r['last_rpm'])<15 and r['max_rpm']<300 and abs(r['displacement'])<2,r
        assert abs(r['mean_iq_ref'])<=0.100001 and abs(r['last_iq_ref'])<=0.100001 and r['max_voltage']<=1.80001,r
        assert m.word('model_stage_calls')==15001 and m.word('model_enable_calls')==1
        passed('cascaded speed/current with independent delayed electrical/mechanical plant and load step: '+str((target,inertia,inductance)))

    for field,value,fmt,reason in [('model_position_valid',0,'I',20),('model_position_age_ticks',31,'I',20),
        ('model_current_b_a',0.7,'f',22),('model_bus_voltage',9.0,'f',7),('model_stage_ok',0,'I',8),('model_step_ticks',3,'I',6)]:
        m=speed_powered();m.write(field,value,fmt);m.tick()
        assert not m.moe() and m.speed()['result']==reason,m.speed()
        m.tick(5);assert m.word('model_enable_calls')==1
        passed('speed mode withdraws without restart: '+field)
    m=speed_powered();m.submit(1);m.tick();assert not m.moe() and m.speed()['result']==2
    passed('speed mode Stop terminates')
    m=speed_powered();m.call('model_trip_event');m.tick();assert not m.moe() and m.speed()['result']==11
    passed('speed mode Trip terminates')
    m=speed_powered();m.advance_guard(150);m.tick();assert not m.moe() and m.speed()['result']==6
    passed('speed mode independent sample heartbeat withdraws')
    m=speed_powered();deadline=m.word('s_deadline_cycles')
    m.memory_write(DWT_CYCCNT,deadline);m.write('s_last_sample_cycles',deadline);m.call('model_guard');m.tick()
    assert not m.moe() and m.speed()['result']==18
    passed('speed mode independent finite deadline withdraws')
    m=speed_powered()
    while (m.word('model_time')+2)%20:m.tick()
    m.write('model_position_turns',0.27,'f');m.tick()
    assert not m.moe() and m.speed()['result']==23
    passed('speed mode raw velocity bound is enforced before low-pass filtering')
    m=speed_powered(80);m.tick(10000);m.submit(1);m.tick();r=m.speed()
    assert not m.moe() and r['result']==2 and r['speed_limited']>0 and abs(r['last_iq_ref'])<=0.100001,r
    assert r['max_voltage']<=1.80001 and r['integral_limited']>0,r
    passed('stalled-feedback fixture keeps speed Iq, current voltage and integral bounded; Stop withdraws')

    for field,value,fmt,mask in [('model_bus_available',0,'I',1),('model_bus_valid',0,'I',2),
        ('model_bus_voltage',math.nan,'f',4),('model_bus_voltage',9.9,'f',8),
        ('model_bus_voltage',14.1,'f',16),('model_bus_future_ticks',1,'I',32),('model_bus_age_ticks',101,'I',64)]:
        m=speed_powered();m.write(field,value,fmt);m.tick();failure=m.bus_failure()
        assert not m.moe() and m.speed()['result']==7 and failure[0:4]==(2,2,mask,int(field!='model_bus_available')),failure
        assert m.word('model_bus_diagnostic_calls')==1 and m.word('model_bus_diagnostic_while_powered')==0
        saved=bytes(m.cpu.mem_read(symbols['g_target_validation_bus_failure'],96))
        m.write(field,12.0 if field=='model_bus_voltage' else (1 if field in ['model_bus_valid','model_bus_available'] else 0),fmt)
        m.submit(1);m.tick(20)
        assert bytes(m.cpu.mem_read(symbols['g_target_validation_bus_failure'],96))==saved
        passed('first bus failure is latched only after safe-off and survives later publications/Stop: '+field+str(mask))

    m=speed_powered();m.submit(20);m.tick();assert m.status('result')==5
    m.write('model_bus_voltage',14.1,'f');m.tick();assert m.bus_failure()[1]==2
    passed('bus failure belongs to active request despite a newer busy mailbox request')

print(f"PASS: {len(cases)} ARM-executed G431 validation cases.")
print("Fake HCC/time only; does not prove electrical behavior, interrupt latency or hardware L1/L2.")
