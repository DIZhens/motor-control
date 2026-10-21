"""单次有限静态电角往返；默认仅离线准备，不安装标定或开放正式运行。

复用开环工具的镜像核对、同启动零点、提交白名单和唯一关断路径。
命令 16 自动执行五秒 0→excursion→0 轨迹，三份保持窗口只形成可审查候选。
"""
import argparse
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
import open_loop_once as loop

require = loop.require
WINDOW_FIELDS = {"sample_count": "I", "first_position_time": "Q", "last_position_time": "Q",
    "mean_position_turns": "f", "span_turns": "f", "first_position_turns": "f",
    "last_position_turns": "f", "mean_nominal_b_a": "f", "mean_nominal_c_a": "f"}


def parameters(voltage, excursion):
    voltage, excursion = loop.parameters(voltage, excursion)
    require(0 < excursion <= 1.0, "静态往返角幅必须满足 0 < excursion <= 1 电角圈")
    return voltage, excursion


def extra_layout(build, gdb, output):
    specs = {
        "survey_config": "&g_target_validation_angle_survey_config",
        "angle_survey": "&g_target_validation_angle_survey",
        "survey_size": "sizeof(TargetValidationAngleSurveySnapshot)",
        "survey_config_size": "sizeof(TargetValidationAngleSurveyConfig)",
        "survey_command": "TARGET_VALIDATION_COMMAND_ANGLE_SURVEY",
        "window_size": "sizeof(TargetValidationAngleWindow)",
        "windows_offset": "&((TargetValidationAngleSurveySnapshot*)0)->windows",
    }
    for name in ["sequence", "request_sequence", "result", "stable_window_mask", "excursion_turns", "voltage_v"]:
        specs["header_" + name] = "&((TargetValidationAngleSurveySnapshot*)0)->" + name
    for name, fmt in WINDOW_FIELDS.items():
        specs["window_" + name] = "&((TargetValidationAngleWindow*)0)->" + name
        specs["size_" + name] = "sizeof(((TargetValidationAngleWindow*)0)->" + name + ")"
    for name in ["voltage_v", "excursion_turns"]:
        specs["config_" + name] = "&((TargetValidationAngleSurveyConfig*)0)->" + name
    command = [str(gdb), "-batch", "-ex", 'file "' + (build / loop.candidate.ARTIFACTS[1]).as_posix() + '"']
    for expression in specs.values():
        command += ["-ex", "p/x (unsigned int)" + expression]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
    (output / "angle-survey-layout.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    values = re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", proc.stdout, re.M)
    require(proc.returncode == 0 and len(values) == len(specs), "静态观测 DWARF 或命令 16 不完整")
    layout = dict(zip(specs, (int(value, 0) for value in values)))
    require(layout["survey_command"] == 16 and layout["survey_config_size"] == 8
        and layout["survey_size"] == 168 and layout["window_size"] == 48 and layout["windows_offset"] == 24
        and layout["config_voltage_v"] == 0 and layout["config_excursion_turns"] == 4,
        "静态观测 ABI 不匹配")
    require([layout["header_" + name] for name in ["sequence", "request_sequence", "result",
        "stable_window_mask", "excursion_turns", "voltage_v"]] == list(range(0, 24, 4)), "静态观测头顺序改变")
    for name, fmt in WINDOW_FIELDS.items():
        require(layout["size_" + name] == struct.calcsize(fmt)
            and layout["window_" + name] + struct.calcsize(fmt) <= 48, "窗口字段越界")
    for name, size in [("survey_config", 8), ("angle_survey", 168)]:
        require(0x20000000 <= layout[name] < layout[name] + size <= 0x20008000, "静态观测地址越界")
    return layout


def prepare(build, output, gdb, expected=None):
    report = loop.prepare(build, output, gdb, expected)
    report["angle_survey_layout"] = extra_layout(output / "artifacts", gdb, output)
    report["angle_survey_schema"] = 2
    report["angle_survey_tool_sha256"] = loop.candidate.digest(Path(__file__))
    report["open_loop_tool_sha256"] = loop.candidate.digest(Path(loop.__file__))
    loop.common.save(output / "prepared.json", report)
    return report


class Session(loop.Session):
    power_command, power_duration_us = 16, 5_500_000
    power_wait_s, sample_count_max = 8.0, 60_000

    def __init__(self, output, prepared, cli, action, voltage, excursion):
        payload = parameters(voltage, excursion)
        frozen, _ = loop.load_prepared(prepared)
        require(frozen.get("angle_survey_schema") == 2 and "angle_survey_layout" in frozen,
            "候选没有本版静态往返清单；禁止连接旧候选")
        require(frozen["angle_survey_tool_sha256"] == loop.candidate.digest(Path(__file__))
            and frozen["open_loop_tool_sha256"] == loop.candidate.digest(Path(loop.__file__)),
            "执行工具已改变，请重新离线冻结候选")
        super().__init__(output, prepared, cli, action, *payload)
        self.survey_layout = frozen["angle_survey_layout"]
        self.layout = dict(self.layout, config=self.survey_layout["survey_config"])
        self.payload_parameters = payload
        self.parameters = (payload[0], 0.0)  # 旧 72 字节诊断中的目标电频固定为零。


def read_survey(session, request, excitation_field="voltage_v"):
    require(excitation_field in ["voltage_v", "current_reference_a"], "未知励磁单位")
    layout = session.survey_layout
    address = layout["angle_survey"]
    raw = [session.upload("survey-frozen-" + str(i), [("survey", address, 168)])["survey"] for i in [1, 2]]
    require(raw[0] == raw[1], "静态观测终态仍在变化")
    seq, active_request, result, mask, excursion, voltage = struct.unpack_from("<4I2f", raw[0])
    require(seq % 2 == 0 and active_request == request and result == 1 and mask <= 7,
        "静态窗口未正常冻结到本次请求")
    require((voltage, excursion) == session.payload_parameters, "静态窗口参数不匹配")
    windows = []
    for i in range(3):
        base = layout["windows_offset"] + i * layout["window_size"]
        window = {name: struct.unpack_from("<" + fmt, raw[0], base + layout["window_" + name])[0]
                  for name, fmt in WINDOW_FIELDS.items()}
        require(all(math.isfinite(window[name]) for name, fmt in WINDOW_FIELDS.items() if fmt == "f"),
            "静态窗口包含非有限数")
        count, first, last = [window[k] for k in ["sample_count", "first_position_time", "last_position_time"]]
        require(0 <= count <= 250 and last >= first and 0 <= window["mean_position_turns"] < 1
            and 0 <= window["first_position_turns"] < 1 and 0 <= window["last_position_turns"] < 1
            and 0 <= window["span_turns"] < 0.5
            and abs(window["mean_nominal_b_a"]) < 0.25 and abs(window["mean_nominal_c_a"]) < 0.25,
            "静态窗口计数、时间、位置或名义电流范围非法")
        if mask & (1 << i):
            require(count >= 200 and 4500 <= last - first < 5000 and window["span_turns"] <= 0.002,
                "固件稳定标志与窗口数据不符")
        windows.append(window)
    if mask == 7:
        require(all(29900 <= windows[i+1]["first_position_time"] - windows[i]["first_position_time"] <= 30100
            and windows[i]["last_position_time"] < windows[i+1]["first_position_time"] for i in range(2)),
            "三个窗口的采集时间不符合 1.5 秒间隔")
    return {"sequence": seq, "request_sequence": request, "result": result, "stable_window_mask": mask,
            excitation_field: voltage, "excursion_turns": excursion, "windows": windows}


def circular_delta(value):
    return (value + 0.5) % 1.0 - 0.5


def analyze(survey, pole_pairs=7, direction=None):
    """比较角度、位移及完整电角圈的比例；不自动安装标定或打开正式控制。"""
    require(isinstance(pole_pairs, int) and 1 <= pole_pairs <= 64 and direction in [None, -1, 1], "非法映射假设")
    positions = [w["mean_position_turns"] for w in survey["windows"]]
    excursion = survey["excursion_turns"]
    outward = circular_delta(positions[1] - positions[0])
    backward = circular_delta(positions[2] - positions[1])
    returned = circular_delta(positions[2] - positions[0])
    inferred = direction is None
    if inferred:
        direction = 1 if outward > 0 else -1
    offsets = [(pole_pairs * direction * p - a) % 1.0 for p, a in zip(positions, [0, excursion, 0])]
    offset = (offsets[0] + sum(circular_delta(o - offsets[0]) for o in offsets) / 3) % 1.0
    error = max(abs(circular_delta(o - offset)) for o in offsets)
    usable = (survey["stable_window_mask"] == 7 and direction * outward > 0.01
        and direction * backward < -0.01 and abs(returned) <= 0.003 and error <= 0.035)
    # 完整一圈且机械位移低于半圈时，端点圆差的分支唯一；本目标标称7极对。
    # 不用短行程、静止或半圈歧义估计极对数；往返均须落在标称值±0.25内。
    full_turn = excursion == 1.0
    observed_poles = [excursion / abs(v) for v in [outward, backward]] \
        if full_turn and all(0.01 < abs(v) < 0.5 for v in [outward, backward]) else []
    poles_match = (usable and full_turn and pole_pairs >= 3 and len(observed_poles) == 2
        and all(abs(p - pole_pairs) <= 0.25 for p in observed_poles))
    return {"nominal_pole_pairs": pole_pairs, "encoder_direction_candidate": direction,
        "direction_source": "observed_outward_delta" if inferred else "explicit_hypothesis",
        "outward_mechanical_turns": outward, "backward_mechanical_turns": backward,
        "return_error_mechanical_turns": returned,
        "offset_samples_electrical_turns": offsets, "offset_candidate_electrical_turns": offset,
        "max_offset_residual_electrical_turns": error,
        "mapping_candidate_consistent": usable, "pole_pairs_confirmed": poles_match,
        "full_electrical_turn_requested": full_turn,
        "pole_pairs_estimates_out_and_back": observed_poles,
        "pole_pairs_tolerance": 0.25,
        "electrical_calibration_confirmed": False, "calibration_applied": False,
        "limits": "极对数结论仅来自完整电角圈往返比例；零位仍为候选，精度、静摩擦及跳极影响未作全区间验收。"}


def execute_once(session, anchor, vbus_dmm):
    report = loop.execute_once(session, anchor, vbus_dmm)
    survey = read_survey(session, report["request_sequence"])
    report.update(result="ANGLE_SURVEY_COMPLETE_OUTPUT_OFF", survey=survey, mapping=analyze(survey))
    return report


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
    parser.add_argument("--voltage-v", type=float, default=0.3)
    parser.add_argument("--excursion-turns", type=float, default=0.25)
    parser.add_argument("--vbus-dmm-v", type=float)
    parser.add_argument("--bench-confirmed", action="store_true", help=loop.BENCH_TEXT)
    args = parser.parse_args(argv)
    voltage, excursion = parameters(args.voltage_v, args.excursion_turns)
    require(not args.output.exists(), "输出目录已存在，拒绝覆盖")
    if args.action == "prepare":
        require(args.prepared is None and args.zero_summary is None, "离线准备不接受运行锚")
        return prepare(args.build_dir, args.output, args.gdb, args.expected_hex_sha256)
    require(args.prepared is not None, "必须指定静态往返候选")
    if args.action in ["zero", "execute"]:
        require(args.bench_confirmed and args.vbus_dmm_v is not None and math.isfinite(args.vbus_dmm_v)
            and 10 <= args.vbus_dmm_v <= 14, "需要接线确认和本轮 10..14 V 实测母线参考")
    require((args.action == "execute") == (args.zero_summary is not None), "只有 execute 必须指定零点锚")
    anchor = json.loads(args.zero_summary.read_text(encoding="utf-8")) if args.zero_summary else None
    session = None
    try:
        session = Session(args.output, args.prepared, args.cli, args.action, voltage, excursion)
        if args.action == "inspect":
            first, final, probe = loop.preflight(session, "survey-inspect", args.vbus_dmm_v)
            report = {"result": "ANGLE_SURVEY_READ_ONLY_INSPECTION", "before": first, "final": final, "capture": probe}
        elif args.action == "zero":
            report = loop.zero(session, args.vbus_dmm_v)
        elif args.action == "execute":
            report = execute_once(session, anchor, args.vbus_dmm_v)
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
        motor_qualified=False, foc_qualified=False, calibration_applied=False,
        bench_confirmation=loop.BENCH_TEXT if args.bench_confirmed else None)
    loop.common.save(args.output / "summary.json", report)
    return report


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, allow_nan=False))
