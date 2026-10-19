"""执行真实 ARM Runtime 与独立合成 PMSM，不连接设备。

理想固定 VBUS、连续平均三相电压和合成 PI 参数只构成测试 fixture；不模拟真实母线
能量回灌、死区、采样噪声或执行周期。模型正反跟踪不表示实物可直接反转或已完成标定。
"""
from pathlib import Path
import argparse
import csv
import json
import math
import os
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_CPU_ARM_CORTEX_M4

ARGS = argparse.ArgumentParser(description=__doc__)
ARGS.add_argument("--trace-output", type=Path, help="可选完整 ZOH 对象轨迹 CSV；同名 .metadata.json 写合成配置")
OPTIONS = ARGS.parse_args()
TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parents[4]
COMPILER = Path(os.environ.get("KEIL_ARMCC_BIN", r"D:\keil5\ARM\ARMCC\bin"))
DT = 1 / 20000
IDLE, RUN, FAULT = 1, 2, 3
OFF, QUIET, STAGED, RUNNING = range(4)
ACCEPTED, NOT_READY, BAD_TARGET, WRONG_STATE = 1, 2, 3, 4
FEEDBACK, CONTROL, ACTUATION, POWER, OVERCURRENT = (1 << n for n in range(8, 13))
PLANT = {"resistance_ohm": 2.0, "inductance_d_h": 0.002,
         "inductance_q_h": 0.002, "flux_linkage_wb": 0.01,
         "inertia_kg_m2": 0.0003, "viscous_friction_nm_s": 0.0001, "pole_pairs": 7}
CONTROL_CONFIG = {"fast_tick_hz": 20000, "position_observation_hz": 1000,
                  "velocity_filter_time_s": 0.001, "speed_divider": 20,
                  "current_kp_v_per_a": 2.0, "current_ki_v_per_a_s": 1000.0,
                  "current_tracking_gain_per_s": 200.0, "current_integral_limit_v": 4.0,
                  "current_reference_limit_a": 0.6, "voltage_ratio": 0.45,
                  "speed_kp_a_per_rpm": 0.006, "speed_ki_a_per_rpm_s": 0.04,
                  "speed_tracking_gain_per_s": 20.0, "max_iq_a": 0.4,
                  "speed_integral_limit_a": 0.4, "max_target_rpm": 200.0,
                  "phase_current_trip_a": 1.2, "actuation_prediction_time_s": DT,
                  "nominal_offset_b_a": 0.02, "nominal_offset_c_a": -0.03,
                  "signed_gain_b": 1.25, "signed_gain_c": -0.8}
CASES = []


def checked(command, directory):
    result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "warning" not in output.lower(), output


class Pmsm:
    """双精度 RK4 对象：三相 ZOH 电压，与控制算法无调用关系。"""
    def __init__(self):
        self.x = [0.0, 0.0, 0.0, 0.0]  # Id、Iq、机械角速度、未取模机械圈数。
        self.load = 0.0

    def phase_currents(self):
        direct, quadrature, _, turns = self.x
        angle = 2 * math.pi * PLANT["pole_pairs"] * turns
        alpha = direct * math.cos(angle) - quadrature * math.sin(angle)
        beta = direct * math.sin(angle) + quadrature * math.cos(angle)
        return -alpha / 2 + math.sqrt(3) / 2 * beta, -alpha / 2 - math.sqrt(3) / 2 * beta

    def integrate(self, va, vb):
        p = PLANT

        def derivative(x):
            direct, quadrature, omega, turns = x
            angle = 2 * math.pi * p["pole_pairs"] * turns
            vd = va * math.cos(angle) + vb * math.sin(angle)
            vq = -va * math.sin(angle) + vb * math.cos(angle)
            omega_e = p["pole_pairs"] * omega
            torque = 1.5 * p["pole_pairs"] * (p["flux_linkage_wb"] * quadrature
                      + (p["inductance_d_h"] - p["inductance_q_h"]) * direct * quadrature)
            return [(vd - p["resistance_ohm"] * direct
                     + omega_e * p["inductance_q_h"] * quadrature) / p["inductance_d_h"],
                    (vq - p["resistance_ohm"] * quadrature - omega_e
                     * (p["inductance_d_h"] * direct + p["flux_linkage_wb"])) / p["inductance_q_h"],
                    (torque - self.load - p["viscous_friction_nm_s"] * omega) / p["inertia_kg_m2"],
                    omega / (2 * math.pi)]

        x = self.x
        k1 = derivative(x)
        k2 = derivative([v + DT / 2 * dv for v, dv in zip(x, k1)])
        k3 = derivative([v + DT / 2 * dv for v, dv in zip(x, k2)])
        k4 = derivative([v + DT * dv for v, dv in zip(x, k3)])
        self.x = [v + DT / 6 * (a + 2 * b + 2 * c + d)
                  for v, a, b, c, d in zip(x, k1, k2, k3, k4)]
        assert all(math.isfinite(v) for v in self.x)


with tempfile.TemporaryDirectory(prefix="g431-foc-runtime-") as temporary_name:
    temporary = Path(temporary_name)
    scatter = temporary / "model.sct"
    scatter.write_text("LR_IROM1 0x08000000 0x40000 {\n ER_IROM1 0x08000000 0x40000 { .ANY (+RO) }\n"
                       " RW_IRAM1 0x20000000 0x10000 { .ANY (+RW +ZI) }\n}\n", encoding="utf-8")
    includes = [ROOT / "firmware/app/motor", ROOT / "control", ROOT / "firmware/platform_contract"]
    compile_command = [str(COMPILER / "armcc.exe"), "--cpu=Cortex-M4.fp", "--c99", "-O3",
                       "--split_sections", "-g"]
    for include in includes:
        compile_command += ["-I", str(include)]
    sources = [TESTS / "foc_runtime_model.c", ROOT / "firmware/app/motor/control_runtime.c",
               ROOT / "firmware/app/motor/motor.c"]
    sources += [ROOT / "control" / name for name in ["position_estimator.c", "current_measurement.c",
                "electrical_angle.c", "current_controller.c", "speed_controller.c", "foc_math.c"]]
    objects = []
    for source in sources:
        obj = temporary / (source.stem + ".o")
        checked(compile_command + ["-c", str(source), "-o", str(obj)], temporary)
        objects.append(str(obj))
    executable = temporary / "model.axf"
    checked([str(COMPILER / "armlink.exe"), "--cpu=Cortex-M4.fp", "--scatter", str(scatter),
             "--entry=model_init", "--keep=foc_runtime_model.o(*)", "-o", str(executable), *objects], temporary)
    with executable.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [(p["p_vaddr"], p.data()) for p in elf.iter_segments() if p["p_type"] == "PT_LOAD"]
        initializers = [(s["sh_addr"], s.data()) for s in elf.iter_sections()
                        if (s["sh_flags"] & 2) and s["sh_type"] == "SHT_PROGBITS"]
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}

    class Model:
        def __init__(self, settings=None, valid_init=True):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for address, size in [(0x08000000, 0x40000), (0x20000000, 0x10000), (0xE000E000, 0x1000)]:
                self.cpu.mem_map(address, size)
            for address, data in segments + initializers:
                self.cpu.mem_write(address, data)
            self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0x00F00000))
            for name, value in (settings or {}).items():
                self.write(name, value)
            self.call("model_init")
            assert bool(self.word("model_init_ok")) == valid_init
            self.trace = []

        def write(self, name, value):
            fmt = "f" if isinstance(value, float) else "i" if value < 0 else "I"
            self.cpu.mem_write(symbols[name], struct.pack("<" + fmt, value))

        def word(self, name):
            return struct.unpack("<I", self.cpu.mem_read(symbols[name], 4))[0]

        def time(self):
            return struct.unpack("<Q", self.cpu.mem_read(symbols["model_time"], 8))[0]

        def call(self, name):
            self.cpu.reg_write(UC_ARM_REG_SP, 0x2000FFF0)
            self.cpu.reg_write(UC_ARM_REG_LR, 0x0803F001)
            self.cpu.emu_start(symbols[name] | 1, 0x0803F000, count=100000)
            assert self.cpu.reg_read(UC_ARM_REG_PC) == 0x0803F000, "unbounded: " + name

        def diag(self):
            return struct.unpack("<13I", self.cpu.mem_read(symbols["model_diag"], 52))

        def values(self):
            return struct.unpack("<16f", self.cpu.mem_read(symbols["model_values"], 64))

        def duty(self):
            return struct.unpack("<3f", self.cpu.mem_read(symbols["model_active_duty"], 12))

        def command(self, kind, target=0.0):
            self.write("model_command_type", kind)
            self.write("model_command_target", float(target))
            self.write("model_has_command", 1)

        def step(self, count=1, plant=None, trace=False):
            for _ in range(count):
                if plant:
                    b, c = plant.phase_currents()
                    self.write("model_current_b", b / 1.25 + 0.02)
                    self.write("model_current_c", c / -0.8 - 0.03)
                    direction = struct.unpack("<i", self.cpu.mem_read(symbols["model_direction"], 4))[0]
                    self.write("model_position_turns", (direction * plant.x[3]) % 1)
                self.call("model_step")
                assert self.word("model_enable_boundary_errors") == 0
                assert self.word("model_enable_non_neutral_errors") == 0
                if plant:
                    duty = self.duty()
                    if self.word("model_power_enabled"):
                        average = sum(duty) / 3
                        phase = [(v - average) * 12.0 for v in duty]
                        va = (2 * phase[0] - phase[1] - phase[2]) / 3
                        vb = (phase[1] - phase[2]) / math.sqrt(3)
                    else:
                        va, vb = 0.0, 0.0
                    diag, values = self.diag(), self.values()
                    if trace:
                        self.trace.append([self.time() * DT, va, vb, *plant.x, plant.load,
                                           diag[0], values[0], diag[3], values[1], values[2], values[3],
                                           values[4], *duty, self.word("model_power_enabled"), 0.0])
                    plant.integrate(va, vb)

        def ready(self):
            self.step(64)
            assert self.diag()[4] == 3 and self.diag()[9] == 16, self.diag()
            assert all(math.isclose(v, e, abs_tol=1e-6) for v, e in zip(self.values()[8:10], (0.02, -0.03)))

        def start(self, target=90.0, plant=None, trace=False):
            old_stages, old_enables = self.word("model_stage_count"), self.word("model_enable_count")
            self.command(0, target)
            self.step(plant=plant, trace=trace)
            assert self.diag()[:4] == (IDLE, 0, ACCEPTED, QUIET), self.diag()
            assert self.word("model_power_enabled") == 0
            assert self.word("model_stage_count") == old_stages
            self.step(plant=plant, trace=trace)
            assert self.diag()[3] == STAGED and self.diag()[0] == IDLE
            assert self.word("model_stage_count") == old_stages + 1
            assert self.word("model_enable_count") == old_enables
            self.step(plant=plant, trace=trace)
            assert self.diag()[:4] == (RUN, 0, ACCEPTED, RUNNING), self.diag()
            assert self.word("model_stage_count") == old_stages + 2
            assert self.word("model_enable_count") == old_enables + 1
            assert self.word("model_power_enabled") == 1 and self.duty() == (0.5, 0.5, 0.5)
            assert self.diag()[6:8] == (0, 0), "enable tick must not also run PI"

        def off(self, fault=None):
            diag, values = self.diag(), self.values()
            assert diag[3] == OFF and self.word("model_power_enabled") == 0, diag
            assert self.word("model_pending_valid") == 0 and self.duty() == (0.5, 0.5, 0.5)
            assert values[0] == 0 and values[4:8] == (0.0, 0.0, 0.0, 0.0), values
            if fault:
                assert diag[0] == FAULT and diag[1] & fault, diag
            else:
                assert diag[0:2] == (IDLE, 0), diag

    for flag in ["model_enabled", "model_current_confirmed", "model_electrical_confirmed", "model_timing_confirmed"]:
        model = Model({flag: 0})
        model.ready()
        model.command(0, 90.0)
        model.step(5)
        assert model.diag()[0:4] == (IDLE, 0, NOT_READY, OFF), (flag, model.diag())
        assert model.word("model_stage_count") == model.word("model_enable_count") == 0
        assert model.diag()[11] == 0
    for settings in [{}, {"model_zero_permitted": 0}]:
        model = Model(settings)
        model.command(0, 90.0)
        model.step(5)
        assert model.diag()[0:4] == (IDLE, 0, NOT_READY, OFF)
        assert model.word("model_stage_count") == model.word("model_enable_count") == 0
    CASES.append("任一控制/电流/电角度/时序许可缺失均不提交duty或使能")

    for settings in [{"model_speed_divider": 0}, {"model_voltage_ratio": 0.5},
                     {"model_prediction_s": 0.0002}, {"model_facts_ok": 0}]:
        Model(settings, valid_init=False)
    model = Model({"model_enabled": 0, "model_voltage_ratio": float("nan"), "model_speed_divider": 0})
    model.ready()
    CASES.append("非法装配包络拒绝初始化，未启用控制时不消费无关控制配置")

    model = Model()
    model.ready()
    model.write("model_current_b", 0.02 + 0.1 / 1.25)
    model.write("model_current_c", -0.03 - 0.2 / -0.8)
    model.step()
    assert model.diag()[5] == 1 and model.diag()[11] == 1
    assert math.isclose(model.values()[2], 0.1, abs_tol=1e-6)
    assert math.isclose(model.values()[3], 0.3 / math.sqrt(3), abs_tol=1e-6)
    assert model.word("model_stage_count") == model.word("model_enable_count") == 0
    CASES.append("真实采样路径扣冻结零偏并施加BC非单位有符号增益，重构A和同步Park反馈且不自动使能")

    model = Model()
    model.ready()
    model.start()
    model.step()
    assert model.diag()[6:8] == (1, 1)
    first_speed_integral = model.values()[7]
    stages = model.word("model_stage_count")
    model.step(19)
    assert model.values()[7] == first_speed_integral
    assert model.word("model_stage_count") == stages + 19
    model.step()
    assert model.values()[7] != first_speed_integral
    CASES.append("Start三拍跨epoch中性提交后才RUN，电流逐拍计算而速度按整数20分频保持")

    model = Model()
    model.ready()
    model.start()
    model.step(40)
    assert model.values()[6] != 0.0
    model.command(1, 50.0)
    model.step()
    assert model.values()[0] == 50.0 and model.diag()[0] == RUN
    for target in [float("nan"), float("inf"), 201.0]:
        model.command(1, target)
        model.step()
        assert model.diag()[0:3] == (RUN, 0, BAD_TARGET) and model.values()[0] == 50.0
    model.command(2)
    model.step()
    model.off()
    frozen = model.values()[8:10]
    model.step(30)
    assert model.values()[8:10] == frozen and model.diag()[4] == 3
    model.start(-60.0)
    assert model.values()[5:8] == (0.0, 0.0, 0.0)
    CASES.append("更新完整目标且非法目标保留旧RUN，Stop入口旧功率事实不误报，重启不复用旧积分与duty")

    for stage in [QUIET, STAGED]:
        model = Model()
        model.ready()
        model.command(0, 90.0)
        model.step(1 if stage == QUIET else 2)
        model.command(2)
        model.step()
        model.off()
        before = model.word("model_stage_count"), model.word("model_enable_count")
        model.step(30)
        assert (model.word("model_stage_count"), model.word("model_enable_count")) == before
    CASES.append("Stop可取消两个待启动阶段，旧epoch不延后使能")

    for failure in ["stage_start", "enable", "stage_run", "trip", "current", "nan", "position",
                    "position_stale", "bus", "bus_stale", "bus_envelope", "timing", "power", "overcurrent", "numeric"]:
        model = Model()
        model.ready()
        expected = ACTUATION
        if failure == "stage_start":
            model.write("model_stage_ok", 0)
            model.command(0, 90.0)
            model.step(2)
        elif failure == "enable":
            model.write("model_enable_ok", 0)
            model.command(0, 90.0)
            model.step(3)
        else:
            model.start()
            model.step(5)
            steps = 1
            if failure == "stage_run":
                model.write("model_stage_ok", 0)
            elif failure == "trip":
                model.write("model_fault_evidence", 8)
                expected = 8
            elif failure == "current":
                model.write("model_current_valid", 0)
                expected = FEEDBACK
            elif failure == "nan":
                model.write("model_current_b", float("nan"))
                expected = FEEDBACK
            elif failure == "position":
                model.write("model_position_valid", 0)
                expected, steps = FEEDBACK, 20
            elif failure == "position_stale":
                model.write("model_position_available", 0)
                expected, steps = FEEDBACK, 102
            elif failure == "bus":
                model.write("model_bus_valid", 0)
                expected, steps = FEEDBACK, 20
            elif failure == "bus_stale":
                model.write("model_bus_available", 0)
                expected, steps = FEEDBACK, 102
            elif failure == "bus_envelope":
                model.write("model_bus_voltage", 17.0)
                expected, steps = FEEDBACK, 20
            elif failure == "timing":
                model.write("model_tick_step", 2)
                expected = FEEDBACK
            elif failure == "power":
                model.write("model_power_enabled", 0)
                expected = POWER
            elif failure == "overcurrent":
                model.write("model_current_b", 2.0)
                expected = OVERCURRENT
            else:
                model.call("model_poison_integral")
                expected = CONTROL
            model.step(steps)
        model.off(expected)
        before = model.word("model_stage_count"), model.word("model_enable_count")
        model.write("model_tick_step", 1)
        model.step(30)
        assert (model.word("model_stage_count"), model.word("model_enable_count")) == before, failure
    CASES.append("启动或运行的actuation/反馈/时序/功率/过流/坏计算均锁故障关断清积分，不自动重启")

    # 实际duty经过下一拍边界，再作为独立对象的ZOH电压；不用控制器输出伪造电流反馈。
    traces = []
    for direction in [1, -1]:
        model = Model({"model_direction": direction})
        model.ready()
        plant = Pmsm()
        model.start(90.0, plant, trace=True)
        for target, ticks, load in [(90.0, 9000, 0.0), (40.0, 7000, 0.001), (-70.0, 11000, -0.001)]:
            if target != model.values()[0]:
                model.command(1, target)
            plant.load = load
            model.step(ticks, plant, trace=True)
            rpm = plant.x[2] * 60 / (2 * math.pi)
            assert model.diag()[0:2] == (RUN, 0), (direction, target, model.diag())
            assert abs(rpm - target) < 3.0, (direction, target, rpm)
            assert abs(model.values()[1] - target) < 3.0
            assert abs(plant.x[0]) < 0.03 and abs(plant.x[1]) <= 0.45, plant.x
            measurement_angle, actuation_angle = model.values()[10:12]
            actual_advance = (actuation_angle - measurement_angle + 0.5) % 1 - 0.5
            expected_advance = 7 * model.values()[1] / 60 * DT
            assert math.isclose(actual_advance, expected_advance, abs_tol=2e-6)
        assert all(abs(row[3]) < 0.12 and abs(row[4]) < 0.6 for row in model.trace)
        assert all(abs(row[14]) <= 0.400001 and math.hypot(row[1], row[2]) <= 12 * 0.450001
                   for row in model.trace), "synthetic current/voltage envelope exceeded"
        model.command(2)
        model.step(plant=plant, trace=True)
        model.off()
        model.step(300, plant, trace=True)
        assert model.diag()[0:2] == (IDLE, 0)
        assert model.values()[8:10] == (struct.unpack("<f", struct.pack("<f", 0.02))[0],
                                        struct.unpack("<f", struct.pack("<f", -0.03))[0])
        traces.append(model.trace)
    CASES.append("真实ARM控制链驱动独立PMSM合成对象，非单位有符号电流修正及双编码器方向下正反变目标跟踪")

    if OPTIONS.trace_output:
        output = OPTIONS.trace_output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        columns = ["time_s", "applied_voltage_alpha_v", "applied_voltage_beta_v", "true_id_a", "true_iq_a",
                   "true_omega_rad_s", "true_mechanical_turns", "load_torque_nm", "state", "target_velocity_rpm",
                   "phase", "estimated_velocity_rpm", "measured_id_a", "measured_iq_a", "iq_reference_a",
                   "active_duty_a", "active_duty_b", "active_duty_c", "power_enabled", "id_reference_a"]
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(traces[0])
        metadata = {"schema_version": 1, "sample_interval_s": DT, "plant": PLANT,
                    "control_config": CONTROL_CONFIG, "encoder_direction": 1,
                    "row_semantics": "积分前真实状态；本行alpha/beta为随后[t,t+dt)的ZOH实际施加电压",
                    "power_off_model": "理想alpha=beta=0，继续相同PMSM方程；不模拟硬件开路、二极管或死区",
                    "integration": "double precision fixed-step RK4", "rows": len(traces[0]),
                    "fixture_boundaries": ["fixed ideal 12 V DC bus", "PI gains are synthetic fixture parameters",
                        "no bus-energy regeneration dynamics", "no dead time or sampling noise",
                        "no instruction-cycle or ISR execution-time model",
                        "successful modeled reversal does not authorize physical motor reversal"],
                    "scope": "合成对象和数值集成验证，不是实物参数、功率或闭环验收"}
        metadata_path = output.with_suffix(".metadata.json")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print("TRACE:", output)
        print("METADATA:", metadata_path)

print(f"PASS: {len(CASES)} direct FOC Runtime C / synthetic PMSM groups")
for case in CASES:
    print("  " + case)
