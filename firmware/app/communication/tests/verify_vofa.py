"""Compile and execute the actual VOFA C codec with ARMCC and Unicorn; no hardware access."""

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
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
FRAME_SIZE = 44
VALID_ALL = 0x1FF
INVALID_FLOAT = b"\x00\x00\xc0\x7f"
CASES = []


def passed(name: str) -> None:
    CASES.append(name)


def run_checked(arguments: list[str], directory: Path) -> None:
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


with tempfile.TemporaryDirectory(prefix="vofa-codec-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text(
        "LR_IROM1 0x08000000 0x00040000 {\n"
        " ER_IROM1 0x08000000 0x00040000 { .ANY (+RO) }\n"
        " RW_IRAM1 0x20000000 0x00008000 { .ANY (+RW +ZI) }\n}\n",
        encoding="utf-8",
    )
    objects = []
    for source in [ROOT / "vofa.c", ROOT / "tests/model.c"]:
        obj = temporary / (source.stem + ".o")
        run_checked(
            [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
             "--split_sections", "-g", "-I", str(ROOT), "-c", str(source), "-o", str(obj)],
            temporary,
        )
        objects.append(str(obj))
    executable = temporary / "model.axf"
    run_checked(
        [str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
         "--entry=model_init", "--keep=model.o(*)", "-o", str(executable), *objects],
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
                nearest = max((value, key) for key, value in symbols.items() if value <= pc)
                raise AssertionError(f"{name}: {error}; pc={pc:#x}, symbol={nearest}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name

        def word(self, name: str) -> int:
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def write_word(self, name: str, value: int) -> None:
            self.cpu.mem_write(symbols[name], struct.pack("<I", value))

        def feed(self, data: bytes) -> None:
            assert len(data) <= 512
            self.cpu.mem_write(symbols["model_input"], data)
            self.write_word("model_input_length", len(data))
            self.call("model_feed")
            assert self.word("model_output_changed_on_reject") == 0

        def commands(self) -> list[tuple[int, float]]:
            count = self.word("model_command_count")
            assert count <= 64
            types = struct.unpack("<" + "I" * count,
                                  self.cpu.mem_read(symbols["model_command_types"], 4 * count))
            targets = struct.unpack("<" + "f" * count,
                                    self.cpu.mem_read(symbols["model_command_targets"], 4 * count))
            return list(zip(types, targets))

        def encode(self, values: tuple[float, ...], state: int = 2, faults: int = 5,
                   flags: int = VALID_ALL, result: int = 1, capacity: int = FRAME_SIZE) -> bytes:
            self.cpu.mem_write(symbols["model_telemetry"],
                               struct.pack("<6f4I", *values, state, faults, flags, result))
            self.write_word("model_encode_capacity", capacity)
            self.call("model_encode")
            frame = bytes(self.cpu.mem_read(symbols["model_frame"], FRAME_SIZE + 8))
            assert frame[:4] == b"\xa5" * 4 and frame[-4:] == b"\xa5" * 4
            return frame[4:-4]

    model = Model()
    model.feed(b"sta")
    model.feed(b"rt:-12.")
    assert model.commands() == [] and model.word("model_rejected_count") == 0
    model.feed(b"5\nspeed:+6.25e1\r\nstop\nclear\n")
    assert model.commands() == [(0, -12.5), (1, 62.5), (2, 0.0), (3, 0.0)]
    assert model.word("model_rejected_count") == 0
    passed("four complete commands, fragmented input, multiple frames and CRLF")

    model = Model()
    for byte in b"speed:.5\nstart:1.\nstart:-0\nstop\r\n":
        model.feed(bytes([byte]))
    assert model.commands() == [(1, 0.5), (0, 1.0), (0, -0.0), (2, 0.0)]
    assert math.copysign(1.0, model.commands()[2][1]) == -1.0
    passed("one-byte fragmentation and documented decimal forms")

    model = Model()
    model.feed(b"start:12")
    model.call("model_init")
    model.feed(b"3\nclear\n")
    assert model.commands() == [(3, 0.0)] and model.word("model_rejected_count") == 1
    passed("new stream initialization discards partial Start without replay")

    invalid = [b"", b"start", b"speed", b"start:", b"speed:", b"start:+", b"start:.",
               b"start:1e", b"start:1e+", b"speed:1x", b"start:1 2", b"start: 1",
               b"speed:1 ", b"start:nan", b"speed:NaN", b"start:inf", b"start:-inf",
               b"start:1e999", b"speed:-1e999", b"start:0x1p2", b"START:1",
               b"stop:1", b"clear 1", b" stop", b"stop ", b"clear\r\r", b"speed:1\r2",
               b"stop\x00garbage", b"clear\t", b"start:1\x80", b"speed:--1",
               b"speed:1.2.3"]
    model = Model()
    for line in invalid:
        model.feed(line + b"\n")
    assert model.commands() == [] and model.word("model_rejected_count") == len(invalid)
    model.feed(b"stop\n")
    assert model.commands() == [(2, 0.0)]
    passed("reject nonfinite, malformed, whitespace, NUL, non-ASCII and trailing garbage")

    model = Model()
    model.feed(b"start:" + b"0" * 40 + b"1\n")
    assert model.commands() == [(0, 1.0)]
    model.feed(b"start:" + b"0" * 41 + b"1")
    assert model.word("model_rejected_count") == 0
    model.feed(b"stop\nclear\n")
    assert model.commands() == [(0, 1.0), (3, 0.0)]
    assert model.word("model_rejected_count") == 1
    model.feed(b"x" * 512)
    model.feed(b"stop\nstart:2\n")
    assert model.commands()[-1] == (0, 2.0) and model.word("model_rejected_count") == 2
    passed("47-byte boundary, overflow discard through newline, then recover")

    model = Model()
    model.feed(b"start:1e38\nspeed:-1e38\nstart:1000000000\n")
    assert [kind for kind, _ in model.commands()] == [0, 1, 0]
    assert model.commands()[2][1] == 1_000_000_000.0
    passed("finite values have no codec business range clamp")

    model = Model()
    model.feed(b"start:3.4028234663852886e38\nspeed:1.401298464324817e-45\n")
    model.feed(b"start:0e999999999999999999999\nspeed:1e-999999999999999999\n")
    assert len(model.commands()) == 4
    assert struct.pack("<f", model.commands()[0][1]) == b"\xff\xff\x7f\x7f"
    assert 0.0 <= model.commands()[1][1] <= 1.401298464324817e-45
    assert model.commands()[2:] == [(0, 0.0), (1, 0.0)]
    model.feed(b"start:3.4028236e38\n")
    assert len(model.commands()) == 4 and model.word("model_rejected_count") == 1
    passed("binary32 maximum, tiny finite input, underflow, huge exponent and overflow")

    values = (1.0, -120.5, 119.25, -0.125, 2.75, 24.0)
    frame = model.encode(values)
    expected = struct.pack("<10f", *values, 2.0, 5.0, float(VALID_ALL), 1.0)
    assert frame == expected + b"\x00\x00\x80\x7f"
    assert model.word("model_encoded_size") == FRAME_SIZE
    passed("ten channels in exact order, explicit little endian and JustFloat tail")

    for bit, channel in [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5),
                         (6, 6), (7, 7), (8, 9)]:
        frame = model.encode(values, flags=VALID_ALL & ~(1 << bit))
        assert frame[channel * 4:channel * 4 + 4] == INVALID_FLOAT
        assert struct.unpack_from("<f", frame, 32)[0] == VALID_ALL & ~(1 << bit)
    frame = model.encode((0.0,) * 6, flags=0xFFFF0000)
    assert all(math.isnan(value) for index, value in enumerate(struct.unpack("<10f", frame[:40]))
               if index != 8)
    assert struct.unpack_from("<f", frame, 32)[0] == 0.0
    passed("each absent validity bit emits NaN, including unimplemented velocity/id/iq")

    for invalid_value in [float("inf"), float("-inf"), float("nan")]:
        for channel in range(6):
            samples = list(values)
            samples[channel] = invalid_value
            frame = model.encode(tuple(samples))
            assert frame[channel * 4:channel * 4 + 4] == INVALID_FLOAT
            assert struct.unpack_from("<f", frame, 32)[0] == VALID_ALL & ~(1 << channel)
            assert all(frame[offset:offset + 4] != b"\x00\x00\x80\x7f"
                       for offset in range(0, 40, 4))
    passed("nonfinite payload canonicalizes to NaN and clears its validity bit")

    for name, channel, bit in [("state", 6, 6), ("faults", 7, 7), ("result", 9, 8)]:
        frame = model.encode(values, **{name: 0xFFFFFF})
        assert struct.unpack_from("<f", frame, channel * 4)[0] == 0xFFFFFF
        frame = model.encode(values, **{name: 0x1000001})
        assert frame[channel * 4:channel * 4 + 4] == INVALID_FLOAT
        assert struct.unpack_from("<f", frame, 32)[0] == VALID_ALL & ~(1 << bit)
    passed("integer channels never silently lose bitmap or enum precision")

    for capacity in [0, FRAME_SIZE - 1]:
        assert model.encode(values, capacity=capacity) == b"\xa5" * FRAME_SIZE
        assert model.word("model_encoded_size") == 0
    model.call("model_null_arguments")
    assert model.word("model_null_result") == 0
    passed("short output remains untouched and null arguments fail without writes")

print(f"PASS: {len(CASES)} VOFA C behavior groups")
for name in CASES:
    print("  " + name)
