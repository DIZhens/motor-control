"""独立无功率 Runtime 计时入口；默认只冻结镜像，显式测量只提交一次完整请求。"""

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time

from elftools.elf.elffile import ELFFile

from current_capture import (CubeBackend, DEFAULT_CLI, DEFAULT_GDB, DHCSR, FLASH_START,
                             FLASH_SIZE, RAM_START, RAM_END, TIM1_BASE, PreparedBuild,
                             digest, read_hex, require, save, validate_map_load_region, word)


CLOCK_HZ, SAMPLES, RESULT_SIZE = 170000000, 80, 256
DEADLINE_COUNT, PERIOD_COUNTS, CONTROL_CYCLES = 1700, 4250, 17000
EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / "validation/local-evidence"
MODES = {"stage": 1, "compute": 2}
ROW_NAMES = ["work 入口至返回", "Platform 锚点至 work 返回", "IRQ C 入口至出口",
             "Platform 锚点至 work 入口", "work 返回至 IRQ C 出口", "速度更新拍 work",
             "观测刷新拍 work", "普通快拍 work", "慢拍 work"]
RESULT_FIELDS = {name: "I" for name in [
    "schema_version", "request_word", "mode", "state", "failure", "expected_samples",
    "completed_samples", "real_stage_calls", "real_stage_accepted", "sink_calls",
    "enable_attempts", "speed_updates", "observation_refreshes", "fast_samples", "slow_samples",
    "limited_samples", "last_shadow_faults", "last_work_cycles", "last_anchor_cycles",
    "last_irq_cycles", "last_timer_count", "last_entry_count", "last_cr1", "last_bdtr",
    "last_stage_budget_cycles", "last_irq_budget_cycles",
    "over_stage_deadline_samples", "over_consumer_budget_samples"]}
for index in range(9):
    RESULT_FIELDS.update({f"rows[{index}].{field}": "I" for field in
                          ["count", "min_cycles", "max_cycles", "sum_cycles"]})
HEALTH_TYPE = "G431TimingHealth"
HEALTH_FIELDS = {name: "I" for name in [
    "schema_version", "sample_count", "time_low", "time_high", "last_sample_cycles",
    "last_sample_interval_cycles", "faults", "guard_trips", "drive_started", "current_valid",
    "power_disabled", "bus_valid", "bus_voltage_v", "position_valid", "permitted", "reserved"]}
HEALTH_FIELDS["bus_voltage_v"] = "f"
SYMBOLS = {"request": "g_target_runtime_timing_request", "sequence": "g_target_runtime_timing_sequence",
           "result": "g_target_runtime_timing_result", "active": "g_target_runtime_timing_active",
           "guard": "g_target_runtime_timing_guard_faults",
           "health_sequence": "g_target_runtime_timing_health_sequence",
           "health": "g_target_runtime_timing_health"}
STATE_NAMES = ["IDLE", "READY", "ACTIVE", "COMPLETE", "FAILED"]
FAILURE_NAMES = ["NONE", "NOT_READY", "ABORTED", "BUSY", "CLOCK", "CONTROL", "STAGE",
                 "POWER", "INCOMPLETE", "IRQ_BUDGET"]
EXPECTED_ENUMS = {"G431_RUNTIME_TIMING_" + name: value for value, name in enumerate(STATE_NAMES)}
EXPECTED_ENUMS.update({"G431_RUNTIME_TIMING_FAILURE_" + name: value for value, name in enumerate(FAILURE_NAMES)})
EXPECTED_ENUMS.update({"G431_RUNTIME_TIMING_" + name.upper(): value for name, value in MODES.items()})
LIMITS = ["母线电源、电池、三相及外部注入必须断开；真实 MOE 必须始终为零。",
          "stage 模式只证明有限窗口的真实 HCC 提交；compute 模式只调用显式 sink，不能算真实 stage 接受。",
          "IRQ 行仅为 C 首末标记区间，含 HAL 和 Platform 尾部；末标记后的统计、发布及异常开销未计入，不证明 WCET。",
          "各行含嵌套区间与测量开销，不能相加推算完整路径或函数净耗时。",
          "失败和超时保留原始结果；不补发、不烧录、不复位、不清故障、不自动切换模式。"]


@dataclass(frozen=True)
class Conditions:
    vbus_dmm_v: float | None = None
    logic_only_confirmed: bool = False

    def validate(self, measure=False):
        if measure:
            require(self.logic_only_confirmed, "测量要求明确确认仅逻辑供电，母线、电池、三相及外部注入断开")
            require(self.vbus_dmm_v is not None and math.isfinite(self.vbus_dmm_v)
                    and 0.0 <= self.vbus_dmm_v <= 3.6, "必须提供有限且处于 0..3.6 V 的万用表 VBUS 参考")


def request_mode(request):
    require(type(request) is int and 0 <= request <= 0xFFFFFFFF, "请求字不是 uint32")
    if request == 0:
        return 0
    require(request >> 2 != 0 and request & 3 in MODES.values(), "请求字模式或 nonce 非法")
    return request & 3


def next_request(previous, mode):
    request_mode(previous)
    require(mode in MODES.values(), "只允许明确的 stage 或 compute 模式")
    nonce = (previous >> 2) + 1
    require(nonce <= 0x3FFFFFFF, "请求 nonce 已耗尽，拒绝自动回绕")
    return (nonce << 2) | mode


def result_header(result):
    require(result["schema_version"] == 2, "Runtime 计时 schema 不符")
    require(result["state"] in range(5) and result["failure"] in range(10), "计时状态或失败原因非法")
    mode = request_mode(result["request_word"])
    require(result["mode"] == mode, "结果模式与完整请求字不一致")
    if result["state"] == 0:
        require(result["request_word"] == 0 and result["failure"] == 0, "IDLE 结果身份非法")
    else:
        require(mode != 0 and result["expected_samples"] == SAMPLES, "活动或冻结结果缺少模式/固定拍数")
    if result["state"] in (1, 2, 3):
        require(result["failure"] == 0, "非失败状态携带失败原因")
    if result["state"] == 4:
        require(result["failure"] != 0, "FAILED 结果没有失败原因")


def timing_row(result, index, expected):
    values = {field: result[f"rows[{index}].{field}"] for field in
              ["count", "min_cycles", "max_cycles", "sum_cycles"]}
    count, low, high, total = (values[field] for field in
                               ["count", "min_cycles", "max_cycles", "sum_cycles"])
    require(count == expected, "计时行次数不完整：" + ROW_NAMES[index])
    if count == 0:
        require(low == high == total == 0, "未执行行含有时间统计")
    else:
        require(0 < low <= high and count * low <= total <= count * high
                and (count > 1 or low == high == total), "计时极值或总和不自洽：" + ROW_NAMES[index])
        if count > 1:
            require((count - 1) * low + high <= total <= (count - 1) * high + low,
                    "计时总和不包含声明的最小和最大样本：" + ROW_NAMES[index])
    return {"name": ROW_NAMES[index], **values,
            "min_us": low * 1e6 / CLOCK_HZ, "max_us": high * 1e6 / CLOCK_HZ,
            "mean_us": total * 1e6 / (count * CLOCK_HZ) if count else None}


def validate_complete(result, request):
    result_header(result)
    require(result["request_word"] == request and result["state"] == 3, "结果不是本次请求的 COMPLETE")
    require(result["completed_samples"] == SAMPLES and result["enable_attempts"] == 0
            and result["last_shadow_faults"] == 0, "完成拍数或无功率统计不完整")
    stage = result["mode"] == MODES["stage"]
    expected = {"real_stage_calls": 80 if stage else 0, "real_stage_accepted": 80 if stage else 0,
                "sink_calls": 0 if stage else 80, "speed_updates": 0 if stage else 8,
                "observation_refreshes": 0 if stage else 8, "fast_samples": 0 if stage else 72,
                "slow_samples": 0 if stage else 8}
    require(all(result[field] == value for field, value in expected.items()), "模式专属 stage/sink 或分支次数不符")
    require(result["limited_samples"] == 0 if stage else 0 < result["limited_samples"] < SAMPLES,
            "模式限幅覆盖不符")
    require(result["last_bdtr"] & 0x8000 == 0 and result["last_cr1"] & 1,
            "最后返回点功率或计数器状态非法")
    require(0 < result["last_work_cycles"] <= result["last_anchor_cycles"] <= result["last_irq_cycles"],
            "最后 work/锚点/IRQ 区间不自洽")
    require(0 <= result["over_stage_deadline_samples"] <= SAMPLES
            and 0 <= result["over_consumer_budget_samples"] <= SAMPLES, "越预算次数异常")
    require(0 < result["last_stage_budget_cycles"] <= result["last_irq_budget_cycles"] < CONTROL_CYCLES,
            "slot绝对截止预算非法")
    require(result["last_anchor_cycles"] < result["last_stage_budget_cycles"]
            and result["last_irq_cycles"] < result["last_irq_budget_cycles"]
            and result["over_stage_deadline_samples"] == result["over_consumer_budget_samples"] == 0,
            "最后返回点或完整窗口超过slot绝对截止")
    require(result["last_entry_count"] <= PERIOD_COUNTS and result["last_timer_count"] <= PERIOD_COUNTS,
            "timer读数超出中心对齐范围")
    counts = [80] * 5 + ([0] * 4 if stage else [8, 8, 72, 8])
    rows = [timing_row(result, index, count) for index, count in enumerate(counts)]
    require(rows[2]["max_cycles"] < CONTROL_CYCLES, "COMPLETE 的 IRQ 标记区间达到100us控制周期预算")
    require(rows[1]["sum_cycles"] == rows[0]["sum_cycles"] + rows[3]["sum_cycles"],
            "work 与同锚点前段统计不自洽")
    require(rows[2]["sum_cycles"] >= rows[1]["sum_cycles"] + rows[4]["sum_cycles"],
            "IRQ 区间未包含锚点路径及尾部")
    for shorter, longer in [(0, 1), (1, 2), (3, 1), (4, 2)]:
        require(all(rows[shorter][field] <= rows[longer][field]
                    for field in ["min_cycles", "max_cycles", "sum_cycles"]), "嵌套计时区间极值异常")
    for key, row in [("last_work_cycles", 0), ("last_anchor_cycles", 1), ("last_irq_cycles", 2)]:
        require(rows[row]["min_cycles"] <= result[key] <= rows[row]["max_cycles"], "最后读数不在对应行极值内")
    if not stage:
        require(rows[0]["sum_cycles"] == rows[7]["sum_cycles"] + rows[8]["sum_cycles"],
                "快慢拍没有完整分割 compute 统计")
    return rows


def layout_spec():
    spec = {}
    for key, name in SYMBOLS.items():
        spec[key], spec[key + "_size"] = "&" + name, "sizeof(" + name + ")"
    spec["bdtr"] = f"&((TIM_TypeDef*){TIM1_BASE})->BDTR"
    spec["row_count"] = "sizeof(g_target_runtime_timing_result.rows)/sizeof(g_target_runtime_timing_result.rows[0])"
    for prefix, type_name, fields in [("result", "G431RuntimeTimingResult", RESULT_FIELDS),
                                      ("health", HEALTH_TYPE, HEALTH_FIELDS)]:
        for field in fields:
            expression = f"(({type_name}*)0)->{field}"
            spec[prefix + "." + field + ".offset"] = "&" + expression
            spec[prefix + "." + field + ".size"] = "sizeof(" + expression + ")"
    spec.update({"enum." + name: name for name in EXPECTED_ENUMS})
    return spec


def validate_layout(layout):
    require(set(layout) == set(layout_spec()), "计时 AXF 布局项目不完整")
    regions = []
    for key in SYMBOLS:
        address, size = layout[key], layout[key + "_size"]
        require(type(address) is int and type(size) is int and size > 0 and size % 4 == 0
                and address % 4 == 0 and RAM_START <= address < address + size <= RAM_END,
                "计时符号地址或大小异常：" + key)
        regions.append((address, address + size))
    ordered = sorted(regions)
    require(all(first[1] <= second[0] for first, second in zip(ordered, ordered[1:])), "计时符号重叠")
    require(all(layout[key + "_size"] == 4 for key in SYMBOLS if key not in ("result", "health")),
            "计时控制字不是 uint32")
    require(layout["result_size"] == RESULT_SIZE and layout["row_count"] == 9, "计时结果不是固定 256 字节/九行")
    require(layout["health_size"] == 64, "计时健康结果不是固定 64 字节")
    require(layout["bdtr"] == TIM1_BASE + 0x44, "TIM1 BDTR 地址不符")
    for prefix, fields in [("result", RESULT_FIELDS), ("health", HEALTH_FIELDS)]:
        occupied = set()
        for field, fmt in fields.items():
            offset, size = layout[prefix + "." + field + ".offset"], layout[prefix + "." + field + ".size"]
            require(type(offset) is int and size == struct.calcsize("<" + fmt)
                    and 0 <= offset < offset + size <= layout[prefix + "_size"], "字段宽度或范围异常：" + prefix + "." + field)
            positions = set(range(offset, offset + size))
            require(not occupied & positions, "计时字段重叠：" + prefix + "." + field)
            occupied |= positions
        if prefix == "result":
            require(occupied == set(range(RESULT_SIZE)), "计时结果存在未解释字节")
    require(all(layout["enum." + name] == value for name, value in EXPECTED_ENUMS.items()), "计时枚举 ABI 不符")


def derive_layout(axf, gdb, output):
    require(gdb.is_file(), "找不到离线 ARM GDB")
    spec, commands = layout_spec(), output / "timing-layout-gdb.commands"
    with commands.open("x", encoding="utf-8") as stream:
        stream.write('file "' + axf.as_posix() + '"\n')
        stream.writelines("p/x (unsigned int)(" + expression + ")\n" for expression in spec.values())
    process = subprocess.run([str(gdb), "-nx", "-batch", "-x", str(commands)], capture_output=True, text=True, timeout=20)
    with (output / "timing-layout-gdb.log").open("x", encoding="utf-8") as stream:
        stream.write(process.stdout + process.stderr)
    values = re.findall(r"^\$\d+ = (0x[0-9a-fA-F]+|\d+)\s*$", process.stdout, re.M)
    require(process.returncode == 0 and len(values) == len(spec), "同次 AXF 的计时真实布局不可用")
    layout = dict(zip(spec, (int(value, 0) for value in values)))
    validate_layout(layout)
    return layout


def verify_artifacts(hex_file, axf, map_file, layout):
    validate_layout(layout)
    image, loaded = read_hex(hex_file), {}
    mapping = map_file.read_text(encoding="utf-8")
    with axf.open("rb") as stream:
        elf = ELFFile(stream)
        require(elf.elfclass == 32 and elf.little_endian and elf["e_machine"] == "EM_ARM", "AXF 不是小端 ARM32")
        for segment in elf.iter_segments():
            if segment["p_type"] != "PT_LOAD" or segment["p_filesz"] == 0:
                continue
            address, payload = segment["p_paddr"], segment.data()
            require(FLASH_START <= address < address + len(payload) <= FLASH_START + FLASH_SIZE,
                    "AXF 包含非 Flash 加载区域")
            for index, value in enumerate(payload):
                require(address + index not in loaded, "AXF 加载字节重叠")
                loaded[address + index] = value
        require(image == loaded, "HEX 和 AXF 加载字节不同")
        validate_map_load_region(mapping, image)
        entries = re.findall(r"Image Entry point\s*:\s*(0x[0-9a-fA-F]+)", mapping)
        require(len(entries) == 1 and int(entries[0], 0) == elf["e_entry"] and (elf["e_entry"] & ~1) in image,
                "MAP 入口与 AXF 不符")
        symbols = elf.get_section_by_name(".symtab")
        require(symbols is not None, "AXF 缺少符号表")
        for key, name in SYMBOLS.items():
            records = symbols.get_symbol_by_name(name) or []
            require(len(records) == 1 and records[0]["st_value"] == layout[key]
                    and records[0]["st_size"] == layout[key + "_size"], "AXF 符号与真实布局不符：" + name)
            rows = re.findall(r"^\s*" + re.escape(name) + r"\s+(0x[0-9a-fA-F]+)\s+Data\s+(\d+)\s+", mapping, re.M)
            require(len(rows) == 1 and (int(rows[0][0], 0), int(rows[0][1]))
                    == (layout[key], layout[key + "_size"]), "MAP 符号与 AXF 不同：" + name)
    return image


def prepare_build(build_directory, expected_hex_sha256, output, gdb=DEFAULT_GDB):
    require(output.resolve().is_relative_to(EVIDENCE_ROOT.resolve()) and output.resolve() != EVIDENCE_ROOT.resolve(),
            "证据目录必须位于本 Target 的 validation/local-evidence 下")
    require(re.fullmatch(r"[0-9a-fA-F]{64}", expected_hex_sha256) is not None, "必须提供完整 HEX SHA256")
    hex_files = list(build_directory.glob("*.hex"))
    require(build_directory.is_dir() and len(hex_files) == 1, "目录必须恰好包含一份计时 HEX")
    source = hex_files[0]
    require(digest(source) == expected_hex_sha256.lower(), "HEX SHA256 与指定候选不符")
    sources = [source.with_suffix(suffix) for suffix in [".hex", ".axf", ".map"]]
    require(all(path.is_file() for path in sources), "缺少同次 HEX/AXF/MAP")
    require(not output.exists(), "输出目录已存在，拒绝覆盖证据")
    artifacts = output / "artifacts"
    artifacts.mkdir(parents=True)
    paths = {path.suffix: artifacts / path.name for path in sources}
    for path in sources:
        shutil.copyfile(path, paths[path.suffix])
    require(digest(paths[".hex"]) == expected_hex_sha256.lower(), "复制期间 HEX 变化")
    layout = derive_layout(paths[".axf"], gdb, output)
    image = verify_artifacts(paths[".hex"], paths[".axf"], paths[".map"], layout)
    hashes = {path.name: digest(path) for path in paths.values()}
    save(output / "prepared.json", {"status": "仅离线冻结，未连接设备", "files": hashes,
                                   "layout": layout, "loaded_flash_bytes": len(image), "limits": LIMITS})
    return PreparedBuild(image, layout, hashes, output)


def decode(data, prefix, fields, layout):
    require(len(data) == layout[prefix + "_size"], "计时 SWD 读取长度不足：" + prefix)
    return {field: struct.unpack_from("<" + fmt, data, layout[prefix + "." + field + ".offset"])[0]
            for field, fmt in fields.items()}


def read_result(backend, layout, tag, timeout_s=2.0, clock=time.monotonic):
    deadline = clock() + timeout_s
    for attempt in range(3):
        require(clock() < deadline, "计时结果只读复核超时")
        raw = backend.read([("seq_before", layout["sequence"], 4), ("result", layout["result"], RESULT_SIZE),
                            ("seq_after", layout["sequence"], 4)],
                           tag + f"-{attempt + 1}", deadline - clock())
        version = word(raw["seq_before"])
        if version == word(raw["seq_after"]) and version % 2 == 0:
            return decode(raw["result"], "result", RESULT_FIELDS, layout), raw["result"], version
    raise RuntimeError("计时结果三次只读均未取得一致版本")


def read_health(backend, layout, conditions, tag, timeout_s=10.0, clock=time.monotonic, sleep=time.sleep):
    blocks = [("guard_before", layout["guard"], 4), ("bdtr_before", layout["bdtr"], 4), ("cpu_before", DHCSR, 4),
              ("seq_before", layout["health_sequence"], 4), ("health", layout["health"], 64),
              ("seq_after", layout["health_sequence"], 4), ("request", layout["request"], 4),
              ("active", layout["active"], 4), ("guard_after", layout["guard"], 4),
              ("bdtr_after", layout["bdtr"], 4), ("cpu_after", DHCSR, 4)]
    deadline, issues = clock() + timeout_s, []
    for attempt in range(8):
        require(clock() < deadline, "计时健康只读复核超时")
        raw = backend.read(blocks, tag + f"-{attempt + 1}", deadline - clock())
        words = {key: word(value) for key, value in raw.items() if key != "health"}
        for suffix in ["before", "after"]:
            if words["guard_" + suffix] != 0:
                issues.append("独立 guard 存在故障")
            if words["bdtr_" + suffix] & 0x8000:
                issues.append("检测到 MOE 功率使能")
            if words["cpu_" + suffix] & ((1 << 17) | (1 << 19)):
                issues.append("CPU 已暂停或锁死")
        if words["seq_before"] == words["seq_after"] and words["seq_before"] % 2 == 0:
            break
        if issues:
            return {"coherent": False, "words": words, "issues": sorted(set(issues + ["健康快照不一致"]))}
        sleep(min(0.001 * (attempt + 1), max(0.0, deadline - clock())))
    else:
        return {"coherent": False, "words": words, "issues": ["八次只读均未取得一致健康快照"]}
    health = decode(raw["health"], "health", HEALTH_FIELDS, layout)
    if health["schema_version"] != 2 or health["reserved"] != 0:
        issues.append("健康 schema 或保留字段不符")
    if health["faults"] != 0 or health["guard_trips"] != 0:
        issues.append("入口存在采样或保护故障")
    for field in ["drive_started", "current_valid", "power_disabled", "bus_valid", "position_valid", "permitted"]:
        if health[field] != 1:
            issues.append("缺少计时健康许可：" + field)
    if health["sample_count"] < 2 or not 12750 <= health["last_sample_interval_cycles"] <= 21250:
        issues.append("采样尚未持续或间隔异常")
    bus = health["bus_voltage_v"]
    if not (math.isfinite(bus) and 0.0 <= bus <= 3.6 and abs(bus - conditions.vbus_dmm_v) <= 0.3):
        issues.append("VBUS 超范围或与万用表参考相差超过 0.3 V")
    if words["active"] != 0:
        issues.append("计时窗口仍处于活动状态")
    if not math.isfinite(bus):
        health["bus_voltage_v"] = None
    return {"coherent": True, "words": words, "health": health, "issues": sorted(set(issues)),
            "time": (health["time_high"] << 32) | health["time_low"],
            "sample_count": health["sample_count"], "request": words["request"]}


def run_measurement(backend, prepared, conditions, mode, clock=time.monotonic, sleep=time.sleep):
    conditions.validate(measure=True)
    require(mode in MODES, "测量前必须明确指定 stage 或 compute 模式")
    layout = prepared.layout
    validate_layout(layout)
    flash = backend.read([("flash", FLASH_START, FLASH_SIZE)], "flash-identity")["flash"]
    require(len(flash) == FLASH_SIZE and all(flash[address - FLASH_START] == value for address, value in prepared.image.items()),
            "实机 Flash 与指定计时候选不符")
    existing, _, _ = read_result(backend, layout, "pre-result", clock=clock)
    result_header(existing)
    require(existing["state"] not in (1, 2), "已有 READY/ACTIVE 窗口")
    before = read_health(backend, layout, conditions, "pre-health-1", clock=clock, sleep=sleep)
    require(not before["issues"], "；".join(before["issues"]))
    sleep(0.02)
    advanced = read_health(backend, layout, conditions, "pre-health-2", clock=clock, sleep=sleep)
    require(not advanced["issues"], "；".join(advanced["issues"]))
    require(advanced["time"] > before["time"] and advanced["sample_count"] > before["sample_count"],
            "采样及 Control Time 没有持续推进")
    require(before["request"] == advanced["request"] == existing["request_word"], "存在未消费或其他主机请求")
    request = next_request(advanced["request"], MODES[mode])
    report = {"status": "FAILED", "success": False, "mode": mode, "request_word": request,
              "before": before, "pre_health": advanced, "files": prepared.hashes,
              "request_write_attempts": 1, "automatic_retry": False, "hardware_qualified": False,
              "foc_qualified": False, "full_isr_wcet": False, "limits": LIMITS, "issues": [], "result": None}
    deadline = clock() + 3.0
    try:
        backend.write_request(layout["request"], request, timeout_s=deadline - clock())
        for _ in range(301):
            require(clock() < deadline, "计时等待超过三秒，不补发请求")
            result, payload, version = read_result(backend, layout, "result-poll", deadline - clock(), clock)
            report["result"] = result
            result_header(result)
            if result["request_word"] == request:
                if result["state"] in (3, 4):
                    report["result_sequence"] = version
                    if result["state"] == 4:
                        report["primary_failure"] = "firmware:" + FAILURE_NAMES[result["failure"]]
                    else:
                        report["rows"] = validate_complete(result, request)
                    confirmed = read_result(backend, layout, "result-confirm", deadline - clock(), clock)
                    require((result, payload, version) == confirmed, "计时冻结结果第二次读取变化")
                    report["result_reread_unchanged"] = True
                    break
                require(result["state"] in (1, 2), "本次请求状态非法")
            else:
                require(result == existing, "结果身份被其他请求改写")
            sleep(min(0.01, max(0.0, deadline - clock())))
        else:
            raise RuntimeError("计时只读轮询超过次数上限")
    except Exception as error:
        report.setdefault("primary_failure", str(error))
        report["issues"].append(str(error))
        if report["result"] is None:
            try:
                report["result"], _, report["result_sequence"] = read_result(backend, layout, "failure-result", 0.5, clock)
            except Exception as read_error:
                report["issues"].append("失败后结果只读不可用：" + str(read_error))
    for tag in ["post-health-1", "post-health-2"]:
        try:
            health = read_health(backend, layout, conditions, tag, clock=clock, sleep=sleep)
            report[tag] = health
            report["issues"].extend(health["issues"])
            if health.get("request") != request:
                report["issues"].append("完成后请求身份改变")
        except Exception as error:
            report[tag] = {"error": str(error)}
            report["issues"].append("后健康读取失败：" + str(error))
        if tag == "post-health-1":
            sleep(0.02)
    after, latest = report["post-health-1"], report["post-health-2"]
    if not (after.get("time", 0) > advanced["time"] and latest.get("time", 0) > after.get("time", 0)
            and after.get("sample_count", 0) > advanced["sample_count"]
            and latest.get("sample_count", 0) > after.get("sample_count", 0)):
        report["issues"].append("完成后采样及 Control Time 未持续推进")
    report["issues"] = sorted(set(report["issues"]))
    if report["issues"]:
        report.setdefault("primary_failure", report["issues"][0])
    if report["result"] is not None:
        result = report["result"]
        report["last_return_point"] = {
            "recorded": result["last_work_cycles"] > 0,
            "work_us": result["last_work_cycles"] * 1e6 / CLOCK_HZ,
            "anchor_us": result["last_anchor_cycles"] * 1e6 / CLOCK_HZ,
            "irq_marked_us": result["last_irq_cycles"] * 1e6 / CLOCK_HZ,
            "entry_to_deadline_cycles": result["last_stage_budget_cycles"],
            "limit": "只有已记录字段可解释本次返回点；compute 的 sink 不具备真实 stage 接受含义，IRQ末标记后仍有开销。"}
    report["success"] = not report.get("primary_failure") and report["result"] is not None and report["result"]["state"] == 3
    report["status"] = ("PASS_POWERLESS_REAL_STAGE_WINDOW_ONLY" if mode == "stage" else
                        "PASS_POWERLESS_COMPUTE_SINK_WINDOW_ONLY") if report["success"] else "FAILED"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--expected-hex-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Target validation/local-evidence 下的新目录")
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--logic-only-confirmed", action="store_true")
    parser.add_argument("--vbus-dmm-v", type=float)
    parser.add_argument("--gdb", type=Path, default=DEFAULT_GDB)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    args = parser.parse_args(argv)
    conditions = Conditions(args.vbus_dmm_v, args.logic_only_confirmed)
    conditions.validate(args.measure)
    require(not args.measure or args.mode in MODES, "显式测量必须指定 --mode stage 或 compute")
    prepared = prepare_build(args.build_dir.resolve(), args.expected_hex_sha256, args.output.resolve(), args.gdb.resolve())
    backend = None
    try:
        if not args.measure:
            print("计时镜像与真实 ABI 已离线冻结；未连接设备，未提交请求。")
            return 0
        backend = CubeBackend(prepared, args.cli.resolve())
        report = run_measurement(backend, prepared, conditions, args.mode)
        save(prepared.output / "result.json", report)
        print("本模式的有限无功率窗口完成，结果与限制已保存。" if report["success"] else
              "计时窗口未通过，原始失败和后健康已保存；未补发。")
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
