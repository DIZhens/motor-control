"""One explicitly selected G431 protection-probe action, anchored to prior evidence.

Default --preflight is read-only. --execute-once submits ONE 2ms/50% probe with
one-use key. Fault stays held. Each recovery command is a separate invocation;
no power retries, reset, halt, flash or automatic rearm. Operator/scope readiness
and interruptible countdown are coordinated by the operator/calling process.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode = True
import logic_check as common

KEY = 0x4310A11E
COUNTERS = ["command_sequence", "active_request_sequence", "mailbox_sequence", "stage_attempts",
            "stage_successes", "enable_attempts", "enable_successes", "guard_irq_count",
            "probe_sequence", "probe_request", "probe_outcome", "probe_held"]
PROBE_FIELDS = ["sequence", "request_sequence", "source", "outcome", "held", "original_code",
    "expected_comp_mask", "expected_break_flag", "bdtr_before", "sr_before", "comp_before",
    "bdtr_after", "sr_after", "comp_after", "start_cycles", "elapsed_cycles", "poll_count"]
MODES = {"ovp": (5, 4, 0x80, 4, 0), "ocp-c": (6, 1, 0x100, 2, 1), "ocp-ab": (7, 10, 0x100, 2, 2)}
require = common.require


def check_off(state):
    require(state["status"] == state["started"] == 1 and state["state"] == 0, "Not healthy OBSERVE")
    require(not state["halted"] and not state["core_lockup"] and state["drive_running"] and state["adc_irq"],
            "Core/ADC/timing stopped")
    require(not state["moe"] and state["high_inputs"] == state["low_inputs"] == 0, "Output not fully off")
    require(not state["guard_running"] and not state["guard_irq"], "Guard still active")
    require(state["invalid_current_count"] == state["legacy_invalid"] == state["moe_violations"] == 0,
            "Invalid sampling or unintended output")
    require(state["current.acquisition_valid"] and state["current.measured_phases"] == 6, "Incomplete B/C")
    require(all(math.isfinite(state[field]) for field in ["phase_b_a", "phase_c_a", "bus_v", "bus.voltage_v"]),
            "Non-finite measurement")
    require(state["bus_valid"] and state["bus.acquisition_valid"] and 10.0 <= state["bus_v"] <= 14.0
            and 10.0 <= state["bus.voltage_v"] <= 14.0, "Bus outside 10..14 V")
    age = state["current.observation_time"] - state["bus.observation_time"]
    require(0 <= age <= 100, "Bus diagnostic stale/inconsistent; no power action")
    require(state["trip_armed"] and state["legacy_trip_valid"] and state["trip_valid"] == 1, "Trip snapshot invalid")
    require(state["bdtr"] & 0x01001000 == 0x01001000 and not state["bdtr"] & 0x4000
            and state["bdtr"] & 0x400, "Break/AOE/idle configuration mismatch")
    require(state["power_key"] == 0 and state["mailbox_sequence"] == state["command_sequence"], "Pending command/key")


def check_healthy(state):
    check_off(state)
    require(not state["trip.condition_active"] and not state["legacy_trip_active"]
            and state["trip.causes"] == state["legacy_trip_causes"] == 0, "Trip not retired")
    require(state["bie"] and state["break_flags"] == state["comp_mask"] == 0, "Break not healthy")
    require(state["probe_held"] == 0 and state["dac_codes"] == [2065, 345, 345], "Threshold/injection mismatch")
    require(state["enable_attempts"] == state["enable_successes"], "Earlier failed enable")
    require(state["af1"] & 0x1008 == 8 and state["af2"] & 0x2C16 == 0x16, "COMP Break route/polarity mismatch")
    require(state["bdtr"] & 0x03FF7000 == 0x03003000, "Break polarity/filter mismatch")
    require([value & 0x3881F1 for value in state["comp_csr"]] == [0x41, 0x41, 0x51, 0x41],
            "Comparator input/enable configuration mismatch")
    require(state["dac_cr"][0] & 3 == 1 and state["dac_cr"][1] & 0x30003 == 0x10001,
            "DAC enable/trigger configuration mismatch")
    require(state["dac_mcr"][0] & 7 == 3 and state["dac_mcr"][1] & 0x70007 == 0x30003,
            "DAC mode configuration mismatch")


def check_anchor(state, previous):
    require(previous["hex_sha256"] == common.EXPECTED, "Previous evidence belongs to another image")
    old = previous["final"]
    require(all(state[field] == old[field] for field in COUNTERS), "Transaction changed; refusing replay")
    require(state["result"] == old["result"] and state["stop_reason"] == old["stop_reason"], "Previous result changed")


def read_probe(data):
    require(len(data) == 68, "Unexpected probe layout")
    capture = dict(zip(PROBE_FIELDS, struct.unpack("<17I", data)))
    require(capture["sequence"] % 2 == 0, "Probe publication still in progress")
    return capture


def check_held(state, capture):
    check_off(state)
    require(capture["source"] in [1, 2, 3] and capture["outcome"] == 2 and capture["held"] == 1,
            "No successful held probe")
    mode = list(MODES.values())[capture["source"] - 1]
    _, comp, flag, cause, channel = mode
    require(state["probe_request"] == capture["request_sequence"] and state["probe_held"] == 1,
            "Held probe identity mismatch")
    require(state["trip.condition_active"] and state["legacy_trip_active"]
            and state["trip.causes"] == state["legacy_trip_causes"] == cause, "Expected held trip missing")
    require(state["comp_mask"] == comp and state["break_flags"] == flag and not state["bie"], "Held source/notification mismatch")
    codes = [2065, 345, 345]
    codes[channel] = 0
    require(state["dac_codes"] == codes, "Injection threshold not held")


def check_observed(before, after, capture, sequence, source):
    command, comp, flag, _, channel = MODES[source]
    check_held(after, capture)
    require(after["command_sequence"] == after["active_request_sequence"] == sequence
            and after["result"] == after["stop_reason"] == 11, "Wrong request or stop reason")
    require(capture["request_sequence"] == sequence and capture["source"] == command - 4, "Wrong captured request/source")
    require(capture["bdtr_before"] & 0x8000 and not capture["bdtr_after"] & 0x8000,
            "Hardware MOE transition not captured before software off")
    require(capture["comp_before"] == 0 and capture["sr_before"] & 0x180 == 0, "Fault already existed before injection")
    require(capture["comp_after"] == capture["expected_comp_mask"] == comp
            and capture["sr_after"] & 0x180 == capture["expected_break_flag"] == flag, "Wrong comparator/Break evidence")
    require(capture["original_code"] == [2065, 345, 345][channel] and 1 <= capture["poll_count"] <= 128,
            "Unexpected threshold/poll evidence")
    require(after["enable_attempts"] - before["enable_attempts"] == 1
            and after["enable_successes"] - before["enable_successes"] == 1, "Expected exactly one enable")
    require(after["stage_attempts"] - before["stage_attempts"] == 5
            and after["stage_successes"] - before["stage_successes"] == 5, "Unexpected probe staging")
    require(after["guard_irq_count"] > before["guard_irq_count"], "No guard IRQ observed")


def submit(session, command, tag, power=False):
    require((power and command in [5, 6, 7]) or (not power and command in [1, 2, 8, 9]), "Invalid operation")
    require(session.last_sequence is not None, "Read mailbox first")
    sequence = ((session.last_sequence + 1) & 0xFFFFFFFF) or 1
    session.last_sequence = sequence
    session.write_attempted = True
    session.execute(tag, ["-w32", hex(session.layout["request"] + 4), hex(command), hex(KEY if power else 0),
                          "0x7d0", "0x3f000000", "-w32", hex(session.layout["request"]), hex(sequence)])
    return sequence


def stable_pair(session, tag):
    first, data1 = session.read(tag + "-1")
    second, data2 = session.read(tag + "-2")
    require(all(first[field] == second[field] for field in COUNTERS), "Transaction changed during read")
    require(first["result"] == second["result"] and first["stop_reason"] == second["stop_reason"], "Result changed")
    require(first["fast_tick_count"] != second["fast_tick_count"] and first["uw_tick"] != second["uw_tick"],
            "Sampling/SysTick not advancing")
    ticks = (second["uw_tick"] - first["uw_tick"]) & 0xFFFFFFFF
    fast = (second["fast_tick_count"] - first["fast_tick_count"]) & 0xFFFFFFFF
    require(0 < ticks < 30000 and 18.0 < fast / ticks < 22.0, "Sampling/SysTick ratio changed or reset during read")
    require(data1["probe"] == data2["probe"], "Unstable probe capture")
    check_off(first)
    check_off(second)
    return first, second, read_probe(data2["probe"])


def scope_reference(path, capture):
    """Qualify a prior packet for recovery planning, never this episode's scope."""
    reference = json.loads(path.read_text(encoding="utf-8"))
    require(reference.get("hex_sha256") == common.EXPECTED and reference.get("scope_qualified") is True,
            "Prior Gate evidence not qualified for this image")
    old = reference["capture"]
    require(old["source"] == capture["source"] and old["source"] in [1, 2, 3], "Prior Gate source differs")
    check_observed(reference["before"], reference["final"], old, reference["sequence"],
                   list(MODES)[old["source"] - 1])
    metadata_path = Path(reference["scope_evidence"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    require(metadata.get("scope_qualified") is True and metadata["observed"].get("packet_end_visible") is True
            and metadata["observed"].get("low_after_packet_visible") is True, "Prior Gate packet end missing")
    photo = metadata_path.with_suffix(".jpg")
    require(hashlib.sha256(photo.read_bytes()).hexdigest() == metadata["sha256"], "Prior Gate photo changed")
    return {"kind": "prior_episode_same_image_source", "summary": str(path.resolve()),
            "summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "scope_evidence": str(metadata_path.resolve()), "photo_sha256": metadata["sha256"],
            "current_episode_scope_qualified": False}


def execute(session, previous, action, source, recovery_scope_reference=None):
    require(recovery_scope_reference is None or action in ["held-rearm", "clear", "verify-sticky", "rearm"],
            "Prior scope reference is only allowed for output-off recovery")
    first, before, capture = stable_pair(session, "before")
    check_anchor(first, previous)
    check_anchor(before, previous)
    if action in ["preflight", "execute"]:
        require(previous["result"] in ["POWERED_STATIC_PASS", "RECOVERY_REARM_PASS_OFF",
                "PWM_FRESH_STARTUP_PASS", "PROBE_READ_ONLY_PREFLIGHT_PASS"], "Not a qualified off-state anchor")
        check_healthy(first)
        check_healthy(before)
        if previous["result"] in ["PWM_FRESH_STARTUP_PASS", "PROBE_READ_ONLY_PREFLIGHT_PASS"]:
            require(capture == previous.get("capture"), "Previous probe evidence changed or missing")
        if previous["result"] == "PWM_FRESH_STARTUP_PASS":
            for state in [first, before]:
                require(all(state[field] == 0 for field in COUNTERS)
                        and state["result"] == state["stop_reason"] == 0, "Not a fresh zero-transaction startup")
            require(all(value == 0 for value in capture.values()), "Fresh startup capture is not empty")
        if action == "preflight":
            return {"result": "PROBE_READ_ONLY_PREFLIGHT_PASS", "before": first, "final": before, "capture": capture}
        sequence = submit(session, MODES[source][0], "submit-probe-once", power=True)
        _, after, observed = stable_pair(session, "after")
        check_observed(before, after, observed, sequence, source)
        return {"result": "PROBE_OBSERVED_OFF_SCOPE_PENDING", "sequence": sequence,
                "source": source, "before": before, "final": after, "capture": observed,
                "scope_qualified": False, "shutdown_latency_qualified": False}
    require(previous["result"] in ["PROBE_OBSERVED_OFF_SCOPE_PENDING", "RECOVERY_HELD_REARM_REJECTED_OFF",
            "RECOVERY_CLEAR_PASS_STICKY_OFF", "RECOVERY_STICKY_STAGE_REJECTED_OFF"], "Invalid recovery predecessor")
    basis = None
    if recovery_scope_reference is not None:
        require(previous.get("scope_qualified") is not True, "Current episode already has its own Gate evidence")
        basis = scope_reference(recovery_scope_reference, capture)
        if previous.get("recovery_scope_reference") is not None:
            require(basis == previous["recovery_scope_reference"], "Recovery scope reference changed")
    else:
        require(previous.get("scope_qualified") is True, "Operator Gate waveform confirmation not recorded")
    old_capture = previous["capture"]
    require(capture == old_capture, "Captured evidence changed")
    if action in ["held-rearm", "clear"]:
        require(previous["result"] == ("PROBE_OBSERVED_OFF_SCOPE_PENDING" if action == "held-rearm"
                else "RECOVERY_HELD_REARM_REJECTED_OFF"), "Recovery order mismatch")
        check_held(first, capture)
        check_held(before, capture)
        sequence = submit(session, 9 if action == "held-rearm" else 8, "submit-" + action)
    else:
        require(previous["result"] == ("RECOVERY_CLEAR_PASS_STICKY_OFF" if action == "verify-sticky"
                else "RECOVERY_STICKY_STAGE_REJECTED_OFF"), "Recovery order mismatch")
        check_cleared(first, capture)
        check_cleared(before, capture)
        sequence = submit(session, 2 if action == "verify-sticky" else 9, "submit-" + action)
    _, after, updated = stable_pair(session, "after")
    check_off(after)
    require(after["command_sequence"] == sequence and after["active_request_sequence"] == before["active_request_sequence"],
            "Recovery request identity mismatch")
    for field in ["stage_attempts", "stage_successes", "enable_attempts", "enable_successes", "guard_irq_count"]:
        require(after[field] == before[field], "Recovery caused actuation/guard activity: " + field)
    if action == "held-rearm":
        check_held(after, updated)
        require(after["result"] == 15 and updated == capture, "Active fault rearm was not rejected")
        result = "RECOVERY_HELD_REARM_REJECTED_OFF"
    elif action == "clear":
        check_cleared(after, updated)
        require(after["result"] == 1 and updated["sequence"] == capture["sequence"] + 2, "Clear not published")
        require(all(updated[k] == capture[k] for k in PROBE_FIELDS if k not in ["sequence", "held"]), "Clear altered original evidence")
        result = "RECOVERY_CLEAR_PASS_STICKY_OFF"
    elif action == "verify-sticky":
        check_cleared(after, updated)
        require(after["result"] == 11 and updated == capture, "Sticky fault did not reject STAGE_ONLY")
        result = "RECOVERY_STICKY_STAGE_REJECTED_OFF"
    else:
        check_healthy(after)
        require(after["result"] == 1 and updated == capture, "Explicit rearm failed")
        result = "RECOVERY_REARM_PASS_OFF"
    return {"result": result, "sequence": sequence, "before": before, "final": after,
            "capture": updated, "scope_qualified": previous.get("scope_qualified") is True,
            "scope_evidence": previous.get("scope_evidence"), "recovery_scope_reference": basis,
            "shutdown_latency_qualified": False}


def check_cleared(state, capture):
    check_off(state)
    require(capture["source"] in [1, 2, 3] and capture["outcome"] == 2, "Invalid original probe evidence")
    cause = list(MODES.values())[capture["source"] - 1][3]
    require(capture["held"] == state["probe_held"] == 0 and state["comp_mask"] == 0
            and not state["trip.condition_active"] and not state["legacy_trip_active"], "Injection not cleared")
    require(state["dac_codes"] == [2065, 345, 345] and not state["bie"]
            and state["trip.causes"] == state["legacy_trip_causes"] == cause, "Sticky episode already retired/missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-summary", type=Path, required=True)
    parser.add_argument("--source", choices=MODES, default="ovp")
    parser.add_argument("--recovery-scope-reference", type=Path,
                        help="Qualified prior same-image/source packet; recovery only, leaves this episode scope unqualified")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute-once", action="store_true")
    mode.add_argument("--held-rearm", action="store_true")
    mode.add_argument("--clear", action="store_true")
    mode.add_argument("--verify-sticky", action="store_true")
    mode.add_argument("--rearm", action="store_true")
    common.add_candidate_arguments(parser)
    args = parser.parse_args()
    common.configure_candidate(args)
    previous = json.loads(args.previous_summary.read_text(encoding="utf-8"))
    require(previous.get("hex_sha256") == common.EXPECTED, "Previous evidence belongs to another image")
    require(not args.output.exists(), "Output directory already exists; refusing overwrite/replay")
    session = common.Session(args.output)
    action = "execute" if args.execute_once else next(name for name in ["preflight", "held-rearm", "clear", "verify-sticky", "rearm"]
            if getattr(args, name.replace("-", "_")))
    try:
        report = execute(session, previous, action, args.source, args.recovery_scope_reference)
        report.update(hex_sha256=common.EXPECTED, previous_evidence=str(args.previous_summary.resolve()),
                      action=action, source=(list(MODES)[report["capture"]["source"] - 1]
                          if "capture" in report else args.source), automatic_retry=False)
    except Exception as error:
        report = {"result": "FAILED_NO_RETRY", "error": str(error), "write_attempted": session.write_attempted}
        if session.write_attempted:
            try:
                submit(session, 1, "exception-safe-off-only")
                report["cleanup"], _ = session.read("exception-after-safe-off")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        common.save(session.out / "summary.json", report)
        raise
    common.save(session.out / "summary.json", report)
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
