"""Read-only, slow SWD observation during manual shaft rotation.

The 32-bit legacy angle and validity fields are independently observed. This is
not a coherent SSI frame capture, nor proof that all 1 kHz frames were valid.
After READY, a host START file containing GO starts a bounded 25 second observation.
No target command, reset, flash, halt or output enable is issued.
"""
import argparse
import hashlib
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


class ReadOnlySession(pwm.Session):
    def execute(self, tag, arguments):
        require(len(arguments) % 4 == 0 and all(arguments[i] == "-u" for i in range(0, len(arguments), 4)),
                "Position watch permits uploads only")
        super().execute(tag, arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--direction", choices=["clockwise", "counterclockwise"], default="clockwise")
    args = parser.parse_args()
    require(not args.output.exists(), "Refusing to overwrite evidence")
    args.output.mkdir()
    samples = []
    try:
        freeze = json.loads((common.TARGET / "validation/local-evidence/20261017-duty-check/source-freeze.json").read_text())
        for name, expected in freeze["files"].items():
            require(hashlib.sha256((common.TARGET / name).read_bytes()).hexdigest() == expected, "Frozen file changed: " + name)
        s = ReadOnlySession(args.output)
        expressions = ["sizeof(PlatformBringupDebug)", "&((PlatformBringupDebug*)0)->position_turns",
                       "sizeof(((PlatformBringupDebug*)0)->position_turns)",
                       "&((PlatformBringupDebug*)0)->position_samples"]
        command = [str(common.GDB), "-batch", "-ex", 'file "' + (common.BUILD / "BLDC_control V1.axf").as_posix() + '"']
        for expression in expressions:
            command += ["-ex", "p/x (unsigned int)" + expression]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
        (args.output / "position-layout.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        values = [int(v, 0) for v in re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", proc.stdout, re.M)]
        require(proc.returncode == 0 and values == [76, 32, 4, 28], "Unexpected position diagnostic layout")
        first, before, probe = checks.stable_pair(s, "preflight")
        for state in [first, before]:
            checks.check_healthy(state)
            pwm.check_configuration(state)
            require(state["position_valid"], "Initial position invalid")
        anchor = {"hex_sha256": common.EXPECTED, "final": before}
        start = time.monotonic()

        def sample(tag, phase):
            state, data = s.read(tag)
            checks.check_healthy(state)
            checks.check_anchor(state, anchor)
            require(checks.read_probe(data["probe"]) == probe, "Probe changed")
            angle = struct.unpack_from("<f", data["legacy"], 32)[0]
            require(math.isfinite(angle) and 0 <= angle < 1, "Angle outside one turn")
            item = {"phase": phase, "elapsed_s": time.monotonic() - start,
                    "position_turns": angle, "position_degrees": angle * 360,
                    "valid_observed": state["position_valid"],
                    "position_publication_count": common.word(data["legacy"], 28),
                    "uw_tick": state["uw_tick"], "angle_validity_atomic_pair": False}
            samples.append(item)
            common.save(args.output / "samples.json", samples)
            return state

        for i in range(3):
            sample("baseline-" + str(i), "baseline")
        common.save(args.output / "preflight.json", {**anchor, "probe": probe, "baseline": samples})
        print("READY " + json.dumps({"output": str(args.output), "angle_deg": samples[-1]["position_degrees"],
                                     "bus_v": before["bus_v"], "enable": before["enable_attempts"]}), flush=True)
        gate = args.output / "START"
        waiting_since = time.monotonic()
        while not gate.exists() and time.monotonic() - waiting_since < 45:
            time.sleep(0.05)
        require(gate.exists() and gate.read_text(encoding="utf-8").strip() == "GO", "Host observation was not started")
        motion_start = time.monotonic()
        index = 0
        while time.monotonic() - motion_start < 25:
            final = sample("motion-" + str(index), "manual-" + args.direction)
            index += 1
        report = {"result": "MANUAL_POSITION_OBSERVATIONS_COLLECTED", "hex_sha256": common.EXPECTED,
                  "target_writes": 0, "before": before, "final": final, "samples": samples,
                  "all_observed_valid": all(x["valid_observed"] for x in samples),
                  "all_SSI_frames_qualified": False, "operator_rotation_completion_pending": True,
                  "rotation_direction_requested": args.direction,
                  "viewing_direction_requested": "from shaft output end toward motor body"}
        print("OBSERVATION_FINISHED " + str(len(samples)), flush=True)
    except Exception as error:
        report = {"result": "READ_ONLY_STOPPED", "error": str(error), "target_writes": 0, "samples": samples}
        common.save(args.output / "summary.json", report)
        raise
    common.save(args.output / "summary.json", report)


if __name__ == "__main__":
    main()
