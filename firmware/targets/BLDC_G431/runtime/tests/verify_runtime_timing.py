"""执行专用诊断与同源Runtime的ARM模型；人工cycle只校验边界/安全，不代表性能。"""

from pathlib import Path
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB,
                     UC_HOOK_CODE, UC_HOOK_MEM_READ)
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP,
                              UC_ARM_REG_PRIMASK, UC_ARM_REG_R0, UC_CPU_ARM_CORTEX_M4)

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent
TARGET = RUNTIME.parent
ROOT = TESTS.parents[4]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
CASES = []
IDLE, READY, ACTIVE, COMPLETE, FAILED = range(5)
NONE, NOT_READY, ABORTED, BUSY, CLOCK, CONTROL, STAGE, POWER, INCOMPLETE, IRQ_BUDGET = range(10)
NAMES = ["schema", "request", "mode", "state", "failure", "expected", "count",
         "stage_calls", "accepted", "sinks", "enables", "speeds", "observations",
         "fast", "slow", "limited", "faults", "work", "anchor", "irq", "timer",
         "entry", "cr1", "bdtr", "over_stage", "over_consumer"]


def checked(arguments, directory):
    result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


def passed(name):
    CASES.append(name)


with tempfile.TemporaryDirectory(prefix="g431-runtime-timing-") as directory:
    temporary = Path(directory)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x10000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    sources = [RUNTIME / "runtime_timing.c", RUNTIME / "runtime_timing_runtime.c",
               TESTS / "runtime_timing_model.c", ROOT / "firmware/app/motor/motor.c"]
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
            for address, size in [(0x08000000, 0x40000), (0x20000000, 0x10000),
                                  (0xE0001000, 0x1000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            self.entries = {}
            names = ["g431_runtime_timing_runtime_init", "current_measurement_init",
                     "current_measurement_calibrate", "position_estimator_init",
                     "position_estimator_update", "current_controller_init", "speed_controller_init",
                     "speed_controller_update", "g431_runtime_timing_runtime_step",
                     "current_controller_update", "g431_runtime_timing_sink"]
            for name in names:
                address = symbols[name] & ~1
                self.entries[name] = 0
                self.cpu.hook_add(UC_HOOK_CODE, self.count_entry, name, begin=address, end=address)
            self.sink_values = []
            self.cpu.hook_add(UC_HOOK_MEM_READ, self.clock_read, begin=0xE0001004, end=0xE0001007)
            self.call("model_init")
            assert self.word("model_result_size") == 248
            assert self.word("model_facts_reads") == 1
            assert self.word("model_stage_count") == self.word("model_real_enable_count") == 0
            assert self.word("model_safe_off_count") == 0

        def count_entry(self, cpu, address, size, name):
            self.entries[name] += 1
            if name == "g431_runtime_timing_runtime_step":
                self.write("model_cycles", self.word("model_cycles") + self.word("model_compute_cost"))
            if name == "g431_runtime_timing_sink":
                pointer = cpu.reg_read(UC_ARM_REG_R0)
                if self.word("model_sink_override"):
                    cpu.mem_write(pointer + 4 * self.word("model_sink_component"),
                                  struct.pack("<I", self.word("model_sink_bits")))
                self.sink_values.append(bytes(cpu.mem_read(pointer, 12)))

        def clock_read(self, cpu, access, address, size, value, data):
            cycles = (self.word("model_cycles") + self.word("model_clock_step")) & 0xFFFFFFFF
            self.write("model_cycles", cycles)
            cpu.mem_write(0xE0001004, struct.pack("<I", cycles))

        def call(self, name):
            mask = self.cpu.reg_read(UC_ARM_REG_PRIMASK)
            self.cpu.reg_write(UC_ARM_REG_SP, 0x2000FFF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            try:
                self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=100000)
            except UcError as error:
                raise AssertionError(f"{name}: {error}; pc={self.cpu.reg_read(UC_ARM_REG_PC):#x}") from error
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, "调用必须有界: " + name
            assert self.cpu.reg_read(UC_ARM_REG_PRIMASK) == mask
            assert self.word("model_real_enable_count") == 0

        def word(self, name):
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def write(self, name, value):
            self.cpu.mem_write(symbols[name], struct.pack("<I", value & 0xFFFFFFFF))

        @property
        def snapshot(self):
            return self.word("g_target_runtime_timing_sequence"), bytes(
                self.cpu.mem_read(symbols["g_target_runtime_timing_result"], 248))

        @property
        def result(self):
            sequence, data = self.snapshot
            assert sequence % 2 == 0
            values = struct.unpack("<62I", data)
            result = dict(zip(NAMES, values[:26]))
            result["rows"] = [values[i:i + 4] for i in range(26, 62, 4)]
            return result

        @property
        def template(self):
            return tuple(bytes(self.cpu.mem_read(symbols[name], sizes[name])) for name in
                         ["s_timing_initial_motor", "s_timing_initial_runtime"])

        def prepare(self, mode=2, nonce=1):
            entries, facts = dict(self.entries), self.word("model_facts_reads")
            self.write("model_request", (nonce << 2) | mode)
            self.call("model_prepare")
            assert self.entries == entries and self.word("model_facts_reads") == facts
            assert self.result["state"] == READY and self.result["request"] == (nonce << 2) | mode
            assert self.word("g_target_runtime_timing_active") == 1

        def run(self, count=80):
            for _ in range(count):
                self.call("model_step")

        def assert_failed(self, reason, count=0):
            result = self.result
            assert (result["state"], result["failure"], result["count"]) == (FAILED, reason, count), result
            assert self.word("g_target_runtime_timing_active") == 0
            assert self.word("model_power_moe") == 0
            snapshot, calls = self.snapshot, self.word("model_stage_count")
            self.run(3)
            assert self.snapshot == snapshot and self.word("model_stage_count") == calls

    model = Model()
    initial = model.snapshot
    model.run(3)
    model.call("model_regular_irq")
    model.call("model_cancel")
    assert model.snapshot == initial and model.result["state"] == IDLE
    assert model.word("model_stage_count") == model.word("model_safe_off_count") == 0
    passed("默认无请求不运行/不stage，启动仅构建私有模板并只读HCC事实")

    model = Model()
    initial_template = model.template
    model.prepare()
    model.run()
    assert model.result["state"] == COMPLETE
    first_values = model.sink_values[:3]
    entries = dict(model.entries)
    model.prepare(nonce=2)
    assert model.entries == entries and model.template == initial_template
    model.run(3)
    assert model.sink_values[-3:] == first_values and model.template == initial_template
    passed("prepare只复制固定模板；显式新nonce重现初始积分和历史，不复用上轮计算")

    model = Model()
    model.prepare(mode=1)
    model.run()
    result = model.result
    assert result["state"] == COMPLETE and result["count"] == result["stage_calls"] == result["accepted"] == 80, result
    assert result["sinks"] == result["enables"] == 0
    assert [row[0] for row in result["rows"]] == [80] * 5 + [0] * 4
    assert model.entries["g431_runtime_timing_runtime_step"] == 0
    assert model.word("model_physical_position_reads") == model.word("model_physical_bus_reads") == 0
    assert result["work"] == 620 and result["over_stage"] == result["over_consumer"] == 0
    passed("stage-only 80次真实HCC且使用不等三相duty；不执行Runtime或假sink")

    model = Model()
    model.prepare()
    model.run()
    result = model.result
    assert result["state"] == COMPLETE and result["count"] == result["sinks"] == 80, result
    assert result["stage_calls"] == result["accepted"] == result["enables"] == 0
    assert result["speeds"] == result["slow"] == 8 and result["observations"] == 4
    assert result["fast"] == 72 and 0 < result["limited"] < 80 and result["faults"] == 0
    assert [row[0] for row in result["rows"]] == [80] * 5 + [8, 4, 72, 8]
    assert model.entries["g431_runtime_timing_runtime_step"] == model.entries["current_controller_update"] == 80
    assert model.word("model_physical_position_reads") == model.word("model_physical_bus_reads") == 4
    assert result["over_stage"] == result["over_consumer"] == 80 and result["work"] == 5020
    assert result["rows"][1][3] == result["rows"][0][3] + result["rows"][3][3]
    assert result["rows"][2][3] >= result["rows"][1][3] + result["rows"][4][3]
    passed("compute-only每拍唯一Runtime/电流控制，跨1700和半周仍测完整调用，80/8/4/72/8覆盖")

    terminal = model.snapshot
    model.run(3)
    model.call("model_cancel")
    model.call("model_prepare")
    assert model.snapshot == terminal
    passed("终态冻结及同一完整request身份不重放，DWT自然回绕支持")

    model = Model()
    model.prepare()
    model.run(79)
    model.call("model_begin_work")
    assert model.word("g_target_runtime_timing_active") == 1 and model.result["state"] == READY
    model.call("model_finish_irq")
    assert model.result["state"] == COMPLETE and model.result["count"] == 80
    passed("第80拍计算返回不能提前COMPLETE，必须配对IRQ末端才提交")

    model = Model()
    model.prepare()
    for _ in range(3):
        model.call("model_regular_irq")
    model.run()
    assert model.result["count"] == 80 and model.entries["g431_runtime_timing_runtime_step"] == 80
    passed("regular-only IRQ不冒充采样，不填充80点窗口")

    model = Model()
    model.prepare()
    model.call("model_without_irq")
    model.assert_failed(CLOCK)
    model = Model()
    model.prepare()
    model.call("model_begin_work")
    model.call("model_begin_work")
    model.assert_failed(CLOCK)
    passed("缺失IRQ入口或上一IRQ出口未配对均失败，不补造尾部")

    for request in [0, 1, 2, 4, 7]:
        model = Model()
        model.write("model_request", request)
        model.call("model_prepare")
        model.assert_failed(NOT_READY)
    model = Model()
    model.write("model_permitted", 0)
    model.call("model_prepare")
    model.assert_failed(NOT_READY)
    passed("零nonce/非法mode/无许可均拒绝，未执行任何stage")

    for reason, function in [(ABORTED, "model_cancel"), (BUSY, "model_busy")]:
        model = Model()
        model.prepare()
        model.run(3)
        before = model.snapshot
        model.write("model_request", 10)
        model.call("model_prepare_only")
        assert model.snapshot == before
        model.call(function)
        model.assert_failed(reason, 3)
    passed("Stop及活动时新请求取消且不自动重启，保留已完成样本")

    model = Model()
    model.prepare()
    model.run(3)
    model.write("model_permitted", 0)
    model.run(1)
    model.assert_failed(NOT_READY, 3)
    model = Model()
    model.prepare()
    model.call("model_begin_work")
    model.write("model_permitted", 0)
    model.call("model_finish_irq")
    model.assert_failed(NOT_READY)
    assert model.result["irq"] > model.result["anchor"] > model.result["work"] > 0
    assert all(row == (0, 0, 0, 0) for row in model.result["rows"])
    passed("工作前或IRQ尾部许可撤销都不能发布成功点")

    model = Model()
    model.prepare(mode=1)
    model.write("model_stage_ok", 0)
    model.run(1)
    model.assert_failed(STAGE)
    result = model.result
    assert result["stage_calls"] == 1 and result["accepted"] == 0
    assert result["work"] == 620 and result["irq"] > result["anchor"] > result["work"]
    assert all(row == (0, 0, 0, 0) for row in result["rows"])
    passed("HCC拒绝保留失败拍调用与IRQ原始耗时，不能算入成功统计")

    model = Model()
    model.prepare(mode=1)
    model.write("model_stage_ok", 0)
    model.call("model_begin_work")
    model.write("model_permitted", 0)
    model.call("model_finish_irq")
    model.assert_failed(STAGE)
    model = Model()
    model.prepare(mode=1)
    model.run(3)
    model.write("model_stamp_fail_early", 2)
    model.run(1)
    model.assert_failed(CLOCK, 3)
    assert model.result["timer"] == model.result["cr1"] == model.result["bdtr"] == 0
    assert model.result["entry"] == 4100
    passed("首个内部故障不被尾部撤许可覆盖；未写输出的stamp失败不沿用旧拍证据")

    for read in [1, 2]:
        for setting, reason in [("model_stamp_fail_read", CLOCK), ("model_moe_read", POWER)]:
            model = Model()
            model.prepare(mode=1)
            model.write(setting, read)
            model.run(1)
            model.assert_failed(reason)
    model = Model()
    model.prepare()
    model.write("model_bad_anchor_read", 2)
    model.run(1)
    model.assert_failed(CLOCK)
    passed("任一stamp读取失败/MOE打开/锚点被换均失败且真实enable从不执行")

    model = Model()
    model.prepare()
    model.write("model_compute_cost", 8200)
    model.run(1)
    model.assert_failed(IRQ_BUDGET)
    assert model.result["irq"] >= 8500 and model.result["work"] == 8220
    passed("IRQ达到下一50us采样即冻结并留时长；不放宽原stage/半周保护")

    for setting in ["model_position_available", "model_bus_available", "model_observation_valid"]:
        model = Model()
        model.prepare()
        model.write(setting, 0)
        model.run(20)
        model.assert_failed(NOT_READY, 19)
    passed("真实position/bus getter在刷新拍确实执行，反馈失效不由合成输入遮盖")

    model = Model()
    model.prepare()
    model.write("model_time_step", 2)
    model.run(2)
    model.assert_failed(CONTROL, 1)
    assert model.result["faults"] != 0
    passed("正式Runtime仍拒绝采样跳拍，未绕过连续性及故障路径")

    model = Model()
    model.prepare(mode=1)
    model.write("model_force_enable", 1)
    model.run(1)
    model.assert_failed(POWER)
    assert model.result["enables"] == 1
    passed("任何诊断enable尝试锁存POWER并永远拒绝真实使能")

    for component in range(3):
        for bits in [0x7FC00000, 0x7F800000, 0xFF800000, 0x3D4CCCCD, 0x3F733334]:
            model = Model()
            model.prepare()
            model.write("model_sink_override", 1)
            model.write("model_sink_component", component)
            model.write("model_sink_bits", bits)
            model.run(1)
            model.assert_failed(CONTROL)
            assert model.result["stage_calls"] == model.result["accepted"] == model.result["sinks"] == 0
    passed("sink逐相拒绝NaN/正负Inf及10%..90%包络外输出，从不伪造HCC接受")

    model = Model()
    model.prepare(mode=1)
    model.write("model_stamp_extra_cost", 500)
    model.run(1)
    model.call("model_cancel")
    assert model.result["work"] == 620
    passed("stamp/许可在直接调用区间外，其额外成本不会混入row0")

    print(f"PASS: {len(CASES)} 组 runtime timing ARM 模型；RAM={ram_bytes} bytes；合成cycle不代表实机性能")
    for name in CASES:
        print("  " + name)
