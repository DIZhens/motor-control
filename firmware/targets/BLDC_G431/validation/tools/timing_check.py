"""G431 output-off timing injection through the real ADC consumer.

Requires an explicit candidate and an untouched logic-only startup (VBUS < 4 V).
Only fixed timing cases and SAFE_OFF may be submitted, always with zero payload.
No flash, reset, halt, power enable, automatic replay or timer-register writes.
"""
import argparse
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
import pwm_once as pwm

common = pwm.common
require = common.require
CASES = {10: "immediate", 11: "before_deadline", 12: "after_deadline",
         14: "cancel", 13: "cross_cycle"}
HEADER = ["sequence", "request_sequence", "case_id", "outcome", "wait_iterations",
          "elapsed_cycles", "stage_called", "stage_accepted", "second_stage_called", "second_stage_accepted"]
OBSERVATIONS = ["entry", "before", "after", "second_after", "cleanup"]
REGISTERS = ["cycles", "cnt", "cr1", "sr", "bdtr", "ccr1", "ccr2", "ccr3"]
FIELDS = HEADER + [point + "." + field for point in OBSERVATIONS for field in REGISTERS]
CAPTURE_SIZE = 200
MOE, DIR, UDIS = 0x8000, 0x10, 2
NEUTRAL, REQUEST = [2125, 2125, 2125], [1275, 1700, 2975]


def capture_layout(output):
    """Resolve every field offline before Session can access SWD."""
    expressions = ["&g_target_validation_timing", "sizeof(TimingProbeCapture)",
                   "&g_target_validation_timing_recovery_ticks",
                   "sizeof(g_target_validation_timing_recovery_ticks)"]
    for field in FIELDS:
        expressions += ["&((TimingProbeCapture*)0)->" + field,
                        "sizeof(((TimingProbeCapture*)0)->" + field + ")"]
    arguments = [str(common.GDB), "-batch", "-ex",
                 'file "' + (common.BUILD / "BLDC_control V1.axf").as_posix() + '"']
    for expression in expressions:
        arguments += ["-ex", "p/x (unsigned int)" + expression]
    process = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
    (output / "timing-layout-gdb.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    values = [int(value, 0) for value in re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", process.stdout, re.M)]
    require(process.returncode == 0 and len(values) == len(expressions), "Timing layout unavailable")
    address, size, recovery, recovery_size = values[:4]
    require(size == CAPTURE_SIZE and recovery_size == 4
            and 0x20000000 <= address <= 0x20008000 - size
            and 0x20000000 <= recovery <= 0x20008000 - 4
            and not (address <= recovery < address + size), "Unexpected timing object layout")
    for index, field in enumerate(FIELDS):
        require(values[4 + 2 * index:6 + 2 * index] == [index * 4, 4], "Timing field mismatch: " + field)
    layout = {"address": address, "size": size, "recovery": recovery}
    common.save(output / "timing-layout.json", layout)
    return layout


def decode_capture(raw):
    require(len(raw) == CAPTURE_SIZE, "Incomplete timing capture")
    fields = dict(zip(FIELDS, struct.unpack("<50I", raw)))
    require(fields["sequence"] % 2 == 0, "Timing capture still publishing")
    return fields


def ccr(capture, point):
    return [capture[point + ".ccr" + str(i)] for i in [1, 2, 3]]


def check_capture(capture, command, sequence):
    require(command in CASES, "Unknown timing case")
    require(capture["sequence"] != 0 and capture["request_sequence"] == sequence
            and capture["case_id"] == command and capture["outcome"] == 1, "Timing case did not pass")
    require(capture["stage_called"] == 1 and capture["stage_accepted"] == int(command in [10, 11, 14])
            and capture["second_stage_called"] == int(command == 14)
            and capture["second_stage_accepted"] == 0, "Unexpected stage calls/returns")
    for point in ["entry", "before", "after", "cleanup"] + (["second_after"] if command == 14 else []):
        require(not capture[point + ".bdtr"] & MOE and not capture[point + ".sr"] & 0x180,
                "MOE or Break observed at " + point)
        require(capture[point + ".cr1"] & 0x61 == 0x21 and not capture[point + ".cr1"] & UDIS,
                "Timer stopped, mode changed or UDIS held at " + point)
    require(ccr(capture, "cleanup") == NEUTRAL, "Cleanup did not restore neutral preload")
    require(0 < capture["elapsed_cycles"] < 17000 and 0 < capture["wait_iterations"] <= 4096,
            "Missing elapsed time or polling bound exceeded")
    before_count = capture["before.cnt"]
    require(capture["before.cr1"] & DIR, "Stage was not attempted in the downcount half")
    if command in [10, 11, 14]:
        require(before_count > 1700 and ccr(capture, "after") == REQUEST, "Accepted request preload mismatch")
        require((capture["before.cycles"] - capture["entry.cycles"]) & 0xFFFFFFFF
                < capture["entry.cnt"] - 1700, "Accepted request was not in the original half-cycle budget")
    if command == 11:
        require(2200 <= before_count <= 3000, "Before-deadline wait point was not reached")
    if command == 12:
        require(0 < before_count <= 1600
                and (capture["before.cycles"] - capture["entry.cycles"]) & 0xFFFFFFFF < capture["entry.cnt"],
                "After-deadline point was not reached in the original downcount half")
    if command in [12, 13]:
        require(ccr(capture, "after") == ccr(capture, "before"), "Rejected stage changed preload")
    if command == 13:
        elapsed = (capture["before.cycles"] - capture["entry.cycles"]) & 0xFFFFFFFF
        require(8500 <= elapsed < 17000 and 1700 < before_count <= capture["entry.cnt"],
                "Cross-cycle attempt did not reach a later valid-looking phase")
    if command == 14:
        require(ccr(capture, "second_after") == NEUTRAL, "Canceled request escaped neutral cleanup")
        require(capture["second_after.cr1"] & DIR and capture["second_after.cnt"] > 1700
                and (capture["second_after.cycles"] - capture["entry.cycles"]) & 0xFFFFFFFF
                < capture["entry.cnt"] - 1700, "Cancellation test also crossed the timing deadline")


class Session(pwm.Session):
    def execute(self, tag, arguments):
        if arguments and arguments[0] == "-u":
            require(len(arguments) % 4 == 0 and all(arguments[i] == "-u" for i in range(0, len(arguments), 4)),
                    "Only block reads are permitted")
        else:
            require(len(arguments) == 9 and arguments[0] == arguments[6] == "-w32"
                    and int(arguments[1], 0) == self.layout["request"] + 4
                    and int(arguments[7], 0) == self.layout["request"]
                    and int(arguments[2], 0) in [1, *CASES]
                    and all(int(value, 0) == 0 for value in arguments[3:6])
                    and 0 < int(arguments[8], 0) <= 0xFFFFFFFF,
                    "Timing tool only permits fixed zero-payload timing/off commands")
        super().execute(tag, arguments)

    def submit_timing(self, command, tag):
        require(command in [1, *CASES] and self.last_sequence is not None, "Read state before fixed timing/off command")
        sequence = ((self.last_sequence + 1) & 0xFFFFFFFF) or 1
        self.last_sequence = sequence
        self.write_attempted = True
        self.execute(tag, ["-w32", hex(self.layout["request"] + 4), hex(command), "0x0", "0x0", "0x0",
                           "-w32", hex(self.layout["request"]), hex(sequence)])
        return sequence


def read_timing(session, layout, tag):
    address = layout["address"]
    blocks = session.upload(tag, [("seq-before", address, 4), ("timing", address, CAPTURE_SIZE),
                                 ("seq-after", address, 4), ("recovery", layout["recovery"], 4)])
    capture = decode_capture(blocks["timing"])
    require(common.word(blocks["seq-before"]) == capture["sequence"] == common.word(blocks["seq-after"]),
            "Timing publication changed during read")
    require(common.word(blocks["recovery"]) == 2, "Two normal sampling intervals have not recovered")
    common.save(session.out / (tag + ".json"), capture)
    return capture


def healthy_pair(session, tag):
    first, _ = session.read(tag + "-1")
    second, _ = session.read(tag + "-2")
    for state in [first, second]:
        common.check_logic_idle(state)
        # 此工具的捕获 ABI/截止判定仅适用于历史 RCR=0，拒绝新 slot 配置且不发请求。
        pwm.check_configuration(state, expected_repetition=0)
        require(state["bus_v"] >= 0.0, "Timing checks require a nonnegative bus observation")
    require(second["fast_tick_count"] != first["fast_tick_count"] and second["uw_tick"] != first["uw_tick"],
            "Sampling or SysTick stopped")
    return first, second


def run(session, layout, inspect_only=False):
    first, before = healthy_pair(session, "startup")
    for state in [first, before]:
        require(all(state[key] == 0 for key in ["command_sequence", "active_request_sequence", "result", "stop_reason",
                                              "stage_attempts", "stage_successes"]), "Timing suite requires untouched startup")
    initial_capture = read_timing(session, layout, "startup-timing")
    require(all(value == 0 for value in initial_capture.values()), "Timing case already executed this boot")
    if inspect_only:
        return {"result": "TIMING_READ_ONLY_PASS", "before": before, "final": before}
    reports = []
    for command, name in CASES.items():
        sequence = session.submit_timing(command, "submit-" + name)
        first, after = healthy_pair(session, name + "-after")
        capture = read_timing(session, layout, name + "-capture")
        check_capture(capture, command, sequence)
        for state in [first, after]:
            require(state["command_sequence"] == state["active_request_sequence"] == sequence
                    and state["result"] == state["stop_reason"] == 1, "Timing request did not finish COMPLETE")
            require([state["mailbox_command"], state["mailbox_duration_us"], state["mailbox_duty_bits"]]
                    == [command, 0, 0], "Timing request payload changed")
            require(state["stage_attempts"] - before["stage_attempts"]
                    == capture["stage_called"] + capture["second_stage_called"]
                    and state["stage_successes"] - before["stage_successes"]
                    == capture["stage_accepted"] + capture["second_stage_accepted"], "Unexpected stage counters")
        report = {"case": name, "command": command, "sequence": sequence, "capture": capture,
                  "before": before, "final": after}
        common.save(session.out / (name + "-summary.json"), report)
        reports.append(report)
        before = after
    first, final = healthy_pair(session, "final-no-replay")
    require(read_timing(session, layout, "final-timing") == reports[-1]["capture"], "Timing case replayed")
    for state in [first, final]:
        require(all(state[key] == before[key] for key in ["command_sequence", "active_request_sequence",
                "stage_attempts", "stage_successes", "result", "stop_reason"]), "Transaction changed after completion")
    return {"result": "TIMING_SUITE_PASS_OUTPUT_OFF", "cases": reports, "final": final,
            "power_enable_calls": 0, "electrical_update_boundary_qualified": False,
            "note": "CCR reads are preload evidence; cross-cycle injection can disturb one ADC callback interval."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inspect-only", action="store_true")
    common.add_candidate_arguments(parser)
    args = parser.parse_args()
    require(args.candidate_manifest is not None, "Timing checks require an explicit candidate manifest")
    common.configure_candidate(args)
    require(not args.output.exists(), "Output exists; refusing evidence overwrite")
    args.output.mkdir()
    session = None
    try:
        common.verify_source_freeze(args.output)
        layout = capture_layout(args.output)
        session = Session(args.output)
        report = run(session, layout, args.inspect_only)
    except Exception as error:
        report = {"result": "FAILED_NO_RETRY", "error": str(error),
                  "write_attempted": bool(session and session.write_attempted)}
        if session and session.write_attempted:
            try:
                session.submit_timing(1, "exception-safe-off-only")
                report["cleanup"], _ = session.read("exception-after-safe-off")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        common.save(args.output / "summary.json", report)
        raise
    report.update(hex_sha256=common.EXPECTED, programmed_this_run=False, automatic_retry=False,
                  candidate_manifest=str(args.candidate_manifest.resolve()))
    common.save(args.output / "summary.json", report)
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
