"""Observe 后台算法耗时诊断；默认只离线核对，显式 --measure 才写一次请求。"""

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
                             digest, read_hex, require, save, word)


CASE_NAMES = ["普通计算", "速度输出保持", "独立施加角", "电流电压限幅", "坏电流拒绝", "坏母线拒绝"]
CASE_COUNT, ITERATIONS, CLOCK_HZ, MAX_ELAPSED = 6, 16, 170000000, 34000
RESULT_FIELDS = {"request_sequence": "I", "state": "I", "failure": "I", "clock_hz": "I",
                 "max_elapsed_cycles": "I", "completed_steps": "I", "next_case": "I"}
for index in range(CASE_COUNT):
    RESULT_FIELDS.update({f"cases[{index}].{field}": fmt for field, fmt in {
        "count": "I", "min_cycles": "I", "max_cycles": "I", "sum_cycles": "Q",
        "last_duty[0]": "f", "last_duty[1]": "f", "last_duty[2]": "f",
        "last_output_valid": "B", "last_voltage_limited": "B", "last_speed_limited": "B"}.items()})
PUBLICATION_FIELDS = {
    "snapshot.control_time_ticks": "Q", "snapshot.control_time_ticks_per_second": "I",
    "snapshot.state": "I", "snapshot.faults": "I", "snapshot.bus_voltage_v": "f",
    "snapshot.status_valid": "B", "snapshot.control_time_valid": "B",
    "snapshot.bus_voltage_valid": "B", "has_sample": "B", "compute_probe_permitted": "B"}
ENUM_FIELDS = {"result.state", "result.failure", "publication.snapshot.state"}
SYMBOLS = {"request": "g_target_runtime_compute_request", "sequence": "g_target_runtime_compute_sequence",
           "result": "g_target_runtime_compute_result", "guard": "g_target_runtime_guard_faults",
           "publication_sequence": "s_publication_sequence", "publication": "s_publication"}
EXPECTED_ENUMS = {"MOTOR_STATE_IDLE": 1, "OBSERVE_COMPUTE_PROBE_IDLE": 0,
                  "OBSERVE_COMPUTE_PROBE_ACTIVE": 1, "OBSERVE_COMPUTE_PROBE_COMPLETE": 2,
                  "OBSERVE_COMPUTE_PROBE_FAILED": 3, "OBSERVE_COMPUTE_PROBE_FAILURE_NONE": 0}
LIMITS = ["独立合成算法状态，诊断 duty 未提交硬件。", "读数包含测量开销和中断抢占。",
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
    for key, name in SYMBOLS.items():
        spec[key], spec[key + "_size"] = "&" + name, "sizeof(" + name + ")"
    spec["bdtr"] = f"&((TIM_TypeDef*){TIM1_BASE})->BDTR"
    spec["case_count"] = "sizeof(((ObserveComputeProbeResult*)0)->cases)/sizeof(ObserveComputeProbeCaseResult)"
    for prefix, type_name, fields in [("result", "ObserveComputeProbeResult", RESULT_FIELDS),
                                      ("publication", "RuntimePublication", PUBLICATION_FIELDS)]:
        for field in fields:
            expression = f"(({type_name}*)0)->{field}"
            spec[prefix + "." + field + ".offset"] = "&" + expression
            spec[prefix + "." + field + ".size"] = "sizeof(" + expression + ")"
    spec.update({"enum." + name: name for name in EXPECTED_ENUMS})
    return spec


def validate_layout(layout):
    require(set(layout) == set(layout_spec()), "AXF 类型布局项目不完整")
    regions = []
    for key in SYMBOLS:
        address, size = layout[key], layout[key + "_size"]
        require(type(address) is int and type(size) is int and size > 0 and address % 4 == 0
                and RAM_START <= address < address + size <= RAM_END, "诊断符号地址或大小异常：" + key)
        regions.append((address, address + size))
    require(all(first[1] <= second[0] for first, second in zip(sorted(regions), sorted(regions)[1:])),
            "诊断符号重叠")
    require(all(layout[key + "_size"] == 4 for key in ["request", "sequence", "guard", "publication_sequence"]),
            "诊断控制字不是 uint32")
    require(TIM1_BASE <= layout["bdtr"] <= TIM1_BASE + 0x100 and layout["bdtr"] % 4 == 0,
            "TIM1 BDTR 类型地址异常")
    require(layout["case_count"] == CASE_COUNT, "算例数量与本工具不符")
    for prefix, fields in [("result", RESULT_FIELDS), ("publication", PUBLICATION_FIELDS)]:
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
    arguments = [str(gdb), "-nx", "-batch", "-ex", 'file "' + axf.as_posix() + '"']
    for expression in spec.values():
        arguments += ["-ex", "p/x (unsigned int)(" + expression + ")"]
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
        regions = re.findall(r"Load Region \S+ \(Base: (0x[0-9a-fA-F]+), Size: (0x[0-9a-fA-F]+)", mapping)
        require(len(regions) == 1 and tuple(int(value, 0) for value in regions[0]) == (min(image), len(image))
                and max(image) - min(image) + 1 == len(image), "MAP 加载区域与 HEX/AXF 不符")
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
            require(result["state"] in range(4) and result["failure"] in range(6), "结果状态或失败原因非法")
            return result, raw["result"], version
    raise RuntimeError("结果三次只读复核未取得一致版本")


def read_health(backend, layout, conditions, tag, timeout_s=20.0, clock=time.monotonic):
    blocks = [("guard_before", layout["guard"], 4), ("bdtr_before", layout["bdtr"], 4), ("cpu_before", DHCSR, 4),
              ("pub_before", layout["publication_sequence"], 4),
              ("publication", layout["publication"], layout["publication_size"]),
              ("pub_after", layout["publication_sequence"], 4), ("request", layout["request"], 4),
              ("guard_after", layout["guard"], 4), ("bdtr_after", layout["bdtr"], 4), ("cpu_after", DHCSR, 4)]
    deadline = clock() + timeout_s
    for attempt in range(3):
        require(clock() < deadline, "健康只读复核超时")
        raw = backend.read(blocks, tag if attempt == 0 else tag + f"-coherent-{attempt + 1}", deadline - clock())
        for suffix in ["before", "after"]:
            require(word(raw["guard_" + suffix]) == 0, "独立 guard 存在故障")
            require(word(raw["bdtr_" + suffix]) & 0x8000 == 0, "检测到 MOE 功率使能")
            require(word(raw["cpu_" + suffix]) & ((1 << 17) | (1 << 19)) == 0, "CPU 已暂停或锁死")
        version = word(raw["pub_before"])
        if version == word(raw["pub_after"]) and version % 2 == 0:
            break
    else:
        raise RuntimeError("健康快照三次只读复核未取得一致版本")
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


def validate_complete(result):
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
        values.update(name=name, mean_cycles=total / count, min_us=low * 1e6 / CLOCK_HZ,
                      mean_us=total / count * 1e6 / CLOCK_HZ, max_us=high * 1e6 / CLOCK_HZ,
                      last_duty=duties)
        cases.append(values)
    return cases


def run_measurement(backend, prepared, conditions, clock=time.monotonic, sleep=time.sleep):
    conditions.validate(measure=True)
    layout = prepared.layout
    validate_layout(layout)
    flash = backend.read([("flash", FLASH_START, FLASH_SIZE)], "flash-identity")["flash"]
    require(len(flash) == FLASH_SIZE and all(flash[address - FLASH_START] == value for address, value in prepared.image.items()),
            "实机 Flash 与指定候选不符")
    existing, _, _ = read_result(backend, layout, "pre-result", clock=clock)
    require(existing["state"] != 1, "已有活动计时请求")
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
                cases = validate_complete(result)
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
    after = read_health(backend, layout, conditions, "post-health-1", clock=clock)
    sleep(0.02)
    latest = read_health(backend, layout, conditions, "post-health-2", clock=clock)
    require(after["time"] > advanced["time"] and latest["time"] > after["time"], "完成后控制时间未持续推进")
    require(after["request"] == latest["request"] == sequence, "完成后请求身份改变")
    return {"status": "后台算法诊断完成", "request_sequence": sequence, "before": before, "after": latest,
            "result": result, "cases": cases, "files": prepared.hashes, "write_count": 1,
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
        print("96 次后台算例已完成；读数包含抢占，不代表完整 ISR 或 WCET。")
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
