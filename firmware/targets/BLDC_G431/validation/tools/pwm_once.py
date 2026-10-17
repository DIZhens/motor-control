"""One G431 no-motor PWM request: 30% or 70%, fixed total deadline 2000 us.

Read-only startup inspection/preflight and explicit one-shot execution are
separate modes. No loop, reset, halt, flash, fault injection or automatic rearm.
The operator/calling process coordinates scope readiness and the 8 s countdown.
"""
import argparse
import json
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode = True
import probe_once as checks

common = checks.common
require = common.require
DURATION_US = 2000
PREDECESSORS = {"POWERED_STATIC_PASS", "RECOVERY_REARM_PASS_OFF", "PROTECTION_ROUND_FINAL_OFF_PASS",
                "PWM_FRESH_STARTUP_PASS", "PWM_READ_ONLY_PREFLIGHT_PASS", "PWM_BURST_COMPLETE_OFF_SCOPE_PENDING"}


def decode_pwm(block):
    word = common.word
    ccmr = [word(block, 24), word(block, 28)]
    channels = []
    for index in range(4):
        value, shift = ccmr[index // 2], 8 * (index % 2)
        mode = ((value >> (shift + 4)) & 7) | (((value >> (shift + 16)) & 1) << 3)
        channels.append({"mode": mode, "selection": (value >> shift) & 3,
                         "preload": bool(value & (1 << (shift + 3)))})
    return {"center_aligned_mode": (word(block) >> 5) & 3,
            "clock_division": (word(block) >> 8) & 3, "prescaler": word(block, 40),
            "period_counts": word(block, 44), "repetition": word(block, 48),
            "channels": channels, "output_config": word(block, 32) & 0x3FFF,
            "sample_compare": word(block, 64), "timer_deadtime_code": word(block, 68) & 0xFF}


class Session(common.Session):
    def read(self, tag):
        state, blocks = super().read(tag)
        state.update(pwm=decode_pwm(blocks["tim1"]), mailbox_command=common.word(blocks["request"], 4),
                     mailbox_duration_us=common.word(blocks["request"], 12),
                     mailbox_duty_bits=common.word(blocks["request"], 16))
        common.save(self.out / (tag + "-state.json"), state)
        return state, blocks


def check_configuration(state):
    cfg = state["pwm"]
    require(cfg["center_aligned_mode"] == 1 and cfg["clock_division"] == cfg["prescaler"] == 0
            and cfg["period_counts"] == 4250 and cfg["repetition"] == 0, "Unexpected PWM timing configuration")
    require([item["mode"] for item in cfg["channels"]] == [6, 6, 6, 7]
            and all(item["selection"] == 0 and item["preload"] for item in cfg["channels"]),
            "Unexpected PWM mode/preload configuration")
    require(cfg["output_config"] == 0x1555 and cfg["sample_compare"] == 4240
            and cfg["timer_deadtime_code"] == 0, "Unexpected output polarity, ADC trigger or timer deadtime")


def duty_bits(percent):
    require(type(percent) is int and percent in [30, 70], "Only 30% or 70% is reviewed for this tool")
    return struct.unpack("<I", struct.pack("<f", percent / 100.0))[0]


def submit(session, percent):
    bits = duty_bits(percent)
    require(session.last_sequence is not None, "Read mailbox before submission")
    sequence = ((session.last_sequence + 1) & 0xFFFFFFFF) or 1
    session.last_sequence = sequence
    session.write_attempted = True
    session.execute("submit-pwm-once", ["-w32", hex(session.layout["request"] + 4), "0x3", hex(checks.KEY),
                    hex(DURATION_US), hex(bits), "-w32", hex(session.layout["request"]), hex(sequence)])
    return sequence


def run(session, previous, action, percent):
    require(action in ["inspect-startup", "preflight", "execute"], "Unknown PWM action")
    bits = duty_bits(percent)
    first, before, capture = checks.stable_pair(session, "before")
    for state in [first, before]:
        checks.check_healthy(state)
        check_configuration(state)
    if action == "inspect-startup":
        require(previous is None, "Fresh startup inspection must not bypass a supplied anchor")
        for state in [first, before]:
            require(all(state[field] == 0 for field in checks.COUNTERS)
                    and state["result"] == state["stop_reason"] == 0, "Not a fresh zero-transaction startup")
        require(all(value == 0 for value in capture.values()), "Fresh startup capture is not empty")
        return {"result": "PWM_FRESH_STARTUP_PASS", "before": first, "final": before, "capture": capture}
    require(previous is not None and previous.get("result") in PREDECESSORS, "Unqualified previous evidence")
    checks.check_anchor(first, previous)
    checks.check_anchor(before, previous)
    require(capture == previous["capture"], "Previous probe evidence changed")
    if action == "preflight":
        return {"result": "PWM_READ_ONLY_PREFLIGHT_PASS", "before": first, "final": before, "capture": capture}
    sequence = submit(session, percent)
    after_first, after, updated = checks.stable_pair(session, "after")
    for state in [after_first, after]:
        checks.check_healthy(state)
        check_configuration(state)
        require(state["command_sequence"] == state["active_request_sequence"] == sequence
                and state["result"] == state["stop_reason"] == 1, "PWM request did not finish COMPLETE")
        require([state["mailbox_command"], state["mailbox_duration_us"], state["mailbox_duty_bits"]]
                == [3, DURATION_US, bits], "Mailbox payload differs from submitted PWM request")
        require(state["enable_attempts"] - before["enable_attempts"] == 1
                and state["enable_successes"] - before["enable_successes"] == 1, "Expected exactly one enable")
        stage = state["stage_successes"] - before["stage_successes"]
        guard = state["guard_irq_count"] - before["guard_irq_count"]
        require(stage == state["stage_attempts"] - before["stage_attempts"] and 2 <= stage <= 42
                and 1 <= guard <= 42, "Unexpected staging/guard counters")
    require(updated == capture, "PWM request altered fault-injection evidence")
    return {"result": "PWM_BURST_COMPLETE_OFF_SCOPE_PENDING", "sequence": sequence,
            "before": before, "final": after, "capture": updated, "scope_qualified": False,
            "requested_common_duty_percent": percent, "mailbox_duty_bits": bits,
            "duration_us_including_staging": DURATION_US,
            "expected_nominal_period_us": 50, "expected_LA_high_us_before_driver_effects": (100 - percent) / 2,
            "note": "Counters are sanity checks, not physical packet-duration or Gate-delay measurements."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-summary", type=Path)
    parser.add_argument("--duty-percent", type=int, choices=[30, 70], required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect-startup", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute-once", action="store_true")
    args = parser.parse_args()
    action = "inspect-startup" if args.inspect_startup else "execute" if args.execute_once else "preflight"
    require((action == "inspect-startup") == (args.previous_summary is None),
            "Supply previous evidence for preflight/execution; inspect-startup is read-only and requires fresh startup")
    previous = json.loads(args.previous_summary.read_text(encoding="utf-8")) if args.previous_summary else None
    require(not args.output.exists(), "Output directory exists; refusing overwrite or replay")
    args.output.mkdir()
    session = None
    try:
        session = Session(args.output)
        report = run(session, previous, action, args.duty_percent)
        report.update(hex_sha256=common.EXPECTED, action=action, automatic_retry=False,
                      previous_evidence=str(args.previous_summary.resolve()) if args.previous_summary else None)
    except Exception as error:
        report = {"result": "FAILED_NO_RETRY", "error": str(error),
                  "write_attempted": session.write_attempted if session else False}
        if session is not None and session.write_attempted:
            try:
                checks.submit(session, 1, "exception-safe-off-only")
                report["cleanup"], _ = session.read("exception-after-safe-off")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        common.save(args.output / "summary.json", report)
        raise
    common.save(args.output / "summary.json", report)
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
