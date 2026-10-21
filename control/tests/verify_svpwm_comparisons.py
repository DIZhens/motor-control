"""只还原比较片段生成临时旧库调用参考；其余直接使用正式源，不维护第二份 FOC 算法。"""

from pathlib import Path
import math
import os
import random
import re
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_CPU_ARM_CORTEX_M4


ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
COMPARISONS = [(
    """    maximum = fabsf(alpha_v);
    if (fabsf(beta_v) > maximum) {
        maximum = fabsf(beta_v);
    }""",
    "    maximum = fmaxf(fabsf(alpha_v), fabsf(beta_v));",
), (
    """    high = phase_a;
    low = phase_a;
    if (phase_b > high) {
        high = phase_b;
    }
    if (phase_c > high) {
        high = phase_c;
    }
    if (phase_b < low) {
        low = phase_b;
    }
    if (phase_c < low) {
        low = phase_c;
    }""",
    """    high = fmaxf(phase_a, fmaxf(phase_b, phase_c));
    low = fminf(phase_a, fminf(phase_b, phase_c));""",
)]


def passed(name):
    CASES.append(name)


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output
    return result.stdout


def bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def floating(value):
    return struct.unpack("<f", struct.pack("<I", value))[0]


def neighbors(value):
    encoded = bits(value)
    return [(encoded + offset) & 0xFFFFFFFF for offset in [-1, 0, 1]]


with tempfile.TemporaryDirectory(prefix="svpwm-comparison-") as directory:
    temporary = Path(directory)
    production = ROOT / "foc_math.c"
    reference_text = production.read_text(encoding="utf-8")
    for replacement, original in COMPARISONS:
        # 匹配不再唯一时直接失败，禁止悄悄对另一实现建立失真的旧版参考。
        assert reference_text.count(replacement) == 1
        reference_text = reference_text.replace(replacement, original)
    reference = temporary / "foc_math_reference.c"
    reference.write_text(reference_text, encoding="utf-8")
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x20000 {\n ER_IROM1 0x08000000 0x20000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x8000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")

    def build(source, name):
        objects = []
        for index, path in enumerate([source, ROOT / "tests/foc_math_model.c"]):
            obj = temporary / f"{name}_{index}.o"
            checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3", "-Otime",
                     "--split_sections", "-g", "-I", str(ROOT), "-c", str(path), "-o", str(obj)],
                    temporary)
            objects.append(str(obj))
        executable = temporary / f"{name}.axf"
        checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
                 "--entry=model_init", f"--keep={name}_1.o(*)", "-o", str(executable), *objects],
                temporary)
        with executable.open("rb") as stream:
            elf = ELFFile(stream)
            segments = [(segment["p_vaddr"], segment.data()) for segment in elf.iter_segments()
                        if segment["p_type"] == "PT_LOAD"]
            initializers = [(section["sh_addr"], section.data()) for section in elf.iter_sections()
                            if (section["sh_flags"] & 2) and section["sh_type"] == "SHT_PROGBITS"]
            symbols = {symbol.name: symbol["st_value"]
                       for symbol in elf.get_section_by_name(".symtab").iter_symbols()}
            sizes = {symbol.name: symbol["st_size"]
                     for symbol in elf.get_section_by_name(".symtab").iter_symbols()}
        assembly = checked([str(COMPILER / "fromelf.exe"), "--text", "-c", str(executable)], temporary)
        begin = symbols["foc_svpwm"] & ~1
        end = begin + sizes["foc_svpwm"]
        instructions = [line for line in assembly.splitlines()
                        if (match := re.match(r"\s*0x([0-9a-fA-F]+):", line))
                        and begin <= int(match.group(1), 16) < end]
        return segments, initializers, symbols, sizes, instructions

    candidate_image = build(production, "candidate")
    reference_image = build(reference, "reference")
    candidate_assembly = "\n".join(candidate_image[4])
    reference_assembly = "\n".join(reference_image[4])
    assert "__hardfp_fmaxf" not in candidate_image[2]
    assert "__hardfp_fminf" not in candidate_image[2]
    assert reference_assembly.count("BL       __hardfp_fmaxf") == 3, reference_assembly
    assert reference_assembly.count("BL       __hardfp_fminf") == 2, reference_assembly
    assert "VCMP" in candidate_assembly
    assert "__hardfp_sqrtf" in candidate_assembly and "__hardfp_sqrtf" in reference_assembly
    passed("真实O3/hardFP反汇编确认3个fmaxf及2个fminf调用消失，硬件浮点比较与原sqrt路径保留")

    class Model:
        def __init__(self, image):
            segments, initializers, self.symbols, _, _ = image
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            self.cpu.mem_map(0x08000000, 0x20000)
            self.cpu.mem_map(0x20000000, 0x8000)
            self.cpu.mem_map(0xE000E000, 0x1000)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0801F001)
            try:
                self.cpu.emu_start(self.symbols[name] | 1, 0x0801F000, count=100000)
            except UcError as error:
                raise AssertionError(f"{name}: {error}; pc={self.cpu.reg_read(UC_ARM_REG_PC):#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0801F000

        def operation(self, encoded):
            self.cpu.mem_write(self.symbols["model_input"], struct.pack("<4I", *encoded))
            self.call("model_svpwm")
            return (bytes(self.cpu.mem_read(self.symbols["model_output"], 20)) +
                    bytes(self.cpu.mem_read(self.symbols["model_result"], 4)) +
                    bytes(self.cpu.mem_read(self.symbols["model_valid"], 4)) +
                    bytes(self.cpu.mem_read(self.symbols["model_limited"], 4)))

    candidate, legacy = Model(candidate_image), Model(reference_image)
    compared = 0

    def compare_raw(encoded, valid=None):
        global compared
        actual, expected = candidate.operation(encoded), legacy.operation(encoded)
        assert actual == expected, (list(map(hex, encoded)), actual.hex(), expected.hex())
        compared += 1
        result, output_valid, limited = struct.unpack("<3I", actual[20:])
        assert result == output_valid
        if valid is not None:
            assert result == int(valid), (list(map(hex, encoded)), result)
        if not result:
            assert actual[:20] == bytes(20) and limited == 0
        return struct.unpack("<5I", actual[:20])

    def compare(alpha, beta, bus=12.0, ratio=0.45, valid=None):
        return compare_raw(list(map(bits, [alpha, beta, bus, ratio])), valid)

    for alpha in [0x00000000, 0x80000000]:
        for beta in [0x00000000, 0x80000000]:
            outputs = compare_raw([alpha, beta, bits(12.0), bits(0.45)], True)
            assert outputs == (alpha, beta, bits(0.5), bits(0.5), bits(0.5))
    for function in [candidate, legacy]:
        function.call("model_null_arguments")
        assert bytes(function.cpu.mem_read(function.symbols["model_result"], 4)) == bytes(4)
    passed("四种正负零输入保留applied符号且三相duty精确0.5，空输出参数拒绝行为不变")

    for magnitude in [1e-30, 0.01, 1.0, 5.4, 12.0, 1e30]:
        for angle_index in range(361):
            angle = angle_index * math.pi / 180.0
            compare(magnitude * math.cos(angle), magnitude * math.sin(angle), valid=True)
    for value in [-12.0, -5.4, -1.0, -1e-30, 1e-30, 1.0, 5.4, 12.0]:
        for alpha, beta in [(value, value), (value, -value), (value, 0.0), (0.0, value)]:
            compare(alpha, beta, valid=True)
    passed("全周与限幅/未限幅幅值、分量相等和轴线排序相等时5个float与全部标志逐位不变")

    for sector in range(7):
        angle = sector * math.pi / 3.0
        for magnitude in [1e-30, 1.0, 5.4, 12.0]:
            for alpha in neighbors(magnitude * math.cos(angle)):
                for beta in neighbors(magnitude * math.sin(angle)):
                    compare_raw([alpha, beta, bits(12.0), bits(0.45)])
    for bus in [1.0, 12.0, 48.0, 1e30]:
        for ratio in [0.01, 0.45, floating(0x3F13CD3A)]:
            for angle_index in range(36):
                angle = angle_index * math.pi / 18.0
                alpha = neighbors(bus * ratio * math.cos(angle))
                beta = neighbors(bus * ratio * math.sin(angle))
                for offset in range(3):
                    compare_raw([alpha[offset], beta[offset], bits(bus), bits(ratio)])
    passed("六扇区边界和电压限幅圆附近相邻float输出及limited标志逐位一致")

    special = [0, 1, 0x007FFFFF, 0x00800000, 0x00800001, 0x7F7FFFFF]
    signed = special + [value | 0x80000000 for value in special]
    for alpha in signed:
        for beta in signed:
            for bus in [1, 0x007FFFFF, 0x00800000, bits(12.0), 0x7F7FFFFF]:
                for ratio in [bits(0.01), bits(0.45), 0x3F13CD3A]:
                    compare_raw([alpha, beta, bus, ratio])
    passed("最小/最大subnormal、最小normal与FLT_MAX四象限、母线及limit下溢拒绝路径逐位一致")

    bad_values = [0x7F800000, 0xFF800000, 0x7FC00000, 0xFFC12345, 0x7F800001, 0x7FFFFFFF]
    for field in range(4):
        for bad in bad_values:
            encoded = list(map(bits, [1.0, -2.0, 12.0, 0.45]))
            encoded[field] = bad
            compare_raw(encoded, False)
    for bus in [0, 0x80000000, bits(-1.0)]:
        compare_raw([bits(1.0), bits(2.0), bus, bits(0.45)], False)
    for ratio in [0, 0x80000000, bits(-0.1), 0x3F13CD3B, bits(1.0)]:
        compare_raw([bits(1.0), bits(2.0), bits(12.0), ratio], False)
    passed("逐参数NaN含信号NaN/Inf及非法母线、比例整拍拒绝，旧输出与限幅标志清零一致")

    generator = random.Random(0x5A431)
    for _ in range(4000):
        compare_raw([generator.getrandbits(32), generator.getrandbits(32),
                     generator.getrandbits(31), generator.choice([bits(0.01), bits(0.45),
                                                                 0x3F13CD3A])])
    passed("4000组确定种子的全指数随机float对照保持逐位一致，不放宽数值容差")

for index, name in enumerate(CASES, 1):
    print(f"PASS {index}: {name}")
print(f"svpwm comparisons: {len(CASES)} groups; {compared} bitwise pairs; "
      f"function bytes {reference_image[3]['foc_svpwm']} -> {candidate_image[3]['foc_svpwm']}; "
      "ARMCC/Unicorn, no performance claim")
