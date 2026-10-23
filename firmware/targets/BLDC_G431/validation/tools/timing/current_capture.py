"""Observe 已知电流对照：默认仅离线准备；显式 --acquire 才进行一次无功率采集。"""

import argparse
from dataclasses import dataclass
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

from elftools.elf.elffile import ELFFile


DEFAULT_CLI = Path(r"D:\cube\cubeclt\STM32CubeCLT_1.22.0\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe")
DEFAULT_GDB = DEFAULT_CLI.parents[2] / "GNU-tools-for-STM32/bin/arm-none-eabi-gdb.exe"
FLASH_START, FLASH_SIZE = 0x08000000, 0x20000
RAM_START, RAM_END = 0x20000000, 0x20008000
DHCSR = 0xE000EDF0
# G431 的外设基址来自本 Target 的 CMSIS 设备映射；BDTR 偏移仍从同次 AXF 类型取得。
TIM1_BASE = 0x40012C00
SAMPLES = 2048
RESULT_FIELDS = {
    "request_sequence": "I", "state": "I", "failure": "I", "sample_count": "I",
    "first_sample_time": "Q", "last_sample_time": "Q", "offset_b_a": "f", "offset_c_a": "f",
    **{f"phase_{phase}.{field}": "f" for phase in "bc"
       for field in ["mean_a", "min_a", "max_a", "variance_a2"]},
}
PUBLICATION_FIELDS = {
    "snapshot.control_time_ticks": "Q", "snapshot.control_time_ticks_per_second": "I",
    "snapshot.state": "I", "snapshot.faults": "I", "snapshot.bus_voltage_v": "f",
    "snapshot.status_valid": "B", "snapshot.control_time_valid": "B",
    "snapshot.bus_voltage_valid": "B", "has_sample": "B",
}
ZERO_FIELDS = {
    "state": "I", "failure": "I", "sample_count": "I",
    "offset_a[1]": "f", "offset_a[2]": "f",
    "config.calibration_sample_count": "I", "config.measured_phases": "I",
}
ENUM_FIELDS = {"capture.state", "capture.failure", "publication.snapshot.state", "zero.state", "zero.failure"}
SYMBOLS = {
    "request": "g_target_runtime_current_request", "sequence": "g_target_runtime_current_sequence",
    "capture": "g_target_runtime_current_capture", "guard": "g_target_runtime_guard_faults",
    "publication_sequence": "s_publication_sequence", "publication": "s_publication",
    "runtime": "s_control_runtime",
}


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Reference:
    phase: str
    current_a: float
    vbus_dmm_v: float
    logic_only_confirmed: bool = False
    source: str = "dmm"  # 末尾新增默认项，保留已有四个位置参数的含义。

    def validate(self, acquire=False):
        require(self.phase in ("B", "C"), "参考相只能为 B 或 C")
        require(self.source in ("dmm", "supply-display"), "参考电流来源只能为 dmm 或 supply-display")
        current_label = "电源实际显示的" if self.source == "supply-display" else "万用表"
        require(math.isfinite(self.current_a) and abs(self.current_a) <= 0.25,
                current_label + "有符号参考电流必须有限且绝对值不超过 0.25 A")
        require(math.isfinite(self.vbus_dmm_v) and 0.0 <= self.vbus_dmm_v <= 3.6,
                "万用表 VBUS 必须有限且位于 0..3.6 V")
        require(not acquire or self.logic_only_confirmed,
                "采集要求明确确认电机母线电源和电机三相断开；启动校零时外部注入源断开，零偏 READY 后才接入限流注入支路")


@dataclass
class PreparedBuild:
    image: dict
    layout: dict
    hashes: dict
    output: Path


def read_hex(path):
    image, base, ended = {}, 0, False
    for line in path.read_text(encoding="ascii").splitlines():
        require(line.startswith(":"), "HEX 记录格式错误")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as error:
            raise RuntimeError("HEX 字节格式错误") from error
        require(len(record) >= 5 and len(record) == record[0] + 5
                and sum(record) % 256 == 0 and not ended, "HEX 长度、校验和或结束记录错误")
        length, kind = record[0], record[3]
        if kind == 4:
            require(length == 2 and record[1:3] == b"\0\0", "HEX 扩展地址错误")
            base = int.from_bytes(record[4:6], "big") << 16
        elif kind == 0:
            address = base + int.from_bytes(record[1:3], "big")
            for index, value in enumerate(record[4:4 + length]):
                require(address + index not in image, "HEX 加载字节重叠")
                image[address + index] = value
        elif kind == 1:
            require(length == 0 and record[1:3] == b"\0\0", "HEX 结束记录错误")
            ended = True
        else:
            require(kind == 5 and length == 4 and record[1:3] == b"\0\0", "HEX 包含不支持的记录")
    require(ended and image and FLASH_START <= min(image) <= max(image) < FLASH_START + FLASH_SIZE,
            "HEX 缺少镜像、结束记录或超出 G431 Flash")
    return image


def validate_map_load_region(mapping, image):
    """MAP 的 Size 可为未压缩布局大小；仅明确 COMPRESSED 项提供物理加载长度。"""
    lines = re.findall(r"(?m)^\s*Load Region\b[^\r\n]*", mapping)
    require(len(lines) == 1, "MAP 必须恰好包含一个加载区域")
    region = re.fullmatch(
        r"\s*Load Region \S+ \(Base:\s*(0x[0-9a-fA-F]+),\s*Size:\s*(0x[0-9a-fA-F]+)"
        r"(?:,\s*Max:\s*(0x[0-9a-fA-F]+))?(?:,\s*ABSOLUTE)?"
        r"(?:,\s*COMPRESSED\[(0x[0-9a-fA-F]+)\])?\)\s*", lines[0])
    require(region is not None, "MAP 加载区域或压缩标记格式非法")
    base, logical_size = int(region[1], 0), int(region[2], 0)
    maximum = int(region[3], 0) if region[3] else None
    physical_size = int(region[4], 0) if region[4] else logical_size
    require(0 < physical_size <= logical_size and (maximum is None or logical_size <= maximum),
            "MAP 物理加载长度或区域上界非法")
    require(bool(image) and (base, physical_size) == (min(image), len(image))
            and max(image) - min(image) + 1 == len(image), "MAP 加载区域与 HEX/AXF 物理字节不同")


def layout_spec():
    spec = {}
    for key, symbol in SYMBOLS.items():
        spec[key] = "&" + symbol
        spec[key + "_size"] = "sizeof(" + symbol + ")"
    spec["zero"] = "&s_control_runtime.current_measurement"
    spec["zero_size"] = "sizeof(s_control_runtime.current_measurement)"
    spec["bdtr"] = f"&((TIM_TypeDef*){TIM1_BASE})->BDTR"
    for prefix, expression, fields in [
        ("capture", "((ObserveCurrentCaptureResult*)0)->", RESULT_FIELDS),
        ("publication", "((RuntimePublication*)0)->", PUBLICATION_FIELDS),
        ("zero", "((CurrentMeasurement*)0)->", ZERO_FIELDS),
    ]:
        for field in fields:
            spec[prefix + "." + field + ".offset"] = "&" + expression + field
            spec[prefix + "." + field + ".size"] = "sizeof(" + expression + field + ")"
    # 枚举只用于确认本工具认识当前 ABI；不把调试器的类型假设带到另一份镜像。
    for name in ["MOTOR_STATE_IDLE", "CURRENT_MEASUREMENT_READY", "CURRENT_MEASUREMENT_PHASE_BC",
                 "OBSERVE_CURRENT_CAPTURE_SAMPLES", "OBSERVE_CURRENT_CAPTURE_IDLE",
                 "OBSERVE_CURRENT_CAPTURE_ACTIVE", "OBSERVE_CURRENT_CAPTURE_COMPLETE",
                 "OBSERVE_CURRENT_CAPTURE_FAILED"]:
        if name != "OBSERVE_CURRENT_CAPTURE_SAMPLES":  # 宏不保证出现在 DWARF 中。
            spec["enum." + name] = name
    return spec


def validate_layout(layout):
    require(set(layout) == set(layout_spec()), "AXF 类型布局项目不完整")
    regions = []
    for key in SYMBOLS:
        address, size = layout[key], layout[key + "_size"]
        require(type(address) is int and type(size) is int and size > 0
                and RAM_START <= address < address + size <= RAM_END, "诊断符号超出 SRAM：" + key)
        require(address % 4 == 0, "诊断符号未按字对齐：" + key)
        regions.append((address, address + size, key))
    for first, second in zip(sorted(regions), sorted(regions)[1:]):
        require(first[1] <= second[0], "诊断符号地址重叠：" + first[2] + " / " + second[2])
    for key in ["request", "sequence", "guard", "publication_sequence"]:
        require(layout[key + "_size"] == 4, "诊断控制字宽度不是 uint32：" + key)
    require(layout["runtime"] <= layout["zero"] < layout["zero"] + layout["zero_size"]
            <= layout["runtime"] + layout["runtime_size"], "零偏对象不在 Runtime 实例内")
    require(TIM1_BASE <= layout["bdtr"] <= TIM1_BASE + 0x100 and layout["bdtr"] % 4 == 0,
            "TIM1 BDTR 类型映射异常")
    for prefix, fields in [("capture", RESULT_FIELDS), ("publication", PUBLICATION_FIELDS), ("zero", ZERO_FIELDS)]:
        occupied = set()
        for field, fmt in fields.items():
            offset = layout[prefix + "." + field + ".offset"]
            size = layout[prefix + "." + field + ".size"]
            width_valid = size in (1, 2, 4) if prefix + "." + field in ENUM_FIELDS else size == struct.calcsize("<" + fmt)
            require(width_valid and 0 <= offset
                    and offset + size <= layout[prefix + "_size"], "字段宽度或范围异常：" + prefix + "." + field)
            positions = set(range(offset, offset + size))
            require(not occupied & positions, "诊断字段重叠：" + prefix + "." + field)
            occupied |= positions
    expected = {"MOTOR_STATE_IDLE": 1, "CURRENT_MEASUREMENT_READY": 3,
                "CURRENT_MEASUREMENT_PHASE_BC": 6, "OBSERVE_CURRENT_CAPTURE_IDLE": 0,
                "OBSERVE_CURRENT_CAPTURE_ACTIVE": 1, "OBSERVE_CURRENT_CAPTURE_COMPLETE": 2,
                "OBSERVE_CURRENT_CAPTURE_FAILED": 3}
    require(all(layout["enum." + key] == value for key, value in expected.items()), "固件枚举 ABI 已变化")


def derive_layout(axf, gdb, output):
    require(gdb.is_file(), "找不到离线 ARM GDB：" + str(gdb))
    require(not (output / "layout-gdb.log").exists(), "离线布局日志已存在，拒绝覆盖")
    spec = layout_spec()
    arguments = [str(gdb), "-nx", "-batch", "-ex", 'file "' + axf.as_posix() + '"']
    for expression in spec.values():
        arguments += ["-ex", "p/x (unsigned int)(" + expression + ")"]
    process = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
    with (output / "layout-gdb.log").open("x", encoding="utf-8") as stream:
        stream.write(process.stdout + process.stderr)
    values = re.findall(r"^\$\d+ = (0x[0-9a-fA-F]+|\d+)\s*$", process.stdout, re.M)
    require(process.returncode == 0 and len(values) == len(spec), "同次 AXF 的符号或类型布局不可用")
    layout = dict(zip(spec, map(lambda value: int(value, 0), values)))
    validate_layout(layout)
    return layout


def verify_artifacts(hex_file, axf, map_file, layout):
    image = read_hex(hex_file)
    mapping = map_file.read_text(encoding="utf-8")
    with axf.open("rb") as stream:
        elf = ELFFile(stream)
        require(elf.elfclass == 32 and elf.little_endian and elf["e_machine"] == "EM_ARM", "AXF 不是小端 ARM32")
        loaded = {}
        for segment in elf.iter_segments():
            if segment["p_type"] != "PT_LOAD" or segment["p_filesz"] == 0:
                continue
            address, payload = segment["p_paddr"], segment.data()
            require(FLASH_START <= address < address + len(payload) <= FLASH_START + FLASH_SIZE,
                    "AXF 包含非 Flash 加载区域")
            for index, value in enumerate(payload):
                require(address + index not in loaded, "AXF 加载区域重叠")
                loaded[address + index] = value
        require(image == loaded, "AXF 加载字节与 HEX 不同，不能混用编译产物")
        validate_map_load_region(mapping, image)
        entries = re.findall(r"Image Entry point\s*:\s*(0x[0-9a-fA-F]+)", mapping)
        require(len(entries) == 1 and int(entries[0], 0) == elf["e_entry"]
                and (elf["e_entry"] & ~1) in image, "MAP 入口与 AXF 不同")
        symbols = elf.get_section_by_name(".symtab")
        require(symbols is not None, "AXF 缺少符号表")
        for key, name in SYMBOLS.items():
            records = symbols.get_symbol_by_name(name) or []
            require(len(records) == 1 and records[0]["st_value"] == layout[key]
                    and records[0]["st_size"] == layout[key + "_size"], "AXF 符号与 GDB 布局不符：" + name)
            rows = re.findall(r"^\s*" + re.escape(name) + r"\s+(0x[0-9a-fA-F]+)\s+Data\s+(\d+)\s+", mapping, re.M)
            require(len(rows) == 1 and (int(rows[0][0], 0), int(rows[0][1]))
                    == (layout[key], layout[key + "_size"]), "MAP 符号与 AXF 不符：" + name)
    return image


def prepare_build(build_directory, expected_hex_sha256, output, gdb=DEFAULT_GDB):
    require(re.fullmatch(r"[0-9a-fA-F]{64}", expected_hex_sha256) is not None, "必须提供完整 HEX SHA256")
    require(build_directory.is_dir(), "找不到 Observe 编译目录")
    hex_files = list(build_directory.glob("*.hex"))
    require(len(hex_files) == 1, "编译目录必须恰好包含一份 Observe HEX")
    source = hex_files[0]
    require(digest(source) == expected_hex_sha256.lower(), "HEX SHA256 与指定候选不符")
    require(not output.exists(), "输出目录已存在，拒绝覆盖证据")
    output.mkdir(parents=True)
    frozen = output / "artifacts"
    frozen.mkdir()
    paths = {}
    for suffix in [".hex", ".axf", ".map"]:
        source_file = source.with_suffix(suffix)
        require(source_file.is_file(), "缺少同次编译产物：" + str(source_file))
        paths[suffix] = frozen / source_file.name
        shutil.copyfile(source_file, paths[suffix])
    require(digest(paths[".hex"]) == expected_hex_sha256.lower(), "复制期间 HEX 已变化")
    layout = derive_layout(paths[".axf"], gdb, output)
    image = verify_artifacts(paths[".hex"], paths[".axf"], paths[".map"], layout)
    hashes = {path.name: digest(path) for path in paths.values()}
    save(output / "prepared.json", {"status": "仅离线准备，尚未接设备", "files": hashes, "layout": layout,
                                   "loaded_flash_bytes": len(image), "hardware_qualified": False})
    return PreparedBuild(image, layout, hashes, output)


def decode(data, prefix, fields, layout):
    require(len(data) == layout[prefix + "_size"], "SWD 读取长度不足：" + prefix)
    values = {}
    for field, fmt in fields.items():
        if prefix + "." + field in ENUM_FIELDS:
            fmt = {1: "B", 2: "H", 4: "I"}[layout[prefix + "." + field + ".size"]]
        values[field] = struct.unpack_from("<" + fmt, data, layout[prefix + "." + field + ".offset"])[0]
    return values


def word(data):
    require(len(data) == 4, "SWD 控制字读取不完整")
    return struct.unpack("<I", data)[0]


class CubeBackend:
    """传输层只开放白名单读和 request 单字写；失败后绝不补发。"""
    def __init__(self, prepared, cli=DEFAULT_CLI):
        require(cli.is_file(), "找不到 STM32CubeProgrammer CLI")
        self.layout, self.output, self.cli = prepared.layout, prepared.output, cli
        self.write_attempted = False
        self.transport_write_attempted = False
        self.counter = 0

    def allowed_read(self, address, size, words=False):
        if type(address) is not int or type(size) is not int or size <= 0:
            return False
        ram_or_register = (RAM_START <= address < address + size <= RAM_END
                           or (address, size) in [(self.layout["bdtr"], 4), (DHCSR, 4)])
        if words:
            return ram_or_register and address % 4 == size % 4 == 0
        return ram_or_register or FLASH_START <= address < address + size <= FLASH_START + FLASH_SIZE

    def execute(self, arguments, tag, timeout_s):
        require(timeout_s > 0.0, "采集等待已超时")
        log_file = self.output / (tag + ".log")
        require(not log_file.exists(), "SWD 操作日志已存在，拒绝覆盖")
        if arguments and arguments[0] == "-w32":
            require(len(arguments) == 3 and self.write_attempted and not self.transport_write_attempted
                    and int(arguments[1], 0) == self.layout["request"]
                    and 0 < int(arguments[2], 0) <= 0xFFFFFFFF,
                    "传输层拒绝非单次 request 单字写入")
            self.transport_write_attempted = True
        elif arguments and arguments[0] == "-r32":
            require(len(arguments) % 3 == 0, "快速读取参数不完整")
            for index in range(0, len(arguments), 3):
                require(arguments[index] == "-r32" and re.fullmatch(r"0x[0-9a-fA-F]+", arguments[index + 1]) is not None
                        and re.fullmatch(r"[0-9]+", arguments[index + 2]) is not None,
                        "快速读取只允许 -r32 地址 字节数")
                require(self.allowed_read(int(arguments[index + 1], 16), int(arguments[index + 2]), words=True),
                        "快速读取只允许对齐 SRAM 或指定只读寄存器，禁止 Flash")
        else:
            require(bool(arguments) and len(arguments) % 4 == 0
                    and all(arguments[index] == "-u" for index in range(0, len(arguments), 4)),
                    "传输层只允许只读上传、快速读取或一次 request 写入，拒绝复位和烧录")
            for index in range(0, len(arguments), 4):
                require(re.fullmatch(r"0x[0-9a-fA-F]+", arguments[index + 1]) is not None
                        and re.fullmatch(r"[0-9]+", arguments[index + 2]) is not None
                        and self.allowed_read(int(arguments[index + 1], 16), int(arguments[index + 2]))
                        and Path(arguments[index + 3]).resolve().parent == self.output.resolve()
                        and Path(arguments[index + 3]).suffix == ".bin",
                        "上传读取地址不在白名单")
        process = subprocess.run([str(self.cli), "-c", "port=SWD", "mode=HOTPLUG", "freq=4000",
                                  *arguments, "-score"], capture_output=True, timeout=timeout_s)
        message = (process.stdout + process.stderr).decode("utf-8", errors="replace")
        with log_file.open("x", encoding="utf-8") as stream:
            stream.write(message)
        require(process.returncode == 0 and re.search(r"\bError\s*:", message, re.I) is None, "SWD 操作失败：" + message)
        require(re.search(r"Device ID\s*:\s*0x468\b", message) is not None, "连接的芯片不是 G431")
        require("Core is running" in message, "CPU 没有保持运行")
        return message

    @staticmethod
    def parse_read32(message, blocks):
        """严格绑定每段标题、字节数、起址及连续十六进制字；不接受额外块或残缺行。"""
        sections = re.split(r"(?mi)^\s*Reading 32-bit memory content\s*$", message.replace("\r", ""))
        require(len(sections) == len(blocks) + 1, "快速读取区块数量不符")
        require(re.search(r"(?m)^\s*0x", sections[0]) is None, "快速读取包含标题前的额外数据")
        result = {}
        for (name, address, size), section in zip(blocks, sections[1:]):
            sizes = re.findall(r"(?m)^\s*Size\s*:\s*(\d+) Bytes\s*$", section)
            addresses = re.findall(r"(?m)^\s*Address\s*:\s*:?\s*(0x[0-9a-fA-F]+)\s*$", section)
            require(len(re.findall(r"(?m)^\s*Size\b.*$", section)) == 1
                    and len(re.findall(r"(?m)^\s*Address\b.*$", section)) == 1
                    and len(sizes) == len(addresses) == 1 and int(sizes[0]) == size and int(addresses[0], 16) == address,
                    "快速读取 Size/Address 与请求不符")
            data = bytearray()
            for line in section.splitlines():
                if re.match(r"^\s*0x", line) is None:
                    continue
                row = re.fullmatch(r"\s*(0x[0-9a-fA-F]{8})\s*:\s*([0-9a-fA-F]{8}(?:\s+[0-9a-fA-F]{8}){0,3})\s*", line)
                require(row is not None, "快速读取数据行格式非法")
                require(int(row[1], 16) == address + len(data), "快速读取数据地址不连续")
                words = row[2].split()
                require(len(data) + 4 * len(words) <= size, "快速读取包含超出请求的字")
                data.extend(struct.pack("<" + "I" * len(words), *(int(value, 16) for value in words)))
            require(len(data) == size, "快速读取缺少完整数据行")
            result[name] = bytes(data)
        return result

    def read(self, blocks, tag, timeout_s=20.0):
        blocks = list(blocks)
        return self._read_blocks(blocks, tag, timeout_s,
                                 bool(blocks) and all(self.allowed_read(address, size, words=True) for _, address, size in blocks))

    def read32(self, blocks, tag, timeout_s=20.0):
        """仅对齐 SRAM/寄存器的连续控制台读取；Flash 保持原始上传。"""
        return self._read_blocks(list(blocks), tag, timeout_s, True)

    def _read_blocks(self, blocks, tag, timeout_s, fast32):
        self.counter += 1
        tag = f"{self.counter:03d}-" + tag
        arguments, destinations = [], {}
        require(bool(blocks), "读取块不能为空")
        for name, address, size in blocks:
            require(name not in destinations and re.fullmatch(r"[a-zA-Z0-9_-]+", name), "读取块名称重复或非法")
            require(self.allowed_read(address, size, words=fast32), "SWD 读取地址或对齐不在白名单")
            destination = self.output / (tag + "-" + name + ".bin")
            require(not destination.exists(), "原始证据文件已存在")
            destinations[name] = (destination, size)
            arguments += ["-r32", hex(address), str(size)] if fast32 else ["-u", hex(address), str(size), str(destination)]
        message = self.execute(arguments, tag, timeout_s)
        if fast32:
            result = self.parse_read32(message, blocks)
            for name, (path, _) in destinations.items():
                with path.open("xb") as stream:
                    stream.write(result[name])
        else:
            result = {name: path.read_bytes() for name, (path, _) in destinations.items()}
        require(all(len(result[name]) == size for name, (_, size) in destinations.items()), "SWD 原始读取长度不足")
        return result

    def write_request(self, address, value, timeout_s=2.0):
        require(not self.write_attempted and address == self.layout["request"]
                and type(value) is int and 0 < value <= 0xFFFFFFFF,
                "只允许向 request 精确地址写一次非零 uint32 新序号")
        self.write_attempted = True  # 在尝试前锁住；丢失应答不能证明命令未执行。
        self.execute(["-w32", hex(address), hex(value)], "request-once", timeout_s)


def read_result(backend, layout, tag, timeout_s=20.0):
    raw = backend.read([("seq_before", layout["sequence"], 4),
                        ("capture", layout["capture"], layout["capture_size"]),
                        ("seq_after", layout["sequence"], 4)], tag, timeout_s)
    before, after = word(raw["seq_before"]), word(raw["seq_after"])
    require(before == after and before % 2 == 0, "电流结果发布版本不一致，停止本次读取")
    result = decode(raw["capture"], "capture", RESULT_FIELDS, layout)
    require(result["state"] in range(4) and result["failure"] in range(7), "电流结果状态或原因非法")
    return result, raw["capture"], before


def read_health(backend, layout, reference, tag, timeout_s=20.0, clock=time.monotonic):
    blocks = [
        ("guard_before", layout["guard"], 4), ("bdtr_before", layout["bdtr"], 4), ("cpu_before", DHCSR, 4),
        ("pub_before", layout["publication_sequence"], 4),
        ("publication", layout["publication"], layout["publication_size"]),
        ("pub_after", layout["publication_sequence"], 4),
        ("zero_before", layout["zero"], layout["zero_size"]),
        ("zero_after", layout["zero"], layout["zero_size"]),
        ("request", layout["request"], 4),
        ("guard_after", layout["guard"], 4), ("bdtr_after", layout["bdtr"], 4), ("cpu_after", DHCSR, 4),
    ]
    deadline = clock() + timeout_s
    for attempt in range(3):
        remaining = deadline - clock()
        require(remaining > 0.0, "只读健康复核已超时")
        raw = backend.read(blocks, tag if attempt == 0 else tag + f"-coherent-{attempt + 1}", remaining)
        # 任一次读取发现功率或保护异常都立即停止，不借后续一致读取掩盖异常。
        for suffix in ["before", "after"]:
            require(word(raw["guard_" + suffix]) == 0, "独立 guard 存在锁存故障")
            require(word(raw["bdtr_" + suffix]) & 0x8000 == 0, "检测到 TIM1 MOE 功率使能")
            require(word(raw["cpu_" + suffix]) & ((1 << 17) | (1 << 19)) == 0, "CPU 已暂停或锁死")
        sequence = word(raw["pub_before"])
        if sequence == word(raw["pub_after"]) and sequence % 2 == 0:
            break
    else:
        raise RuntimeError("运行快照三次只读复核均未取得一致版本")
    publication = decode(raw["publication"], "publication", PUBLICATION_FIELDS, layout)
    require(all(publication[field] == 1 for field in ["snapshot.status_valid", "snapshot.control_time_valid",
                                                    "snapshot.bus_voltage_valid", "has_sample"]), "运行快照缺少有效状态、时基或母线")
    require(publication["snapshot.state"] == 1 and publication["snapshot.faults"] == 0, "Motor 并非无故障 IDLE")
    bus = publication["snapshot.bus_voltage_v"]
    require(math.isfinite(bus) and 0.0 <= bus <= 3.6 and abs(bus - reference.vbus_dmm_v) <= 0.3,
            "软件 VBUS 超出逻辑供电范围或与万用表相差超过 0.3 V")
    require(publication["snapshot.control_time_ticks_per_second"] > 0, "控制时基频率无效")
    zero = decode(raw["zero_before"], "zero", ZERO_FIELDS, layout)
    zero_after = decode(raw["zero_after"], "zero", ZERO_FIELDS, layout)
    def zero_signature(data):
        return b"".join(data[layout["zero." + field + ".offset"]:
                             layout["zero." + field + ".offset"] + layout["zero." + field + ".size"]]
                        for field in ZERO_FIELDS).hex()
    signature = zero_signature(raw["zero_before"])
    require(zero == zero_after and signature == zero_signature(raw["zero_after"]),
            "冻结零偏或校零状态在读取期间变化")
    require(zero["state"] == 3 and zero["failure"] == 0 and zero["sample_count"] == SAMPLES
            and zero["config.calibration_sample_count"] == SAMPLES and zero["config.measured_phases"] == 6,
            "B/C 零偏尚未 READY、窗口不完整或存在失败")
    require(all(math.isfinite(zero[field]) for field in ["offset_a[1]", "offset_a[2]"]), "冻结零偏不是有限数")
    return {"time": publication["snapshot.control_time_ticks"],
            "ticks_per_second": publication["snapshot.control_time_ticks_per_second"],
            "bus_voltage_v": bus, "zero": zero, "zero_signature": signature, "request": word(raw["request"])}


def validate_complete(result):
    require(result["state"] == 2 and result["failure"] == 0, "采集没有完整成功")
    require(result["sample_count"] == SAMPLES and result["last_sample_time"] >= result["first_sample_time"]
            and result["last_sample_time"] - result["first_sample_time"] == 2 * (SAMPLES - 1),
            "采集不是完整连续的 2048 点窗口")
    for phase in "bc":
        values = {field: result[f"phase_{phase}.{field}"] for field in ["mean_a", "min_a", "max_a", "variance_a2"]}
        require(all(math.isfinite(value) for value in values.values())
                and values["min_a"] <= values["mean_a"] <= values["max_a"]
                and values["variance_a2"] >= 0.0, "相电流统计不是有限且自洽的完整结果：" + phase.upper())
        require(values["min_a"] != values["max_a"] or values["variance_a2"] == 0.0,
                "恒定相电流窗口不允许出现非零方差：" + phase.upper())
    require(math.isfinite(result["offset_b_a"]) and math.isfinite(result["offset_c_a"]), "采集冻结零偏不是有限数")


def run_acquisition(backend, prepared, reference, clock=time.monotonic, sleep=time.sleep):
    reference.validate(acquire=True)
    layout = prepared.layout
    validate_layout(layout)
    flash = backend.read([("flash", FLASH_START, FLASH_SIZE)], "flash-identity")["flash"]
    require(len(flash) == FLASH_SIZE and all(flash[address - FLASH_START] == value
                                            for address, value in prepared.image.items()), "实机 Flash 加载字节与指定 Observe 候选不符")
    existing, _, _ = read_result(backend, layout, "pre-result")
    require(existing["state"] != 1, "已有活动采集，禁止提交第二个请求")
    before = read_health(backend, layout, reference, "pre-health-1")
    sleep(0.02)
    advanced = read_health(backend, layout, reference, "pre-health-2")
    require(advanced["time"] > before["time"] and advanced["ticks_per_second"] == before["ticks_per_second"],
            "控制时间没有按同一时基推进")
    require(advanced["zero_signature"] == before["zero_signature"], "提交前冻结零偏发生变化")
    require(before["request"] == advanced["request"] == existing["request_sequence"], "存在未消费或被其他主机改写的采集请求")
    sequence = ((advanced["request"] + 1) & 0xFFFFFFFF) or 1
    deadline = clock() + 2.0
    backend.write_request(layout["request"], sequence, timeout_s=deadline - clock())
    while True:
        remaining = deadline - clock()
        require(remaining > 0.0, "等待采集超过 2 秒；不补发、不复位")
        result, payload, version = read_result(backend, layout, "capture-poll", timeout_s=remaining)
        if result["request_sequence"] == sequence:
            require(result["state"] != 3, "采集失败，原因编号：" + str(result["failure"]))
            if result["state"] == 2:
                validate_complete(result)
                break
            require(result["state"] == 1 and result["failure"] == 0 and result["sample_count"] == 0,
                    "活动采集发布了非法状态或部分窗口")
        else:
            require(result == existing, "采集身份被其他请求改写")
        sleep(min(0.01, max(0.0, deadline - clock())))
    remaining = deadline - clock()
    require(remaining > 0.0, "复核完整结果前已超过 2 秒")
    repeated, repeated_payload, repeated_version = read_result(backend, layout, "capture-confirm", timeout_s=remaining)
    require(payload == repeated_payload and version == repeated_version and result == repeated,
            "完整采集结果第二次读取发生变化")
    # 完成结果可先于下一份周期业务快照到达；只读等待其时间追平，不重发采集。
    post_deadline = clock() + 2.0
    for attempt in range(3):
        remaining = post_deadline - clock()
        require(remaining > 0.0, "采集后运行快照只读复核超过 2 秒")
        after = read_health(backend, layout, reference,
                            "post-health" if attempt == 0 else f"post-health-catchup-{attempt + 1}",
                            timeout_s=remaining, clock=clock)
        require(after["request"] == sequence and after["zero_signature"] == before["zero_signature"],
                "采集后请求或冻结零偏发生变化")
        if after["time"] >= result["last_sample_time"]:
            break
        sleep(min(0.01, max(0.0, post_deadline - clock())))
    else:
        raise RuntimeError("采集后运行快照三次只读复核仍未追平完整窗口")
    require(after["request"] == sequence and after["zero_signature"] == before["zero_signature"],
            "采集后请求或冻结零偏发生变化")
    require(after["time"] >= result["last_sample_time"] >= result["first_sample_time"] > advanced["time"]
            and after["time"] > advanced["time"] and after["ticks_per_second"] == before["ticks_per_second"],
            "完整窗口与采集前后控制时间不一致")
    require(struct.pack("<2f", result["offset_b_a"], result["offset_c_a"])
            == struct.pack("<2f", before["zero"]["offset_a[1]"], before["zero"]["offset_a[2]"]),
            "窗口使用的冻结零偏与前后 READY 零偏不同")
    phases = {}
    for phase in "bc":
        raw = {field: result[f"phase_{phase}.{field}"] for field in ["mean_a", "min_a", "max_a", "variance_a2"]}
        offset = result[f"offset_{phase}_a"]
        phases[phase.upper()] = {"raw": raw, "frozen_offset_a": offset,
                                "corrected": {field: value if field == "variance_a2" else value - offset
                                              for field, value in raw.items()}}
    return {"status": "无功率已知电流采集完成", "request_sequence": sequence,
            "reference_phase": reference.phase, "reference_current_a": reference.current_a,
            "reference_source": reference.source,
            "reference_quality": "coarse-comparison" if reference.source == "supply-display" else "dmm-reading",
            "reference_limitations": ("电源实际显示电流仅用于基础幅值和方向粗对照，不证明绝对精度，不是万用表电流测量。"
                                      if reference.source == "supply-display"
                                      else "独立万用表电流读数不自动构成精度、增益或FOC验收。"),
            "vbus_dmm_v": reference.vbus_dmm_v, "before": before, "after": after,
            "capture": result, "phases": phases, "files": prepared.hashes,
            "write_count": 1, "automatic_retry": False, "gain_applied": False,
            "motor_or_foc_qualified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True, help="仅包含本次 Observe HEX/AXF/MAP 的目录")
    parser.add_argument("--expected-hex-sha256", required=True, help="明确指定的本次 HEX SHA256")
    parser.add_argument("--output", type=Path, required=True, help="新的证据目录，禁止覆盖")
    parser.add_argument("--reference-phase", choices=["B", "C"], required=True, help="独立浮地参考电流实际流过的采样相")
    parser.add_argument("--reference-source", choices=["dmm", "supply-display"], default="dmm",
                        help="参考电流来源；默认万用表，电源实际显示仅作粗对照，不证明绝对精度")
    parser.add_argument("--reference-current-a", type=float, required=True,
                        help="指定来源的实际有符号电流，绝对值不超过 0.25 A；不是电源限流设定值")
    parser.add_argument("--vbus-dmm-v", type=float, required=True, help="当前 VBUS 对 GND 的万用表实测电压，0..3.6 V")
    parser.add_argument("--logic-only-confirmed", action="store_true", help="确认电机母线电源和电机三相断开；启动校零时外部注入源断开，零偏 READY 后才接入限流注入支路")
    parser.add_argument("--acquire", action="store_true", help="显式执行一次 HOTPLUG 无功率采集；默认仅离线准备")
    parser.add_argument("--gdb", type=Path, default=DEFAULT_GDB, help="离线 ARM GDB 路径")
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI, help="STM32CubeProgrammer CLI 路径")
    args = parser.parse_args(argv)
    reference = Reference(args.reference_phase, args.reference_current_a, args.vbus_dmm_v,
                          args.logic_only_confirmed, source=args.reference_source)
    reference.validate(acquire=args.acquire)
    prepared = prepare_build(args.build_dir.resolve(), args.expected_hex_sha256, args.output.resolve(), args.gdb.resolve())
    backend = None
    try:
        if not args.acquire:
            print("离线候选与布局已核对；未连接设备、未提交采集请求。")
            return 0
        backend = CubeBackend(prepared, args.cli.resolve())
        report = run_acquisition(backend, prepared, reference)
        save(prepared.output / "result.json", report)
        print("2048 点已冻结并复核；原始值与扣除既有零偏后的统计已保存，未应用增益。")
        return 0
    except Exception as error:
        save(prepared.output / "failure.json", {"status": "失败，未自动补发或清理", "error": str(error),
             "request_write_attempted": bool(backend and backend.write_attempted), "files": prepared.hashes})
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("停止：" + str(error), file=sys.stderr)
        sys.exit(1)
