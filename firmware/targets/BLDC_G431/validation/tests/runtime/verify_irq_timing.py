"""执行正式 ARM IRQ 观测模块；传入合成 cycle，只验证边界和统计，不代表实测性能。"""

from pathlib import Path
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP,
                              UC_ARM_REG_PRIMASK, UC_ARM_REG_R0, UC_ARM_REG_R1,
                              UC_ARM_REG_R2, UC_CPU_ARM_CORTEX_M4)

TESTS = Path(__file__).resolve().parent
RUNTIME = Path(__file__).resolve().parents[3] / 'runtime'
TARGET = Path(__file__).resolve().parents[3]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
IDLE, ACTIVE, COMPLETE, FAILED = range(4)
BUSY, ORDER, MISSING, CLOCK, NOT_READY = range(1, 6)


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def passed(name):
    CASES.append(name)


with tempfile.TemporaryDirectory(prefix="g431-irq-timing-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x10000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    obj = temporary / "irq_timing.o"
    checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
             "--split_sections", "-g", "-DSTM32G431xx", "-I", str(RUNTIME),
             "-I", str(TARGET / 'Drivers/CMSIS/Include'), "-I",
             str(TARGET / 'Drivers/CMSIS/Device/ST/STM32G4xx/Include'),
             "-c", str(TARGET / 'validation/timing/irq_timing.c'), "-o", str(obj)], temporary)
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=g431_irq_timing_init", "--keep=irq_timing.o(*)", "-o", str(executable),
             str(obj)], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(p["p_vaddr"], p.data()) for p in elf.iter_segments() if p["p_type"] == "PT_LOAD"]
        initializers = [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                        if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
        sizes = {s.name: s["st_size"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
        ram_bytes = sum(s["sh_size"] for s in elf.iter_sections()
                        if (s["sh_flags"] & 3) == 3)

    class Model:
        def __init__(self, mask=0):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address, size in [(0x08000000, 0x40000), (0x20000000, 0x10000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.reg_write(UC_ARM_REG_PRIMASK, mask)
            self.call("init")

        def call(self, name, *arguments):
            before_mask = self.cpu.reg_read(UC_ARM_REG_PRIMASK)
            for register, value in zip([UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2], arguments):
                self.cpu.reg_write(register, value & 0xFFFFFFFF)
            self.cpu.reg_write(UC_ARM_REG_SP, 0x2000FFF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                self.cpu.emu_start(symbols["g431_irq_timing_" + name] | 1, 0x0803F000, count=10000)
            except UcError as error:
                raise AssertionError(f"{name}: {error}; pc={self.cpu.reg_read(UC_ARM_REG_PC):#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, "调用必须有界: " + name
            assert self.cpu.reg_read(UC_ARM_REG_PRIMASK) == before_mask

        def word(self, name):
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        @property
        def active(self):
            return self.word("g_target_irq_timing_active")

        @property
        def snapshot(self):
            return (self.word("g_target_irq_timing_sequence"),
                    bytes(self.cpu.mem_read(symbols["g_target_irq_timing_result"],
                                            sizes["g_target_irq_timing_result"])))

        @property
        def result(self):
            sequence, data = self.snapshot
            assert sequence % 2 == 0 and len(data) == 84
            words = struct.unpack("<21I", data)
            return {"schema": words[0], "request": words[1], "state": words[2],
                    "failure": words[3], "count": words[4],
                    "rows": [words[i:i + 4] for i in range(5, 21, 4)]}

        def arm(self, request=1, permitted=True):
            self.call("irq_enter", 50, request, int(permitted))
            self.call("irq_exit", 9999, int(permitted))
            assert self.active == 0

        def irq(self, stamps=(100, 110, 150, 160, 300, 305, 340), request=1,
                permitted=True, exit_permitted=True, regular=False, stop_at=None):
            self.call("irq_enter", stamps[0], request, int(permitted))
            if not regular:
                for index, name in enumerate(["platform_enter", "consumer_enter", "observe_enter",
                                              "observe_exit", "consumer_exit"], 1):
                    if stop_at is not None and index > stop_at:
                        break
                    self.call(name, stamps[index])
            self.call("irq_exit", stamps[6], int(exit_permitted))

    for mask in [0, 1]:
        model = Model(mask)
        initial = model.snapshot
        assert model.result == {"schema": 1, "request": 0, "state": IDLE, "failure": 0,
                                "count": 0, "rows": [(0, 0, 0, 0)] * 4}
        model.irq(request=0)
        assert model.snapshot == initial and model.active == 0
    passed("默认零请求与活动外挂点无副作用；实际ARM执行不访问外设、不改调用方PRIMASK")

    model = Model()
    model.arm()
    active = model.snapshot
    assert model.result["state"] == ACTIVE and model.result["count"] == 0
    expected = [[] for _ in range(4)]
    for i in range(256):
        base = i * 8500
        offsets = [0, 10 + i % 4, 50 + i % 7, 60 + i % 5,
                   200 + i % 5, 205 + i % 5, 240 + i % 7]
        model.irq(tuple(base + value for value in offsets))
        for row, span in zip(expected, [offsets[6], offsets[2] - offsets[1],
                                      offsets[5] - offsets[2], offsets[4] - offsets[3]]):
            row.append(span)
        if i < 255:
            assert model.snapshot == active
    result = model.result
    assert result["state"] == COMPLETE and result["failure"] == 0 and result["count"] == 256
    for actual, values in zip(result["rows"], expected):
        assert actual == (256, min(values), max(values), sum(values))
    frozen = model.snapshot
    model.irq()
    assert model.snapshot == frozen and model.active == 0
    passed("接受请求的准备IRQ跳过，256个完整IRQ四行独立统计后冻结，活动期间公开快照不变")

    for mixed in [False, True]:
        model = Model()
        model.arm()
        for i in range(256):
            model.irq(regular=not mixed or i % 2 == 1)
        result = model.result
        assert result["state"] == COMPLETE and result["count"] == 256
        assert result["rows"][0] == (256, 240, 240, 256 * 240)
        assert [row[0] for row in result["rows"]] == ([256, 128, 128, 128] if mixed else [256, 0, 0, 0])
    passed("regular-only和regular/injected混合窗口合法，缺少injected边界只减少子行count而不伪造耗时")

    model = Model()
    model.arm()
    model.irq()
    model.irq(request=0)
    model.call("irq_enter", 100, 2, 1)
    assert model.result["state"] == FAILED and model.result["failure"] == BUSY
    assert model.result["request"] == 2 and model.result["count"] == 0
    frozen = model.snapshot
    model.irq(request=2)
    assert model.snapshot == frozen
    model.arm(3)
    assert model.result["state"] == ACTIVE and model.result["request"] == 3
    model.irq(request=3)
    model.call("irq_enter", 100, 3, 1)
    model.call("irq_enter", 200, 3, 1)
    assert model.result["state"] == FAILED and model.result["failure"] == ORDER
    passed("重复nonce不重开、零nonce不取消，活动新nonce记BUSY；新身份可显式重试，重复IRQ入口拒绝")

    for stage in ["start", "warmup_exit", "entry", "final_exit"]:
        model = Model()
        model.arm(permitted=stage != "start")
        if stage == "warmup_exit":
            model.call("irq_exit", 10000, 0)
        elif stage == "entry":
            model.irq()
            model.irq(permitted=False)
        elif stage == "final_exit":
            for _ in range(255):
                model.irq()
            model.irq(exit_permitted=False)
        result = model.result
        assert result["state"] == FAILED and result["failure"] == NOT_READY
        assert result["count"] == {"start": 0, "warmup_exit": 0, "entry": 1, "final_exit": 255}[stage]
        assert all(row[0] == result["count"] for row in result["rows"])
        frozen = model.snapshot
        model.irq()
        assert model.snapshot == frozen
    passed("启动、准备拍出口、采样入口和最后IRQ出口撤销许可都失败，最后一点不被误报COMPLETE")

    for stop_at in range(1, 5):
        model = Model()
        model.arm()
        model.irq()
        model.irq(stop_at=stop_at)
        result = model.result
        assert result["state"] == FAILED and result["failure"] == MISSING and result["count"] == 1
        assert all(row[0] == 1 for row in result["rows"])
    model = Model()
    model.arm()
    model.call("irq_enter", 100, 1, 1)
    model.call("observe_enter", 120)
    model.call("irq_exit", 400, 1)
    assert model.result["failure"] == ORDER and model.result["count"] == 0
    passed("出现Platform边界后必须完整嵌套，任何缺失或乱序都拒绝整拍且保留先前完整统计")

    for stamps in [(100, 110, 150, 160, 300, 305, 100),
                   (100, 110, 150, 160, 300, 305, 99),
                   (100, 110, 150, 160, 300, 305, 34101),
                   (100, 150, 150, 160, 300, 305, 340),
                   (100, 110, 150, 300, 160, 305, 340),
                   (100, 90, 150, 160, 300, 305, 340),
                   (100, 110, 150, 160, 300, 400, 340)]:
        model = Model()
        model.arm()
        model.irq(stamps)
        result = model.result
        assert result["state"] == FAILED and result["failure"] == CLOCK and result["count"] == 0
        assert result["rows"] == [(0, 0, 0, 0)] * 4
    passed("零差、倒退、越界、子段时钟倒序及跨出父IRQ的边界拒绝，绝不提交半份行统计")

    for base in [0, 0xFFFFFF00]:
        model = Model()
        model.arm()
        for _ in range(256):
            model.irq(tuple((base + offset) & 0xFFFFFFFF for offset in [0, 1, 2, 3, 33997, 33998, 34000]))
        assert model.result["state"] == COMPLETE
        assert model.result["rows"][0] == (256, 34000, 34000, 8704000)
    passed("cycle零起点、单次自然回绕及34000上界等值合法，固定256点uint32累加不溢出")

for index, name in enumerate(CASES, 1):
    print(f"PASS {index}: {name}")
print(f"g431 irq timing: {len(CASES)} groups passed; ARM RAM={ram_bytes} bytes; synthetic cycles, no hardware timing claim")
