"""有限电流磁场往返；默认离线，不安装标定，不授予转子FOC运行许可。"""
import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import time

import current_hold_once as hold
import angle_survey_once as angle

loop, require = hold.loop, hold.require


def parameters(current):
    value, _ = hold.parameters(current, 0.0)
    require(value > 0, "励磁电流须为0.01～0.08名义A的正数")
    return value, 1.0


def prepare(build, output, gdb, expected=None):
    report = hold.prepare(build, output, gdb, expected)
    layout = angle.extra_layout(output / "artifacts", gdb, output)
    specs = {"survey_config": "&g_target_validation_current_survey_config",
        "angle_survey": "&g_target_validation_current_survey",
        "size": "sizeof(TargetValidationCurrentSurveySnapshot)",
        "config_size": "sizeof(TargetValidationCurrentSurveyConfig)",
        "command": "TARGET_VALIDATION_COMMAND_CURRENT_SURVEY"}
    for name in ["sequence", "request_sequence", "result", "stable_window_mask",
                 "excursion_turns", "current_reference_a", "windows"]:
        specs[name] = "&((TargetValidationCurrentSurveySnapshot*)0)->" + name
    for name in ["current_reference_a", "excursion_turns"]:
        specs["config_" + name] = "&((TargetValidationCurrentSurveyConfig*)0)->" + name
    command = [str(gdb), "-batch", "-ex", 'file "' + (output / "artifacts" / loop.candidate.ARTIFACTS[1]).as_posix() + '"']
    for expression in specs.values():
        command += ["-ex", "p/x (unsigned int)" + expression]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
    (output / "current-survey-layout.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    values = re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", proc.stdout, re.M)
    require(proc.returncode == 0 and len(values) == len(specs), "电流往返DWARF不完整")
    actual = dict(zip(specs, (int(v, 0) for v in values)))
    require(actual["size"] == 168 and actual["config_size"] == 8 and actual["command"] == 18
        and [actual[n] for n in ["sequence", "request_sequence", "result", "stable_window_mask",
             "excursion_turns", "current_reference_a", "windows"]] == list(range(0, 28, 4))
        and actual["config_current_reference_a"] == 0 and actual["config_excursion_turns"] == 4,
        "电流往返ABI改变")
    for name, size in [("survey_config", 8), ("angle_survey", 168)]:
        require(0x20000000 <= actual[name] < actual[name] + size <= 0x20008000, "电流往返地址越界")
        layout[name] = actual[name]
    report.update(current_survey_schema=1, current_survey_layout=layout,
        current_survey_tool_sha256=loop.candidate.digest(Path(__file__)),
        angle_survey_tool_sha256=loop.candidate.digest(Path(angle.__file__)))
    loop.common.save(output / "prepared.json", report)
    return report


class Session(hold.Session):
    power_command, power_duration_us = 18, 5_500_000

    def __init__(self, output, prepared, cli, action, current):
        payload = parameters(current)
        frozen, _ = loop.load_prepared(prepared)
        require(frozen.get("current_survey_schema") == 1 and "current_survey_layout" in frozen,
            "旧候选没有有限电流往返，拒绝连接")
        require(frozen["current_survey_tool_sha256"] == loop.candidate.digest(Path(__file__))
            and frozen["angle_survey_tool_sha256"] == loop.candidate.digest(Path(angle.__file__)),
            "电流往返工具已改变，请重新离线冻结")
        super().__init__(output, prepared, cli, action, payload[0], 0.0)
        self.survey_layout = frozen["current_survey_layout"]
        self.layout = dict(self.layout, config=self.survey_layout["survey_config"])
        self.payload_parameters = payload


def execute_once(session, anchor, vbus_dmm, clock=time.monotonic):
    first, before, probe = loop.preflight(session, "current-survey-before", vbus_dmm)
    stats = loop.check_zero_anchor(session, anchor, first, before, probe)
    request = loop.submit(session, 18, "submit-current-survey-once")
    deadline, index = clock() + 8.0, 0
    session.deadline = deadline
    while clock() < deadline:
        session.call_timeout_s = max(0.001, deadline - clock())
        observed = hold.read_result(session, "current-survey-poll-" + str(index))
        index += 1
        if observed["coherent"] and observed["request_sequence"] == request and not observed["active"]:
            break
        require(observed["stop_reason"] in [0, 1] or observed["moe"], "独立关断已发生，禁止重试")
    else:
        raise RuntimeError("主机8秒期限到，只允许一次关断")
    session.call_timeout_s, session.deadline = 15.0, None
    tracking = hold.validate_result(observed, session, stats, counts=(50002, 50001, 7500),
        references=(session.payload_parameters[0], 0.0))
    survey = angle.read_survey(session, request, excitation_field="current_reference_a")
    mapping = angle.analyze(survey)
    _, final, updated = loop.preflight(session, "current-survey-final", vbus_dmm, stationary=False)
    require(final["command_sequence"] == final["active_request_sequence"] == request
        and final["result"] == final["stop_reason"] == 1 and updated == probe, "终态事务或保护改变")
    for name, delta in [("enable_attempts", 1), ("enable_successes", 1),
                        ("stage_attempts", 50001), ("stage_successes", 50001)]:
        require(final[name] - before[name] == delta, "不是唯一使能或存在拒绝提交")
    return {"result": "CURRENT_SURVEY_COMPLETE_OUTPUT_OFF", "request_sequence": request,
        "before": before, "final": final, "current_hold": observed, "tracking": tracking,
        "survey": survey, "mapping": mapping, "zero_request_sequence": anchor["request_sequence"],
        "combined_screen_passed": tracking["tracking_screen_passed"] and mapping["pole_pairs_confirmed"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--action", choices=["prepare", "inspect", "zero", "execute", "stop"], default="prepare")
    parser.add_argument("--build-dir", type=Path, default=loop.common.DEFAULT_BUILD)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--zero-summary", type=Path)
    parser.add_argument("--gdb", type=Path, default=loop.common.DEFAULT_GDB)
    parser.add_argument("--cli", type=Path, default=loop.common.DEFAULT_CLI)
    parser.add_argument("--expected-hex-sha256")
    parser.add_argument("--current-reference-a", type=float, default=0.06)
    parser.add_argument("--vbus-dmm-v", type=float)
    parser.add_argument("--bench-confirmed", action="store_true", help=loop.BENCH_TEXT)
    args = parser.parse_args(argv)
    current, _ = parameters(args.current_reference_a)
    require(not args.output.exists(), "输出目录已存在，拒绝覆盖")
    if args.action == "prepare":
        require(args.prepared is None and args.zero_summary is None, "离线准备不接受运行锚")
        return prepare(args.build_dir, args.output, args.gdb, args.expected_hex_sha256)
    require(args.prepared is not None, "必须指定已冻结电流往返候选")
    if args.action in ["zero", "execute"]:
        require(args.bench_confirmed and args.vbus_dmm_v is not None and math.isfinite(args.vbus_dmm_v)
            and 10 <= args.vbus_dmm_v <= 14, "需要现场接线确认和10..14V母线参考")
    require((args.action == "execute") == (args.zero_summary is not None), "仅execute须提供零点锚")
    anchor = json.loads(args.zero_summary.read_text(encoding="utf-8")) if args.zero_summary else None
    session = None
    try:
        session = Session(args.output, args.prepared, args.cli, args.action, current)
        if args.action == "execute":
            report = execute_once(session, anchor, args.vbus_dmm_v)
        elif args.action == "zero":
            report = loop.zero(session, args.vbus_dmm_v)
        elif args.action == "inspect":
            first, final, probe = loop.preflight(session, "current-survey-inspect", args.vbus_dmm_v)
            report = {"result": "CURRENT_SURVEY_READ_ONLY_INSPECTION", "before": first, "final": final, "capture": probe}
        else:
            session.read("stop-before")
            request = loop.submit(session, 1, "submit-safe-off-once")
            final, _ = session.read("stop-after")
            require(not final["moe"] and not final["guard_running"], "尚不能确认关断")
            report = {"result": "SAFE_OFF_SENT_ONCE_OUTPUT_OFF", "request_sequence": request, "final": final}
    except Exception as error:
        report = {"result": "FAILED_NO_RETRY", "error": str(error), "action": args.action,
            "write_attempted": bool(session and session.write_attempted)}
        if session and session.write_attempted and 1 not in session.writes:
            try:
                session.call_timeout_s, session.deadline = 5.0, None
                report["cleanup_request_sequence"] = loop.submit(session, 1, "exception-safe-off-only")
                report["cleanup"], _ = session.read("exception-after-safe-off")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        if args.output.exists():
            report.update(automatic_retry=False, calibration_applied=False, foc_qualified=False)
            loop.common.save(args.output / "summary.json", report)
        raise
    report.update(action=args.action, hex_sha256=session.hex_sha256, automatic_retry=False,
        foc_qualified=False, calibration_applied=False, bench_confirmation=loop.BENCH_TEXT if args.bench_confirmed else None)
    loop.common.save(args.output / "summary.json", report)
    return report


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, allow_nan=False))
