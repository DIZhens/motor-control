"""Prepare/check a G431 candidate offline; never connects to a target or authorizes output."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

SCHEMA = 1
STATUS = "OFFLINE_PREPARED_NOT_HARDWARE_VALIDATED"
ARTIFACTS = ["BLDC_control V1.hex", "BLDC_control V1.axf", "BLDC_control V1.map"]
SYMBOLS = {"g_platform_bringup": ("legacy", 76), "g_target_validation_request": ("request", 20),
           "g_target_validation": ("snapshot", 184), "g_target_validation_capture": ("capture", 80),
           "g_target_validation_probe": ("probe", 68), "g_target_validation_stop_reason": ("stop", 4),
           "uwTick": ("tick", 4)}


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def digest(path):
    require(path.is_file(), "Candidate file missing: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_paths(target):
    """Freeze project members, headers/configuration and all host entry points."""
    project = target / "MDK-ARM/BLDC_control V1.uvprojx"
    require(project.is_file(), "Target project missing")
    paths = {project, *target.glob("*.ioc")}
    for member in ET.parse(project).findall(".//FilePath"):
        paths.add((project.parent / member.text.replace("\\", "/")).resolve())
    for directory in [target / name for name in ["Core", "Drivers", "platform"]] + [target.parents[1] / "platform_contract"]:
        paths.update(directory.rglob("*.h"))
    paths.update((target / "validation").glob("*.h"))
    paths.update((target / "validation/tools").glob("*.py"))
    # Relative paths may include ../../platform_contract, whose source is shared.
    import os
    return {Path(os.path.relpath(path, target)).as_posix(): path for path in paths}


def read_hex(path):
    image, base, ended = {}, 0, False
    for line in path.read_text(encoding="ascii").splitlines():
        require(line.startswith(":"), "Malformed HEX record")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as error:
            raise RuntimeError("Malformed HEX bytes") from error
        require(len(record) >= 5 and len(record) == record[0] + 5
                and sum(record) % 256 == 0 and not ended, "HEX length/checksum/end mismatch")
        kind, length = record[3], record[0]
        if kind == 4:
            require(length == 2 and record[1:3] == b"\0\0", "Malformed HEX address record")
            base = int.from_bytes(record[4:6], "big") << 16
        elif kind == 0:
            address = base + int.from_bytes(record[1:3], "big")
            for index, value in enumerate(record[4:4 + length]):
                current = address + index
                require(current not in image, "Overlapping HEX data")
                image[current] = value
        elif kind == 1:
            require(length == 0 and record[1:3] == b"\0\0", "Malformed HEX end record")
            ended = True
        else:
            require(kind == 5 and length == 4, "Unexpected HEX record")
    require(ended and bool(image), "Missing HEX image/end")
    require(0x08000000 <= min(image) <= max(image) < 0x08020000, "Unexpected Flash range")
    return image


def verify_artifacts(build, layout):
    """Bind symbols to the exact flash image, not merely to three unrelated hashes."""
    from elftools.elf.elffile import ELFFile
    image = read_hex(build / ARTIFACTS[0])
    mapping = (build / ARTIFACTS[2]).read_text(encoding="utf-8")
    with (build / ARTIFACTS[1]).open("rb") as stream:
        elf = ELFFile(stream)
        require(elf.elfclass == 32 and elf.little_endian and elf["e_machine"] == "EM_ARM",
                "Candidate AXF is not little-endian ARM32")
        loaded = {}
        for segment in elf.iter_segments():
            if segment["p_type"] != "PT_LOAD" or segment["p_filesz"] == 0:
                continue
            # Physical/load addresses include ROM initializers whose execution address is RAM.
            address, payload = segment["p_paddr"], segment.data()
            require(0x08000000 <= address < address + len(payload) <= 0x08020000,
                    "AXF contains an unsupported load region")
            for offset, value in enumerate(payload):
                require(address + offset not in loaded, "Overlapping AXF load regions")
                loaded[address + offset] = value
        require(loaded == image, "AXF load bytes differ from HEX; mixed build artifacts")
        # ARMCC 的 Size 是压缩前尺寸；COMPRESSED[] 才是 Flash 实际加载尺寸。
        # 仍逐字节核对 AXF/HEX，压缩标志不允许绕过尺寸、地址或单区域检查。
        regions = re.findall(r"^\s*Load Region\b[^\r\n]*", mapping, re.M)
        require(len(regions) == 1, "MAP must contain exactly one load region")
        region = re.fullmatch(r"\s*Load Region \S+ \(Base: (0x[0-9a-fA-F]+), "
                              r"Size: (0x[0-9a-fA-F]+), Max: (0x[0-9a-fA-F]+)([^()]*)\)\s*", regions[0])
        require(region is not None, "Malformed MAP load region")
        base, uncompressed, maximum = [int(value, 0) for value in region.groups()[:3]]
        attributes = region.group(4)
        require(not attributes or attributes.startswith(", "), "Malformed MAP load region attributes")
        attributes = attributes[2:].split(", ") if attributes else []
        compressed = None
        require(len(attributes) == len(set(attributes)), "Repeated MAP load region attribute")
        for attribute in attributes:
            if attribute == "ABSOLUTE":
                continue
            match = re.fullmatch(r"COMPRESSED\[(0x[0-9a-fA-F]+)\]", attribute)
            require(match is not None and compressed is None, "Unsupported/repeated MAP compression attribute")
            compressed = int(match.group(1), 0)
        actual = uncompressed if compressed is None else compressed
        require(0x08000000 <= base < base + maximum <= 0x08020000
                and 0 < actual <= uncompressed <= maximum
                and (base, actual) == (min(image), len(image))
                and max(image) - min(image) + 1 == len(image),
                "MAP load region differs from AXF/HEX")
        entries = re.findall(r"Image Entry point\s*:\s*(0x[0-9a-fA-F]+)", mapping)
        require(len(entries) == 1 and int(entries[0], 0) == elf["e_entry"]
                and (elf["e_entry"] & ~1) in image, "MAP entry point differs from AXF")
        symbols = elf.get_section_by_name(".symtab")
        require(symbols is not None, "AXF symbol table missing")
        for name, (key, size) in SYMBOLS.items():
            values = symbols.get_symbol_by_name(name) or []
            require(len(values) == 1 and values[0]["st_size"] == size, "AXF symbol absent/size changed: " + name)
            address = values[0]["st_value"]
            require(0x20000000 <= address < address + size <= 0x20008000
                    and layout.get(key) == address, "AXF symbol differs from candidate layout: " + name)
            rows = re.findall(r"^\s*" + re.escape(name) + r"\s+(0x[0-9a-fA-F]+)\s+Data\s+(\d+)\s+", mapping, re.M)
            require(len(rows) == 1 and (int(rows[0][0], 0), int(rows[0][1])) == (address, size),
                    "MAP symbol differs from AXF: " + name)


def load(path, target):
    path = path.resolve()
    require(path.is_file(), "Candidate manifest missing: " + str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    require(data.get("schema_version") == SCHEMA and data.get("target") == "BLDC_G431"
            and data.get("status") == STATUS, "Unsupported candidate identity/status")
    build = (path.parent / data["build_directory"]).resolve()
    require(set(data["files"]) == set(ARTIFACTS), "Candidate artifact set incomplete")
    for name in ARTIFACTS:
        require(digest(build / name) == data["files"][name], "Candidate artifact changed: " + name)
    required = source_paths(target)
    require(set(data["source_files"]) == set(required), "Candidate source/tool set changed or incomplete")
    for name, source in required.items():
        require(digest(source) == data["source_files"][name], "Candidate source/tool changed: " + name)
    require(isinstance(data.get("layout"), dict) and data["layout"]
            and all(type(value) is int and value >= 0 for value in data["layout"].values()),
            "Candidate layout missing or invalid")
    verify_artifacts(build, data["layout"])
    return data, build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New JSON file; existing evidence is never overwritten")
    parser.add_argument("--gdb", type=Path)
    args = parser.parse_args()
    import logic_check as common
    build, output = args.build_dir.resolve(), args.output.resolve()
    require(not output.exists(), "Refusing to overwrite candidate manifest")
    require(output.parent.is_dir(), "Create the new candidate directory first")
    gdb = (args.gdb or common.GDB).resolve()
    require(gdb.is_file(), "GDB executable missing")
    import os
    data = {"schema_version": SCHEMA, "target": "BLDC_G431", "status": STATUS,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "build_directory": Path(os.path.relpath(build, output.parent)).as_posix(),
            "files": {name: digest(build / name) for name in ARTIFACTS},
            "source_files": {name: digest(path) for name, path in sorted(source_paths(common.TARGET).items())}}
    read_hex(build / ARTIFACTS[0])
    data["layout"] = common.offline_layout(build, gdb, output.with_suffix(".layout.log"))
    verify_artifacts(build, data["layout"])
    # Do not publish a mixed snapshot if compilation/source edits happened during GDB.
    for name in ARTIFACTS:
        require(digest(build / name) == data["files"][name], "Build changed while preparing manifest")
    current_sources = source_paths(common.TARGET)
    require(set(current_sources) == set(data["source_files"]), "Source set changed while preparing manifest")
    for name, path in current_sources.items():
        require(digest(path) == data["source_files"].get(name), "Source changed while preparing manifest")
    with output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"manifest": str(output), "status": STATUS,
                      "hex_sha256": data["files"][ARTIFACTS[0]], "hardware_access": False}))


if __name__ == "__main__":
    main()
