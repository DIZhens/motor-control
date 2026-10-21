"""使用 ARMCC 与 Unicorn 执行正式 Motor / VOFA 适配，不访问硬件。"""

from pathlib import Path
import math
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4


ROOT = Path(__file__).resolve().parents[1]
MOTOR = ROOT.parent / "motor"
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
INVALID_FLOAT = b"\x00\x00\xc0\x7f"


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


with tempfile.TemporaryDirectory(prefix="motor-vofa-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00040000 {\n"
        " ER_IROM1 0x08000000 0x00040000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / "motor_vofa.c", ROOT / "vofa.c",
                   ROOT / "tests/motor_vofa_model.c"]:
        obj = temporary / (source.stem + ".o")
        run_checked(
            [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
             "--split_sections", "-g", "-I", str(ROOT), "-I", str(MOTOR),
             "-c", str(source), "-o", str(obj)],
            temporary,
        )
        objects.append(str(obj))
    executable = temporary / "model.axf"
    run_checked(
        [str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
         "--entry=model_init", "--keep=motor_vofa_model.o(*)", "-o", str(executable),
         *objects],
        temporary,
    )
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(section["p_vaddr"], section.data()) for section in elf.iter_segments()
                    if section["p_type"] == "PT_LOAD"]
        initializers = [(section["sh_addr"], section.data()) for section in elf.iter_sections()
                        if (section["sh_flags"] & 2) and section["sh_type"] == "SHT_PROGBITS"]
        symbols = {symbol.name: symbol["st_value"]
                   for symbol in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self) -> None:
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x40000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.call("model_init")

        def call(self, name: str) -> None:
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=1000000)
            except UcError as error:
                pc = self.cpu.reg_read(UC_ARM_REG_PC)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name

        def word(self, name: str) -> int:
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def write(self, name: str, data: bytes) -> None:
            self.cpu.mem_write(symbols[name], data)

        def write_word(self, name: str, value: int) -> None:
            self.write(name, struct.pack("<I", value))

        def command(self, kind: int, target: float) -> tuple[int, float]:
            self.write_word("model_command_type", kind)
            self.write("model_command_target", struct.pack("<f", target))
            self.call("model_command")
            assert self.word("model_command_changed_on_reject") == 0
            return (self.word("model_command_output_type"), struct.unpack(
                "<f", self.cpu.mem_read(symbols["model_command_output_target"], 4))[0])

        def telemetry(self) -> tuple[tuple[float, ...], int, bytes]:
            self.call("model_telemetry_map")
            assert self.word("model_snapshot_changed") == 0
            assert self.word("model_encoded_size") == 44
            raw = bytes(self.cpu.mem_read(symbols["model_frame"], 44))
            assert raw[-4:] == b"\x00\x00\x80\x7f"
            mapped = struct.unpack("<6f4I", self.cpu.mem_read(symbols["model_telemetry"], 40))
            return struct.unpack("<10f", raw[:40]), mapped[8], raw

    model = Model()
    for kind, target in [(0, -12.5), (1, 1e20), (2, 123.0), (3, -456.0)]:
        output = model.command(kind, target)
        assert model.word("model_command_accepted") == 1
        assert output[0] == kind, (kind, output, model.word("model_command_accepted"))
        assert math.isclose(output[1], target, rel_tol=1e-6) if kind < 2 else output[1] == 0.0
    model.command(0, float("nan"))
    assert model.word("model_command_accepted") == 1
    assert math.isnan(struct.unpack("<f", model.cpu.mem_read(symbols["model_command_output_target"], 4))[0])
    CASES.append("完整命令逐项映射，停止与清除归零目标，业务范围不由适配器裁决")

    for kind in [4, 0x7FFFFFFF, 0xFFFFFFFF]:
        model.command(kind, 12.0)
        assert model.word("model_command_accepted") == 0
        assert bytes(model.cpu.mem_read(symbols["model_command_output"], 8)) == b"\xa5" * 8
    model.call("model_null_arguments")
    assert model.word("model_null_failures") == 0
    CASES.append("未知枚举与空指针拒绝不污染命令，空快照清零，空输出安全返回")

    model = Model()
    channels, flags, _ = model.telemetry()
    assert flags == 0x1FF
    assert channels == (1.5, -120.5, 119.25, -0.125, 2.75, 24.0, 1.0, 5.0, 511.0, 1.0)
    CASES.append("非 20 kHz 时间单位映射，既有十通道顺序与帧尾保持一致")

    validity_groups = [(1, 0x1C2, {1, 6, 7, 9}), (2, 1, {0}), (4, 4, {2}),
                       (8, 8, {3}), (16, 16, {4}), (32, 32, {5})]
    for source_bit, expected_flags, valid_channels in validity_groups:
        model.write_word("model_validity", source_bit)
        channels, flags, raw = model.telemetry()
        assert flags == expected_flags and channels[8] == float(expected_flags)
        for index in range(10):
            if index != 8:
                assert math.isfinite(channels[index]) if index in valid_channels else math.isnan(channels[index])
                if index not in valid_channels:
                    assert raw[index * 4:index * 4 + 4] == INVALID_FLOAT
    CASES.append("状态同组与五个观测有效性独立，缺失通道仍编码规范 NaN")

    model.write_word("model_validity", 0)
    channels, flags, _ = model.telemetry()
    assert flags == 0 and all(math.isnan(value) for index, value in enumerate(channels) if index != 8)
    model.write_word("model_validity", 0x3F)
    model.write_word("model_rate", 0)
    channels, flags, _ = model.telemetry()
    assert flags == 0x1FE and math.isnan(channels[0]) and channels[8] == 510.0
    model.write_word("model_rate", 1000000)
    model.write("model_ticks", struct.pack("<Q", 12345678901234))
    channels, flags, _ = model.telemetry()
    assert flags == 0x1FF and math.isclose(channels[0], 12345678.901234, rel_tol=1e-6)
    CASES.append("全部无效无旧值泄漏，零频率禁止时间有效，64 位时间不截断")

    model = Model()
    for index in range(5):
        for invalid in [float("nan"), float("inf"), float("-inf")]:
            values = [-120.5, 119.25, -0.125, 2.75, 24.0]
            values[index] = invalid
            model.write("model_values", struct.pack("<5f", *values))
            channels, mapped_flags, raw = model.telemetry()
            assert mapped_flags == 0x1FF
            assert raw[(index + 1) * 4:(index + 2) * 4] == INVALID_FLOAT
            assert channels[8] == float(0x1FF & ~(1 << (index + 1)))
    CASES.append("适配保留业务有效位，由既有编码器收紧非有限数值并规范 NaN")

print(f"PASS: {len(CASES)} Motor / VOFA C 适配行为组")
for case in CASES:
    print("  " + case)
