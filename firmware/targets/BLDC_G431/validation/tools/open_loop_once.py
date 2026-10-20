"""G431 有限低电压开环工具；默认只离线冻结候选，不连接目标板。

inspect 只读；zero 仅采集 4096 点关断零点；execute 明确提交一次命令 15；
stop 只提交一次 SAFE_OFF。没有烧录、复位、重试、重新使能或标定安装入口。
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import candidate_manifest as candidate
import current_check as current

pwm, common, checks = current.pwm, current.common, current.checks
require = common.require
KEY, COMMAND, DURATION_US = 0x4310A11E, 15, 3500000
RESULT_FIELDS = dict.fromkeys(["sequence", "request_sequence", "active", "result", "module_state",
                              "module_reason", "sample_count", "enabled_samples"], "I")
RESULT_FIELDS.update(dict.fromkeys(["voltage_v", "electrical_frequency_hz", "offset_b_a", "offset_c_a",
    "peak_abs_current_a", "start_position_turns", "end_position_turns", "displacement_turns",
    "min_duty", "max_duty"], "f"))
BENCH_TEXT = "12 V 供电正常；ST-Link 的 3V3 已断开；无外部注入；电机原三相接线已恢复；机械固定且允许短时转动"


def parameters(voltage, frequency):
    require(math.isfinite(voltage) and 0 < voltage <= 0.5, "电压必须满足 0 < V <= 0.5")
    require(math.isfinite(frequency) and 0 < abs(frequency) <= 5, "电频必须非零且绝对值不超过 5 Hz")
    # 同时验证落到目标 float32 后仍满足限制，避免小值下溢或边界舍入。
    values = struct.unpack("<2f", struct.pack("<2f", voltage, frequency))
    require(0 < values[0] <= 0.5 and 0 < abs(values[1]) <= 5, "float32 参数超出限制")
    return values


def extra_layout(build, gdb, output):
    """从同一个 AXF 的 DWARF 获取扩展 ABI，不假定链接地址。"""
    specs = {"config": "&g_target_validation_open_loop_config", "open_loop": "&g_target_validation_open_loop",
        "config_size": "sizeof(g_target_validation_open_loop_config)",
        "open_loop_size": "sizeof(g_target_validation_open_loop)",
        "position_offset": "&((PlatformBringupDebug*)0)->position_turns",
        "position_count_offset": "&((PlatformBringupDebug*)0)->position_samples"}
    groups = [("result", RESULT_FIELDS, "TargetValidationOpenLoopSnapshot"),
              ("current", current.FIELDS, "TargetValidationCurrentCapture")]
    for group, fields, typename in groups:
        for field in fields:
            specs[group + "_offset_" + field] = "&((" + typename + "*)0)->" + field
            specs[group + "_size_" + field] = "sizeof(((" + typename + "*)0)->" + field + ")"
    for index, field in enumerate(["voltage_v", "electrical_frequency_hz"]):
        specs["config_offset_" + str(index)] = "&((TargetValidationOpenLoopConfig*)0)->" + field
        specs["config_field_size_" + str(index)] = "sizeof(((TargetValidationOpenLoopConfig*)0)->" + field + ")"
    command = [str(gdb), "-batch", "-ex", 'file "' + (build / candidate.ARTIFACTS[1]).as_posix() + '"']
    for expression in specs.values():
        command += ["-ex", "p/x (unsigned int)" + expression]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
    (output / "open-loop-layout-gdb.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    values = re.findall(r"^\$\d+ = (0x[0-9a-f]+|\d+)\s*$", proc.stdout, re.M)
    require(proc.returncode == 0 and len(values) == len(specs), "开环扩展 DWARF 不完整")
    layout = dict(zip(specs, map(lambda x: int(x, 0), values)))
    require(layout["config_size"] == 8 and layout["open_loop_size"] == 72
            and [layout["config_offset_" + str(i)] for i in range(2)] == [0, 4]
            and all(layout["config_field_size_" + str(i)] == 4 for i in range(2))
            and layout["position_offset"] == 32 and layout["position_count_offset"] == 28,
            "开环配置/结果/位置 ABI 不匹配")
    result = {k: layout[k] for k in ["config", "open_loop", "position_offset", "position_count_offset"]}
    for group, fields, _ in groups:
        result[group + "_fields"] = {}
        for field, fmt in fields.items():
            offset = layout[group + "_offset_" + field]
            require(layout[group + "_size_" + field] == struct.calcsize(fmt)
                    and offset + struct.calcsize(fmt) <= (72 if group == "result" else 80), "扩展字段越界")
            result[group + "_fields"][field] = offset
    require([result["result_fields"][k] for k in RESULT_FIELDS] == list(range(0, 72, 4)), "结果字段顺序改变")
    for name, size in [("config", 8), ("open_loop", 72)]:
        require(0x20000000 <= result[name] < result[name] + size <= 0x20008000, "扩展地址越界")
    return result


def prepare(build, output, gdb, expected=None):
    """唯一默认路径；只复制文件、提取类型并核对三种构建产物。"""
    build, output = build.resolve(), output.resolve()
    require(not output.exists(), "证据目录已存在，拒绝覆盖")
    hashes = {name: candidate.digest(build / name) for name in candidate.ARTIFACTS}
    require(expected is None or hashes[candidate.ARTIFACTS[0]] == expected.lower(), "HEX 与指定摘要不符")
    def source_hashes():
        paths = candidate.source_paths(common.TARGET)
        import os
        for directory in [common.TARGET.parents[1] / "app/motor", common.TARGET.parents[2] / "control"]:
            for path in directory.glob("*.h"):
                paths[Path(os.path.relpath(path, common.TARGET)).as_posix()] = path
        return {name: candidate.digest(path) for name, path in paths.items()}
    sources = source_hashes()
    output.mkdir(parents=True)
    frozen = output / "artifacts"
    frozen.mkdir()
    for name in candidate.ARTIFACTS:
        shutil.copyfile(build / name, frozen / name)
        require(candidate.digest(frozen / name) == hashes[name], "冻结时构建产物改变")
    layout = common.offline_layout(frozen, gdb, output / "layout-gdb.log")
    candidate.verify_artifacts(frozen, layout)
    layout.update(extra_layout(frozen, gdb, output))
    for name in candidate.ARTIFACTS:
        require(candidate.digest(build / name) == hashes[name], "提取期间原构建产物改变")
    require(sources == source_hashes(),
            "提取期间源码改变，请重新准备一个新目录")
    report = {"schema_version": 1, "result": "OPEN_LOOP_OFFLINE_PREPARED", "hardware_access": False,
        "files": hashes, "source_files": sources, "layout": layout, "build_directory": "artifacts",
        "hex_sha256": hashes[candidate.ARTIFACTS[0]], "motor_qualified": False, "foc_qualified": False}
    common.save(output / "prepared.json", report)
    return report


def load_prepared(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    require(data.get("schema_version") == 1 and data.get("result") == "OPEN_LOOP_OFFLINE_PREPARED",
            "不是开环离线候选")
    build = (path.parent / data["build_directory"]).resolve()
    require(set(data["files"]) == set(candidate.ARTIFACTS), "候选产物不完整")
    for name in candidate.ARTIFACTS:
        require(candidate.digest(build / name) == data["files"][name], "冻结文件已改变")
    require(data["hex_sha256"] == data["files"][candidate.ARTIFACTS[0]], "HEX 摘要不一致")
    candidate.verify_artifacts(build, data["layout"])
    return data, build


def float_bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


class Session(pwm.Session):
    """复用既有只读解码；写白名单只有零点、一次开环和一次关断。"""
    def __init__(self, output, prepared, cli, action, voltage, frequency):
        self.out, self.cli, self.action = output.resolve(), cli, action
        self.out.mkdir(parents=True, exist_ok=False)
        self.candidate, build = load_prepared(prepared)
        self.layout = self.candidate["layout"]
        self.last_sequence, self.write_attempted = None, False
        self.writes, self.call_timeout_s, self.deadline = [], 15.0, None
        self.power_payload_attempted, self.power_commit_ready = False, False
        self.parameters = parameters(voltage, frequency)
        self.hex_sha256 = self.candidate["hex_sha256"]
        # 检查与读回都针对冻结镜像；不碰当前可变化的构建目录。
        image = candidate.read_hex(build / candidate.ARTIFACTS[0])
        start, end = min(image), max(image) + 1
        raw = self.upload("identity", [("flash", start, end - start)])["flash"]
        require(all(raw[a - start] == value for a, value in image.items()), "板上 Flash 与冻结候选不同")
        common.save(self.out / "identity.json", {"hex_sha256": self.hex_sha256,
            "bytes_matched": len(image), "programmed_this_run": False,
            "prepared_sha256": candidate.digest(prepared), "prepared": str(prepared.resolve())})

    def execute(self, tag, arguments):
        power_command = getattr(self, "power_command", COMMAND)
        power_duration = getattr(self, "power_duration_us", DURATION_US)
        payload_parameters = getattr(self, "payload_parameters", self.parameters)
        if "-w32" in arguments:
            if len(arguments) == 10:
                expected = ["-w32", hex(self.layout["config"]), hex(float_bits(payload_parameters[0])),
                    hex(float_bits(payload_parameters[1])), "-w32", hex(self.layout["request"] + 4),
                    hex(power_command), hex(KEY), hex(power_duration), hex(float_bits(0.5))]
                require(self.action == "execute" and not self.power_payload_attempted and arguments == expected,
                        "开环 payload 非法或重复")
                self.power_payload_attempted = True
            elif len(arguments) == 3:
                require(self.action == "execute" and self.power_payload_attempted and self.power_commit_ready
                        and arguments == ["-w32", hex(self.layout["request"]), hex(self.last_sequence)]
                        and power_command not in self.writes, "开环 sequence 未通过读回或重复")
                self.writes.append(power_command)
            else:
                require(len(arguments) == 9 and arguments[0] == arguments[6] == "-w32"
                        and int(arguments[1], 0) == self.layout["request"] + 4
                        and int(arguments[7], 0) == self.layout["request"], "非法 mailbox 写入布局")
                command, key, duration, duty, sequence = [int(arguments[i], 0) for i in [2, 3, 4, 5, 8]]
                require(sequence != 0 and command not in self.writes, "拒绝重复请求")
                require(key == 0 and duty == float_bits(0.5)
                        and ((command == 4 and self.action == "zero" and duration == 2000)
                             or (command == 1 and self.action in ["zero", "execute", "stop"] and duration == 0)),
                        "只允许零点或关断命令")
                self.writes.append(command)
            # 先记尝试再调用外部程序；超时或部分写入也禁止重试。
            self.write_attempted = True
        else:
            require(arguments and len(arguments) % 4 == 0
                    and all(arguments[i] == "-u" for i in range(0, len(arguments), 4)), "只允许上传读取")
        command_line = [str(self.cli), "-c", "port=SWD", "mode=HOTPLUG", "freq=4000", *arguments, "-score"]
        timeout = self.call_timeout_s
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            require(timeout > 0, "主机总期限到，禁止继续轮询或重发")
        with (self.out / (tag + ".log")).open("wb") as log:
            proc = subprocess.run(command_line, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        text = (self.out / (tag + ".log")).read_text(encoding="utf-8", errors="replace")
        require(proc.returncode == 0 and "Error:" not in text, "SWD 失败；保留日志且不重试")
        require(bool(re.search(r"Device ID\s*:\s*0x468\b", text)) and "Core is running" in text,
                "芯片错误或 CPU 未运行")

    def read(self, tag):
        state, blocks = super().read(tag)
        state["position_turns"] = struct.unpack_from("<f", blocks["legacy"], self.layout["position_offset"])[0]
        state["position_samples"] = common.word(blocks["legacy"], self.layout["position_count_offset"])
        common.save(self.out / (tag + "-state.json"), state)
        return state, blocks


def submit(session, command, tag):
    power_command = getattr(session, "power_command", COMMAND)
    require(command in [1, 4, power_command] and session.last_sequence is not None, "先读 mailbox 再提交")
    previous_sequence = session.last_sequence
    sequence = ((previous_sequence + 1) & 0xFFFFFFFF) or 1
    duration = getattr(session, "power_duration_us", DURATION_US) if command == power_command else 2000 if command == 4 else 0
    session.last_sequence = sequence
    if command == power_command:
        voltage, frequency = parameters(*getattr(session, "payload_parameters", session.parameters))
        session.execute(tag + "-payload", ["-w32", hex(session.layout["config"]), hex(float_bits(voltage)),
            hex(float_bits(frequency)), "-w32", hex(session.layout["request"] + 4), hex(command), hex(KEY),
            hex(duration), hex(float_bits(0.5))])
        echoed = session.upload(tag + "-payload-readback", [("config", session.layout["config"], 8),
            ("request", session.layout["request"], 20)])
        # 请求尚未提交；必须保留原 sequence（包括 0xffffffff 绕回的情况）。
        require(echoed["config"] == struct.pack("<2f", voltage, frequency)
                and echoed["request"][4:] == struct.pack("<3If", command, KEY, duration, 0.5)
                and common.word(echoed["request"]) == previous_sequence, "参数读回不匹配或事务已改变")
        session.power_commit_ready = True
        session.execute(tag + "-sequence", ["-w32", hex(session.layout["request"]), hex(sequence)])
    else:
        session.execute(tag, ["-w32", hex(session.layout["request"] + 4), hex(command), "0x0",
            hex(duration), hex(float_bits(0.5)), "-w32", hex(session.layout["request"]), hex(sequence)])
    return sequence


def preflight(session, tag, vbus_dmm=None, stationary=True):
    # 旧 helper 的计数比例是 20 kHz；本候选 callback 已为 10 kHz，
    # ControlTime 仍是 20 kHz，不能把两种时基混为一谈。
    first, blocks1 = session.read(tag + "-1")
    second, blocks2 = session.read(tag + "-2")
    probe = check_pair(first, second, blocks1, blocks2, vbus_dmm, stationary)
    return first, second, probe


def check_pair(first, second, blocks1, blocks2, vbus_dmm=None, stationary=True):
    """只读健康对照；UIE 可能在正常 IRQ 中瞬时为零，实际时基须持续推进。"""
    require(all(first[k] == second[k] for k in checks.COUNTERS)
            and first["result"] == second["result"] and first["stop_reason"] == second["stop_reason"],
            "读取期间事务发生变化")
    ticks = (second["uw_tick"] - first["uw_tick"]) & 0xFFFFFFFF
    samples = (second["fast_tick_count"] - first["fast_tick_count"]) & 0xFFFFFFFF
    require(0 < ticks < 30000 and 9 < samples / ticks < 11, "10 kHz 采样/SysTick 未推进或复位")
    control_ticks = (second["current.observation_time"] - first["current.observation_time"]) % (1 << 64)
    require(18 < control_ticks / ticks < 22, "20 kHz ControlTime 未推进或复位")
    require(blocks1["probe"] == blocks2["probe"], "保护证据读取期间改变")
    probe = checks.read_probe(blocks2["probe"])
    for state in [first, second]:
        checks.check_healthy(state)
        pwm.check_configuration(state, check_update_irq=False)
        require(state["position_valid"] and math.isfinite(state["position_turns"])
                and 0 <= state["position_turns"] < 1, "位置无效")
        require(vbus_dmm is None or abs(state["bus_v"] - vbus_dmm) <= 0.3
                and abs(state["bus.voltage_v"] - vbus_dmm) <= 0.3, "软件母线与万用表差超过 0.3 V")
    delta = (second["position_turns"] - first["position_turns"] + 0.5) % 1 - 0.5
    require((not stationary or abs(delta) <= 0.002) and second["position_samples"] != first["position_samples"],
            "转轴未静止或位置发布未推进")
    return probe


def zero_bounds(stats):
    for phase in ["b", "c"]:
        require(abs(stats["phase_" + phase + ".mean_a"]) <= 0.1
                and stats["phase_" + phase + ".peak_to_peak_a"] <= 0.05, "零点均值或峰峰值超限")


def zero(session, vbus_dmm):
    _, before, probe = preflight(session, "zero-before", vbus_dmm)
    request = submit(session, 4, "submit-zero-once")
    time.sleep(0.5)
    _, after, updated = preflight(session, "zero-after", vbus_dmm)
    require(all(after[k] == before[k] for k in current.UNCHANGED) and updated == probe,
            "零点采集中出现功率动作或保护变更")
    require(after["command_sequence"] == after["active_request_sequence"] == request
            and after["result"] == after["stop_reason"] == 1 and after["mailbox_command"] == 4,
            "零点请求未完整结束")
    raw = [session.upload("zero-capture-" + str(i), [("capture", session.layout["capture"], 80)])["capture"] for i in [1, 2]]
    require(raw[0] == raw[1], "零点发布仍在变化")
    stats = current.decode_capture(raw[0], session.layout["current_fields"], request)
    zero_bounds(stats)
    return {"result": "OPEN_LOOP_ZERO_READY_OUTPUT_OFF", "hex_sha256": session.hex_sha256,
            "zero_origin": "captured",
            "final": after, "capture": updated, "statistics": stats,
            "zero_capture_hex": raw[0].hex(), "zero_capture_sha256": hashlib.sha256(raw[0]).hexdigest(),
            "request_sequence": request, "sample_count": 4096, "vbus_dmm_v": vbus_dmm}


def recover_zero(session, failed_zero_summary_path, vbus_dmm):
    """只读恢复 UIE 瞬态误判后的同一完整零点；不重采、不写目标或旧证据。"""
    failed_path = Path(failed_zero_summary_path).resolve()
    folder = failed_path.parent
    failure = json.loads(failed_path.read_text(encoding="utf-8"))
    require(failure.get("result") == "FAILED_NO_RETRY" and failure.get("action") == "zero"
            and failure.get("error") == "Unexpected PWM update IRQ ownership"
            and failure.get("write_attempted") is True and failure.get("automatic_retry") is False,
            "只恢复明确的 UIE 瞬态误判，不接受其他失败")
    identity = json.loads((folder / "identity.json").read_text(encoding="utf-8"))
    require(identity["hex_sha256"] == session.hex_sha256, "原零点候选不同")

    def archived(tag):
        # 使用现有完整解码器解码原始块，写出的派生 JSON 只进入新的恢复目录。
        reader = object.__new__(Session)
        reader.out, reader.layout = session.out, session.layout
        def upload(_, blocks):
            data = {name: (folder / (tag + "-" + name + ".bin")).read_bytes() for name, _, _ in blocks}
            require(all(len(data[name]) == size for name, _, size in blocks), "原始 SWD 块不完整")
            return data
        reader.upload = upload
        return Session.read(reader, "recovery-original-" + tag)

    names = ["zero-before-1", "zero-before-2", "zero-after-1", "zero-after-2", "exception-after-safe-off"]
    original = {name: archived(name) for name in names}
    before1, before2, after1, after2, cleanup = [original[name][0] for name in names]
    probe_before = check_pair(before1, before2, original[names[0]][1], original[names[1]][1], vbus_dmm)
    probe_after = check_pair(after1, after2, original[names[2]][1], original[names[3]][1], vbus_dmm)
    require(not after1["pwm"]["update_irq_enabled"] or not after2["pwm"]["update_irq_enabled"],
            "原失败未包含 UIE 瞬态零")
    require(probe_before == probe_after and failure.get("cleanup") == cleanup, "原保护或清理证据不一致")
    request = after2["active_request_sequence"]
    stop_request = ((request + 1) & 0xFFFFFFFF) or 1
    require(request == (((before2["mailbox_sequence"] + 1) & 0xFFFFFFFF) or 1)
            and failure.get("cleanup_request_sequence") == stop_request,
            "原零点到唯一 SAFE_OFF 的序号不连续")
    for state in [before1, before2, after1, after2, cleanup]:
        require(all(state[k] == 0 for k in current.UNCHANGED), "原现场已有 stage/enable，不作此类恢复")
    for state in [after1, after2]:
        require(state["command_sequence"] == state["mailbox_sequence"] == state["active_request_sequence"] == request
                and state["result"] == state["stop_reason"] == 1 and state["mailbox_command"] == 4,
                "原 4096 点窗口未 COMPLETE")
    require(cleanup["command_sequence"] == cleanup["mailbox_sequence"] == stop_request
            and cleanup["active_request_sequence"] == request and cleanup["mailbox_command"] == 1
            and cleanup["result"] == cleanup["stop_reason"] == 2,
            "原清理不是唯一 SAFE_OFF")
    checks.check_healthy(cleanup)
    pwm.check_configuration(cleanup, check_update_irq=False)
    require(all(cleanup[k] == after2[k] for k in checks.COUNTERS
                if k not in ["command_sequence", "mailbox_sequence"]), "SAFE_OFF 外出现额外事务或计数变化")
    require(original[names[3]][1]["probe"] == original[names[4]][1]["probe"], "清理改写保护证据")
    expected_writes = {
        "submit-zero-once.log": [(session.layout["request"] + 4, [4, 0, 2000, float_bits(0.5)]),
                                 (session.layout["request"], [request])],
        "exception-safe-off-only.log": [(session.layout["request"] + 4, [1, 0, 0, float_bits(0.5)]),
                                        (session.layout["request"], [stop_request])]}
    found_writes = {}
    for path in folder.glob("*.log"):
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = re.findall(r"w @(0x[0-9a-fA-F]+):\s*((?:0x[0-9a-fA-F]+\s*)+)", text)
        if rows:
            found_writes[path.name] = [(int(address, 0), [int(value, 0) for value in values.split()])
                                      for address, values in rows]
    require(found_writes == expected_writes, "原失败目录存在额外写入或 payload 不同")
    raw = original[names[2]][1]["capture"]
    require(raw == original[names[3]][1]["capture"] == original[names[4]][1]["capture"],
            "原窗口/清理后的零点原始数据不同")
    stats = current.decode_capture(raw, session.layout["current_fields"], request)
    zero_bounds(stats)
    first, final, probe = preflight(session, "zero-recovery-live", vbus_dmm)
    require(probe == probe_after, "本启动保护锚已变化")
    for state in [first, final]:
        require(all(state[k] == cleanup[k] for k in checks.COUNTERS)
                and state["result"] == state["stop_reason"] == 2 and state["mailbox_command"] == 1,
                "本启动事务/计数与原清理锚不同")
        elapsed = (state["uw_tick"] - cleanup["uw_tick"]) & 0xFFFFFFFF
        samples = (state["fast_tick_count"] - cleanup["fast_tick_count"]) & 0xFFFFFFFF
        ticks = (state["current.observation_time"] - cleanup["current.observation_time"]) % (1 << 64)
        require(0 < elapsed < 1800000 and 9 < samples / elapsed < 11 and 18 < ticks / elapsed < 22,
                "与原启动时基不连续或超过 30 分钟")
    live = session.upload("zero-recovery-unchanged", [("capture", session.layout["capture"], 80)])["capture"]
    require(live == raw, "当前零点已变化，不能恢复")
    return {"result": "OPEN_LOOP_ZERO_READY_OUTPUT_OFF", "zero_origin": "recovered_after_safe_off",
        "hex_sha256": session.hex_sha256, "final": final, "capture": probe, "statistics": stats,
        "zero_capture_hex": raw.hex(), "zero_capture_sha256": hashlib.sha256(raw).hexdigest(),
        "request_sequence": request, "sample_count": 4096, "vbus_dmm_v": vbus_dmm,
        "recovery_failure_summary": str(failed_path), "recovery_failure_sha256": candidate.digest(failed_path),
        "cleanup_request_sequence": stop_request, "target_writes": 0, "automatic_retry": False,
        "motor_qualified": False, "foc_qualified": False, "calibration_applied": False}


def check_zero_anchor(session, anchor, first, before, probe):
    require(anchor.get("result") == "OPEN_LOOP_ZERO_READY_OUTPUT_OFF"
            and anchor.get("hex_sha256") == session.hex_sha256 and anchor.get("sample_count") == 4096,
            "缺少本候选完整零点报告")
    raw = bytes.fromhex(anchor["zero_capture_hex"])
    require(hashlib.sha256(raw).hexdigest() == anchor["zero_capture_sha256"], "零点原始证据损坏")
    stats = current.decode_capture(raw, session.layout["current_fields"], anchor["request_sequence"])
    require(stats == anchor["statistics"] and probe == anchor["capture"], "零点统计或保护锚改变")
    zero_bounds(stats)
    old = anchor["final"]
    require(old["active_request_sequence"] == anchor["request_sequence"], "零点请求与活动锚不同")
    if anchor.get("zero_origin") == "captured":
        require(old["result"] == old["stop_reason"] == 1 and old["mailbox_command"] == 4
                and old["command_sequence"] == anchor["request_sequence"], "直接采集锚不合法")
    else:
        require(anchor.get("zero_origin") == "recovered_after_safe_off"
                and anchor.get("target_writes") == 0 and anchor.get("automatic_retry") is False
                and candidate.digest(Path(anchor["recovery_failure_summary"])) == anchor["recovery_failure_sha256"]
                and old["result"] == old["stop_reason"] == 2 and old["mailbox_command"] == 1
                and old["command_sequence"] == anchor["cleanup_request_sequence"]
                == (((anchor["request_sequence"] + 1) & 0xFFFFFFFF) or 1), "恢复来源或 SAFE_OFF 锚不合法")
    for state in [first, before]:
        require(all(state[k] == old[k] for k in checks.COUNTERS)
                and state["result"] == old["result"] and state["stop_reason"] == old["stop_reason"]
                and state["mailbox_command"] == old["mailbox_command"], "启动/事务计数已改变，不能复用零点")
        elapsed = (state["uw_tick"] - old["uw_tick"]) & 0xFFFFFFFF
        fast = (state["fast_tick_count"] - old["fast_tick_count"]) & 0xFFFFFFFF
        ticks = (state["current.observation_time"] - old["current.observation_time"]) % (1 << 64)
        require(0 < elapsed < 1800000 and 9 < fast / elapsed < 11 and 18 < ticks / elapsed < 22,
                "时基不连续或零点锚超过 30 分钟")
    live = session.upload("zero-anchor-current", [("capture", session.layout["capture"], 80)])["capture"]
    require(live == raw, "目标零点请求/计数/偏移已改变")
    return stats


def read_result(session, tag):
    """sequence 在块读前后单独读取；关断事实另读，不能用旧 active 推断。"""
    address = session.layout["open_loop"]
    before = session.upload(tag + "-sequence-before", [("sequence", address, 4)])["sequence"]
    blocks = session.upload(tag, [("open-loop", address, 72), ("stop", session.layout["stop"], 4),
        ("bdtr", 0x40012C44, 4), ("dhcsr", 0xE000EDF0, 4)])
    after = session.upload(tag + "-sequence-after", [("sequence", address, 4)])["sequence"]
    raw = blocks["open-loop"]
    sequence = common.word(before)
    coherent = sequence % 2 == 0 and sequence == common.word(raw) == common.word(after)
    report = {"coherent": coherent, "stop_reason": common.word(blocks["stop"]),
        "moe": bool(common.word(blocks["bdtr"]) & 0x8000),
        "halted": bool(common.word(blocks["dhcsr"]) & ((1 << 17) | (1 << 19)))}
    if coherent:
        report.update({name: struct.unpack_from("<" + fmt, raw, session.layout["result_fields"][name])[0]
                       for name, fmt in RESULT_FIELDS.items()})
    common.save(session.out / (tag + "-result.json"), report)
    return report


def execute_once(session, anchor, vbus_dmm, clock=time.monotonic):
    first, before, probe = preflight(session, "open-loop-before", vbus_dmm)
    stats = check_zero_anchor(session, anchor, first, before, probe)
    request = submit(session, getattr(session, "power_command", COMMAND), "submit-open-loop-once")
    wait_s = getattr(session, "power_wait_s", 6.0)
    deadline, index = clock() + wait_s, 0
    session.deadline = deadline
    while clock() < deadline:
        # 每次外部调用也受剩余总期限限制；所有读取串行，绝不补发请求。
        session.call_timeout_s = max(0.001, deadline - clock())
        observed = read_result(session, "open-loop-poll-" + str(index))
        index += 1
        if observed["coherent"] and observed["request_sequence"] == request and not observed["active"]:
            break
        if observed["stop_reason"] not in [0, 1] and not observed["moe"]:
            raise RuntimeError("开环已被独立关断；新结果可能因 ADC 停止而未更新，禁止重试")
    else:
        raise RuntimeError(f"主机 {wait_s:g} 秒期限到；保留结果，只允许关断，禁止重试")
    session.call_timeout_s, session.deadline = 15.0, None
    require(not observed["moe"] and not observed["halted"] and observed["stop_reason"] == 1
            and observed["result"] == 1 and observed["module_state"] == 6 and observed["module_reason"] == 0,
            "开环未正常 COMPLETE 且实际关断")
    require(all(math.isfinite(observed[k]) for k, fmt in RESULT_FIELDS.items() if fmt == "f"), "结果出现非有限值")
    require(observed["voltage_v"] == session.parameters[0] and observed["electrical_frequency_hz"] == session.parameters[1]
            and observed["offset_b_a"] == stats["phase_b.mean_a"] and observed["offset_c_a"] == stats["phase_c.mean_a"],
            "请求参数或冻结零点不一致")
    require(0 < observed["enabled_samples"] <= observed["sample_count"] <= getattr(session, "sample_count_max", 40000)
            and 0 <= observed["peak_abs_current_a"] < 0.25
            and 0.1 <= observed["min_duty"] <= observed["max_duty"] <= 0.9, "开环样本/电流/占空比结果异常")
    _, final, updated = preflight(session, "open-loop-final", vbus_dmm, stationary=False)
    require(final["command_sequence"] == final["active_request_sequence"] == request
            and final["result"] == final["stop_reason"] == 1 and updated == probe,
            "最终事务/保护状态不匹配")
    require(final["enable_attempts"] - before["enable_attempts"] == 1
            and final["enable_successes"] - before["enable_successes"] == 1
            and final["stage_attempts"] > before["stage_attempts"]
            and final["stage_attempts"] - before["stage_attempts"] == final["stage_successes"] - before["stage_successes"],
            "不是一次使能或存在拒绝的提交")
    return {"result": "OPEN_LOOP_COMPLETE_OUTPUT_OFF", "request_sequence": request,
            "before": before, "final": final, "open_loop": observed, "zero_request_sequence": anchor["request_sequence"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--action", choices=["prepare", "inspect", "zero", "execute", "stop"], default="prepare")
    parser.add_argument("--build-dir", type=Path, default=common.DEFAULT_BUILD)
    parser.add_argument("--prepared", type=Path, help="prepare 生成的 prepared.json")
    parser.add_argument("--zero-summary", type=Path)
    parser.add_argument("--expected-hex-sha256")
    parser.add_argument("--gdb", type=Path, default=common.DEFAULT_GDB)
    parser.add_argument("--cli", type=Path, default=common.DEFAULT_CLI)
    parser.add_argument("--voltage-v", type=float, default=0.3)
    parser.add_argument("--electrical-frequency-hz", type=float, default=2.0)
    parser.add_argument("--vbus-dmm-v", type=float)
    parser.add_argument("--bench-confirmed", action="store_true", help="逐次确认：" + BENCH_TEXT)
    args = parser.parse_args(argv)
    voltage, frequency = parameters(args.voltage_v, args.electrical_frequency_hz)
    require(not args.output.exists(), "输出目录已存在，拒绝覆盖或复用")
    if args.action == "prepare":
        require(args.prepared is None and args.zero_summary is None, "prepare 不接受目标/零点锚")
        return prepare(args.build_dir, args.output, args.gdb, args.expected_hex_sha256)
    require(args.prepared is not None, "硬件动作必须指定已冻结 prepared.json")
    if args.action in ["zero", "execute"]:
        require(args.bench_confirmed, "缺少本轮现场确认：" + BENCH_TEXT)
        require(args.vbus_dmm_v is not None and math.isfinite(args.vbus_dmm_v)
                and 10 <= args.vbus_dmm_v <= 14, "请提供本轮 10..14 V 母线万用表实测值")
    require((args.action == "execute") == (args.zero_summary is not None), "只有 execute 必须提供零点锚")
    anchor = json.loads(args.zero_summary.read_text(encoding="utf-8")) if args.zero_summary else None
    session = None
    try:
        session = Session(args.output, args.prepared, args.cli, args.action, voltage, frequency)
        if args.action == "inspect":
            first, final, probe = preflight(session, "inspect", args.vbus_dmm_v)
            report = {"result": "OPEN_LOOP_READ_ONLY_INSPECTION", "before": first, "final": final, "capture": probe}
        elif args.action == "zero":
            report = zero(session, args.vbus_dmm_v)
        elif args.action == "execute":
            report = execute_once(session, anchor, args.vbus_dmm_v)
        else:
            session.read("stop-before")
            request = submit(session, 1, "submit-safe-off-once")
            final, _ = session.read("stop-after")
            require(not final["moe"] and not final["guard_running"], "SAFE_OFF 后尚不能确认实际关断")
            report = {"result": "SAFE_OFF_SENT_ONCE_OUTPUT_OFF", "request_sequence": request, "final": final}
    except Exception as error:
        report = {"result": "FAILED_NO_RETRY", "error": str(error), "action": args.action,
                  "write_attempted": bool(session and session.write_attempted)}
        if session and session.write_attempted and 1 not in session.writes:
            try:
                session.call_timeout_s, session.deadline = 5.0, None
                report["cleanup_request_sequence"] = submit(session, 1, "exception-safe-off-only")
                report["cleanup"], _ = session.read("exception-after-safe-off")
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        if args.output.exists():
            report.update(automatic_retry=False, motor_qualified=False, foc_qualified=False)
            common.save(args.output / "summary.json", report)
        raise
    report.update(action=args.action, hex_sha256=session.hex_sha256, automatic_retry=False,
                  motor_qualified=False, foc_qualified=False, calibration_applied=False,
                  bench_confirmation=BENCH_TEXT if args.bench_confirmed else None)
    common.save(args.output / "summary.json", report)
    return report


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, allow_nan=False))
