"""Host PWM transaction checks with synthetic memory only; no ST-Link access."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pwm_once as pwm

cases = 0


def rejected(function):
    global cases
    try:
        function()
    except RuntimeError:
        cases += 1
    else:
        raise AssertionError("Unsafe PWM transaction accepted")


def healthy():
    block = bytearray(104)
    for offset, value in [(0, 0xA1), (12, 1), (48, 3), (24, 0x6868), (28, 0x7868), (32, 0x1555),
                          (44, 4250), (64, 4240), (68, 0x03003400)]:
        struct.pack_into("<I", block, offset, value)
    state = dict(status=1, started=1, state=0, halted=False, core_lockup=False,
        drive_running=True, adc_irq=True, moe=False, high_inputs=0, low_inputs=0,
        guard_running=False, guard_irq=False, invalid_current_count=0, legacy_invalid=0,
        moe_violations=0, phase_b_a=-0.012, phase_c_a=0.0, bus_v=12.0, bus_valid=True,
        trip_armed=True, legacy_trip_valid=True, trip_valid=1, legacy_trip_active=False,
        legacy_trip_causes=0, bdtr=0x03003400, af1=9, af2=23, power_key=0,
        bie=True, break_flags=0, comp_mask=0, dac_codes=[2065, 345, 345],
        comp_csr=[0x41, 0x41, 0x51, 0x41], dac_cr=[1, 0x10001], dac_mcr=[3, 0x30003],
        result=0, stop_reason=0, fast_tick_count=1000, uw_tick=50,
        mailbox_command=0, mailbox_duration_us=2000, mailbox_duty_bits=0x3F000000,
        pwm=pwm.decode_pwm(block))
    state.update({field: 0 for field in pwm.checks.COUNTERS})
    state.update({"current.acquisition_valid": True, "current.measured_phases": 6,
        "trip.condition_active": False, "trip.causes": 0, "bus.acquisition_valid": True,
        "bus.voltage_v": 12.0, "current.observation_time": 1000, "bus.observation_time": 990})
    return state


class FakeSession:
    def __init__(self, state=None, after_changes=None, mutate_capture=False):
        self.state = deepcopy(state or healthy())
        self.capture = dict.fromkeys(pwm.checks.PROBE_FIELDS, 0)
        self.layout = {"request": 0x2000089C}
        self.last_sequence = self.state["mailbox_sequence"]
        self.write_attempted = False
        self.calls = []
        self.after_changes = after_changes or {}
        self.mutate_capture = mutate_capture

    def read(self, tag):
        for field, increment in [("fast_tick_count", 20), ("uw_tick", 1),
                                  ("current.observation_time", 20), ("bus.observation_time", 20)]:
            self.state[field] += increment
        self.last_sequence = self.state["mailbox_sequence"]
        return deepcopy(self.state), {"probe": struct.pack("<17I", *(self.capture[k] for k in pwm.checks.PROBE_FIELDS))}

    def execute(self, tag, arguments):
        self.calls.append(arguments)
        assert len(arguments) == 9
        assert arguments[0] == arguments[6] == "-w32"
        assert int(arguments[1], 0) == self.layout["request"] + 4
        assert int(arguments[7], 0) == self.layout["request"]
        command, key, duration, bits, sequence = [int(arguments[i], 0) for i in [2, 3, 4, 5, 8]]
        if command == 1:
            assert tag == "exception-safe-off-only" and key == 0
            self.state.update(command_sequence=sequence, mailbox_sequence=sequence,
                              moe=False, power_key=0, result=2, stop_reason=2)
            return
        assert tag == "submit-pwm-once"
        assert command == 3 and key == pwm.checks.KEY and duration == 2000
        assert bits in [0x3E99999A, 0x3F333333]
        self.state.update(command_sequence=sequence, active_request_sequence=sequence,
            mailbox_sequence=sequence, mailbox_command=command, mailbox_duration_us=duration,
            mailbox_duty_bits=bits, result=1, stop_reason=1)
        for field, increment in [("stage_attempts", 40), ("stage_successes", 40),
                ("enable_attempts", 1), ("enable_successes", 1), ("guard_irq_count", 39)]:
            self.state[field] += increment
        self.state.update(self.after_changes)
        if self.mutate_capture:
            self.capture["sequence"] = 2


def anchor(session):
    return {"result": "POWERED_STATIC_PASS", "hex_sha256": pwm.common.EXPECTED,
            "final": deepcopy(session.state), "capture": deepcopy(session.capture)}


for percent, bits, width in [(30, 0x3E99999A, 35), (70, 0x3F333333, 15)]:
    session = FakeSession()
    fresh = pwm.run(session, None, "inspect-startup", percent)
    fresh["hex_sha256"] = pwm.common.EXPECTED
    assert fresh["result"] == "PWM_FRESH_STARTUP_PASS" and not session.calls
    cases += 1
    preflight = pwm.run(session, fresh, "preflight", percent)
    preflight["hex_sha256"] = pwm.common.EXPECTED
    assert preflight["result"] == "PWM_READ_ONLY_PREFLIGHT_PASS" and not session.calls
    cases += 1
    report = pwm.run(session, preflight, "execute", percent)
    report["hex_sha256"] = pwm.common.EXPECTED
    assert report["result"] == "PWM_BURST_COMPLETE_OFF_SCOPE_PENDING" and len(session.calls) == 1
    assert not report["scope_qualified"] and report["mailbox_duty_bits"] == bits
    assert report["expected_LA_high_us_before_driver_effects"] == width
    assert not report["final"]["moe"] and not report["final"]["power_key"]
    cases += 1
    rejected(lambda: pwm.run(session, preflight, "execute", percent))
    assert len(session.calls) == 1

for percent in [0, 10, 50, 90, 100, 30.0, True, float("nan"), "30"]:
    session = FakeSession()
    rejected(lambda: pwm.run(session, anchor(session), "execute", percent))
    assert not session.calls

for changes in [dict(moe=True), dict(bus_v=9.9), dict(bus_v=float("nan")), dict(core_lockup=True),
        dict(adc_irq=False), dict(high_inputs=0x100), dict(power_key=pwm.checks.KEY), dict(probe_held=1),
        dict(dac_codes=[0, 345, 345]), dict(bie=False), {"trip.causes": 2},
        {"bus.observation_time": 899}, {"bus.observation_time": 1001}]:
    session = FakeSession(dict(healthy(), **changes))
    rejected(lambda: pwm.run(session, anchor(session), "execute", 30))
    assert not session.calls

for name, value in [("center_aligned_mode", 0), ("clock_division", 1), ("prescaler", 1),
        ("period_counts", 4249), ("repetition", 0), ("repetition", 1),
        ("update_irq_enabled", False), ("output_config", 0x1557),
        ("sample_compare", 4000), ("timer_deadtime_code", 10),
        ("channels", [{"mode": 7, "selection": 0, "preload": True}] * 4),
        ("channels", [{"mode": m, "selection": 1, "preload": True} for m in [6, 6, 6, 7]]),
        ("channels", [{"mode": m, "selection": 0, "preload": False} for m in [6, 6, 6, 7]])]:
    state = healthy()
    state["pwm"][name] = value
    session = FakeSession(state)
    rejected(lambda: pwm.run(session, anchor(session), "execute", 30))
    assert not session.calls

for change in [dict(result=11), dict(stop_reason=6), dict(enable_attempts=2), dict(enable_successes=2),
        dict(stage_successes=1, stage_attempts=1), dict(stage_successes=43, stage_attempts=43),
        dict(stage_attempts=41), dict(guard_irq_count=0), dict(guard_irq_count=43), dict(moe=True),
        dict(mailbox_command=5), dict(mailbox_duration_us=10000), dict(mailbox_duty_bits=0x3F000000)]:
    session = FakeSession(after_changes=change)
    rejected(lambda: pwm.run(session, anchor(session), "execute", 30))
    assert len(session.calls) == 1

session = FakeSession(mutate_capture=True)
rejected(lambda: pwm.run(session, anchor(session), "execute", 30))
assert len(session.calls) == 1
for change in [dict(hex_sha256="wrong"), dict(result="FAILED_NO_RETRY")]:
    session = FakeSession()
    rejected(lambda: pwm.run(session, dict(anchor(session), **change), "execute", 30))
    assert not session.calls
session = FakeSession()
rejected(lambda: pwm.run(session, anchor(session), "inspect-startup", 30))
session.state["command_sequence"] = session.state["mailbox_sequence"] = 1
rejected(lambda: pwm.run(session, None, "inspect-startup", 30))
assert not session.calls

session = FakeSession()
session.state.update(command_sequence=10, mailbox_sequence=10, active_request_sequence=6,
    stage_attempts=10, stage_successes=10, enable_attempts=2, enable_successes=2, guard_irq_count=8,
    probe_sequence=12, probe_request=6, probe_outcome=2, result=1, stop_reason=1)
session.capture.update(sequence=12, request_sequence=6, source=3, outcome=2, held=0, original_code=345)
previous = anchor(session)
previous["result"] = "RECOVERY_REARM_PASS_OFF"
result = pwm.run(session, previous, "execute", 30)
assert result["sequence"] == 11 and result["final"]["enable_successes"] == 3
assert result["capture"] == previous["capture"] and len(session.calls) == 1
cases += 1

for after_write in [False, True]:
    with TemporaryDirectory(prefix="g431-pwm-cli-") as directory:
        folder = Path(directory)
        session = FakeSession(after_changes={"result": 11} if after_write else None)
        if not after_write:
            session.state["bus_v"] = 9.0
        previous = folder / "previous.json"
        previous.write_text(json.dumps(anchor(session)), encoding="utf-8")
        output = folder / "result"
        argv = ["pwm_once.py", "--output", str(output), "--previous-summary", str(previous),
                "--duty-percent", "30", "--execute-once"]
        with patch.object(pwm, "Session", return_value=session), patch.object(sys, "argv", argv):
            rejected(pwm.main)
        failure = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        assert failure["result"] == "FAILED_NO_RETRY" and failure["write_attempted"] is after_write
        if after_write:
            assert [int(call[2], 0) for call in session.calls] == [3, 1]
            assert [int(call[3], 0) for call in session.calls] == [pwm.checks.KEY, 0]
            assert not failure["cleanup"]["moe"] and "cleanup_error" not in failure
        else:
            assert not session.calls and "cleanup" not in failure

transient = healthy()
transient["pwm"]["update_irq_enabled"] = False
rejected(lambda: pwm.check_configuration(transient))
pwm.check_configuration(transient, check_update_irq=False)
cases += 1
transient["pwm"]["period_counts"] = 4249
rejected(lambda: pwm.check_configuration(transient, check_update_irq=False))

print(f"PASS: {cases} host PWM-tool safety cases; no hardware access.")
