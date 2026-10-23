"""有限静止αβ电流环；默认只冻结候选，显式execute才提交唯一命令17。"""
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
import open_loop_once as loop

require = loop.require
FIELDS = dict.fromkeys(["sequence", "request_sequence", "active", "result", "sample_count",
    "enabled_samples", "window_samples", "voltage_limited_samples"], "I")
FIELDS.update(dict.fromkeys(["alpha_reference_a", "beta_reference_a", "offset_b_a", "offset_c_a",
    "mean_alpha_a", "mean_beta_a", "peak_abs_current_a", "max_voltage_v", "min_duty", "max_duty",
    "mean_voltage_alpha_v", "mean_voltage_beta_v", "peak_tracking_error_a"], "f"))
SIZE = 84


def parameters(alpha, beta):
    require(all(math.isfinite(v) and abs(v) <= 0.08 for v in [alpha, beta]), "αβ目标必须有限且各不超过0.08 A")
    result = struct.unpack("<2f", struct.pack("<2f", alpha, beta))
    norm = math.hypot(*result)
    low, high = struct.unpack("<2f", struct.pack("<2f", 0.01, 0.08))
    require(low <= norm <= high, "αβ目标矢量模长必须在0.01～0.08名义A内")
    return result


def extra_layout(build, gdb, output):
    specs = {"config": "&g_target_validation_current_hold_config",
        "result": "&g_target_validation_current_hold", "size": "sizeof(CurrentHoldSnapshot)",
        "config_size": "sizeof(CurrentHoldConfig)", "command": "TARGET_VALIDATION_COMMAND_CURRENT_HOLD",
        "config_alpha": "&((CurrentHoldConfig*)0)->alpha_reference_a",
        "config_beta": "&((CurrentHoldConfig*)0)->beta_reference_a"}
    specs.update({"field_"+n: "&((CurrentHoldSnapshot*)0)->"+n for n in FIELDS})
    specs.update({"size_"+n: "sizeof(((CurrentHoldSnapshot*)0)->"+n+")" for n in FIELDS})
    command = [str(gdb), "-batch", "-ex", 'file "'+(build/loop.candidate.ARTIFACTS[1]).as_posix()+'"']
    for expression in specs.values():
        command += ["-ex", "p/x (unsigned int)"+expression]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
    (output/"current-hold-layout.log").write_text(proc.stdout+proc.stderr, encoding="utf-8")
    values = re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", proc.stdout, re.M)
    require(proc.returncode == 0 and len(values) == len(specs), "电流环诊断DWARF不完整")
    layout = dict(zip(specs, (int(v, 0) for v in values)))
    require(layout["size"] == SIZE and layout["config_size"] == 8 and layout["command"] == 17
        and layout["config_alpha"] == 0 and layout["config_beta"] == 4
        and [layout["field_"+n] for n in FIELDS] == list(range(0, SIZE, 4))
        and all(layout["size_"+n] == 4 for n in FIELDS), "电流环诊断ABI不符")
    for key, size in [("config", 8), ("result", SIZE)]:
        require(0x20000000 <= layout[key] < layout[key]+size <= 0x20008000, "诊断地址不在G431 SRAM")
    return layout


def prepare(build, output, gdb, expected=None):
    report = loop.prepare(build, output, gdb, expected)
    report["current_hold_layout"] = extra_layout(output/"artifacts", gdb, output)
    report["current_hold_schema"] = 1
    report["current_hold_tool_sha256"] = loop.candidate.digest(Path(__file__).resolve().parent / 'current_hold_once.py')
    report["open_loop_tool_sha256"] = loop.candidate.digest(Path(loop.__file__))
    loop.common.save(output/"prepared.json", report)
    return report


class Session(loop.Session):
    power_command, power_duration_us = 17, 3500000

    def __init__(self, output, prepared, cli, action, alpha, beta):
        payload = parameters(alpha, beta)
        frozen, _ = loop.load_prepared(prepared)
        require(frozen.get("current_hold_schema") == 1 and "current_hold_layout" in frozen,
            "旧候选没有电流环诊断；拒绝连接")
        require(frozen["current_hold_tool_sha256"] == loop.candidate.digest(Path(__file__).resolve().parent / 'current_hold_once.py')
            and frozen["open_loop_tool_sha256"] == loop.candidate.digest(Path(loop.__file__)),
            "主机工具已改变，请重新离线冻结候选")
        # 父类仅复用身份读取，不以此占位值提交旋转命令。
        super().__init__(output, prepared, cli, action, 0.3, 1.0)
        self.hold_layout = frozen["current_hold_layout"]
        self.layout = dict(self.layout, config=self.hold_layout["config"])
        self.payload_parameters = payload


def read_result(session, tag):
    address = session.hold_layout["result"]
    before = session.upload(tag+"-before", [("sequence", address, 4)])["sequence"]
    blocks = session.upload(tag, [("hold", address, SIZE), ("stop", session.layout["stop"], 4),
        ("bdtr", 0x40012C44, 4), ("dhcsr", 0xE000EDF0, 4)])
    after = session.upload(tag+"-after", [("sequence", address, 4)])["sequence"]
    raw = blocks["hold"]
    sequence = loop.common.word(before)
    result = {"coherent": sequence % 2 == 0 and sequence == loop.common.word(raw) == loop.common.word(after),
        "stop_reason": loop.common.word(blocks["stop"]), "moe": bool(loop.common.word(blocks["bdtr"]) & 0x8000),
        "halted": bool(loop.common.word(blocks["dhcsr"]) & ((1<<17)|(1<<19)))}
    if result["coherent"]:
        result.update({n: struct.unpack_from("<"+fmt, raw, session.hold_layout["field_"+n])[0]
            for n, fmt in FIELDS.items()})
    loop.common.save(session.out/(tag+".json"), result)
    return result


def validate_result(observed, session, stats, *, counts=(30002, 30001, 5000), references=None,
                    voltage_limit_v=0.4, current_limit_a=0.25, tracking_limits=(0.01, 0.03)):
    require(not observed["moe"] and not observed["halted"] and observed["stop_reason"] == observed["result"] == 1,
        "电流环未正常完成并实际关断")
    require(all(math.isfinite(observed[n]) for n, fmt in FIELDS.items() if fmt == "f"), "电流环结果包含坏数值")
    require(tuple(observed[n] for n in ["alpha_reference_a", "beta_reference_a"]) == (session.payload_parameters if references is None else references)
        and observed["offset_b_a"] == stats["phase_b.mean_a"] and observed["offset_c_a"] == stats["phase_c.mean_a"],
        "目标或冻结零点不匹配")
    require(observed["sample_count"] == counts[0] and observed["enabled_samples"] == counts[1]
        and observed["window_samples"] == counts[2]
        and 0 <= observed["voltage_limited_samples"] < counts[1] - 1
        and 0 <= observed["max_voltage_v"] <= voltage_limit_v + 0.00001
        and 0 <= observed["peak_abs_current_a"] < current_limit_a
        and 0 <= observed["peak_tracking_error_a"] <= 0.6
        and abs(observed["mean_alpha_a"]) <= current_limit_a + 0.05 and abs(observed["mean_beta_a"]) <= current_limit_a + 0.05
        and math.hypot(observed["mean_voltage_alpha_v"], observed["mean_voltage_beta_v"])
            <= observed["max_voltage_v"]+0.00001
        and 0.1 <= observed["min_duty"] <= observed["max_duty"] <= 0.9, "电流环窗口、限幅或计数异常")
    error = math.hypot(observed["mean_alpha_a"]-observed["alpha_reference_a"],
        observed["mean_beta_a"]-observed["beta_reference_a"])
    return {"mean_vector_error_nominal_a": error,
        "tracking_screen_passed": error <= tracking_limits[0] and observed["peak_tracking_error_a"] <= tracking_limits[1],
        "mean_error_limit_nominal_a": tracking_limits[0], "peak_error_limit_nominal_a": tracking_limits[1],
        "current_gain_accuracy_confirmed": False, "rotor_foc_qualified": False}


def submit_power_once(session):
    require(session.last_sequence is not None and session.action == "execute", "须先读取事务并显式execute")
    previous = session.last_sequence
    sequence = ((previous+1) & 0xffffffff) or 1
    alpha, beta = parameters(*session.payload_parameters)
    session.last_sequence = sequence
    session.execute("submit-current-hold-payload", ["-w32", hex(session.layout["config"]),
        hex(loop.float_bits(alpha)), hex(loop.float_bits(beta)), "-w32", hex(session.layout["request"]+4),
        "0x11", hex(loop.KEY), hex(3500000), hex(loop.float_bits(0.5))])
    echoed = session.upload("current-hold-payload-readback", [("config",session.layout["config"],8),
        ("request",session.layout["request"],20)])
    require(echoed["config"] == struct.pack("<2f",alpha,beta)
        and echoed["request"][4:] == struct.pack("<3If",17,loop.KEY,3500000,0.5)
        and loop.common.word(echoed["request"]) == previous, "电流参数读回不匹配或事务已改变")
    session.power_commit_ready = True
    session.execute("submit-current-hold-sequence", ["-w32",hex(session.layout["request"]),hex(sequence)])
    return sequence


def execute_once(session, anchor, vbus_dmm, clock=time.monotonic):
    first, before, probe = loop.preflight(session, "current-hold-before", vbus_dmm)
    stats = loop.check_zero_anchor(session, anchor, first, before, probe)
    request = submit_power_once(session)
    deadline, index = clock()+6.0, 0
    session.deadline = deadline
    while clock() < deadline:
        session.call_timeout_s = max(0.001, deadline-clock())
        observed = read_result(session, "current-hold-poll-"+str(index))
        index += 1
        if observed["coherent"] and observed["request_sequence"] == request and not observed["active"]:
            break
        require(observed["stop_reason"] in [0, 1] or observed["moe"], "独立关断已发生；不等待旧ADC结果，不重试")
    else:
        raise RuntimeError("主机6秒期限到，只允许关断，不重试")
    session.call_timeout_s, session.deadline = 15.0, None
    tracking = validate_result(observed, session, stats)
    _, final, updated = loop.preflight(session, "current-hold-final", vbus_dmm, stationary=False)
    require(final["command_sequence"] == final["active_request_sequence"] == request
        and final["result"] == final["stop_reason"] == 1 and updated == probe, "最终事务或保护状态不匹配")
    require(final["enable_attempts"]-before["enable_attempts"] == 1
        and final["enable_successes"]-before["enable_successes"] == 1
        and final["stage_attempts"]-before["stage_attempts"] == 30001
        and final["stage_successes"]-before["stage_successes"] == 30001, "不是唯一使能或存在提交失败")
    return {"result": "CURRENT_HOLD_COMPLETE_OUTPUT_OFF", "request_sequence": request,
        "before": before, "final": final, "current_hold": observed, "tracking": tracking,
        "zero_request_sequence": anchor["request_sequence"]}


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
    parser.add_argument("--alpha-reference-a", type=float, default=0.06)
    parser.add_argument("--beta-reference-a", type=float, default=0.0)
    parser.add_argument("--vbus-dmm-v", type=float)
    parser.add_argument("--bench-confirmed", action="store_true", help=loop.BENCH_TEXT)
    args = parser.parse_args(argv)
    alpha, beta = parameters(args.alpha_reference_a, args.beta_reference_a)
    require(not args.output.exists(), "输出目录已存在")
    if args.action == "prepare":
        require(args.prepared is None and args.zero_summary is None, "离线准备不接受运行锚")
        return prepare(args.build_dir, args.output, args.gdb, args.expected_hex_sha256)
    require(args.prepared is not None, "必须指定已冻结电流环候选")
    if args.action in ["zero", "execute"]:
        require(args.bench_confirmed and args.vbus_dmm_v is not None and math.isfinite(args.vbus_dmm_v)
            and 10 <= args.vbus_dmm_v <= 14, "需要本轮接线确认和10..14 V母线实测参考")
    require((args.action == "execute") == (args.zero_summary is not None), "只有execute必须指定零点锚")
    anchor = json.loads(args.zero_summary.read_text(encoding="utf-8")) if args.zero_summary else None
    session = None
    try:
        session = Session(args.output, args.prepared, args.cli, args.action, alpha, beta)
        if args.action == "zero":
            report = loop.zero(session, args.vbus_dmm_v)
        elif args.action == "execute":
            report = execute_once(session, anchor, args.vbus_dmm_v)
        elif args.action == "inspect":
            first, final, probe = loop.preflight(session, "current-hold-inspect", args.vbus_dmm_v)
            report = {"result": "CURRENT_HOLD_READ_ONLY_INSPECTION", "before": first, "final": final, "capture": probe}
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
            loop.common.save(args.output/"summary.json", report)
        raise
    report.update(action=args.action, hex_sha256=session.hex_sha256, automatic_retry=False,
        foc_qualified=False, calibration_applied=False, bench_confirmation=loop.BENCH_TEXT if args.bench_confirmed else None)
    loop.common.save(args.output/"summary.json", report)
    return report


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, allow_nan=False))
