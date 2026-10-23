"""Observe 软件与 CORDIC 后台对照；默认只离线核对，显式 --measure 才写一次请求。"""

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


CASE_NAMES = ["普通计算", "速度输出保持", "独立施加角", "电流电压限幅", "坏电流拒绝", "坏母线拒绝"]
CASE_COUNT, ITERATIONS, CLOCK_HZ, MAX_ELAPSED = 6, 16, 170000000, 34000
SEGMENT_NAMES = ["总计", "电流预处理", "测量角", "施加角", "速度更新或保持", "电流控制"]
ERROR_FIELDS = ["max_sine_error", "max_cosine_error", "max_norm_error", "max_phasor_error", "max_duty_error"]
TIMING_FIELDS = {"count": "I", "min_cycles": "I", "max_cycles": "I", "sum_cycles": "Q",
                 "clean_count": "I", "clean_min_cycles": "I", "clean_max_cycles": "I",
                 "clean_sum_cycles": "Q", "irq_entries": "I"}
CONTROLLER_SEGMENT_NAMES = ["控制器 mark0 到 mark6", "入口及输入校验", "测量与施加角准备及测量 Park",
                            "PI 与逆 Park", "SVPWM", "施加 Park 与积分计算", "输出及历史提交"]
CONTROLLER_FIELDS = {field: "I" for field in ["request_sequence", "state", "failure", "call_count",
                                            "valid_calls", "rejected_calls"]}
for index in range(7):
    CONTROLLER_FIELDS.update({f"timing[{index}].{field}": fmt for field, fmt in TIMING_FIELDS.items()})
IRQ_NAMES = ["ADC IRQ USER CODE 区间", "既有 cycle 锚点到 consumer", "consumer 同步调用", "Observe sample_ready"]
IRQ_FIELDS = {field: "I" for field in ["schema_version", "request_sequence", "state", "failure", "irq_count"]}
for index in range(4):
    IRQ_FIELDS.update({f"rows[{index}].{field}": "I" for field in
                       ["count", "min_cycles", "max_cycles", "sum_cycles"]})
PROBE_FIELDS = {"request_sequence": "I", "state": "I", "failure": "I", "clock_hz": "I",
                 "max_elapsed_cycles": "I", "completed_steps": "I", "next_case": "I"}
for index in range(CASE_COUNT):
    PROBE_FIELDS.update({f"cases[{index}].{field}": fmt for field, fmt in {
        "count": "I", "min_cycles": "I", "max_cycles": "I", "sum_cycles": "Q",
        "last_duty[0]": "f", "last_duty[1]": "f", "last_duty[2]": "f",
        "last_output_valid": "B", "last_voltage_limited": "B", "last_speed_limited": "B"}.items()})
    PROBE_FIELDS.update({f"cases[{index}].last_phasor[{component}]": "f" for component in range(4)})
    for segment in range(6):
        PROBE_FIELDS.update({f"cases[{index}].segments[{segment}].{field}": fmt for field, fmt in TIMING_FIELDS.items()})
RESULT_FIELDS = {"schema_version": "I", "request_sequence": "I", "state": "I", "failure": "I",
                 "completed_steps": "I", "sweep_count": "I", **{field: "f" for field in ERROR_FIELDS}}
for backend in ["software", "cordic"]:
    RESULT_FIELDS.update({backend + "." + field: fmt for field, fmt in PROBE_FIELDS.items()})
PUBLICATION_FIELDS = {
    "snapshot.control_time_ticks": "Q", "snapshot.control_time_ticks_per_second": "I",
    "snapshot.state": "I", "snapshot.faults": "I", "snapshot.bus_voltage_v": "f",
    "snapshot.status_valid": "B", "snapshot.control_time_valid": "B",
    "snapshot.bus_voltage_valid": "B", "has_sample": "B", "compute_probe_permitted": "B"}
ENUM_FIELDS = {"result.state", "result.failure", "publication.snapshot.state",
               "result.software.state", "result.software.failure", "result.cordic.state", "result.cordic.failure"}
SYMBOLS = {"request": "g_target_runtime_compute_request", "sequence": "g_target_runtime_compute_sequence",
           "result": "g_target_runtime_compute_result", "guard": "g_target_runtime_guard_faults",
           "publication_sequence": "s_publication_sequence", "publication": "s_publication",
           "controllerseq": "g_observe_controller_profile_sequence", "controller": "g_observe_controller_profile_result",
           "irqseq": "g_target_irq_timing_sequence", "irq": "g_target_irq_timing_result"}
EXPECTED_ENUMS = {"MOTOR_STATE_IDLE": 1, "OBSERVE_COMPUTE_PROBE_IDLE": 0,
                  "OBSERVE_COMPUTE_PROBE_ACTIVE": 1, "OBSERVE_COMPUTE_PROBE_COMPLETE": 2,
                  "OBSERVE_COMPUTE_PROBE_FAILED": 3, "OBSERVE_COMPUTE_PROBE_FAILURE_NONE": 0,
                  "OBSERVE_CONTROLLER_PROFILE_IDLE": 0, "OBSERVE_CONTROLLER_PROFILE_ACTIVE": 1,
                  "OBSERVE_CONTROLLER_PROFILE_COMPLETE": 2, "OBSERVE_CONTROLLER_PROFILE_FAILED": 3,
                  "OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE": 0,
                  "G431_IRQ_TIMING_IDLE": 0, "G431_IRQ_TIMING_ACTIVE": 1, "G431_IRQ_TIMING_COMPLETE": 2,
                  "G431_IRQ_TIMING_FAILED": 3, "G431_IRQ_TIMING_FAILURE_NONE": 0}
LAYOUT_FIELDS = [("result", "ObserveComputeComparisonResult", RESULT_FIELDS),
                 ("publication", "RuntimePublication", PUBLICATION_FIELDS),
                 ("controller", "ObserveControllerProfileResult", CONTROLLER_FIELDS),
                 ("irq", "G431IrqTimingResult", IRQ_FIELDS)]
LIMITS = ["独立合成算法状态，诊断 duty 未提交硬件。", "总读数包含测量开销和中断抢占；clean 只表示所覆盖 IRQ 进入计数未变化。",
          "不同 clean 样本集合的分段均值不可相加；IRQ 区间包含抢占和部分插桩，不含异常入栈出栈及末尾统计。",
          "不是完整快环 ISR 时间或 WCET，也不证明 stage 截止通过。",
          "不改变实物电流、电角度或时序标定许可，不授权功率输出。"]


@dataclass(frozen=True)
class Conditions:
    vbus_dmm_v: float | None = None
    logic_only_confirmed: bool = False

    def validate(self, measure=False):
        if self.vbus_dmm_v is not None:
            require(math.isfinite(self.vbus_dmm_v) and 0.0 <= self.vbus_dmm_v <= 3.6,
                    "万用表 VBUS 必须为有限的 0..3.6 V")
        require(not measure or self.logic_only_confirmed and self.vbus_dmm_v is not None,
                "实测须确认母线电源、电池、电机三相和外部注入源断开，并提供万用表 VBUS")


def layout_spec():
    spec = {}
    spec["config.fast_tick_period_ticks"] = "s_control_config.fast_tick_period_ticks"
    for key, name in SYMBOLS.items():
        spec[key], spec[key + "_size"] = "&" + name, "sizeof(" + name + ")"
    spec["bdtr"] = f"&((TIM_TypeDef*){TIM1_BASE})->BDTR"
    spec["case_count"] = "sizeof(((ObserveComputeComparisonResult*)0)->software.cases)/sizeof(ObserveComputeProbeCaseResult)"
    spec["controller_row_count"] = "sizeof(g_observe_controller_profile_result.timing)/sizeof(g_observe_controller_profile_result.timing[0])"
    spec["irq_row_count"] = "sizeof(g_target_irq_timing_result.rows)/sizeof(g_target_irq_timing_result.rows[0])"
    for prefix, type_name, fields in LAYOUT_FIELDS:
        for field in fields:
            expression = f"(({type_name}*)0)->{field}"
            spec[prefix + "." + field + ".offset"] = "&" + expression
            spec[prefix + "." + field + ".size"] = "sizeof(" + expression + ")"
    spec.update({"enum." + name: name for name in EXPECTED_ENUMS})
    return spec


def validate_layout(layout):
    require(layout.get("config.fast_tick_period_ticks") == 1,
            "此历史Observe诊断仅支持50us采样；100us slot请使用 runtime_timing.py 与Timing入口")
    require(set(layout) == set(layout_spec()), "AXF 类型布局项目不完整")
    regions = []
    for key in SYMBOLS:
        address, size = layout[key], layout[key + "_size"]
        require(type(address) is int and type(size) is int and size > 0 and address % 4 == 0
                and RAM_START <= address < address + size <= RAM_END, "诊断符号地址或大小异常：" + key)
        regions.append((address, address + size))
    require(all(first[1] <= second[0] for first, second in zip(sorted(regions), sorted(regions)[1:])),
            "诊断符号重叠")
    require(all(layout[key + "_size"] == 4 for key in
                ["request", "sequence", "guard", "publication_sequence", "controllerseq", "irqseq"]),
            "诊断控制字不是 uint32")
    require(TIM1_BASE <= layout["bdtr"] <= TIM1_BASE + 0x100 and layout["bdtr"] % 4 == 0,
            "TIM1 BDTR 类型地址异常")
    require(layout["case_count"] == CASE_COUNT, "算例数量与本工具不符")
    require(layout["controller_row_count"] == 7 and layout["irq_row_count"] == 4, "附加计时行数与本工具不符")
    for prefix, _, fields in LAYOUT_FIELDS:
        occupied = set()
        for field, fmt in fields.items():
            offset, size = layout[prefix + "." + field + ".offset"], layout[prefix + "." + field + ".size"]
            width_ok = size in (1, 2, 4) if prefix + "." + field in ENUM_FIELDS else size == struct.calcsize("<" + fmt)
            require(type(offset) is int and width_ok and 0 <= offset < offset + size <= layout[prefix + "_size"],
                    "字段宽度或范围异常：" + prefix + "." + field)
            positions = set(range(offset, offset + size))
            require(not occupied & positions, "诊断字段重叠：" + prefix + "." + field)
            occupied |= positions
    require(all(layout["enum." + key] == value for key, value in EXPECTED_ENUMS.items()), "枚举 ABI 不符")


def derive_layout(axf, gdb, output):
    require(gdb.is_file(), "找不到离线 ARM GDB")
    spec = layout_spec()
    # 两后端分段字段较多；使用离线批文件，避免超过 Windows 命令行长度上限。
    commands = output / "layout-gdb.commands"
    with commands.open("x", encoding="utf-8") as stream:
        stream.write('file "' + axf.as_posix() + '"\n')
        stream.writelines("p/x (unsigned int)(" + expression + ")\n" for expression in spec.values())
    arguments = [str(gdb), "-nx", "-batch", "-x", str(commands)]
    process = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
    with (output / "layout-gdb.log").open("x", encoding="utf-8") as stream:
        stream.write(process.stdout + process.stderr)
    values = re.findall(r"^\$\d+ = (0x[0-9a-fA-F]+|\d+)\s*$", process.stdout, re.M)
    require(process.returncode == 0 and len(values) == len(spec), "同次 AXF 的真实类型布局不可用")
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
                    and records[0]["st_size"] == layout[key + "_size"], "AXF 符号与布局不符：" + name)
            rows = re.findall(r"^\s*" + re.escape(name) + r"\s+(0x[0-9a-fA-F]+)\s+Data\s+(\d+)\s+", mapping, re.M)
            require(len(rows) == 1 and (int(rows[0][0], 0), int(rows[0][1]))
                    == (layout[key], layout[key + "_size"]), "MAP 符号与 AXF 不符：" + name)
    return image


def prepare_build(build_directory, expected_hex_sha256, output, gdb=DEFAULT_GDB):
    require(re.fullmatch(r"[0-9a-fA-F]{64}", expected_hex_sha256) is not None, "必须提供完整 HEX SHA256")
    hex_files = list(build_directory.glob("*.hex"))
    require(build_directory.is_dir() and len(hex_files) == 1, "目录必须恰好包含一份 Observe HEX")
    source = hex_files[0]
    require(digest(source) == expected_hex_sha256.lower(), "HEX SHA256 与指定候选不符")
    sources = [source.with_suffix(suffix) for suffix in [".hex", ".axf", ".map"]]
    require(all(path.is_file() for path in sources), "缺少同次 HEX/AXF/MAP")
    require(not output.exists(), "输出目录已存在，拒绝覆盖证据")
    frozen = output / "artifacts"
    frozen.mkdir(parents=True)
    paths = {}
    for path in sources:
        paths[path.suffix] = frozen / path.name
        shutil.copyfile(path, paths[path.suffix])
    require(digest(paths[".hex"]) == expected_hex_sha256.lower(), "复制期间 HEX 变化")
    layout = derive_layout(paths[".axf"], gdb, output)
    image = verify_artifacts(paths[".hex"], paths[".axf"], paths[".map"], layout)
    hashes = {path.name: digest(path) for path in paths.values()}
    save(output / "prepared.json", {"status": "仅离线准备，未接设备", "files": hashes,
                                   "layout": layout, "loaded_flash_bytes": len(image), "limits": LIMITS})
    return PreparedBuild(image, layout, hashes, output)


def decode(data, prefix, fields, layout):
    require(len(data) == layout[prefix + "_size"], "SWD 读取长度不足：" + prefix)
    result = {}
    for field, fmt in fields.items():
        if prefix + "." + field in ENUM_FIELDS:
            fmt = {1: "B", 2: "H", 4: "I"}[layout[prefix + "." + field + ".size"]]
        result[field] = struct.unpack_from("<" + fmt, data, layout[prefix + "." + field + ".offset"])[0]
    return result


def read_result(backend, layout, tag, timeout_s=20.0, clock=time.monotonic):
    deadline = clock() + timeout_s
    for attempt in range(3):
        require(clock() < deadline, "结果只读复核超时")
        raw = backend.read([("seq_before", layout["sequence"], 4),
                            ("result", layout["result"], layout["result_size"]),
                            ("seq_after", layout["sequence"], 4)],
                           tag if attempt == 0 else tag + f"-coherent-{attempt + 1}", deadline - clock())
        version = word(raw["seq_before"])
        if version == word(raw["seq_after"]) and version % 2 == 0:
            result = decode(raw["result"], "result", RESULT_FIELDS, layout)
            require(result["schema_version"] == 1, "结果 schema 版本不符，禁止按旧单后端布局解释")
            require(result["state"] in range(4) and result["failure"] in range(6), "结果状态或失败原因非法")
            return result, raw["result"], version
    raise RuntimeError("结果三次只读复核未取得一致版本")


def read_auxiliary(backend, layout, prefix, tag, timeout_s=20.0, clock=time.monotonic):
    fields = CONTROLLER_FIELDS if prefix == "controller" else IRQ_FIELDS
    deadline = clock() + timeout_s
    for attempt in range(3):
        require(clock() < deadline, "附加结果只读复核超时：" + prefix)
        raw = backend.read([("seq_before", layout[prefix + "seq"], 4),
                            (prefix, layout[prefix], layout[prefix + "_size"]),
                            ("seq_after", layout[prefix + "seq"], 4)],
                           tag if attempt == 0 else tag + f"-coherent-{attempt + 1}", deadline - clock())
        version = word(raw["seq_before"])
        if version == word(raw["seq_after"]) and version % 2 == 0:
            result = decode(raw[prefix], prefix, fields, layout)
            require(result["state"] in range(4) and result["failure"] in range(8 if prefix == "controller" else 6),
                    "附加结果状态或失败原因非法：" + prefix)
            if prefix == "irq":
                require(result["schema_version"] == 1, "IRQ 结果 schema 不符")
            return result, raw[prefix], version
    raise RuntimeError("附加结果三次只读复核未取得一致版本：" + prefix)


def read_health(backend, layout, conditions, tag, timeout_s=20.0, clock=time.monotonic):
    blocks = [("guard_before", layout["guard"], 4), ("bdtr_before", layout["bdtr"], 4), ("cpu_before", DHCSR, 4),
              ("pub_before", layout["publication_sequence"], 4),
              ("publication", layout["publication"], layout["publication_size"]),
              ("pub_after", layout["publication_sequence"], 4), ("request", layout["request"], 4),
              ("guard_after", layout["guard"], 4), ("bdtr_after", layout["bdtr"], 4), ("cpu_after", DHCSR, 4)]
    deadline = clock() + timeout_s
    for attempt in range(8):
        require(clock() < deadline, "健康只读复核超时")
        raw = backend.read(blocks, tag if attempt == 0 else tag + f"-coherent-{attempt + 1}", deadline - clock())
        for suffix in ["before", "after"]:
            require(word(raw["guard_" + suffix]) == 0, "独立 guard 存在故障")
            require(word(raw["bdtr_" + suffix]) & 0x8000 == 0, "检测到 MOE 功率使能")
            require(word(raw["cpu_" + suffix]) & ((1 << 17) | (1 << 19)) == 0, "CPU 已暂停或锁死")
        version = word(raw["pub_before"])
        if version == word(raw["pub_after"]) and version % 2 == 0:
            break
        # 发布每 10 ms 更新；只读冲突短暂错开相位，避免 USB 读周期持续撞上同一边界。
        if attempt < 7:
            time.sleep(min(0.001 * (attempt + 1), max(0.0, deadline - clock())))
    else:
        raise RuntimeError("健康快照八次只读复核未取得一致版本")
    publication = decode(raw["publication"], "publication", PUBLICATION_FIELDS, layout)
    require(all(publication[field] == 1 for field in ["snapshot.status_valid", "snapshot.control_time_valid",
                                                    "snapshot.bus_voltage_valid", "has_sample", "compute_probe_permitted"]),
            "缺少有效观测、时基或后台诊断许可")
    require(publication["snapshot.state"] == 1 and publication["snapshot.faults"] == 0, "Motor 并非无故障 IDLE")
    bus = publication["snapshot.bus_voltage_v"]
    require(math.isfinite(bus) and 0.0 <= bus <= 3.6 and abs(bus - conditions.vbus_dmm_v) <= 0.3,
            "软件 VBUS 超范围或与万用表相差超过 0.3 V")
    require(publication["snapshot.control_time_ticks_per_second"] == 20000, "控制时基频率不符")
    return {"time": publication["snapshot.control_time_ticks"], "ticks_per_second": 20000,
            "bus_voltage_v": bus, "request": word(raw["request"])}


def timing_summary(values, expected_count):
    count, low, high, total = (values[field] for field in ["count", "min_cycles", "max_cycles", "sum_cycles"])
    clean_count = values["clean_count"]
    clean_low, clean_high, clean_sum = (values[field] for field in ["clean_min_cycles", "clean_max_cycles", "clean_sum_cycles"])
    require(count == expected_count and 0 <= clean_count <= count, "分段或 clean 次数不符")
    def consistent(n, minimum, maximum, summed):
        if n == 0:
            return minimum == maximum == summed == 0
        return (0 < minimum <= maximum <= MAX_ELAPSED and n * minimum <= summed <= n * maximum
                and (minimum == maximum or n >= 2 and (n - 1) * minimum + maximum <= summed <= (n - 1) * maximum + minimum))
    require(consistent(count, low, high, total) and consistent(clean_count, clean_low, clean_high, clean_sum),
            "分段或 clean 极值/总和不自洽")
    require((values["irq_entries"] == 0) == (clean_count == count)
            and values["irq_entries"] >= count - clean_count, "IRQ 与 clean 次数不自洽")
    if clean_count:
        require(low <= clean_low <= clean_high <= high, "clean 极值超出完整样本范围")
    require((count - clean_count) * low <= total - clean_sum <= (count - clean_count) * high,
            "clean 总和与完整样本不自洽")
    if clean_count == count:
        require((low, high, total) == (clean_low, clean_high, clean_sum), "全部 clean 时两套统计必须相同")
    def converted(n, minimum, maximum, summed):
        return {"count": n, "min_us": minimum * 1e6 / CLOCK_HZ if n else None,
                "mean_us": summed / n * 1e6 / CLOCK_HZ if n else None,
                "max_us": maximum * 1e6 / CLOCK_HZ if n else None}
    return {**values, "all": converted(count, low, high, total),
            "clean": converted(clean_count, clean_low, clean_high, clean_sum)}


def validate_probe(result, request):
    require(result["request_sequence"] == request, "内外层请求身份不符")
    require(result["state"] == 2 and result["failure"] == 0 and result["completed_steps"] == CASE_COUNT * ITERATIONS
            and result["next_case"] == 0, "计时没有完整成功或固定 96 步不完整")
    require(result["clock_hz"] == CLOCK_HZ and result["max_elapsed_cycles"] == MAX_ELAPSED, "计时频率或单例上界不符")
    cases = []
    for index, name in enumerate(CASE_NAMES):
        values = {field: result[f"cases[{index}].{field}"] for field in
                  ["count", "min_cycles", "max_cycles", "sum_cycles", "last_output_valid", "last_voltage_limited", "last_speed_limited"]}
        count, low, high, total = (values[field] for field in ["count", "min_cycles", "max_cycles", "sum_cycles"])
        require(count == ITERATIONS and 0 < low <= high <= MAX_ELAPSED and count * low <= total <= count * high,
                "算例次数、极值或总和不自洽：" + name)
        require(low == high or count >= 2 and (count - 1) * low + high <= total <= (count - 1) * high + low,
                "算例极值未出现在统计总和：" + name)
        require(all(values[field] in (0, 1) for field in ["last_output_valid", "last_voltage_limited", "last_speed_limited"]),
                "算例布尔状态非法")
        require(values["last_output_valid"] == int(index < 4), "预期接受/拒绝路径不符：" + name)
        if index == 3:
            require(values["last_voltage_limited"] == values["last_speed_limited"] == 1, "未覆盖预期双限幅")
        duties = [result[f"cases[{index}].last_duty[{phase}]"] for phase in range(3)]
        require(all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in duties), "诊断 duty 非法")
        if index >= 4:
            require(duties == [0.5] * 3 and values["last_voltage_limited"] == 0, "拒绝路径未保留中性无效输出")
        phasor = [result[f"cases[{index}].last_phasor[{component}]"] for component in range(4)]
        require(all(math.isfinite(value) and abs(value) <= 1.00002 for value in phasor), "诊断相量非法")
        segments = []
        for segment, segment_name in enumerate(SEGMENT_NAMES):
            expected = ITERATIONS if segment in ([0, 1] if index == 4 else [0, 1, 2, 4, 5] + ([3] if index == 2 else [])) else 0
            timing = {field: result[f"cases[{index}].segments[{segment}].{field}"] for field in TIMING_FIELDS}
            summary = timing_summary(timing, expected)
            summary["name"] = segment_name
            segments.append(summary)
        require(all(values[field] == segments[0][field] for field in ["count", "min_cycles", "max_cycles", "sum_cycles"]),
                "total 与兼容算例统计不一致")
        values.update(name=name, mean_cycles=total / count, min_us=low * 1e6 / CLOCK_HZ,
                      mean_us=total / count * 1e6 / CLOCK_HZ, max_us=high * 1e6 / CLOCK_HZ,
                      last_duty=duties, last_phasor=phasor, segments=segments)
        cases.append(values)
    return cases


def validate_complete(result):
    require(result["schema_version"] == 1, "结果 schema 版本不符")
    require(result["request_sequence"] != 0 and result["state"] == 2 and result["failure"] == 0
            and result["completed_steps"] == 192 and result["sweep_count"] == 257,
            "对照没有完整成功，或 192 步/257 点扫描不完整")
    require(all(math.isfinite(result[field]) and 0.0 <= result[field] <= 2e-5 for field in ERROR_FIELDS),
            "软件/CORDIC 全周或配对误差非法或超限")
    backends = {}
    for backend in ["software", "cordic"]:
        nested = {field: result[backend + "." + field] for field in PROBE_FIELDS}
        backends[backend] = validate_probe(nested, result["request_sequence"])
    comparison = []
    for software, cordic in zip(backends["software"], backends["cordic"]):
        require(all(software[field] == cordic[field] for field in ["last_output_valid", "last_voltage_limited", "last_speed_limited"]),
                "两个后端接受/拒绝或限幅路径不同")
        for field, maximum in [("last_duty", "max_duty_error"), ("last_phasor", "max_phasor_error")]:
            require(all(abs(a - b) <= 2e-5 and abs(a - b) <= result[maximum] + 1e-7
                        for a, b in zip(software[field], cordic[field])), "后端配对结果与误差摘要不符")
        segments = []
        for a, b in zip(software["segments"], cordic["segments"]):
            sa, sb = a["clean"]["mean_us"], b["clean"]["mean_us"]
            segments.append({"name": a["name"], "software": a, "cordic": b,
                             "clean_mean_speedup": sa / sb if sa is not None and sb is not None else None})
        comparison.append({"name": software["name"], "segments": segments})
    return backends, comparison


def validate_controller(result, request):
    require(result["request_sequence"] == request, "控制器分段请求身份不符")
    require(result["state"] == 2 and result["failure"] == 0, "控制器分段没有完整成功")
    require((result["call_count"], result["valid_calls"], result["rejected_calls"]) == (160, 128, 32),
            "控制器有效或拒绝调用次数不完整")
    segments = []
    for index, name in enumerate(CONTROLLER_SEGMENT_NAMES):
        values = {field: result[f"timing[{index}].{field}"] for field in TIMING_FIELDS}
        segments.append({"name": name, **timing_summary(values, 128)})
    total, pieces = segments[0], segments[1:]
    require(all(total[field] == sum(piece[field] for piece in pieces) for field in ["sum_cycles", "irq_entries"]),
            "控制器相邻边界与总段累计不一致")
    require(total["clean_count"] <= min(piece["clean_count"] for piece in pieces)
            and all(piece["max_cycles"] <= total["max_cycles"] and piece["min_cycles"] <= total["min_cycles"]
                    for piece in pieces), "控制器子段超出总段边界")
    return {"result": result, "segments": segments,
            "limit": "mark0 到 mark6 及相邻边界独立计差，包含探针开销；不能当作无探针 WCET。"}


def validate_irq(result, request):
    require(result["schema_version"] == 1 and result["request_sequence"] == request, "IRQ schema 或请求身份不符")
    require(result["state"] == 2 and result["failure"] == 0 and result["irq_count"] == 256,
            "IRQ 窗口没有完整成功或次数不完整")
    rows = []
    for index, name in enumerate(IRQ_NAMES):
        values = {field: result[f"rows[{index}].{field}"] for field in
                  ["count", "min_cycles", "max_cycles", "sum_cycles"]}
        count, low, high, total = (values[field] for field in ["count", "min_cycles", "max_cycles", "sum_cycles"])
        require((count == 256 if index == 0 else 0 < count <= 256), "IRQ 各行次数不完整")
        require(0 < low <= high <= MAX_ELAPSED and count * low <= total <= count * high
                and (low == high or count >= 2 and (count - 1) * low + high <= total <= (count - 1) * high + low),
                "IRQ 极值或总和不自洽")
        rows.append({"name": name, **values, "min_us": low * 1e6 / CLOCK_HZ,
                     "mean_cycles": total / count, "mean_us": total / count * 1e6 / CLOCK_HZ,
                     "max_us": high * 1e6 / CLOCK_HZ})
    require(rows[1]["count"] == rows[2]["count"] == rows[3]["count"], "IRQ injected 嵌套行次数不一致")
    return {"result": result, "rows": rows,
            "limit": "含抢占和部分插桩；IRQ USER CODE 区间不含异常入栈出栈及末尾统计，不能推算完整 ISR/WCET。"}


def wait_irq_result(backend, layout, request, previous, deadline, clock=time.monotonic, sleep=time.sleep):
    # IRQ 窗口在主算法冻结后才开始，避免附加 ISR 统计干扰后台算法读数。
    for attempt in range(101):
        remaining = deadline - clock()
        require(remaining > 0.0, "IRQ 窗口只读等待超过 1 秒，不补发请求")
        snapshot = read_auxiliary(backend, layout, "irq",
                                  "irq-result" if attempt == 0 else f"irq-result-poll-{attempt + 1}",
                                  remaining, clock)
        result = snapshot[0]
        require(result["state"] != 3, "IRQ 窗口失败，原因编号：" + str(result["failure"]))
        require(result["failure"] == 0, "IRQ 活动结果失败字段不为零")
        if result["request_sequence"] == request:
            if result["state"] == 2:
                return snapshot
            require(result["state"] in (0, 1), "IRQ 窗口状态非法")
        else:
            require(result == previous, "IRQ 窗口身份被其他请求改写")
        sleep(min(0.01, max(0.0, deadline - clock())))
    raise RuntimeError("IRQ 窗口只读次数超过上界，不补发请求")


def run_measurement(backend, prepared, conditions, clock=time.monotonic, sleep=time.sleep):
    conditions.validate(measure=True)
    layout = prepared.layout
    validate_layout(layout)
    flash = backend.read([("flash", FLASH_START, FLASH_SIZE)], "flash-identity")["flash"]
    require(len(flash) == FLASH_SIZE and all(flash[address - FLASH_START] == value for address, value in prepared.image.items()),
            "实机 Flash 与指定候选不符")
    existing, _, _ = read_result(backend, layout, "pre-result", clock=clock)
    require(existing["state"] != 1, "已有活动计时请求")
    previous_auxiliary = {}
    for prefix in ["controller", "irq"]:
        auxiliary, _, _ = read_auxiliary(backend, layout, prefix, "pre-" + prefix, clock=clock)
        require(auxiliary["state"] != 1, "已有活动附加计时请求：" + prefix)
        require(auxiliary["request_sequence"] == existing["request_sequence"], "附加计时旧身份不一致：" + prefix)
        previous_auxiliary[prefix] = auxiliary
    before = read_health(backend, layout, conditions, "pre-health-1", clock=clock)
    sleep(0.02)
    advanced = read_health(backend, layout, conditions, "pre-health-2", clock=clock)
    require(advanced["time"] > before["time"], "控制时间未持续推进")
    require(before["request"] == advanced["request"] == existing["request_sequence"], "存在未消费或其他主机改写的请求")
    sequence = ((advanced["request"] + 1) & 0xFFFFFFFF) or 1
    deadline = clock() + 2.0
    backend.write_request(layout["request"], sequence, timeout_s=deadline - clock())
    for _ in range(201):
        remaining = deadline - clock()
        require(remaining > 0.0, "计时等待超过 2 秒，不补发请求")
        result, payload, version = read_result(backend, layout, "result-poll", remaining, clock)
        if result["request_sequence"] == sequence:
            require(result["state"] != 3, "计时失败，原因编号：" + str(result["failure"]))
            if result["state"] == 2:
                backends, comparison = validate_complete(result)
                break
            require(result["state"] == 1 and result["failure"] == 0 and result["completed_steps"] == 0,
                    "活动结果发布了非法状态或部分窗口")
        else:
            require(result == existing, "计时身份被其他请求改写")
        sleep(min(0.01, max(0.0, deadline - clock())))
    else:
        raise RuntimeError("计时读取次数超过上界，不补发请求")
    require(clock() < deadline, "完整结果复核已超时")
    repeated, repeated_payload, repeated_version = read_result(backend, layout, "result-confirm", deadline - clock(), clock)
    require(result == repeated and payload == repeated_payload and version == repeated_version, "冻结结果第二次读取变化")
    auxiliary_reports = {}
    for prefix, validate in [("controller", validate_controller), ("irq", validate_irq)]:
        auxiliary_deadline = deadline if prefix == "controller" else clock() + 1.0
        if prefix == "irq":
            auxiliary, auxiliary_payload, auxiliary_version = wait_irq_result(
                backend, layout, sequence, previous_auxiliary[prefix], auxiliary_deadline, clock, sleep)
        else:
            auxiliary, auxiliary_payload, auxiliary_version = read_auxiliary(
                backend, layout, prefix, prefix + "-result", auxiliary_deadline - clock(), clock)
        auxiliary_reports[prefix] = validate(auxiliary, sequence)
        confirmation = read_auxiliary(backend, layout, prefix, prefix + "-confirm", auxiliary_deadline - clock(), clock)
        require((auxiliary, auxiliary_payload, auxiliary_version) == confirmation,
                "附加冻结结果第二次读取变化：" + prefix)
        auxiliary_reports[prefix]["sequence"] = auxiliary_version
    after = read_health(backend, layout, conditions, "post-health-1", clock=clock)
    sleep(0.02)
    latest = read_health(backend, layout, conditions, "post-health-2", clock=clock)
    require(after["time"] > advanced["time"] and latest["time"] > after["time"], "完成后控制时间未持续推进")
    require(after["request"] == latest["request"] == sequence, "完成后请求身份改变")
    return {"status": "后台算法诊断完成", "request_sequence": sequence, "before": before, "after": latest,
            "result": result, "backends": backends, "comparison": comparison,
            "controller_profile": auxiliary_reports["controller"], "irq_timing": auxiliary_reports["irq"],
            "errors": {field: result[field] for field in ERROR_FIELDS}, "files": prepared.hashes, "write_count": 1,
            "automatic_retry": False, "hardware_qualified": False, "stage_timing_qualified": False,
            "full_isr_wcet": False, "limits": LIMITS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True, help="同次 Observe HEX/AXF/MAP 目录")
    parser.add_argument("--expected-hex-sha256", required=True, help="指定候选的完整 HEX SHA256")
    parser.add_argument("--output", type=Path, required=True, help="新的证据目录，禁止覆盖")
    parser.add_argument("--measure", action="store_true", help="显式执行一次 HOTPLUG 后台诊断")
    parser.add_argument("--logic-only-confirmed", action="store_true", help="确认母线电源、电池、三相、外部注入源断开，仅逻辑供电")
    parser.add_argument("--vbus-dmm-v", type=float, help="实测时必填，当前 VBUS 万用表读数 0..3.6 V")
    parser.add_argument("--gdb", type=Path, default=DEFAULT_GDB)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    args = parser.parse_args(argv)
    conditions = Conditions(args.vbus_dmm_v, args.logic_only_confirmed)
    conditions.validate(args.measure)
    prepared = prepare_build(args.build_dir.resolve(), args.expected_hex_sha256, args.output.resolve(), args.gdb.resolve())
    backend = None
    try:
        if not args.measure:
            print("离线镜像与类型已核对；未连接设备，未提交请求。")
            return 0
        backend = CubeBackend(prepared, args.cli.resolve())
        report = run_measurement(backend, prepared, conditions)
        save(prepared.output / "result.json", report)
        print("软件/CORDIC 各 96 次算例与 257 点全周对照完成；分段 clean 统计不代表完整 ISR 或 WCET。")
        return 0
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
