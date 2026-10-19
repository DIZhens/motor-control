"""ARM 执行同源 ControlRuntime 与真实诊断模块；HCC/clock 是替身，不作实机预算声明。"""

from pathlib import Path
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP,
                              UC_ARM_REG_PRIMASK, UC_CPU_ARM_CORTEX_M4)

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent
TARGET = RUNTIME.parent
ROOT = TESTS.parents[4]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
IDLE, READY, ACTIVE, COMPLETE, FAILED = range(5)
NONE, NOT_READY, ABORTED, BUSY, CLOCK, CONTROL, STAGE, POWER, INCOMPLETE = range(9)


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def passed(name):
    CASES.append(name)


with tempfile.TemporaryDirectory(prefix="g431-stage-budget-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x10000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    sources = [RUNTIME / "stage_budget.c", RUNTIME / "stage_budget_runtime.c",
               TESTS / "stage_budget_model.c", ROOT / "firmware/app/motor/motor.c"]
    sources += [ROOT / "control" / name for name in ["position_estimator.c", "current_measurement.c",
                "electrical_angle.c", "current_controller.c", "speed_controller.c", "foc_math.c"]]
    objects = []
    for index, source in enumerate(sources):
        obj = temporary / f"source_{index}.o"
        checked([str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3", "-Otime",
                 "--split_sections", "-g", "-DSTM32G431xx", "-I", str(RUNTIME),
                 "-I", str(ROOT / "control"), "-I", str(ROOT / "firmware/app/motor"),
                 "-I", str(ROOT / "firmware/platform_contract"),
                 "-I", str(TARGET / "Drivers/CMSIS/Include"), "-I",
                 str(TARGET / "Drivers/CMSIS/Device/ST/STM32G4xx/Include"),
                 "-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=model_init", "--keep=source_2.o(*)", "-o", str(executable), *objects], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(p["p_vaddr"], p.data()) for p in elf.iter_segments() if p["p_type"] == "PT_LOAD"]
        initializers = [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                        if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
        sizes = {s.name: s["st_size"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
        ram_bytes = sum(s["sh_size"] for s in elf.iter_sections() if (s["sh_flags"] & 3) == 3)

    class Model:
        def __init__(self):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address, size in [(0x08000000, 0x40000), (0x20000000, 0x10000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            # 实际 ARM 函数入口计数，证明运行期 prepare 不再走昂贵初始化路径。
            self.initializer_entries = {}
            for name in ["g431_stage_budget_runtime_init", "current_measurement_init",
                         "current_measurement_calibrate", "position_estimator_init",
                         "position_estimator_update", "current_controller_init",
                         "speed_controller_init", "speed_controller_update"]:
                address = symbols[name] & ~1
                self.initializer_entries[name] = 0
                self.cpu.hook_add(UC_HOOK_CODE, self.count_initializer, name,
                                  begin=address, end=address)
            self.call("model_init")
            assert self.word("model_result_size") == 180
            assert self.word("model_facts_reads") == 1
            assert self.word("model_stage_count") == self.word("model_real_enable_count") == 0
            assert self.word("model_safe_off_count") == 0

        def count_initializer(self, cpu, address, size, name):
            self.initializer_entries[name] += 1

        def call(self, name):
            before_mask = self.cpu.reg_read(UC_ARM_REG_PRIMASK)
            self.cpu.reg_write(UC_ARM_REG_SP, 0x2000FFF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=100000)
            except UcError as error:
                raise AssertionError(f"{name}: {error}; pc={self.cpu.reg_read(UC_ARM_REG_PC):#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, "调用必须有界: " + name
            assert self.cpu.reg_read(UC_ARM_REG_PRIMASK) == before_mask
            assert self.word("model_real_enable_count") == 0

        def word(self, name):
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def write(self, name, value):
            self.cpu.mem_write(symbols[name], struct.pack("<I", value & 0xFFFFFFFF))

        @property
        def snapshot(self):
            return (self.word("g_target_stage_budget_sequence"),
                    bytes(self.cpu.mem_read(symbols["g_target_stage_budget_result"], 180)))

        @property
        def result(self):
            seq, data = self.snapshot
            assert seq % 2 == 0
            values = struct.unpack("<45I", data)
            names = ["schema", "request", "state", "failure", "count", "stage_calls", "accepted",
                     "enable_attempts", "observations", "speeds", "margin", "faults", "limited",
                     "fast", "slow", "last_elapsed", "last_step", "last_count", "last_entry",
                     "last_cr1", "last_bdtr"]
            result = dict(zip(names, values[:21]))
            result["rows"] = [values[i:i + 4] for i in range(21, 45, 4)]
            return result

        def prepare(self):
            entries = dict(self.initializer_entries)
            facts = self.word("model_facts_reads")
            self.call("model_prepare")
            assert self.initializer_entries == entries and self.word("model_facts_reads") == facts
            assert self.result["state"] == READY and self.result["request"] == 1
            assert self.word("g_target_stage_budget_active") == 1

        @property
        def template(self):
            return tuple(bytes(self.cpu.mem_read(symbols[name], sizes[name])) for name in
                         ["s_stage_initial_motor", "s_stage_initial_runtime"])

        @property
        def duty(self):
            return bytes(self.cpu.mem_read(symbols["model_last_duty"], 12))

        def run(self, count=80):
            for _ in range(count):
                self.call("model_step")

        def assert_failed(self, reason, count=0):
            result = self.result
            assert (result["state"], result["failure"], result["count"]) == (FAILED, reason, count), result
            assert self.word("g_target_stage_budget_active") == 0
            assert self.word("model_power_moe") == 0
            assert self.word("model_safe_off_count") > 0
            snapshot, calls = self.snapshot, self.word("model_stage_count")
            self.run(3)
            assert self.snapshot == snapshot and self.word("model_stage_count") == calls

    model = Model()
    initial = model.snapshot
    model.run(3)
    model.call("model_cancel")
    assert model.snapshot == initial and model.result["state"] == IDLE
    assert model.word("model_stage_count") == 0 and model.word("model_safe_off_count") == 0
    passed("启动仅只读HCC事实；默认无请求不stage、不使能、不发布合成RUN")

    model = Model()
    assert all(value > 0 for value in model.initializer_entries.values())
    initial_template = model.template
    initial_entries = dict(model.initializer_entries)
    model.prepare()
    assert model.initializer_entries == initial_entries and model.template == initial_template
    first_duty = []
    for _ in range(3):
        model.run(1)
        first_duty.append(model.duty)
    model.run(77)
    assert model.result["state"] == COMPLETE and model.template == initial_template
    entries = dict(model.initializer_entries)
    model.call("model_prepare_second")
    assert model.initializer_entries == entries and model.word("model_facts_reads") == 1
    assert model.template == initial_template
    for expected in first_duty:
        model.run(1)
        assert model.duty == expected
    assert model.template == initial_template
    passed("启动一次构建模板且无功率动作；prepare只复制，新窗口逐位重现初始积分/历史，模板不变")

    model = Model()
    model.prepare()
    ready = model.snapshot
    model.call("model_prepare")
    model.call("model_prepare_second")
    assert model.snapshot == ready and model.word("model_stage_count") == 0
    model.run(80)
    result = model.result
    assert result["state"] == COMPLETE and result["failure"] == NONE, result
    assert result["count"] == result["stage_calls"] == result["accepted"] == 80
    assert result["speeds"] == result["slow"] == 8 and result["observations"] == 4
    assert result["fast"] == 72 and 0 < result["limited"] < 80 and result["faults"] == 0
    assert [row[0] for row in result["rows"]] == [80, 80, 8, 4, 72, 8]
    assert result["rows"][0] == (80, 760, 760, 60800)
    assert result["rows"][1] == (80, 640, 640, 51200)
    assert result["margin"] == 1640 and result["last_bdtr"] == 0
    assert model.word("model_physical_position_reads") == model.word("model_physical_bus_reads") == 4
    terminal = model.snapshot
    model.run(3)
    model.call("model_cancel")
    model.call("model_prepare")
    assert model.snapshot == terminal
    passed("80拍覆盖速度8/观测4/快72/慢8/正常与限幅，原锚点统计和回绕准确，终态冻结")

    for name in ["model_position_available", "model_bus_available", "model_observation_valid"]:
        model = Model()
        model.prepare()
        model.write(name, 0)
        model.run(20)
        model.assert_failed(NOT_READY, 19)
        assert model.word("model_physical_position_reads") == model.word("model_physical_bus_reads") == 1
    passed("刷新拍确实调用真实HCC latest端口；无publication或采集无效使诊断失去许可")

    model = Model()
    model.write("model_permitted", 0)
    model.call("model_prepare")
    model.assert_failed(NOT_READY)
    assert model.word("model_stage_count") == 0
    passed("无许可准备失败且没有stage")

    for reason, call in [(ABORTED, "model_cancel"), (BUSY, "model_busy")]:
        model = Model()
        model.prepare()
        model.run(3)
        model.call(call)
        model.assert_failed(reason, 3)
    passed("Stop取消与新请求BUSY保留前三拍证据，不自动恢复")

    model = Model()
    model.prepare()
    model.run(3)
    model.write("model_permitted", 0)
    model.run(1)
    model.assert_failed(NOT_READY, 3)
    assert model.word("model_stage_count") == 3
    passed("每拍现场许可复核，失去许可不执行下一stage")

    for read in [1, 2, 3]:
        model = Model()
        model.prepare()
        model.write("model_stamp_fail_read", read)
        model.run(1)
        model.assert_failed(CLOCK)
    passed("入口/调用前/调用后任一时钟读取失败均冻结")

    for read in [1, 2, 3]:
        model = Model()
        model.prepare()
        model.write("model_moe_read", read)
        model.run(1)
        model.assert_failed(POWER)
    passed("入口/调用前/调用后任一MOE打开均失败，真实enable端口从未调用")

    model = Model()
    model.prepare()
    model.write("model_stage_ok", 0)
    model.run(1)
    model.assert_failed(STAGE)
    assert model.result["stage_calls"] == 1 and model.result["accepted"] == 0
    assert model.result["last_elapsed"] == 760 and model.result["last_count"] == 3340
    assert all(row == (0, 0, 0, 0) for row in model.result["rows"])
    passed("真实stage拒绝保留完整返回点原始证据，不混入成功统计")

    model = Model()
    model.prepare()
    model.write("model_stage_cost", 2400)
    model.run(1)
    model.assert_failed(CLOCK)
    assert model.result["stage_calls"] == model.result["accepted"] == 1
    assert model.result["last_elapsed"] == 2560 and model.result["last_count"] == 1540
    passed("stage返回已跨1700截止线即失败；HCC接受不能替代诊断的原半周复核")

    for read in [2, 3]:
        model = Model()
        model.prepare()
        model.write("model_bad_anchor_read", read)
        model.run(1)
        model.assert_failed(CLOCK)
    passed("stage前后原锚点变化均拒绝，不允许刷新起点制造预算")

    model = Model()
    model.prepare()
    model.write("model_time_step", 2)
    model.run(2)
    model.assert_failed(CONTROL, 1)
    assert model.result["faults"] != 0 and model.word("model_stage_count") == 1
    passed("正式Runtime拒绝同步采样跳拍，不绕过freshness或故障路径")

    model = Model()
    model.prepare()
    model.write("model_force_enable", 1)
    model.run(1)
    model.assert_failed(POWER)
    assert model.result["enable_attempts"] == 1
    passed("诊断enable端口始终拒绝并锁存，不可能调用真实使能")

    model = Model()
    model.prepare()
    model.call("model_cancel")
    model.call("model_prepare_second")
    assert model.result["state"] == READY and model.result["request"] == 2
    model.run(80)
    assert model.result["state"] == COMPLETE
    passed("仅新的显式请求可在终态后构建新试具，旧nonce不会重跑")

    print(f"PASS: {len(CASES)} 组 stage budget ARM 模型；RAM={ram_bytes} bytes；合成cycle不代表实机性能")
    for name in CASES:
        print("  " + name)
