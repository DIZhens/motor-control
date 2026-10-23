"""Host command/evidence state-machine checks. No hardware or analog model."""
from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import probe_once as probe
import pwm_once as pwm

cases = 0


def rejected(function):
    global cases
    try:
        function()
    except RuntimeError:
        cases += 1
    else:
        raise AssertionError("Unsafe input was accepted")


def healthy():
    state = dict(status=1, started=1, state=0, halted=False, core_lockup=False,
        drive_running=True, adc_irq=True, moe=False, high_inputs=0, low_inputs=0,
        guard_running=False, guard_irq=False, invalid_current_count=0, legacy_invalid=0,
        moe_violations=0, phase_b_a=-0.012, phase_c_a=0.0, bus_v=12.0, bus_valid=True,
        trip_armed=True, legacy_trip_valid=True, trip_valid=1, legacy_trip_active=False,
        legacy_trip_causes=0, bdtr=0x03003400, af1=9, af2=23, power_key=0,
        bie=True, break_flags=0, comp_mask=0, dac_codes=[2065, 345, 345],
        comp_csr=[0x41, 0x41, 0x51, 0x41], dac_cr=[1, 0x10001], dac_mcr=[3, 0x30003],
        result=0, stop_reason=0, fast_tick_count=1000, uw_tick=50)
    state.update({field: 0 for field in probe.COUNTERS})
    state.update({"current.acquisition_valid": True, "current.measured_phases": 6,
        "trip.condition_active": False, "trip.causes": 0, "bus.acquisition_valid": True,
        "bus.voltage_v": 12.0, "current.observation_time": 1000, "bus.observation_time": 990})
    return state


def observed(before, source, sequence=1):
    command, comp, flag, cause, channel = probe.MODES[source]
    after = deepcopy(before)
    after.update(command_sequence=sequence, active_request_sequence=sequence, mailbox_sequence=sequence,
        result=11, stop_reason=11, probe_sequence=4, probe_request=sequence, probe_outcome=2, probe_held=1,
        legacy_trip_active=True, legacy_trip_causes=cause, comp_mask=comp, break_flags=flag, bie=False)
    after["trip.condition_active"] = True
    after["trip.causes"] = cause
    after["dac_codes"][channel] = 0
    for name in ["stage_attempts", "stage_successes"]:
        after[name] += 5
    for name in ["enable_attempts", "enable_successes"]:
        after[name] += 1
    after["guard_irq_count"] += 4
    capture = dict(sequence=4, request_sequence=sequence, source=command - 4, outcome=2, held=1,
        original_code=[2065, 345, 345][channel], expected_comp_mask=comp, expected_break_flag=flag,
        bdtr_before=before["bdtr"] | 0x8000, bdtr_after=before["bdtr"], sr_before=0, sr_after=flag,
        comp_before=0, comp_after=comp, start_cycles=1000, elapsed_cycles=170, poll_count=1)
    return after, capture


class FakeSession:
    def __init__(self, state, capture=None):
        self.state = deepcopy(state)
        self.capture = deepcopy(capture or dict.fromkeys(probe.PROBE_FIELDS, 0))
        self.layout = {"request": 0x2000089C}
        self.last_sequence = state["mailbox_sequence"]
        self.write_attempted = False
        self.calls = []

    def read(self, tag):
        self.state["fast_tick_count"] += 20
        self.state["uw_tick"] += 1
        self.state["current.observation_time"] += 20
        self.state["bus.observation_time"] += 20
        self.last_sequence = self.state["mailbox_sequence"]
        return deepcopy(self.state), {"probe": struct.pack("<17I", *(self.capture[k] for k in probe.PROBE_FIELDS))}

    def execute(self, tag, arguments):
        command, key, sequence = int(arguments[2], 0), int(arguments[3], 0), int(arguments[-1], 0)
        self.calls.append((command, key, sequence))
        if command in [5, 6, 7]:
            assert key == probe.KEY
            source = list(probe.MODES)[command - 5]
            self.state, self.capture = observed(self.state, source, sequence)
            return
        assert key == 0
        self.state.update(command_sequence=sequence, mailbox_sequence=sequence)
        if command == 9:
            if self.capture["held"]:
                self.state.update(result=15, stop_reason=15)
            else:
                self.state.update(result=1, stop_reason=1, bie=True, break_flags=0, legacy_trip_causes=0)
                self.state["trip.causes"] = 0
        elif command == 8:
            self.capture["held"] = 0
            self.capture["sequence"] += 2
            self.state.update(result=1, stop_reason=1, probe_held=0, probe_sequence=self.capture["sequence"],
                comp_mask=0, legacy_trip_active=False, dac_codes=[2065, 345, 345])
            self.state["trip.condition_active"] = False
        elif command == 2:
            self.state["result"] = 11
        else:
            raise AssertionError("Unexpected test command")


def anchored(state, result="POWERED_STATIC_PASS", capture=None):
    previous = {"hex_sha256": probe.common.EXPECTED, "result": result, "final": deepcopy(state)}
    if capture is not None:
        previous.update(capture=deepcopy(capture), scope_qualified=True, scope_evidence="synthetic host fixture")
    return previous


for source in probe.MODES:
    before = healthy()
    session = FakeSession(before)
    previous = anchored(before)
    read_only = probe.execute(session, previous, "preflight", source)
    assert not session.calls and read_only["result"] == "PROBE_READ_ONLY_PREFLIGHT_PASS"
    assert read_only["capture"] == session.capture
    read_only["hex_sha256"] = probe.common.EXPECTED
    cases += 1
    result = probe.execute(session, read_only, "execute", source)
    assert session.calls == [(probe.MODES[source][0], probe.KEY, 1)]
    assert result["result"] == "PROBE_OBSERVED_OFF_SCOPE_PENDING" and not result["scope_qualified"]
    cases += 1
    result["hex_sha256"] = probe.common.EXPECTED
    denied = FakeSession(result["final"], result["capture"])
    rejected(lambda: probe.execute(denied, result, "held-rearm", source))
    assert not denied.calls
    result["scope_qualified"] = True
    result["scope_evidence"] = "synthetic host fixture"
    for action in ["held-rearm", "clear", "verify-sticky", "rearm"]:
        result = probe.execute(session, result, action, source)
        result["hex_sha256"] = probe.common.EXPECTED
        assert session.calls[-1][1] == 0
        assert result["final"]["enable_attempts"] == result["final"]["enable_successes"] == 1
        assert not result["final"]["moe"]
        cases += 1
    assert result["result"] == "RECOVERY_REARM_PASS_OFF"
    assert [item[0] for item in session.calls] == [probe.MODES[source][0], 9, 8, 2, 9]
    stale = FakeSession(result["final"], result["capture"])
    rejected(lambda: probe.execute(stale, previous, "execute", source))
    assert not stale.calls


def startup_session():
    state = healthy()
    state["pwm"] = {"center_aligned_mode": 1, "clock_division": 0, "prescaler": 0,
        "period_counts": 4250, "repetition": 3, "update_irq_enabled": True, "output_config": 0x1555,
        "sample_compare": 4240, "timer_deadtime_code": 0,
        "channels": [{"mode": mode, "selection": 0, "preload": True} for mode in [6, 6, 6, 7]]}
    return FakeSession(state)


def cli_summary(module, session, output, arguments):
    def fake_session(path):
        path.mkdir(exist_ok=True)
        session.out = path
        session.write_attempted = False
        return session

    owner = pwm if module is pwm else probe.common
    argv = [module.__name__ + ".py", "--output", str(output), *arguments]
    with patch.object(owner, "Session", side_effect=fake_session), patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
        module.main()
    path = output / "summary.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


# Exercise the actual CLI summary files, rather than constructing accepted result labels.
for source in probe.MODES:
    with TemporaryDirectory(prefix="g431-probe-chain-") as directory:
        folder = Path(directory)
        session = startup_session()
        fresh_path, fresh = cli_summary(pwm, session, folder / "startup",
            ["--duty-percent", "30", "--inspect-startup"])
        assert fresh["result"] == "PWM_FRESH_STARTUP_PASS" and not session.calls
        cases += 1
        ready_path, ready = cli_summary(probe, session, folder / "preflight",
            ["--previous-summary", str(fresh_path), "--source", source, "--preflight"])
        assert ready["result"] == "PROBE_READ_ONLY_PREFLIGHT_PASS" and not session.calls
        assert ready["capture"] == fresh["capture"] and ready["hex_sha256"] == fresh["hex_sha256"]
        cases += 1
        _, result = cli_summary(probe, session, folder / "execute",
            ["--previous-summary", str(ready_path), "--source", source, "--execute-once"])
        assert result["result"] == "PROBE_OBSERVED_OFF_SCOPE_PENDING"
        assert session.calls == [(probe.MODES[source][0], probe.KEY, 1)]
        cases += 1
        for old in [fresh, ready]:
            rejected(lambda: probe.execute(session, old, "execute", source))
            assert session.calls == [(probe.MODES[source][0], probe.KEY, 1)]

for previous in [fresh, ready]:
    for changes in [dict(hex_sha256="wrong"), dict(result="FAILED_NO_RETRY"),
                    dict(capture=None), dict(capture=dict(previous["capture"], source=1))]:
        session = startup_session()
        rejected(lambda: probe.execute(session, dict(previous, **changes), "execute", "ovp"))
        assert not session.calls
    for changes in [dict(command_sequence=1, mailbox_sequence=1),
                    dict(result=1, stop_reason=1), dict(stage_attempts=1, stage_successes=1),
                    dict(enable_attempts=1, enable_successes=1), dict(guard_irq_count=1),
                    dict(probe_sequence=2), dict(moe=True)]:
        session = startup_session()
        session.state.update(changes)
        rejected(lambda: probe.execute(session, previous, "execute", "ovp"))
        assert not session.calls

# A fresh-start label must not admit an already-used startup or a nonempty capture.
for nonempty_capture in [False, True]:
    session = startup_session()
    if nonempty_capture:
        session.capture["source"] = 1
    else:
        session.state.update(stage_attempts=1, stage_successes=1)
    invalid_fresh = anchored(session.state, "PWM_FRESH_STARTUP_PASS", session.capture)
    rejected(lambda: probe.execute(session, invalid_fresh, "preflight", "ovp"))
    assert not session.calls

for changes in [dict(moe=True), dict(bus_v=0.0), dict(bus_v=float("nan")), dict(core_lockup=True),
                dict(adc_irq=False), dict(high_inputs=0x100), dict(power_key=probe.KEY),
                dict(legacy_trip_causes=4), dict(probe_held=1), dict(bdtr=0x8000),
                {"bus.observation_time": 899}, {"bus.observation_time": 1001}, dict(af2=0),
                dict(dac_cr=[0, 0x10001]), dict(dac_mcr=[0, 0x30003]),
                dict(comp_csr=[0x41, 0x41, 0x41, 0x41]), dict(bdtr=0x01003400)]:
    state = dict(healthy(), **changes)
    session = FakeSession(state)
    rejected(lambda: probe.execute(session, anchored(state), "execute", "ovp"))
    assert not session.calls

before = healthy()
after, capture = observed(before, "ovp")
for changed_capture in [dict(bdtr_before=0), dict(bdtr_after=0x8000), dict(comp_before=4),
                        dict(comp_after=1), dict(sr_after=0), dict(source=2), dict(original_code=345),
                        dict(outcome=4), dict(poll_count=129), dict(held=0)]:
    rejected(lambda: probe.check_observed(before, after, dict(capture, **changed_capture), 1, "ovp"))
for changes in [dict(result=1), dict(enable_attempts=2), dict(stage_successes=6), dict(bie=True)]:
    rejected(lambda: probe.check_observed(before, dict(after, **changes), capture, 1, "ovp"))

for command, power in [(3, True), (8, True), (5, False), (0, False)]:
    session = FakeSession(healthy())
    rejected(lambda: probe.submit(session, command, "invalid", power))
    assert not session.calls

with TemporaryDirectory(prefix="g431-scope-reference-") as directory:
    folder = Path(directory)
    photo = folder / "scope.jpg"
    photo.write_bytes(b"synthetic host fixture, not electrical evidence")
    metadata_path = folder / "scope.json"
    metadata = {"scope_qualified": True, "observed": {"packet_end_visible": True,
                "low_after_packet_visible": True}, "sha256": hashlib.sha256(photo.read_bytes()).hexdigest()}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    old_after, old_capture = observed(healthy(), "ovp")
    reference = anchored(old_after, "PROBE_OBSERVED_OFF_SCOPE_PENDING", old_capture)
    reference.update(before=healthy(), sequence=1, scope_evidence=str(metadata_path))
    reference_path = folder / "qualified.json"
    reference_path.write_text(json.dumps(reference), encoding="utf-8")
    session = FakeSession(healthy())
    current = probe.execute(session, anchored(healthy()), "execute", "ovp")
    current["hex_sha256"] = probe.common.EXPECTED
    for action in ["held-rearm", "clear", "verify-sticky", "rearm"]:
        current = probe.execute(session, current, action, "ovp", reference_path)
        current["hex_sha256"] = probe.common.EXPECTED
        assert current["scope_qualified"] is False and current["scope_evidence"] is None
        assert current["recovery_scope_reference"]["kind"] == "prior_episode_same_image_source"
        assert session.calls[-1][1] == 0 and current["final"]["enable_successes"] == 1
        cases += 1
    for action in ["preflight", "execute"]:
        denied = FakeSession(healthy())
        rejected(lambda: probe.execute(denied, anchored(healthy()), action, "ovp", reference_path))
        assert not denied.calls
    for change in [dict(hex_sha256="wrong"), dict(scope_qualified=False), dict(sequence=2),
                   dict(capture=dict(old_capture, source=2)), dict(capture=dict(old_capture, bdtr_after=0x8000))]:
        reference_path.write_text(json.dumps(dict(reference, **change)), encoding="utf-8")
        denied = FakeSession(old_after, old_capture)
        pending = anchored(old_after, "PROBE_OBSERVED_OFF_SCOPE_PENDING", old_capture)
        pending["scope_qualified"] = False
        rejected(lambda: probe.execute(denied, pending, "held-rearm", "ovp", reference_path))
        assert not denied.calls
    reference_path.write_text(json.dumps(reference), encoding="utf-8")
    for change in [dict(scope_qualified=False), dict(observed={"packet_end_visible": False,
            "low_after_packet_visible": True}), dict(sha256="wrong")]:
        metadata_path.write_text(json.dumps(dict(metadata, **change)), encoding="utf-8")
        denied = FakeSession(old_after, old_capture)
        rejected(lambda: probe.execute(denied, pending, "held-rearm", "ovp", reference_path))
        assert not denied.calls
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    denied = FakeSession(old_after, old_capture)
    pending["recovery_scope_reference"] = {"summary_sha256": "changed"}
    rejected(lambda: probe.execute(denied, pending, "held-rearm", "ovp", reference_path))
    assert not denied.calls

print(f"PASS: {cases} host probe-tool safety cases; no hardware access.")
