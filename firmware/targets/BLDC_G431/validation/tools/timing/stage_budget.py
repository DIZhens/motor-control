"""无功率真实 stage 预算诊断；默认仅离线准备，显式 --measure 才写一次请求。"""

import argparse
import math
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

from elftools.elf.elffile import ELFFile

import compute_probe
from current_capture import (CubeBackend, DEFAULT_CLI, DEFAULT_GDB, DHCSR, FLASH_START,
                             FLASH_SIZE, RAM_START, RAM_END, TIM1_BASE, PreparedBuild,
                             require, save, word)


CLOCK_HZ, SAMPLES, MAX_CYCLES, DEADLINE_COUNT = 170000000, 80, 4250, 1700
EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / 'local-evidence'
ROW_NAMES = ["原 callback 锚点到 stage 返回", "诊断 step 入口到 stage 返回", "速度更新拍",
             "位置观测刷新拍", "普通快拍", "慢拍"]
RESULT_FIELDS = {field: "I" for field in [
    "schema_version", "request_sequence", "state", "failure", "completed_samples", "stage_calls",
    "stage_accepted", "enable_attempts", "observation_refreshes", "speed_updates", "min_remaining_counts",
    "last_shadow_faults", "limited_samples", "fast_samples", "slow_samples", "last_elapsed_cycles",
    "last_step_cycles", "last_timer_count", "last_entry_count", "last_cr1", "last_bdtr"]}
for index in range(6):
    RESULT_FIELDS.update({f"rows[{index}].{field}": "I" for field in
                          ["count", "min_cycles", "max_cycles", "sum_cycles"]})
PUBLICATION_FIELDS = {key: value for key, value in compute_probe.PUBLICATION_FIELDS.items()
                      if key != "compute_probe_permitted"}
PUBLICATION_FIELDS["stage_probe_permitted"] = "B"
SYMBOLS = {"request": "g_target_runtime_stage_request", "sequence": "g_target_stage_budget_sequence",
           "result": "g_target_stage_budget_result", "active": "g_target_stage_budget_active",
           "guard": "g_target_runtime_guard_faults", "publication_sequence": "s_publication_sequence",
           "publication": "s_publication"}
STATE_NAMES = ["IDLE", "READY", "ACTIVE", "COMPLETE", "FAILED"]
FAILURE_NAMES = ["NONE", "NOT_READY", "ABORTED", "BUSY", "CLOCK", "CONTROL", "STAGE", "POWER", "INCOMPLETE"]
EXPECTED_ENUMS = {"G431_STAGE_BUDGET_" + name: value for value, name in enumerate(STATE_NAMES)}
EXPECTED_ENUMS.update({"G431_STAGE_BUDGET_FAILURE_" + name: value for value, name in enumerate(FAILURE_NAMES)})
LIMITS = ["只在母线电源、电池、三相及外部注入源断开时执行；真实 MOE 必须保持为零。",
          "合成电流、位置和 RUN 只属于隔离试具；实际 HCC stage 返回值和原 callback 锚点保持真实。",
          "六行是不同分类与嵌套区间，不能相加；读数包含测量入口开销和抢占。",
          "COMPLETE 只说明固定 80 拍诊断通过，不确认实物电流增益、角度标定、带电 FOC 或完整 ISR/WCET。",
          "FAILED 保留完整字段及最后返回点差距；失败、超时和应答丢失均不补发、复位或清故障。"]


def layout_spec():
    spec = {}
    spec["config.fast_tick_period_ticks"] = "s_control_config.fast_tick_period_ticks"
    for key, name in SYMBOLS.items():
        spec[key], spec[key + "_size"] = "&" + name, "sizeof(" + name + ")"
    spec["bdtr"] = f"&((TIM_TypeDef*){TIM1_BASE})->BDTR"
    spec["row_count"] = "sizeof(g_target_stage_budget_result.rows)/sizeof(g_target_stage_budget_result.rows[0])"
    for prefix, type_name, fields in [("result", "G431StageBudgetResult", RESULT_FIELDS),
                                      ("publication", "RuntimePublication", PUBLICATION_FIELDS)]:
        for field in fields:
            expression = f"(({type_name}*)0)->{field}"
            spec[prefix + "." + field + ".offset"] = "&" + expression
            spec[prefix + "." + field + ".size"] = "sizeof(" + expression + ")"
    spec.update({"enum." + name: name for name in EXPECTED_ENUMS})
    return spec


def validate_layout(layout):
    require(layout.get("config.fast_tick_period_ticks") == 1,
            "此历史Observe诊断仅支持50us采样；100us slot请使用 runtime_timing.py 与Timing入口")
    require(set(layout) == set(layout_spec()), "stage AXF 布局项目不完整")
    regions = []
    for key in SYMBOLS:
        address, size = layout[key], layout[key + "_size"]
        require(type(address) is int and type(size) is int and size > 0 and address % 4 == 0
                and RAM_START <= address < address + size <= RAM_END, "stage 符号地址或大小异常：" + key)
        regions.append((address, address + size))
    ordered = sorted(regions)
    require(all(first[1] <= second[0] for first, second in zip(ordered, ordered[1:])), "stage 符号重叠")
    require(all(layout[key + "_size"] == 4 for key in
                ["request", "sequence", "active", "guard", "publication_sequence"]), "stage 控制字不是 uint32")
    require(layout["result_size"] == 180 and layout["row_count"] == 6, "stage 固定布局不是 180 字节/六行")
    require(TIM1_BASE <= layout["bdtr"] <= TIM1_BASE + 0x100 and layout["bdtr"] % 4 == 0, "TIM1 BDTR 地址异常")
    for prefix, fields in [("result", RESULT_FIELDS), ("publication", PUBLICATION_FIELDS)]:
        occupied = set()
        for field, fmt in fields.items():
            offset, size = layout[prefix + "." + field + ".offset"], layout[prefix + "." + field + ".size"]
            width = size in (1, 2, 4) if prefix == "publication" and field == "snapshot.state" else size == struct.calcsize("<" + fmt)
            require(type(offset) is int and width and 0 <= offset < offset + size <= layout[prefix + "_size"],
                    "stage 字段宽度或范围异常：" + prefix + "." + field)
            positions = set(range(offset, offset + size))
            require(not occupied & positions, "stage 字段重叠：" + prefix + "." + field)
            occupied |= positions
        if prefix == "result":
            require(occupied == set(range(180)), "stage 结果存在未解释字节")
    require(all(layout["enum." + name] == value for name, value in EXPECTED_ENUMS.items()), "stage 枚举 ABI 不符")


def derive_layout(axf, gdb, output):
    require(gdb.is_file(), "找不到离线 ARM GDB")
    spec = layout_spec()
    commands = output / "stage-layout-gdb.commands"
    with commands.open("x", encoding="utf-8") as stream:
        stream.write('file "' + axf.as_posix() + '"\n')
        stream.writelines("p/x (unsigned int)(" + expression + ")\n" for expression in spec.values())
    process = subprocess.run([str(gdb), "-nx", "-batch", "-x", str(commands)], capture_output=True, text=True, timeout=20)
    with (output / "stage-layout-gdb.log").open("x", encoding="utf-8") as stream:
        stream.write(process.stdout + process.stderr)
    values = re.findall(r"^\$\d+ = (0x[0-9a-fA-F]+|\d+)\s*$", process.stdout, re.M)
    require(process.returncode == 0 and len(values) == len(spec), "同次 AXF 的 stage 真实布局不可用")
    layout = dict(zip(spec, (int(value, 0) for value in values)))
    validate_layout(layout)
    return layout


def verify_symbols(axf, mapping, layout, compute_layout):
    validate_layout(layout)
    for key in ["guard", "publication_sequence", "publication", "bdtr"]:
        require(layout[key] == compute_layout[key], "stage 与原冻结布局的公共地址不同：" + key)
        if key != "bdtr":
            require(layout[key + "_size"] == compute_layout[key + "_size"], "公共对象大小不同：" + key)
    for field in PUBLICATION_FIELDS:
        if field == "stage_probe_permitted":
            continue
        for suffix in ["offset", "size"]:
            key = "publication." + field + "." + suffix
            require(layout[key] == compute_layout[key], "公共 publication 字段与原冻结布局不同：" + field)
    for key in ["request", "sequence", "result", "active"]:
        start, end = layout[key], layout[key] + layout[key + "_size"]
        require(all(end <= compute_layout[other] or compute_layout[other] + compute_layout[other + "_size"] <= start
                    for other in compute_probe.SYMBOLS), "stage 私有对象与原诊断对象重叠")
    with axf.open("rb") as stream:
        symbols = ELFFile(stream).get_section_by_name(".symtab")
        require(symbols is not None, "AXF 缺少符号表")
        for key, name in SYMBOLS.items():
            entries = symbols.get_symbol_by_name(name) or []
            require(len(entries) == 1 and entries[0]["st_value"] == layout[key]
                    and entries[0]["st_size"] == layout[key + "_size"], "stage AXF 符号与布局不符：" + name)
            rows = re.findall(r"^\s*" + re.escape(name) + r"\s+(0x[0-9a-fA-F]+)\s+Data\s+(\d+)\s+", mapping, re.M)
            require(len(rows) == 1 and (int(rows[0][0], 0), int(rows[0][1]))
                    == (layout[key], layout[key + "_size"]), "stage MAP 与 AXF 符号不同：" + name)


def prepare_build(build_directory, expected_hex_sha256, output, gdb=DEFAULT_GDB):
    require(output.resolve().is_relative_to(EVIDENCE_ROOT) and output.resolve() != EVIDENCE_ROOT,
            "证据目录必须位于本 Target 的 validation/local-evidence 下")
    original = compute_probe.prepare_build(build_directory, expected_hex_sha256, output, gdb)
    artifacts = output / "artifacts"
    axf, = artifacts.glob("*.axf")
    mapping, = artifacts.glob("*.map")
    layout = derive_layout(axf, gdb, output)
    verify_symbols(axf, mapping.read_text(encoding="utf-8"), layout, original.layout)
    save(output / "stage-prepared.json", {"status": "仅离线准备，未连接设备", "files": original.hashes,
                                          "stage_layout": layout, "limits": LIMITS})
    return PreparedBuild(original.image, layout, original.hashes, output)


def decode(data, prefix, fields, layout):
    require(len(data) == layout[prefix + "_size"], "stage SWD 读取长度不足：" + prefix)
    values = {}
    for field, fmt in fields.items():
        if prefix == "publication" and field == "snapshot.state":
            fmt = {1: "B", 2: "H", 4: "I"}[layout[prefix + "." + field + ".size"]]
        values[field] = struct.unpack_from("<" + fmt, data, layout[prefix + "." + field + ".offset"])[0]
    return values


def read_result(backend, layout, tag, timeout_s=20.0, clock=time.monotonic):
    deadline = clock() + timeout_s
    for attempt in range(3):
        require(clock() < deadline, "stage 结果只读复核超时")
        raw = backend.read([("seq_before", layout["sequence"], 4), ("result", layout["result"], 180),
                            ("seq_after", layout["sequence"], 4)],
                           tag if attempt == 0 else tag + f"-coherent-{attempt + 1}", deadline - clock())
        version = word(raw["seq_before"])
        if version == word(raw["seq_after"]) and version % 2 == 0:
            result = decode(raw["result"], "result", RESULT_FIELDS, layout)
            return result, raw["result"], version
    raise RuntimeError("stage 结果三次只读复核均未取得一致版本")


def result_header(result):
    require(result["schema_version"] == 1, "stage schema 版本不符")
    require(result["state"] in range(5) and result["failure"] in range(9), "stage 状态或失败原因非法")


def read_health(backend, layout, conditions, tag, timeout_s=20.0, clock=time.monotonic, sleep=time.sleep):
    blocks = [("guard_before", layout["guard"], 4), ("bdtr_before", layout["bdtr"], 4), ("cpu_before", DHCSR, 4),
              ("pub_before", layout["publication_sequence"], 4), ("publication", layout["publication"], layout["publication_size"]),
              ("pub_after", layout["publication_sequence"], 4), ("request", layout["request"], 4),
              ("active", layout["active"], 4), ("guard_after", layout["guard"], 4),
              ("bdtr_after", layout["bdtr"], 4), ("cpu_after", DHCSR, 4)]
    deadline, issues = clock() + timeout_s, []
    for attempt in range(8):
        require(clock() < deadline, "stage 健康只读复核超时")
        raw = backend.read(blocks, tag if attempt == 0 else tag + f"-coherent-{attempt + 1}", deadline - clock())
        words = {key: word(value) for key, value in raw.items() if key != "publication"}
        for suffix in ["before", "after"]:
            if words["guard_" + suffix] != 0:
                issues.append("独立 guard 存在故障")
            if words["bdtr_" + suffix] & 0x8000:
                issues.append("检测到 MOE 功率使能")
            if words["cpu_" + suffix] & ((1 << 17) | (1 << 19)):
                issues.append("CPU 已暂停或锁死")
        if words["pub_before"] == words["pub_after"] and words["pub_before"] % 2 == 0:
            break
        if issues:
            return {"coherent": False, "words": words, "issues": sorted(set(issues + ["快照版本不一致"]))}
        sleep(min(0.001 * (attempt + 1), max(0.0, deadline - clock())))
    else:
        return {"coherent": False, "words": words, "issues": ["健康快照八次只读未取得一致版本"]}
    publication = decode(raw["publication"], "publication", PUBLICATION_FIELDS, layout)
    for field in ["snapshot.status_valid", "snapshot.control_time_valid", "snapshot.bus_voltage_valid", "has_sample", "stage_probe_permitted"]:
        if publication[field] != 1:
            issues.append("缺少有效状态或 stage 诊断许可：" + field)
    if publication["snapshot.state"] != 1 or publication["snapshot.faults"] != 0:
        issues.append("真实 Motor 并非无故障 IDLE")
    bus = publication["snapshot.bus_voltage_v"]
    if not (math.isfinite(bus) and 0.0 <= bus <= 3.6 and abs(bus - conditions.vbus_dmm_v) <= 0.3):
        issues.append("VBUS 超范围或与万用表参考相差超过 0.3 V")
    if publication["snapshot.control_time_ticks_per_second"] != 20000:
        issues.append("控制时基频率不符")
    if words["active"] != 0:
        issues.append("stage 活动窗口尚未结束")
    if not math.isfinite(bus):
        publication["snapshot.bus_voltage_v"] = None
    return {"coherent": True, "words": words, "publication": publication, "issues": sorted(set(issues)),
            "time": publication["snapshot.control_time_ticks"], "request": words["request"],
            "bus_voltage_v": bus if math.isfinite(bus) else None}


def validate_complete(result):
    result_header(result)
    require(result["state"] == 3 and result["failure"] == 0, "stage 窗口没有完整成功")
    require(all(result[field] == value for field, value in {
        "completed_samples": 80, "stage_calls": 80, "stage_accepted": 80, "enable_attempts": 0,
        "speed_updates": 8, "observation_refreshes": 4, "fast_samples": 72, "slow_samples": 8,
        "last_shadow_faults": 0}.items()), "stage 固定拍数、覆盖或安全统计不完整")
    require(0 < result["limited_samples"] < SAMPLES and 0 < result["min_remaining_counts"] <= MAX_CYCLES - DEADLINE_COUNT,
            "限幅覆盖或剩余截止预算不符")
    require(result["last_bdtr"] & 0x8000 == 0 and result["last_cr1"] & 0x11 == 0x11
            and DEADLINE_COUNT < result["last_timer_count"] <= result["last_entry_count"] <= MAX_CYCLES
            and 0 < result["last_step_cycles"] <= result["last_elapsed_cycles"] <= MAX_CYCLES,
            "stage 最后返回点的功率、方向或时间事实异常")
    rows = []
    for index, count in enumerate([80, 80, 8, 4, 72, 8]):
        values = {field: result[f"rows[{index}].{field}"] for field in ["count", "min_cycles", "max_cycles", "sum_cycles"]}
        n, low, high, total = (values[field] for field in ["count", "min_cycles", "max_cycles", "sum_cycles"])
        require(n == count and 0 < low <= high <= MAX_CYCLES and n * low <= total <= n * high
                and (low == high or n >= 2 and (n - 1) * low + high <= total <= (n - 1) * high + low),
                "stage 分行次数、极值或总和不自洽")
        rows.append({"name": ROW_NAMES[index], **values, "min_us": low * 1e6 / CLOCK_HZ,
                     "mean_us": total / n * 1e6 / CLOCK_HZ, "max_us": high * 1e6 / CLOCK_HZ})
    require(rows[0]["sum_cycles"] == rows[4]["sum_cycles"] + rows[5]["sum_cycles"], "快慢分类与完整原锚点累计不一致")
    require(rows[2] == {**rows[5], "name": ROW_NAMES[2]}, "速度更新拍与本固定试具慢拍统计不一致")
    require(result["min_remaining_counts"] <= result["last_timer_count"] - DEADLINE_COUNT
            and rows[0]["min_cycles"] <= result["last_elapsed_cycles"] <= rows[0]["max_cycles"]
            and rows[1]["min_cycles"] <= result["last_step_cycles"] <= rows[1]["max_cycles"]
            and all(rows[1][field] <= rows[0][field] for field in ["min_cycles", "max_cycles", "sum_cycles"]),
            "stage 最后返回点或嵌套区间与完整统计不一致")
    return rows


def run_measurement(backend, prepared, conditions, clock=time.monotonic, sleep=time.sleep):
    conditions.validate(measure=True)
    layout = prepared.layout
    validate_layout(layout)
    flash = backend.read([("flash", FLASH_START, FLASH_SIZE)], "flash-identity")["flash"]
    require(len(flash) == FLASH_SIZE and all(flash[address - FLASH_START] == value for address, value in prepared.image.items()),
            "实机 Flash 与指定 stage 候选不符")
    existing, _, _ = read_result(backend, layout, "pre-result", clock=clock)
    result_header(existing)
    require(existing["state"] not in (1, 2), "已有 READY/ACTIVE stage 窗口")
    before = read_health(backend, layout, conditions, "pre-health-1", clock=clock, sleep=sleep)
    require(not before["issues"], "；".join(before["issues"]))
    sleep(0.02)
    advanced = read_health(backend, layout, conditions, "pre-health-2", clock=clock, sleep=sleep)
    require(not advanced["issues"], "；".join(advanced["issues"]))
    require(advanced["time"] > before["time"], "控制时间未持续推进")
    require(before["request"] == advanced["request"] == existing["request_sequence"], "存在未消费或其他主机请求")
    request = ((advanced["request"] + 1) & 0xFFFFFFFF) or 1
    report = {"status": "FAILED", "success": False, "request_sequence": request, "before": before,
              "pre_health": advanced, "files": prepared.hashes, "write_count": 1, "automatic_retry": False,
              "hardware_qualified": False, "foc_qualified": False, "full_isr_wcet": False,
              "limits": LIMITS, "issues": [], "result": None}
    deadline = clock() + 2.0
    try:
        backend.write_request(layout["request"], request, timeout_s=deadline - clock())
        for _ in range(201):
            require(clock() < deadline, "stage 等待超过 2 秒，不补发请求")
            result, payload, version = read_result(backend, layout, "result-poll", deadline - clock(), clock)
            report["result"] = result
            result_header(result)
            if result["request_sequence"] == request:
                if result["state"] in (3, 4):
                    report["result_sequence"] = version
                    if result["state"] == 4:
                        require(result["failure"] != 0, "FAILED 结果没有失败原因")
                        report["primary_failure"] = "firmware:" + FAILURE_NAMES[result["failure"]]
                    else:
                        report["rows"] = validate_complete(result)
                    confirmed = read_result(backend, layout, "result-confirm", deadline - clock(), clock)
                    require((result, payload, version) == confirmed, "stage 冻结结果第二次读取变化")
                    report["result_reread_unchanged"] = True
                    break
                require(result["state"] in (1, 2) and result["failure"] == 0, "stage 活动状态非法")
            else:
                require(result == existing, "stage 身份被其他请求改写")
            sleep(min(0.01, max(0.0, deadline - clock())))
        else:
            raise RuntimeError("stage 只读次数超过上界，不补发请求")
    except Exception as error:
        report.setdefault("primary_failure", str(error))
        report["issues"].append(str(error))
        if report["result"] is None:
            try:
                report["result"], _, report["result_sequence"] = read_result(
                    backend, layout, "failure-result", 0.5, clock)
            except Exception as read_error:
                report["issues"].append("失败后结果只读复核不可用：" + str(read_error))
    for tag in ["post-health-1", "post-health-2"]:
        try:
            health = read_health(backend, layout, conditions, tag, clock=clock, sleep=sleep)
            report[tag] = health
            report["issues"].extend(health["issues"])
            if health.get("request") != request:
                report["issues"].append("完成后 stage 请求身份改变")
        except Exception as error:
            report[tag] = {"error": str(error)}
            report["issues"].append("后健康读取失败：" + str(error))
        if tag == "post-health-1":
            sleep(0.02)
    after, latest = report["post-health-1"], report["post-health-2"]
    if not (after.get("time", 0) > advanced["time"] and latest.get("time", 0) > after.get("time", 0)):
        report["issues"].append("完成后控制时间未持续推进")
    report["issues"] = sorted(set(report["issues"]))
    if report["issues"]:
        report.setdefault("primary_failure", report["issues"][0])
    if report["result"] is not None:
        result = report["result"]
        report["last_return_point"] = {
            "recorded": result["stage_calls"] > 0 and result["last_elapsed_cycles"] > 0,
            "callback_elapsed_us": result["last_elapsed_cycles"] * 1e6 / CLOCK_HZ,
            "step_elapsed_us": result["last_step_cycles"] * 1e6 / CLOCK_HZ,
            "timer_count_minus_deadline": result["last_timer_count"] - DEADLINE_COUNT,
            "entry_count_minus_deadline": result["last_entry_count"] - DEADLINE_COUNT,
            "limit": "派生差值仅解释保存的顺序读数；未记录返回点时不得据零字段估算裕量。"}
    report["success"] = not report.get("primary_failure") and report["result"] is not None and report["result"]["state"] == 3
    report["status"] = "PASS_POWERLESS_STAGE_WINDOW_ONLY" if report["success"] else "FAILED"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--expected-hex-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Target validation/local-evidence 下的新目录")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--logic-only-confirmed", action="store_true")
    parser.add_argument("--vbus-dmm-v", type=float)
    parser.add_argument("--gdb", type=Path, default=DEFAULT_GDB)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    args = parser.parse_args(argv)
    conditions = compute_probe.Conditions(args.vbus_dmm_v, args.logic_only_confirmed)
    conditions.validate(args.measure)
    prepared = prepare_build(args.build_dir.resolve(), args.expected_hex_sha256, args.output.resolve(), args.gdb.resolve())
    backend = None
    try:
        if not args.measure:
            print("stage 离线镜像与真实布局已核对；未连接设备，未提交请求。")
            return 0
        backend = CubeBackend(prepared, args.cli.resolve())
        report = run_measurement(backend, prepared, conditions)
        save(prepared.output / "result.json", report)
        print("stage 80 拍无功率窗口通过。" if report["success"] else "stage 诊断未通过，完整结果与后健康已保存。")
        return 0 if report["success"] else 1
    except Exception as error:
        save(prepared.output / "failure.json", {"error": str(error), "request_write_attempted": bool(backend and backend.write_attempted),
                                                "automatic_retry": False, "files": prepared.hashes})
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("停止：" + str(error), file=sys.stderr)
        sys.exit(1)
