"""Output-off B/C statistics on the reviewed G431 image, with a 12 V supply.

Only CAPTURE_CURRENT (4, key=0) and failure cleanup SAFE_OFF (1) may be written.
No reset, flash, PWM, threshold change or automatic retry. Statistics are nominal
amperes; successful acquisition does not establish offset/gain calibration.
"""
import argparse
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import pwm_once as pwm

common = pwm.common
checks = pwm.checks
require = common.require
FIELDS = {"sequence": "I", **{"statistics." + name: "I" for name in [
    "request_sequence", "result", "sample_count", "interval_min_cycles", "interval_max_cycles"]},
    "statistics.first_sample_time": "Q", "statistics.last_sample_time": "Q",
    **{"statistics.phase_" + phase + "." + name: "f" for phase in ["b", "c"]
       for name in ["mean_a", "min_a", "max_a", "variance_a2"]}}
UNCHANGED = ["stage_attempts", "stage_successes", "enable_attempts", "enable_successes"]


def capture_layout(output):
    specs = ["sizeof(TargetValidationCurrentCapture)"]
    for field in FIELDS:
        specs += ["&((TargetValidationCurrentCapture*)0)->" + field,
                  "sizeof(((TargetValidationCurrentCapture*)0)->" + field + ")"]
    args = [str(common.GDB), "-batch", "-ex",
            'file "' + (common.BUILD / "BLDC_control V1.axf").as_posix() + '"']
    for expr in specs:
        args += ["-ex", "p/x (unsigned int)" + expr]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=20)
    (output / "current-layout-gdb.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    values = [int(v, 0) for v in re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", proc.stdout, re.M)]
    require(proc.returncode == 0 and len(values) == len(specs) and values[0] == 80,
            "Current capture layout unavailable")
    layout = {}
    for i, (field, fmt) in enumerate(FIELDS.items()):
        offset, size = values[1 + 2*i:3 + 2*i]
        require(size == struct.calcsize(fmt) and offset + size <= 80, "Current field layout mismatch: " + field)
        layout[field] = offset
    common.save(output / "current-layout.json", layout)
    return layout


def decode_capture(data, layout, request):
    require(len(data) == 80, "Incomplete current capture")
    fields = {name: struct.unpack_from("<" + fmt, data, layout[name])[0] for name, fmt in FIELDS.items()}
    require(fields["sequence"] % 2 == 0, "Current capture is being published")
    stats = {name.removeprefix("statistics."): value for name, value in fields.items() if name != "sequence"}
    require(stats["request_sequence"] == request and stats["result"] == 1
            and stats["sample_count"] == 4096, "Wrong or partial current window")
    require((stats["last_sample_time"] - stats["first_sample_time"]) % (1 << 64) == 4095,
            "Non-contiguous ControlTime window")
    require(4250 <= stats["interval_min_cycles"] <= stats["interval_max_cycles"] <= 12750,
            "Consumer interval outside 25..75 us")
    for phase in ["b", "c"]:
        vals = {name: stats["phase_" + phase + "." + name] for name in ["mean_a", "min_a", "max_a", "variance_a2"]}
        require(all(math.isfinite(v) for v in vals.values()) and vals["variance_a2"] >= 0
                and vals["min_a"] <= vals["mean_a"] <= vals["max_a"], "Invalid phase statistics")
        stats["phase_" + phase + ".stddev_a"] = math.sqrt(vals["variance_a2"])
        stats["phase_" + phase + ".peak_to_peak_a"] = vals["max_a"] - vals["min_a"]
    return {"publication_sequence": fields["sequence"], **stats}


class Session(pwm.Session):
    def execute(self, tag, arguments):
        if "-w32" in arguments:
            require(len(arguments) == 9 and arguments[0] == arguments[6] == "-w32"
                    and int(arguments[1], 0) == self.layout["request"] + 4
                    and int(arguments[7], 0) == self.layout["request"]
                    and int(arguments[2], 0) in [1, 4] and int(arguments[3], 0) == 0,
                    "Current tool only allows capture/off with zero key")
        super().execute(tag, arguments)


def healthy_pair(session, tag):
    first, second, probe = checks.stable_pair(session, tag)
    for state in [first, second]:
        checks.check_healthy(state)
        pwm.check_configuration(state)
    return first, second, probe


def window(session, layout, index, previous=None):
    prefix = "window-" + str(index)
    first, before, probe = healthy_pair(session, prefix + "-before")
    if previous:
        for state in [first, before]:
            checks.check_anchor(state, previous)
        require(probe == previous["capture"], "Probe evidence changed between windows")
    request = session.submit_logic(4, prefix + "-submit-capture")
    time.sleep(0.3)
    first, after, updated = healthy_pair(session, prefix + "-after")
    for state in [first, after]:
        require(state["command_sequence"] == state["active_request_sequence"] == request
                and state["result"] == state["stop_reason"] == 1 and state["mailbox_command"] == 4,
                "Current request did not complete")
        require(all(state[k] == before[k] for k in UNCHANGED), "Unexpected stage/enable during capture")
        require(state["guard_irq_count"] != before["guard_irq_count"], "Capture guard did not run")
    require(updated == probe, "Current request changed protection evidence")
    blocks = [("capture", session.layout["capture"], 80)]
    raw1 = session.upload(prefix + "-latched-1", blocks)["capture"]
    raw2 = session.upload(prefix + "-latched-2", blocks)["capture"]
    require(raw1 == raw2, "Current capture changed during read")
    stats = decode_capture(raw1, layout, request)
    report = {"result": "CURRENT_WINDOW_COMPLETE_OUTPUT_OFF", "hex_sha256": common.EXPECTED,
              "before": before, "final": after, "capture": updated, "statistics": stats}
    common.save(session.out / (prefix + "-summary.json"), report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows", type=int, choices=range(1, 6), default=5)
    common.add_candidate_arguments(parser)
    args = parser.parse_args()
    common.configure_candidate(args)
    require(not args.output.exists(), "Output exists; refusing overwrite")
    args.output.mkdir()
    session = None
    reports = []
    try:
        common.verify_source_freeze(args.output)
        layout = capture_layout(args.output)
        session = Session(args.output)
        for index in range(1, args.windows + 1):
            report = window(session, layout, index, reports[-1] if reports else None)
            reports.append(report)
        first, final, probe = healthy_pair(session, "final-no-replay")
        for state in [first, final]:
            checks.check_anchor(state, reports[-1])
        require(probe == reports[-1]["capture"], "Final probe changed")
        report = {"result": "CURRENT_WINDOWS_COMPLETE_OUTPUT_OFF", "hex_sha256": common.EXPECTED,
                  "window_count": len(reports), "total_samples": 4096 * len(reports),
                  "powered_requests_this_run": 0, "calibration_applied": False, "final": final,
                  "windows": [r["statistics"] for r in reports]}
    except Exception as error:
        report = {"result": "FAILED_NO_RETRY", "error": str(error), "completed_windows": len(reports),
                  "write_attempted": bool(session and session.write_attempted)}
        if session and session.write_attempted:
            try:
                session.submit_logic(1, "exception-safe-off-only")
                report["cleanup"], _ = session.read("exception-after-safe-off")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        common.save(args.output / "summary.json", report)
        raise
    common.save(args.output / "summary.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
