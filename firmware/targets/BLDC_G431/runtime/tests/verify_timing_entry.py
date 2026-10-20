"""执行真实Timing入口、私有Runtime和诊断；HCC/MMIO为模型，不代表电气或性能验收。"""

from pathlib import Path
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB,
                     UC_HOOK_CODE, UC_HOOK_MEM_READ)
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP,
                              UC_ARM_REG_PRIMASK, UC_CPU_ARM_CORTEX_M4)

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent
TARGET = RUNTIME.parent
ROOT = TESTS.parents[4]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
TIM1, TIM6, DWT = 0x40012C00, 0x40001000, 0xE0001004
MOE, CEN, DIR, CMS = 1 << 15, 1, 1 << 4, 1 << 5
CASES = []
NAMES = ["schema", "request", "mode", "state", "failure", "expected", "count",
         "stage_calls", "accepted", "sinks", "enables", "speeds", "observations",
         "fast", "slow", "limited", "faults", "work", "anchor", "irq", "timer",
         "entry", "cr1", "bdtr", "stage_budget", "irq_budget", "over_stage", "over_consumer"]


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def passed(name):
    CASES.append(name)


with tempfile.TemporaryDirectory(prefix="g431-timing-entry-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x8000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    sources = [RUNTIME / "timing_entry.c", RUNTIME / "runtime_timing.c",
               RUNTIME / "runtime_timing_runtime.c", TESTS / "timing_entry_model.c",
               ROOT / "firmware/app/motor/motor.c"]
    sources += [ROOT / "control" / name for name in ["position_estimator.c", "current_measurement.c",
                "electrical_angle.c", "current_controller.c", "speed_controller.c", "foc_math.c"]]
    command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3", "-Otime",
               "--split_sections", "-g", "-DSTM32G431xx", "-DBLDC_G431_TIMING_ENTRY"]
    for path in [TESTS / "stubs", RUNTIME, ROOT / "control", ROOT / "firmware/app/motor",
                 ROOT / "firmware/platform_contract", TARGET / "Drivers/CMSIS/Include",
                 TARGET / "Drivers/CMSIS/Device/ST/STM32G4xx/Include"]:
        command += ["-I", str(path)]
    objects = []
    for index, source in enumerate(sources):
        obj = temporary / f"source_{index}.o"
        checked(command + ["-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=entry_model_start", "--keep=source_3.o(*)", "-o", str(executable), *objects], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(p["p_vaddr"], p.data()) for p in elf.iter_segments() if p["p_type"] == "PT_LOAD"]
        initializers = [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                        if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
        ram_bytes = sum(s["sh_size"] for s in elf.iter_sections() if (s["sh_flags"] & 3) == 3)
    assert "control_runtime_step" not in symbols and "target_runtime_start" not in symbols

    class Model:
        def __init__(self, settings=None, start=True):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address, size in [(0x08000000, 0x40000), (0x20000000, 0x8000),
                                  (0x40001000, 0x1000), (0x40012000, 0x2000),
                                  (0xE0001000, 0x1000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.memory_write(0xE000ED88, 0x00F00000)
            self.memory_write(TIM6 + 0x28, 169)
            self.memory_write(TIM1 + 0x44, MOE)
            self.entries = {name: 0 for name in ["g431_runtime_timing_runtime_step",
                           "current_controller_update", "g431_runtime_timing_prepare",
                           "g431_runtime_timing_step", "g431_runtime_timing_runtime_init"]}
            for name in self.entries:
                address = symbols[name] & ~1
                self.cpu.hook_add(UC_HOOK_CODE, self.enter, name, begin=address, end=address)
            self.cpu.hook_add(UC_HOOK_MEM_READ, self.clock_read, begin=DWT, end=DWT + 3)
            for name, value in (settings or {}).items():
                if isinstance(value, float):
                    self.cpu.mem_write(symbols[name], struct.pack("<f", value))
                else:
                    self.write(name, value)
            if start:
                self.call("entry_model_start")

        def enter(self, cpu, address, size, name):
            self.entries[name] += 1
            if name == "g431_runtime_timing_runtime_step":
                self.write("model_cycles", self.word("model_cycles") + self.word("model_compute_cost"))

        def clock_read(self, cpu, access, address, size, value, data):
            cycles = (self.word("model_cycles") + self.word("model_clock_step")) & 0xFFFFFFFF
            self.write("model_cycles", cycles)
            self.memory_write(DWT, cycles)
            if self.word("entry_start_count"):
                elapsed = (cycles - self.word("model_anchor")) & 0xFFFFFFFF
                entry = self.word("model_entry_count")
                if elapsed < 8500:
                    if elapsed <= entry:
                        count, direction = entry - elapsed, DIR
                    else:
                        count, direction = elapsed - entry, 0
                    self.memory_write(TIM1 + 0x24, count)
                    self.memory_write(TIM1, CEN | CMS | direction)

        def call(self, name):
            mask = self.cpu.reg_read(UC_ARM_REG_PRIMASK)
            self.cpu.reg_write(UC_ARM_REG_SP, 0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=100000)
            except UcError as error:
                raise AssertionError(f"{name}: {error}; pc={self.cpu.reg_read(UC_ARM_REG_PC):#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, name
            assert self.cpu.reg_read(UC_ARM_REG_PRIMASK) == mask
            assert self.word("model_real_enable_count") == self.word("entry_trip_rearm_count") == 0

        def memory_write(self, address, value):
            self.cpu.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

        def word(self, name):
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def write(self, name, value):
            self.memory_write(symbols[name], value)

        @property
        def health(self):
            assert self.word("g_target_runtime_timing_health_sequence") % 2 == 0
            values = struct.unpack("<12If3I", self.cpu.mem_read(symbols["g_target_runtime_timing_health"], 64))
            names = ["schema", "samples", "time_low", "time_high", "last_sample", "interval",
                     "faults", "guard", "started", "current", "power_off", "bus", "voltage",
                     "position", "permitted", "reserved"]
            return dict(zip(names, values))

        @property
        def result(self):
            assert self.word("g_target_runtime_timing_sequence") % 2 == 0
            values = struct.unpack("<64I", self.cpu.mem_read(symbols["g_target_runtime_timing_result"], 256))
            result = dict(zip(NAMES, values[:28]))
            result["rows"] = [values[i:i + 4] for i in range(28, 64, 4)]
            return result

        def tick(self, count=1):
            for _ in range(count):
                self.call("entry_model_tick")

        def request(self, mode=2, nonce=1):
            self.write("g_target_runtime_timing_request", (nonce << 2) | mode)
            self.tick()

        def permission(self):
            self.call("entry_model_get_permission")
            return self.word("entry_permitted")

    model = Model()
    assert model.health["started"] == 1 and model.health["power_off"] == 1
    assert model.word("entry_bind_count") == model.word("entry_start_count") == 1
    assert model.word("entry_arm_count") == 4 and model.word("model_safe_off_count") == 1
    model.tick(5)
    assert model.permission() == 1 and model.entries["g431_runtime_timing_runtime_step"] == 0
    assert model.word("model_stage_count") == 0
    model.call("entry_model_start")
    assert model.word("entry_start_count") == 1
    passed("真实入口只启动持续采样，不使能/不rearm/无业务Runtime；start不自动重试")

    for setting, value in [("entry_cordic_ok", 0), ("entry_facts_ok", 0), ("entry_rate", 10000),
                           ("entry_period", 1), ("entry_bind_ok", 0), ("entry_arm_ok", 0),
                           ("entry_start_ok", 0), ("entry_trip_ok", 0), ("entry_trip_causes", 2)]:
        model = Model({setting: value})
        assert model.health["faults"] & 1 and model.health["started"] == 0
        starts = model.word("entry_start_count")
        model.write(setting, 1)
        model.call("entry_model_start")
        assert model.word("entry_start_count") == starts and model.permission() == 0
    passed("启动配置/绑定/arm/trip失败锁存STARTUP，无自动恢复或再次start")

    model = Model()
    model.request()
    assert model.result["state"] == 1 and model.result["count"] == 0
    assert model.entries["g431_runtime_timing_prepare"] == 1
    assert model.entries["g431_runtime_timing_runtime_step"] == 0
    model.tick(81)
    result = model.result
    assert result["state"] == 3 and result["count"] == result["sinks"] == 80, result
    assert model.entries["g431_runtime_timing_runtime_step"] == model.entries["current_controller_update"] == 80
    assert model.entries["g431_runtime_timing_step"] == 81
    assert result["stage_calls"] == result["accepted"] == result["enables"] == 0
    assert [row[0] for row in result["rows"]] == [80] * 5 + [8, 8, 72, 8]
    assert model.word("model_physical_position_reads") == model.word("model_physical_bus_reads") == 90
    passed("准备拍不计算；后80拍各单次同源Runtime，真实prefix getter与刷新getter开销均存在")

    model = Model()
    model.request()
    model.tick(80)
    assert model.result["state"] == 1 and model.word("g_target_runtime_timing_active") == 1
    assert model.entries["g431_runtime_timing_runtime_step"] == 80
    model.write("entry_current_valid", 0)
    model.tick()
    assert model.result["state"] == 4 and model.result["failure"] == 1 and model.result["count"] == 80
    assert model.health["faults"] & 8 and model.permission() == 0
    assert model.entries["g431_runtime_timing_runtime_step"] == model.result["sinks"] == 80
    passed("第80拍后采样被Platform判无效时，空拍收尾发布FAILED且不运行第81次控制")

    model = Model()
    model.request(mode=1)
    model.tick(81)
    assert model.result["state"] == 3 and model.result["accepted"] == model.result["stage_calls"] == 80
    assert model.result["sinks"] == model.entries["g431_runtime_timing_runtime_step"] == 0
    assert model.result["rows"][4][0] == 80 and model.result["rows"][4][1] >= 400
    passed("stage-only真实调用80次且无Runtime，IRQ尾部包含模型注入的Platform/HAL成本")

    model = Model()
    model.request()
    model.tick(3)
    prepares = model.entries["g431_runtime_timing_prepare"]
    steps = model.entries["g431_runtime_timing_runtime_step"]
    model.request(mode=1, nonce=2)
    assert model.result["state"] == 4 and model.result["failure"] == 3 and model.result["count"] == 3
    model.tick(5)
    assert model.entries["g431_runtime_timing_prepare"] == prepares
    assert model.entries["g431_runtime_timing_runtime_step"] == steps
    model.request(nonce=3)
    model.tick(81)
    assert model.result["state"] == 3 and model.result["request"] == 14
    passed("活动时新nonce只BUSY取消，不执行/重开；终态只有再次显式新nonce可测")

    model = Model()
    model.tick()
    sequence, public_samples = model.word("g_target_runtime_timing_health_sequence"), model.health["samples"]
    previous = model.word("s_last_sample_cycles")
    model.tick(99)
    assert model.word("s_last_sample_cycles") != previous
    assert model.word("g_target_runtime_timing_health_sequence") == sequence
    assert model.health["samples"] == public_samples == 1
    model.tick()
    assert model.health["samples"] == 101 and model.word("g_target_runtime_timing_health_sequence") == sequence + 2
    passed("心跳每拍更新而health按100拍低频发布，不以发布周期冒充采样心跳")

    for setting, value in [("entry_current_valid", 0), ("entry_current_mask", 4),
                           ("entry_current_b", float("nan")), ("entry_current_c", float("inf"))]:
        model = Model({setting: value})
        model.request()
        assert model.result["state"] == 4 and model.result["failure"] == 1
        assert model.health["faults"] & 8 and model.permission() == 0
        assert model.entries["g431_runtime_timing_runtime_step"] == 0
    passed("真实采样完整性/mask/有限数异常锁存CURRENT，不执行试具计算")

    for setting, value in [("model_position_available", 0), ("entry_position_referenced", 0),
                           ("entry_position_turns", -0.01), ("entry_position_turns", 1.0),
                           ("entry_position_turns", float("nan")), ("entry_position_age", 101),
                           ("entry_position_future", 1), ("model_bus_available", 0),
                           ("entry_bus_voltage", 3.61), ("entry_bus_voltage", -0.01),
                           ("entry_bus_voltage", float("inf")), ("entry_bus_age", 101),
                           ("entry_bus_future", 1)]:
        model = Model({setting: value})
        model.request()
        assert model.result["state"] == 4 and model.result["failure"] == 1, (setting, value, model.result)
        assert model.permission() == 0 and model.entries["g431_runtime_timing_runtime_step"] == 0
    passed("真实position参考/范围/新鲜度及逻辑母线0..3.6V许可严格检查")

    for setting, value in [("entry_trip_active", 1), ("entry_trip_causes", 1), ("entry_trip_ok", 0)]:
        model = Model()
        model.request()
        model.write(setting, value)
        model.tick()
        assert model.result["state"] == 4 and model.result["failure"] == 1
        assert model.health["faults"] & 16 and model.permission() == 0
    model = Model()
    model.request()
    model.memory_write(TIM1 + 0x44, MOE)
    model.tick()
    assert model.health["faults"] & 32 and model.permission() == 0
    assert struct.unpack("<I", model.cpu.mem_read(TIM1 + 0x44, 4))[0] & MOE == 0
    passed("真实trip与意外MOE锁存故障并关断，不可用合成RUN掩盖")

    for notification_only in [0, 1]:
        model = Model()
        model.request()
        model.tick(79)
        model.write("entry_trip_notify_only", notification_only)
        model.write("entry_trip_on_cordic_call", model.word("entry_cordic_calls") + 1)
        model.tick()
        assert model.result["state"] == 4 and model.result["failure"] == 1 and model.result["count"] == 79
        assert model.permission() == 0
        model.tick()
        assert model.health["faults"] & 16
    passed("第80拍计算中trip或仅通知均在IRQ尾部撤许可，不能发布COMPLETE")

    model = Model()
    model.tick()
    model.write("model_clock_step", 0)
    sample = model.word("s_last_sample_cycles")
    model.write("model_cycles", sample + 21250)
    model.call("entry_model_guard")
    assert model.word("g_target_runtime_timing_guard_faults") == 0
    model.write("model_cycles", sample + 21251)
    model.call("entry_model_guard")
    assert model.word("g_target_runtime_timing_guard_faults") & 4
    assert model.permission() == 0
    model = Model()
    model.tick()
    model.write("model_clock_step", 0)
    model.write("model_cycles", model.word("s_last_sample_cycles") + 100)
    model.call("entry_model_guard")
    model.call("entry_model_guard")
    assert model.word("g_target_runtime_timing_guard_faults") & 2
    passed("独立TIM6 guard保留125us严格上界和DWT停滞检测，故障后许可立即撤销")

    for cycles in [12749, 21251]:
        model = Model()
        model.tick()
        model.write("entry_tick_cycles", cycles)
        model.tick(2)
        assert model.health["faults"] & 4 and model.permission() == 0, (cycles, model.health,
            struct.unpack("<16I", model.cpu.mem_read(symbols["s_health"], 64)))
    passed("真实consumer入口75..125us间隔越界锁存CADENCE，不自动清故障")

    model = Model()
    model.request()
    model.write("SystemCoreClock", 160000000)
    model.tick()
    assert model.health["faults"] & 2 and model.result["state"] == 4 and model.permission() == 0
    passed("时钟事实改变立即拒绝下一计算并锁存CLOCK")

    print(f"PASS: {len(CASES)} 组 timing entry ARM 集成模型；RAM={ram_bytes} bytes；合成cycle不代表实机性能")
    for name in CASES:
        print("  " + name)
