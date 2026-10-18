"""G431 probe-image logic-only commissioning. Never submits a power key.

Uses offline AXF type information and fresh HOTPLUG Flash verification. Does not
flash, reset, halt, rearm, mask ADC IRQs or enable power. Raw SWD diagnostic blocks
are not claimed to form one coherent ADC snapshot. Run with PSU OUTPUT OFF.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import time

TARGET = Path(__file__).resolve().parents[2]
BUILD = TARGET / "MDK-ARM/BLDC_control V1"
CLI = Path(r"D:\cube\cubeclt\STM32CubeCLT_1.22.0\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe")
GDB = CLI.parents[2] / "GNU-tools-for-STM32/bin/arm-none-eabi-gdb.exe"
EXPECTED = "29ab28611691dd220624708ecf826be9ea31f4c82753418dcc6be64d8f5facff"
DEFAULT_BUILD, DEFAULT_CLI, DEFAULT_GDB, DEFAULT_EXPECTED = BUILD, CLI, GDB, EXPECTED
CANDIDATE = None
SNAP_FIELDS = ["started", "state", "result", "command_sequence", "active_request_sequence",
               "fast_tick_count", "invalid_current_count", "interval_min_cycles", "interval_max_cycles",
               "callback_max_cycles", "stage_attempts", "stage_successes", "enable_attempts",
               "enable_successes", "capture_samples", "guard_irq_count", "trip_valid", "trip.causes",
               "trip.condition_active", "current.measured_phases", "current.acquisition_valid",
               "current.observation_time", "bus.observation_time", "bus.voltage_v", "bus.acquisition_valid"]


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def word(data, offset=0):
    return struct.unpack_from("<I", data, offset)[0]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def add_candidate_arguments(parser):
    parser.add_argument("--candidate-manifest", type=Path,
                        help="Explicit offline candidate identity; does not authorize a powered action")
    parser.add_argument("--cli", type=Path, help="STM32_Programmer_CLI executable")
    parser.add_argument("--gdb", type=Path, help="arm-none-eabi-gdb executable")


def configure_candidate(args):
    global BUILD, CLI, GDB, EXPECTED, CANDIDATE
    BUILD, CLI, GDB, EXPECTED = DEFAULT_BUILD, DEFAULT_CLI, DEFAULT_GDB, DEFAULT_EXPECTED
    CANDIDATE = args.candidate_manifest.resolve() if args.candidate_manifest else None
    CLI = args.cli.resolve() if args.cli else CLI
    GDB = args.gdb.resolve() if args.gdb else GDB
    if CANDIDATE:
        import candidate_manifest
        candidate, BUILD = candidate_manifest.load(CANDIDATE, TARGET)
        EXPECTED = candidate["files"]["BLDC_control V1.hex"]
    if CANDIDATE or args.cli or args.gdb:
        require(CLI.is_file() and GDB.is_file(), "Selected CLI/GDB executable missing")


def verify_source_freeze(output):
    if CANDIDATE:
        import candidate_manifest
        candidate, _ = candidate_manifest.load(CANDIDATE, TARGET)
        files = candidate["source_files"]
    else:
        freeze = json.loads((TARGET / "validation/local-evidence/20261017-duty-check/source-freeze.json").read_text())
        files = freeze["files"]
        for name, expected in files.items():
            require(hashlib.sha256((TARGET / name).read_bytes()).hexdigest() == expected,
                    "Frozen source changed: " + name)
    save(output / "source-verification.json", {"verified": files,
         "candidate_manifest": str(CANDIDATE) if CANDIDATE else None})


def offline_layout(build, gdb, log):
    spec = {"legacy": "&g_platform_bringup", "request": "&g_target_validation_request",
            "snapshot": "&g_target_validation", "capture": "&g_target_validation_capture",
            "probe": "&g_target_validation_probe", "stop": "&g_target_validation_stop_reason",
            "tick": "&uwTick", "request_size": "sizeof(TargetValidationMailbox)",
            "snapshot_size": "sizeof(TargetValidationSnapshot)", "probe_size": "sizeof(ProtectionProbeCapture)",
            "legacy_size": "sizeof(PlatformBringupDebug)",
            "capture_size": "sizeof(TargetValidationCurrentCapture)"}
    for field in SNAP_FIELDS:
        spec["s_" + field] = "&((TargetValidationSnapshot*)0)->" + field
        spec["z_" + field] = "sizeof(((TargetValidationSnapshot*)0)->" + field + ")"
    arguments = [str(gdb), "-batch", "-ex", 'file "' + (build / "BLDC_control V1.axf").as_posix() + '"']
    for expression in spec.values():
        arguments += ["-ex", "p/x (unsigned int)" + expression]
    process = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
    log.write_text(process.stdout + process.stderr, encoding="utf-8")
    values = re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", process.stdout, re.M)
    require(process.returncode == 0 and len(values) == len(spec), "Offline AXF layout unavailable")
    layout = dict(zip(spec, [int(value, 0) for value in values]))
    require([layout[k] for k in ["request_size", "snapshot_size", "probe_size", "legacy_size", "capture_size"]]
            == [20, 184, 68, 76, 80], "Unexpected layout sizes")
    return layout


def decode_snapshot(data, layout):
    state = {}
    for field in SNAP_FIELDS:
        offset, size = layout["s_" + field], layout["z_" + field]
        require(size in [1, 4, 8] and offset + size <= len(data), "Unexpected diagnostic field width: " + field)
        value = struct.unpack_from("<f", data, offset)[0] if field == "bus.voltage_v" else int.from_bytes(data[offset:offset + size], "little")
        state[field] = bool(value) if field in ["trip.condition_active", "current.acquisition_valid", "bus.acquisition_valid"] else value
    return state


class Session:
    def __init__(self, output):
        self.out = output.resolve()
        self.out.mkdir(exist_ok=True)
        self.last_sequence = None
        self.write_attempted = False
        candidate = None
        if CANDIDATE:
            import candidate_manifest
            candidate, selected_build = candidate_manifest.load(CANDIDATE, TARGET)
            require(selected_build == BUILD and candidate["files"]["BLDC_control V1.hex"] == EXPECTED,
                    "Candidate selection changed")
        require(hashlib.sha256((BUILD / "BLDC_control V1.hex").read_bytes()).hexdigest() == EXPECTED,
                "Selected HEX is not the reviewed probe candidate")
        self.layout = offline_layout(BUILD, GDB, self.out / "layout-gdb.log")
        manifest = candidate or json.loads((TARGET / "validation/local-evidence/20261015-protection-probe/BUILD_MANIFEST.json").read_text())
        for name in ["BLDC_control V1.axf", "BLDC_control V1.map"]:
            require(hashlib.sha256((BUILD / name).read_bytes()).hexdigest() == manifest["files"][name].lower(),
                    "AXF/MAP differs from reviewed build")
        if candidate:
            require(self.layout == candidate["layout"], "Candidate AXF layout differs from manifest")
            checked, checked_build = candidate_manifest.load(CANDIDATE, TARGET)
            require(checked == candidate and checked_build == BUILD, "Candidate changed during offline checks")
            save(self.out / "candidate-identity.json", {"manifest": str(CANDIDATE),
                 "manifest_sha256": hashlib.sha256(CANDIDATE.read_bytes()).hexdigest(),
                 "status": candidate["status"], "hardware_qualified": False})
        else:
            for name in ["legacy", "request", "snapshot", "probe", "stop"]:
                require(self.layout[name] == manifest["layout"][name], "Candidate layout differs: " + name)
        save(self.out / "layout.json", self.layout)
        import candidate_manifest
        image = candidate_manifest.read_hex(BUILD / "BLDC_control V1.hex")
        start, end = min(image), max(image) + 1
        require(0x08000000 <= start < end <= 0x08020000, "Unexpected Flash range")
        flash = self.upload("identity", [("flash", start, end - start)])["flash"]
        require(all(flash[a - start] == value for a, value in image.items()), "Live Flash differs from candidate")
        save(self.out / "identity.json", {"hex_sha256": EXPECTED, "bytes_matched": len(image),
                                         "source": "fresh HOTPLUG upload", "programmed_this_run": False})

    def execute(self, tag, arguments):
        require(not any(argument in ["-halt", "-rst", "-run", "-d", "-e", "-w"] for argument in arguments),
                "Forbidden diagnostic operation")
        process = subprocess.run([str(CLI), "-c", "port=SWD", "mode=HOTPLUG", "freq=4000",
                                  *arguments, "-score"], capture_output=True, timeout=20)
        output = (process.stdout + process.stderr).decode("utf-8", errors="replace")
        (self.out / (tag + ".log")).write_text(output, encoding="utf-8")
        require(process.returncode == 0 and "Error:" not in output, output)
        require(bool(re.search(r"Device ID\s*:\s*0x468\b", output)) and "Core is running" in output,
                "Wrong chip or core stopped")

    def upload(self, tag, blocks):
        arguments = []
        for name, address, size in blocks:
            arguments += ["-u", hex(address), str(size), str(self.out / (tag + "-" + name + ".bin"))]
        self.execute(tag, arguments)
        result = {name: (self.out / (tag + "-" + name + ".bin")).read_bytes() for name, _, _ in blocks}
        require(all(len(result[name]) == size for name, _, size in blocks), "Incomplete SWD upload")
        return result

    def read(self, tag):
        data = self.upload(tag, [("legacy", self.layout["legacy"], 76), ("request", self.layout["request"], 20),
            ("snapshot", self.layout["snapshot"], 184), ("capture", self.layout["capture"], 80),
            ("probe", self.layout["probe"], 68), ("stop", self.layout["stop"], 4),
            ("tick", self.layout["tick"], 4), ("tim1", 0x40012C00, 104), ("tim6", 0x40001000, 32),
            ("gpioa", 0x48000000, 40), ("gpiob", 0x48000400, 40), ("comp", 0x40010200, 16),
            ("dac1", 0x50000800, 64), ("dac3", 0x50001000, 64),
            ("dhcsr", 0xE000EDF0, 4), ("nvic", 0xE000E100, 4)])
        state = decode_snapshot(data["snapshot"], self.layout)
        legacy = data["legacy"]
        bdtr = word(data["tim1"], 68)
        state.update(status=word(legacy), stop_reason=word(data["stop"]), uw_tick=word(data["tick"]),
            legacy_invalid=word(legacy, 12), moe_violations=word(legacy, 72),
            bus_v=struct.unpack_from("<f", legacy, 44)[0], bus_valid=bool(legacy[48]),
            phase_b_a=struct.unpack_from("<f", legacy, 20)[0], phase_c_a=struct.unpack_from("<f", legacy, 24)[0],
            position_valid=bool(legacy[36]), trip_armed=bool(legacy[60]), legacy_trip_valid=bool(legacy[61]),
            legacy_trip_active=bool(legacy[62]), legacy_trip_causes=word(legacy, 64),
            bdtr=bdtr, moe=bool(bdtr & 0x8000), drive_running=bool(word(data["tim1"]) & 1),
            bie=bool(word(data["tim1"], 12) & 0x80), break_flags=word(data["tim1"], 16) & 0x180,
            high_inputs=word(data["gpioa"], 16) & 0x700, low_inputs=word(data["gpiob"], 16) & 0xE000,
            guard_running=bool(word(data["tim6"]) & 1), guard_irq=bool(word(data["tim6"], 12) & 1),
            halted=bool(word(data["dhcsr"]) & (1 << 17)), adc_irq=bool(word(data["nvic"]) & (1 << 18)),
            core_lockup=bool(word(data["dhcsr"]) & (1 << 19)),
            comp_mask=sum((1 << i) for i in range(4) if word(data["comp"], i * 4) & (1 << 30)),
            comp_csr=[word(data["comp"], i * 4) for i in range(4)],
            dac_cr=[word(data["dac1"]), word(data["dac3"])],
            dac_mcr=[word(data["dac1"], 60), word(data["dac3"], 60)],
            af1=word(data["tim1"], 96), af2=word(data["tim1"], 100),
            dac_codes=[word(data["dac1"], 8), word(data["dac3"], 8), word(data["dac3"], 20)],
            mailbox_sequence=word(data["request"]), power_key=word(data["request"], 8),
            probe_sequence=word(data["probe"]), probe_request=word(data["probe"], 4),
            probe_outcome=word(data["probe"], 12), probe_held=word(data["probe"], 16),
            whole_snapshot_coherent=False)
        save(self.out / (tag + "-state.json"), state)
        self.last_sequence = state["mailbox_sequence"]
        return state, data

    def submit_logic(self, command, tag):
        require(command in [1, 2, 3, 4, 5, 6, 7], "Logic tool cannot clear injection or rearm")
        require(self.last_sequence is not None, "Read state before submitting")
        sequence = ((self.last_sequence + 1) & 0xFFFFFFFF) or 1
        self.last_sequence = sequence
        self.write_attempted = True
        # Power key is a literal ZERO, including for deliberately rejected 3/5/6/7.
        self.execute(tag, ["-w32", hex(self.layout["request"] + 4), hex(command), "0x0", "0x7d0", "0x3f000000",
                           "-w32", hex(self.layout["request"]), hex(sequence)])
        return sequence


def check_logic_idle(state):
    require(state["status"] == state["started"] == 1 and state["state"] == 0, "Not healthy OBSERVE")
    require(not state["halted"] and not state["core_lockup"] and state["drive_running"] and state["adc_irq"],
            "CPU/ADC/timing stopped")
    require(not state["moe"] and state["high_inputs"] == state["low_inputs"] == 0, "Output not fully off")
    require(not state["guard_running"] and not state["guard_irq"], "Guard unexpectedly running")
    require(state["enable_attempts"] == state["enable_successes"] == 0, "Power was attempted on this boot")
    require(state["invalid_current_count"] == state["legacy_invalid"] == state["moe_violations"] == 0,
            "Invalid current or unintended output recorded")
    require(state["current.acquisition_valid"] and state["current.measured_phases"] == 6, "B/C acquisition incomplete")
    require(state["trip_armed"] and state["legacy_trip_valid"] and state["trip_valid"] == 1
            and not state["legacy_trip_active"] and not state["trip.condition_active"]
            and state["legacy_trip_causes"] == state["trip.causes"] == 0, "Protection unhealthy")
    require(state["bie"] and state["break_flags"] == state["comp_mask"] == 0, "Break/COMP not healthy")
    require(state["bdtr"] & 0x01001000 == 0x01001000 and not state["bdtr"] & 0x4000, "Break/AOE configuration")
    require(state["power_key"] == 0 and state["mailbox_sequence"] == state["command_sequence"], "Pending command/key")
    require(state["probe_outcome"] == state["probe_held"] == 0 and state["dac_codes"] == [2065, 345, 345],
            "Probe injection or thresholds already changed")
    # A biased sensing path under ST-Link 3V3 is possible. This ADC bound excludes
    # the 12 V bench stage; it does not qualify physical rail voltage or leakage.
    require(state["bus_valid"] and math.isfinite(state["bus_v"]) and -0.1 <= state["bus_v"] < 4.0,
            "Logic-only check requires bus observation below 4 V and OUTPUT OFF")


def run(session, inspect_only):
    first, _ = session.read("startup-1")
    check_logic_idle(first)
    second, _ = session.read("startup-2")
    check_logic_idle(second)
    require(second["fast_tick_count"] != first["fast_tick_count"] and second["uw_tick"] != first["uw_tick"],
            "Sampling or SysTick did not advance")
    if inspect_only:
        return {"result": "LOGIC_READ_ONLY_PASS", "before": first, "final": second}
    require(second["command_sequence"] == second["active_request_sequence"] == second["result"] == 0,
            "Logic suite requires untouched startup; no automatic replay")
    reports = []
    for command, expected in [(2, 1), (4, 1), (3, 3), (5, 3), (6, 3), (7, 3)]:
        before, _ = session.read("before-command-" + str(command))
        check_logic_idle(before)
        sequence = session.submit_logic(command, "submit-command-" + str(command))
        if command == 4:
            time.sleep(0.3)
        after, data = session.read("after-command-" + str(command))
        check_logic_idle(after)
        require(after["command_sequence"] == sequence and after["result"] == expected, "Wrong command result")
        require(after["stage_attempts"] - before["stage_attempts"] == (2 if command == 2 else 0)
                and after["stage_successes"] - before["stage_successes"] == (2 if command == 2 else 0),
                "Unexpected staging")
        report = {"command": command, "sequence": sequence, "result": expected, "enable_attempts": 0}
        if command == 4:
            stable = session.upload("capture-stable", [("capture", session.layout["capture"], 80)])["capture"]
            require(stable == data["capture"] and word(stable) % 2 == 0, "Unstable current capture")
            require(word(stable, 8) == sequence and word(stable, 12) == 1 and word(stable, 16) == 4096,
                    "Current capture incomplete")
            report["sample_count"] = 4096
        reports.append(report)
    final, _ = session.read("final-no-replay")
    check_logic_idle(final)
    require(final["command_sequence"] == sequence and final["stage_successes"] == 2, "Unexpected repeated action")
    return {"result": "LOGIC_SUITE_PASS", "hex_sha256": EXPECTED, "programmed_this_run": False,
            "powered_pulses_this_run": 0, "requests": reports, "initial": first, "final": final}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inspect-only", action="store_true")
    add_candidate_arguments(parser)
    args = parser.parse_args()
    configure_candidate(args)
    session = Session(args.output)
    try:
        report = run(session, args.inspect_only)
    except Exception as error:
        report = {"result": "FAILED_NO_RETRY", "error": str(error)}
        if session.write_attempted:
            try:
                session.submit_logic(1, "exception-safe-off")
                report["cleanup"], _ = session.read("exception-after-safe-off")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        save(session.out / "summary.json", report)
        raise
    save(session.out / "summary.json", report)
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
