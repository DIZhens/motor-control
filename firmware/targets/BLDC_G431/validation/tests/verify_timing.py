"""Verify legacy half-cycle probes reject the new G431 slot configuration.

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
    for counter in [0x10000, 0xFFFFFF00]:
        for case_id in [10, 11, 12, 13, 14]:
            m = TimingModel(image, cycle_counter=counter)
            assert m.get(0x40012C30) == 3 and m.get(0x40012C0C) & 1
            data = m.probe(case_id)
            assert data["outcome"] == PRECONDITION, data
            assert data["case_id"] == case_id and data["request_sequence"] == 77
            assert data["stage_called"] == data["stage_accepted"] == 0
            assert data["second_stage_called"] == data["second_stage_accepted"] == 0
            assert data["wait_iterations"] == 0 and not m.stage_entries
            m.clean(data)
            cases.append(f"case {case_id}: RCR=3 refuses legacy half-cycle probe at DWT {counter:X}")
    for case_id in [0, 9, 15, 0xFFFFFFFF]:
        m = TimingModel(image); data = m.probe(case_id)
        assert data["outcome"] == PRECONDITION and not m.stage_entries
        m.clean(data)
        cases.append(f"unknown case {case_id} cannot bypass slot rejection")
    m = TimingModel(image); first = m.probe(10)
    m.write("model_timing_request_sequence", 78)
    second = m.probe(14)
    assert second["sequence"] == first["sequence"] + 2 and second["request_sequence"] == 78
    assert all(not any(second[name].values()) for name in ["before", "after", "second_after"])
    m.clean(second)
    cases.append("consecutive rejected probes retain no old stage observations")
    print(f"G431 legacy timing probe compatibility: {len(cases)} cases PASS")
    print("Scope: rejection and neutral cleanup only; new slot deadline tests are in verify_platform.py.")
    print("No target flashing, WCET or electrical proof.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="g431-timing-model-") as temp:
        run(compile_image(Path(temp), [ROOT / "validation/timing_probe.c"], ["PLATFORM_MODEL_TIMING_PROBE"]))
