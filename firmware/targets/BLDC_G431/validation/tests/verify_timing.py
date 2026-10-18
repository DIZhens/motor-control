"""Run the actual power-off timing probe against the actual G431 Platform ARM code.

This imports the register model used by verify_platform. Explicit DWT-read time
steps exercise waiting/cleanup logic; they do not predict instruction latency.
"""
from pathlib import Path
import struct
import tempfile
from unicorn.arm_const import UC_ARM_REG_PRIMASK

from verify_platform import (Model, ROOT, compile_image, CR1, CNT, BDTR, DWT_CTRL,
                             DWT_CYCCNT, SR, CEN, DIR, UDIS, MOE, BREAKS, NEUTRAL)

HEADER = ["sequence", "request_sequence", "case_id", "outcome", "wait_iterations",
          "elapsed_cycles", "stage_called", "stage_accepted", "second_stage_called",
          "second_stage_accepted"]
REGISTERS = ["cycles", "cnt", "cr1", "sr", "bdtr", "ccr1", "ccr2", "ccr3"]
OBSERVATIONS = ["entry", "before", "after", "second_after", "cleanup"]
PASS, PRECONDITION, TIMEOUT, MISMATCH = 1, 2, 3, 4


class TimingModel(Model):
    def __init__(self, image, clock_step=17, **kwargs):
        super().__init__(image, **kwargs)
        self.clock_step = clock_step
        self.freeze_timer = False
        self.stage_entries = []
        self.enable_calls = 0
        self.power_writes = []
        self.timebase_writes = []
        self.on_read = self.clock_read
        self.on_write = self.observe_power_write
        self.on_instruction = self.observe_call
        self.instruction_budget = 2000000
        assert self.word("model_timing_capture_size") == 200

    def clock_read(self, model, address):
        if address == DWT_CYCCNT:
            if self.freeze_timer:
                if self.get(DWT_CTRL) & 1:
                    self.put(DWT_CYCCNT, self.get(DWT_CYCCNT) + self.clock_step)
            else:
                self.advance(self.clock_step)

    def observe_power_write(self, model, address, value):
        if address == BDTR:
            self.power_writes.append(value)
        if address in [DWT_CTRL, DWT_CYCCNT, CNT, 0x40012C14, 0x40012C28, 0x40012C2C, 0x40012C30]:
            self.timebase_writes.append((address, value))

    def observe_call(self, model, address):
        if address == (self.symbols["realtime_motor_io_stage_phase_duty"] & ~1):
            self.stage_entries.append((self.get(DWT_CYCCNT), self.get(CNT), self.get(CR1)))
        elif address == (self.symbols["realtime_motor_io_power_enable"] & ~1):
            self.enable_calls += 1

    def probe(self, case_id=10, **tick_options):
        self.write("model_timing_case", case_id)
        self.tick(128, **tick_options)
        return self.capture()

    def capture(self):
        words = struct.unpack("<50I", self.cpu.mem_read(self.symbols["g_target_validation_timing"], 200))
        result = dict(zip(HEADER, words[:10]))
        for index, name in enumerate(OBSERVATIONS):
            result[name] = dict(zip(REGISTERS, words[10 + index * 8:18 + index * 8]))
        return result

    def clean(self, data):
        assert not self.moe() and tuple(self.shadow) == NEUTRAL
        assert self.enable_calls == 0 and all(not value & MOE for value in self.power_writes)
        assert not self.timebase_writes, self.timebase_writes
        assert not data["cleanup"]["bdtr"] & MOE
        assert tuple(data["cleanup"][name] for name in ["ccr1", "ccr2", "ccr3"]) == NEUTRAL
        assert data["sequence"] != 0 and data["sequence"] % 2 == 0
        assert data["wait_iterations"] <= 4096
        assert data["outcome"] == self.word("model_timing_result")


def run(image):
    cases = []

    def passed(name):
        cases.append(name)

    for case_id, first, second in [(10, 1, 0), (11, 1, 0), (12, 0, 0), (13, 0, 0), (14, 1, 1)]:
        m = TimingModel(image); data = m.probe(case_id)
        assert data["outcome"] == PASS, data
        assert data["case_id"] == case_id and data["request_sequence"] == 77
        assert data["stage_called"] == 1 and data["stage_accepted"] == first
        assert data["second_stage_called"] == second and data["second_stage_accepted"] == 0
        assert len(m.stage_entries) == 1 + second
        assert 0 < data["elapsed_cycles"] < 17000
        for name in OBSERVATIONS:
            assert not data[name]["bdtr"] & MOE
        if case_id in [10, 11, 14]:
            assert m.stage_entries[0][1] > 1700 and m.stage_entries[0][2] & DIR
        if case_id == 12:
            assert m.stage_entries[0][1] <= 1600 and m.stage_entries[0][2] & DIR
        if case_id == 13:
            assert (m.stage_entries[0][0] - data["entry"]["cycles"]) & 0xFFFFFFFF >= 8500
            assert m.stage_entries[0][1] > 1700 and m.stage_entries[0][2] & DIR
        m.clean(data)
        m.advance(10000)
        assert m.active == NEUTRAL and not m.moe()
        frozen = m.capture(); m.tick(0)
        assert m.capture() == frozen and not m.moe()
        passed(f"case {case_id}: real stage result, no power, neutral cleanup, stable capture")

    for case_id in [10, 11, 12, 13, 14]:
        m = TimingModel(image, cycle_counter=0xFFFFFF00)
        data = m.probe(case_id, advance=0)
        assert data["outcome"] == PASS, data
        m.clean(data)
        passed(f"case {case_id}: DWT wrap retains bounded semantics")

    m = TimingModel(image); cancel = m.probe(14)
    assert cancel["outcome"] == PASS and cancel["second_stage_called"] == 1
    assert any(cancel["second_after"].values())
    m.write("model_timing_request_sequence", 78); after = m.probe(12)
    assert after["outcome"] == PASS and after["request_sequence"] == 78
    assert after["sequence"] == cancel["sequence"] + 2, (cancel, after)
    assert after["second_stage_called"] == after["second_stage_accepted"] == 0
    assert not any(after["second_after"].values())
    m.write("model_timing_request_sequence", 79); rejected = m.probe(0)
    assert rejected["outcome"] == PRECONDITION and rejected["request_sequence"] == 79
    assert rejected["sequence"] == after["sequence"] + 2 and rejected["wait_iterations"] == 0
    assert rejected["stage_called"] == rejected["stage_accepted"] == 0
    assert rejected["second_stage_called"] == rejected["second_stage_accepted"] == 0
    assert all(not any(rejected[name].values()) for name in ["before", "after", "second_after"])
    m.clean(rejected)
    passed("consecutive cancel, late and rejected cases never retain old scratch observations")

    m = TimingModel(image); m.write("model_timing_eligible", 0); data = m.probe()
    assert data["outcome"] == PRECONDITION and not m.stage_entries
    m.clean(data); passed("external prerequisites rejected before any stage")

    m = TimingModel(image); m.cpu.reg_write(UC_ARM_REG_PRIMASK, 1); data = m.probe()
    assert data["outcome"] == PRECONDITION and not m.stage_entries
    assert m.cpu.reg_read(UC_ARM_REG_PRIMASK) == 1
    m.clean(data); passed("probe cannot busy-wait with interrupts masked")

    for register, value in [(0x40021008, 4 << 11), (0x40012C0C, 1),
                             (0x40012C18, 1 << 11), (0x40012C18, 1 << 3),
                             (0x40012C1C, 1 << 11), (0x40012C28, 1),
                             (0x40012C2C, 4249), (0x40012C30, 1)]:
        m = TimingModel(image)
        m.on_marker = lambda model, marker, register=register, value=value: (
            model.put(register, value) if marker == 1 else None)
        data = m.probe()
        assert data["outcome"] == PRECONDITION and not m.stage_entries
        m.clean(data); passed(f"unsupported timebase/preload register {register:X}/{value} rejects probe")

    m = TimingModel(image)
    def invalidate_epoch(model, marker):
        if marker == 1:
            model.pending_irq = True
            model.on_marker = None
    m.on_marker = invalidate_epoch; data = m.probe()
    assert m.irq_calls == 1 and data["outcome"] == MISMATCH
    assert data["stage_called"] == 1 and data["stage_accepted"] == 0
    m.clean(data); passed("unexpected real stage refusal is MISMATCH, never a timing PASS")

    for case_id in [0, 9, 15, 0xFFFFFFFF]:
        m = TimingModel(image); data = m.probe(case_id)
        assert data["outcome"] == PRECONDITION and not m.stage_entries
        m.clean(data); passed(f"unsupported case {case_id} publishes rejection and cleanup")

    for register, mask in [(DWT_CTRL, 1), (0xE000EDFC, 1 << 24), (CR1, CEN)]:
        m = TimingModel(image)
        m.on_marker = lambda model, marker, register=register, mask=mask: (
            model.put(register, model.get(register) & ~mask) if marker == 1 else None)
        data = m.probe()
        assert data["outcome"] == PRECONDITION and not m.stage_entries
        m.clean(data); passed(f"disabled clock/timer prerequisite {register:X}/{mask} cannot stage")

    for register, bits in [(SR, BREAKS), (BDTR, MOE), (CR1, UDIS)]:
        m = TimingModel(image)
        m.on_marker = lambda model, marker, register=register, bits=bits: (
            model.put(register, model.get(register) | bits) if marker == 1 else None)
        data = m.probe()
        assert data["outcome"] == PRECONDITION and not m.stage_entries
        m.clean(data); passed(f"unsafe entry register {register:X}/{bits} is rejected and powers off")

    for case_id in [10, 11, 12, 13, 14]:
        m = TimingModel(image, clock_step=0); data = m.probe(case_id)
        assert data["outcome"] == TIMEOUT and not m.stage_entries, data
        assert data["wait_iterations"] == 4096 and data["elapsed_cycles"] == 0
        m.clean(data); passed(f"case {case_id}: stopped DWT with enable bit set has finite loop bound")

    m = TimingModel(image); m.freeze_timer = True; data = m.probe(11)
    assert data["outcome"] == TIMEOUT and not m.stage_entries
    assert data["wait_iterations"] < 4096
    assert data["entry"]["cnt"] - 1700 <= data["elapsed_cycles"] < 17000
    m.clean(data); passed("non-progressing timer exits at the original half-cycle budget")

    m = TimingModel(image); data = m.probe(11, count=2100)
    assert data["outcome"] == TIMEOUT and not m.stage_entries
    assert 1700 < data["entry"]["cnt"] < 2200 and data["before"]["cnt"] < 2200
    assert data["wait_iterations"] == 1 and data["elapsed_cycles"] < 17000
    m.clean(data); passed("late probe entry cannot wait another cycle for the before-deadline case")

    m = TimingModel(image, clock_step=50); data = m.probe(10, count=1900)
    assert data["outcome"] == TIMEOUT and not m.stage_entries, data
    assert data["entry"]["cnt"] > 1700 and data["before"]["cnt"] <= 1700
    assert data["wait_iterations"] == 1 and data["elapsed_cycles"] < 17000
    m.clean(data); passed("immediate entry that exhausts its preparation budget exits without stage")

    m = TimingModel(image)
    def stop_in_wait(model, address):
        model.clock_read(model, address)
        if address == DWT_CYCCNT and model.get(CNT) < 3500:
            model.put(CR1, model.get(CR1) & ~CEN)
    m.on_read = stop_in_wait; data = m.probe(11)
    assert data["outcome"] == PRECONDITION and not m.stage_entries
    m.clean(data); passed("timer stopping during wait aborts without stage")

    m = TimingModel(image)
    def break_in_wait(model, address):
        model.clock_read(model, address)
        if address == DWT_CYCCNT and model.get(CNT) < 3500:
            model.put(SR, model.get(SR) | BREAKS)
    m.on_read = break_in_wait; data = m.probe(11)
    assert data["outcome"] == PRECONDITION and not m.stage_entries
    m.clean(data); passed("hardware break during wait aborts without stage or flag clearing")
    assert m.get(SR) & BREAKS == BREAKS

    print(f"G431 real timing probe / real Platform ARM regression: {len(cases)} cases PASS")
    print("Scope: no-power logic and modeled register ordering; no target flashing, WCET or electrical proof.")
    for name in cases:
        print("PASS " + name)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="g431-timing-model-") as temp:
        run(compile_image(Path(temp), [ROOT / "validation/timing_probe.c"], ["PLATFORM_MODEL_TIMING_PROBE"]))
