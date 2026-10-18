"""Run the real Hardware Trip snapshot reader with modeled comparator/Break bits.

Checks the complete cause combinations and sticky/PRIMASK semantics. This is not
an analog threshold, interrupt latency, or electrical protection test.
"""
from pathlib import Path
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_PC, UC_ARM_REG_PRIMASK)
from verify_platform import ROOT, COMPILER


def run(directory):
    scatter = directory / "snapshot.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x20000 {\n"
                       " ER_IROM1 0x08000000 0x20000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x8000 { .ANY (+RW +ZI) }\n}\n")
    settings = ET.parse(ROOT / "MDK-ARM/BLDC_control V1.uvprojx").find(".//Cads/VariousControls")
    command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
               "--split_sections", "-g"]
    for include in settings.findtext("IncludePath").split(";"):
        command += ["-I", str((ROOT / "MDK-ARM" / include.replace("\\", "/")).resolve())]
    command += ["-D" + value for value in settings.findtext("Define").split(",")]
    objects = []
    for source in [ROOT / "platform/hardware_trip.c", ROOT / "validation/tests/trip_snapshot_model.c"]:
        obj = directory / (source.stem + ".o")
        result = subprocess.run(command + ["-c", str(source), "-o", str(obj)], capture_output=True, text=True)
        assert result.returncode == 0 and "warning" not in result.stderr.lower(), result.stderr
        objects.append(str(obj))
    image = directory / "snapshot.axf"
    result = subprocess.run([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
                             "--entry=trip_snapshot_model_read", "-o", str(image), *objects],
                            cwd=directory, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        contents = [(s["p_vaddr"], s.data()) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        contents += [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                     if s["sh_flags"] & 2 and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
    cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
    for address, size in [(0x08000000, 0x20000), (0x20000000, 0x8000), (0x40010000, 0x3000)]:
        cpu.mem_map(address, size)
    for address, data in contents:
        cpu.mem_write(address, data)

    def put(address, value):
        cpu.mem_write(address, struct.pack("<I", value))

    def word(name):
        return struct.unpack("<I", cpu.mem_read(symbols[name], 4))[0]

    def read(masked):
        cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
        cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
        cpu.reg_write(UC_ARM_REG_PRIMASK, masked)
        cpu.emu_start(symbols["trip_snapshot_model_read"] | 1, 0x0801F000, count=10000)
        assert cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000
        assert cpu.reg_read(UC_ARM_REG_PRIMASK) == masked
        raw = bytes(cpu.mem_read(symbols["model_snapshot"], 8))
        return raw[0], struct.unpack_from("<I", raw, 4)[0]

    cases = 0
    for masked in [0, 1]:
        for comparators in range(16):
            for flags in range(4):
                for sticky in [0, 1, 2, 4, 7]:
                    cpu.mem_write(symbols["s_armed"], b"\1")
                    put(symbols["s_latched_causes"], sticky)
                    for index in range(4):
                        put(0x40010200 + 4 * index, (1 << 30) if comparators & (1 << index) else 0)
                    put(0x40012C10, flags << 7)
                    active = (2 if comparators & 0b1011 else 0) | (4 if comparators & 0b0100 else 0)
                    expected = sticky | active | (4 if flags & 1 else 0) | (2 if flags & 2 else 0)
                    assert read(masked) == (bool(active), expected), (comparators, flags, sticky)
                    assert word("model_snapshot_result") == 1
                    for index in range(4):
                        put(0x40010200 + 4 * index, 0)
                    put(0x40012C10, 0)
                    assert read(masked) == (0, expected), "physical condition disappearing lost sticky evidence"
                    cases += 1
        for armed, null in [(0, 0), (1, 1)]:
            cpu.mem_write(symbols["s_armed"], bytes([armed]))
            put(symbols["model_null_output"], null)
            cpu.mem_write(symbols["model_snapshot"], b"\xA5" * 8)
            read(masked)
            assert word("model_snapshot_result") == 0
            assert bytes(cpu.mem_read(symbols["model_snapshot"], 8)) == b"\xA5" * 8
            put(symbols["model_null_output"], 0)
            cases += 1
    assert word("model_unexpected_calls") == 0
    print(f"PASS: {cases} actual Hardware Trip snapshot cases; all comparator/Break combinations, sticky causes, PRIMASK and invalid outputs.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="g431-trip-snapshot-") as temp:
        run(Path(temp))
