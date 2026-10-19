"""后台计算计时工具的纯主机替身测试，不运行外部进程或访问设备。"""

from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import math
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import compute_probe as tool


def fixture_layout():
    layout = {key: 0 for key in tool.layout_spec()}
    for key, address, size in [("request", 0x20000000, 4), ("sequence", 0x20000004, 4),
                               ("guard", 0x20000008, 4), ("publication_sequence", 0x2000000C, 4),
                               ("result", 0x20001000, 8192), ("publication", 0x20000300, 64)]:
        layout[key], layout[key + "_size"] = address, size
    layout["bdtr"], layout["case_count"] = tool.TIM1_BASE + 68, tool.CASE_COUNT
    for prefix, fields in [("result", tool.RESULT_FIELDS), ("publication", tool.PUBLICATION_FIELDS)]:
        offset = 0
        for field, fmt in fields.items():
            size = 1 if prefix + "." + field in tool.ENUM_FIELDS else struct.calcsize("<" + fmt)
            layout[prefix + "." + field + ".offset"] = offset
            layout[prefix + "." + field + ".size"] = size
            offset += size
    layout.update({"enum." + name: value for name, value in tool.EXPECTED_ENUMS.items()})
    tool.validate_layout(layout)
    return layout


def encode(prefix, fields, values, layout):
    payload = bytearray(layout[prefix + "_size"])
    for field, fmt in fields.items():
        if prefix + "." + field in tool.ENUM_FIELDS:
            fmt = {1: "B", 2: "H", 4: "I"}[layout[prefix + "." + field + ".size"]]
        struct.pack_into("<" + fmt, payload, layout[prefix + "." + field + ".offset"], values.get(field, 0))
    return bytes(payload)


def probe_fixture(sequence):
    result = {"request_sequence": sequence, "state": 2, "failure": 0, "clock_hz": tool.CLOCK_HZ,
              "max_elapsed_cycles": tool.MAX_ELAPSED, "completed_steps": 96, "next_case": 0}
    for index in range(6):
        values = {"count": 16, "min_cycles": 100, "max_cycles": 200, "sum_cycles": 2400,
                  "last_output_valid": int(index < 4), "last_voltage_limited": int(index == 3),
                  "last_speed_limited": int(index == 3)}
        values.update({f"last_duty[{phase}]": 0.5 for phase in range(3)})
        values.update({f"last_phasor[{component}]": 0.0 if index == 4 else float(component % 2) for component in range(4)})
        for segment in range(6):
            executed = segment in ([0, 1] if index == 4 else [0, 1, 2, 4, 5] + ([3] if index == 2 else []))
            timing = {"count": 16, "min_cycles": 100, "max_cycles": 200, "sum_cycles": 2400,
                      "clean_count": 16, "clean_min_cycles": 100, "clean_max_cycles": 200,
                      "clean_sum_cycles": 2400, "irq_entries": 0}
            values.update({f"segments[{segment}].{field}": value if executed else 0 for field, value in timing.items()})
        result.update({f"cases[{index}].{field}": value for field, value in values.items()})
    return result


def complete(sequence):
    result = {"schema_version": 1, "request_sequence": sequence, "state": 2, "failure": 0,
              "completed_steps": 192, "sweep_count": 257, **{field: 0.0 for field in tool.ERROR_FIELDS}}
    for backend in ["software", "cordic"]:
        result.update({backend + "." + field: value for field, value in probe_fixture(sequence).items()})
    return result


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, delay):
        self.value += delay


class FakeBackend:
    def __init__(self, prepared):
        self.prepared, self.layout = prepared, prepared.layout
        self.request, self.old_sequence = 0, 0
        self.write_attempted = False
        self.reads, self.writes = [], []
        self.mutate = lambda tag, raw: None
        self.result_mutate = lambda tag, result: None
        self.active_forever, self.old_forever, self.write_error = False, False, None

    def read(self, blocks, tag, timeout_s=20.0):
        self.reads.append(tag)
        assert timeout_s > 0.0
        if tag == "flash-identity":
            flash = bytearray(b"\xff" * tool.FLASH_SIZE)
            for address, value in self.prepared.image.items():
                flash[address - tool.FLASH_START] = value
            raw = {"flash": bytes(flash)}
        elif tag.startswith(("pre-result", "result-poll", "result-confirm")):
            result = {"schema_version": 1, "request_sequence": self.old_sequence, "state": 0}
            if not tag.startswith("pre-result") and not self.old_forever:
                result = complete(self.request)
                if self.active_forever:
                    result.update(state=1, completed_steps=0)
            self.result_mutate(tag, result)
            raw = {"seq_before": struct.pack("<I", 2), "seq_after": struct.pack("<I", 2),
                   "result": encode("result", tool.RESULT_FIELDS, result, self.layout)}
        else:
            base = tag.split("-coherent-")[0]
            publication = {"snapshot.control_time_ticks": {"pre-health-1": 100, "pre-health-2": 200,
                                                           "post-health-1": 300, "post-health-2": 400}[base],
                           "snapshot.control_time_ticks_per_second": 20000, "snapshot.state": 1,
                           "snapshot.faults": 0, "snapshot.bus_voltage_v": 2.68,
                           "snapshot.status_valid": 1, "snapshot.control_time_valid": 1,
                           "snapshot.bus_voltage_valid": 1, "has_sample": 1, "compute_probe_permitted": 1}
            raw = {name: struct.pack("<I", 0) for name in
                   ["guard_before", "guard_after", "bdtr_before", "bdtr_after", "cpu_before", "cpu_after"]}
            raw.update(pub_before=struct.pack("<I", 4), pub_after=struct.pack("<I", 4),
                       publication=encode("publication", tool.PUBLICATION_FIELDS, publication, self.layout),
                       request=struct.pack("<I", self.request))
        self.mutate(tag, raw)
        for name, address, size in blocks:
            assert len(raw[name]) == size
            if name in ["result", "publication", "request"]:
                assert address == self.layout[name]
        return raw

    def write_request(self, address, value, timeout_s=2.0):
        assert not self.write_attempted and address == self.layout["request"]
        assert type(value) is int and 0 < value <= 0xFFFFFFFF and 0 < timeout_s <= 2.0
        self.write_attempted = True
        self.writes.append((address, value))
        self.request = value
        if self.write_error:
            raise RuntimeError(self.write_error)


def elf_fixture(layout, payload=b"\x01\x02\x03\x04"):
    """真实最小 ELF32/ARM，覆盖产物绑定而不调用编译器。"""
    strings, symbols = bytearray(b"\0"), bytearray(16)
    for key, name in tool.SYMBOLS.items():
        offset = len(strings)
        strings.extend(name.encode("ascii") + b"\0")
        symbols.extend(struct.pack("<IIIBBH", offset, layout[key], layout[key + "_size"], 0x11, 0, 0xFFF1))
    names = b"\0.strtab\0.symtab\0.shstrtab\0"
    strings_offset = 84 + len(payload)
    symbols_offset = strings_offset + len(strings)
    names_offset = symbols_offset + len(symbols)
    sections_offset = names_offset + len(names)
    header = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + b"\0" * 9,
                         2, 40, 1, 0x08000001, 52, sections_offset, 0x05000400, 52, 32, 1, 40, 4, 3)
    program = struct.pack("<8I", 1, 84, tool.FLASH_START, tool.FLASH_START, len(payload), len(payload), 5, 4)
    sections = b"\0" * 40
    sections += struct.pack("<10I", 1, 3, 0, 0, strings_offset, len(strings), 0, 0, 1, 0)
    sections += struct.pack("<10I", 9, 2, 0, 0, symbols_offset, len(symbols), 1, 1, 4, 16)
    sections += struct.pack("<10I", 17, 3, 0, 0, names_offset, len(names), 0, 0, 1, 0)
    return header + program + payload + strings + symbols + names + sections


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="compute-probe-host-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "evidence"
        self.output.mkdir()
        self.layout = fixture_layout()
        self.prepared = tool.PreparedBuild({tool.FLASH_START + index: value for index, value in enumerate(b"\1\2\3\4")},
                                          self.layout, {"Observe.hex": "a" * 64}, self.output)
        self.backend, self.clock = FakeBackend(self.prepared), Clock()
        self.conditions = tool.Conditions(2.68, True)
        self.process_guard = patch.object(tool.subprocess, "run", side_effect=AssertionError("测试禁止外部进程"))
        self.process_guard.start()

    def tearDown(self):
        self.process_guard.stop()
        self.temporary.cleanup()

    def measure(self):
        return tool.run_measurement(self.backend, self.prepared, self.conditions, self.clock.now, self.clock.sleep)

    def alter_publication(self, raw, field, value):
        data = bytearray(raw["publication"])
        fmt = "B" if "publication." + field in tool.ENUM_FIELDS else tool.PUBLICATION_FIELDS[field]
        struct.pack_into("<" + fmt, data, self.layout["publication." + field + ".offset"], value)
        raw["publication"] = bytes(data)

    def test_complete_exactly_one_write_with_explicit_limits(self):
        report = self.measure()
        self.assertEqual(self.backend.writes, [(self.layout["request"], 1)])
        self.assertEqual(len(report["comparison"]), 6)
        self.assertEqual(report["backends"]["software"][0]["mean_cycles"], 150)
        self.assertEqual(report["comparison"][0]["segments"][0]["clean_mean_speedup"], 1)
        self.assertFalse(report["hardware_qualified"] or report["stage_timing_qualified"] or report["full_isr_wcet"])
        self.assertFalse(report["automatic_retry"])

    def test_conditions_rejected_before_any_device_read(self):
        for conditions in [tool.Conditions(), tool.Conditions(2.68), tool.Conditions(None, True),
                           tool.Conditions(math.nan, True), tool.Conditions(-0.1, True), tool.Conditions(3.61, True)]:
            self.conditions = conditions
            with self.subTest(conditions=conditions), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(self.backend.reads, [])
            self.assertEqual(self.backend.writes, [])

    def test_busy_pending_or_flash_mismatch_causes_zero_write(self):
        self.backend.result_mutate = lambda tag, result: result.update(state=1) if tag == "pre-result" else None
        with self.assertRaisesRegex(RuntimeError, "活动"):
            self.measure()
        self.backend.result_mutate = lambda tag, result: None
        self.backend.request = 7
        with self.assertRaisesRegex(RuntimeError, "未消费"):
            self.measure()
        self.backend.request = 0
        self.backend.mutate = lambda tag, raw: raw.update(flash=b"\0" * tool.FLASH_SIZE) if tag == "flash-identity" else None
        with self.assertRaisesRegex(RuntimeError, "Flash"):
            self.measure()
        self.assertEqual(self.backend.writes, [])

    def test_pre_health_failures_cause_zero_write(self):
        modes = {"guard_after": 1, "bdtr_before": 0x8000, "cpu_after": 1 << 17}
        for name, value in modes.items():
            self.backend = FakeBackend(self.prepared)
            self.backend.mutate = lambda tag, raw, name=name, value=value: raw.update({name: struct.pack("<I", value)}) if tag.startswith("pre-health") else None
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(self.backend.writes, [])
        for field, value in [("snapshot.state", 2), ("snapshot.faults", 1), ("compute_probe_permitted", 0),
                             ("snapshot.control_time_valid", 0), ("has_sample", 0), ("snapshot.bus_voltage_v", 3.1),
                             ("snapshot.bus_voltage_v", math.nan), ("snapshot.control_time_ticks_per_second", 1000),
                             ("snapshot.control_time_ticks", 100)]:
            self.backend = FakeBackend(self.prepared)
            self.backend.mutate = lambda tag, raw, field=field, value=value: self.alter_publication(raw, field, value) if tag.startswith("pre-health") else None
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(self.backend.writes, [])

    def test_active_or_old_result_timeout_never_resends(self):
        for mode in ["active_forever", "old_forever"]:
            self.backend = FakeBackend(self.prepared)
            self.clock = Clock()
            setattr(self.backend, mode, True)
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, "2 秒|次数"):
                self.measure()
            self.assertEqual(len(self.backend.writes), 1)
            self.assertLessEqual(self.clock.value, 2.020001)

    def test_failed_partial_or_foreign_identity_is_rejected(self):
        for values in [{"state": 3, "failure": 2}, {"state": 1, "completed_steps": 1}, {"request_sequence": 99},
                       {"state": 9}, {"failure": 10}]:
            self.backend = FakeBackend(self.prepared)
            self.backend.result_mutate = lambda tag, result, values=values: result.update(values) if tag.startswith("result-poll") else None
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(len(self.backend.writes), 1)

    def test_invalid_statistics_or_path_flags_rejected(self):
        for field, value in [("completed_steps", 191), ("software.next_case", 1), ("software.clock_hz", 1), ("software.max_elapsed_cycles", 1),
                             ("software.cases[0].count", 15), ("software.cases[0].min_cycles", 0), ("software.cases[0].max_cycles", 34001),
                             ("software.cases[0].sum_cycles", 1599), ("software.cases[0].sum_cycles", 1600),
                             ("software.cases[0].last_output_valid", 0), ("software.cases[3].last_voltage_limited", 0),
                             ("software.cases[3].last_speed_limited", 0), ("software.cases[4].last_duty[0]", 0.1),
                             ("software.cases[5].last_duty[0]", math.nan), ("software.cases[0].last_duty[0]", 1.01)]:
            result = complete(1)
            result[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                tool.validate_complete(result)

    def test_second_result_must_remain_frozen(self):
        self.backend.result_mutate = lambda tag, result: result.update({"software.cases[0].sum_cycles": 2401}) if tag == "result-confirm" else None
        with self.assertRaisesRegex(RuntimeError, "第二次"):
            self.measure()
        self.assertEqual(len(self.backend.writes), 1)

    def test_old_schema_rejected_before_request_write(self):
        self.backend.result_mutate = lambda tag, result: result.update(schema_version=0) if tag == "pre-result" else None
        with self.assertRaisesRegex(RuntimeError, "schema"):
            self.measure()
        self.assertEqual(self.backend.writes, [])

    def test_sweep_errors_nested_identity_and_completion_checked(self):
        failures = [("schema_version", 2), ("sweep_count", 256), ("software.completed_steps", 95),
                    ("cordic.request_sequence", 2), ("software.cases[0].last_phasor[0]", math.nan)]
        failures += [(field, value) for field in tool.ERROR_FIELDS for value in [math.nan, math.inf, -0.1, 2.0001e-5]]
        for field, value in failures:
            result = complete(1)
            result[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                tool.validate_complete(result)

    def test_segment_counts_clean_and_irq_corruption_rejected(self):
        failures = [("software.cases[0].segments[1].count", 15),
                    ("software.cases[4].segments[2].count", 16),
                    ("cordic.cases[2].segments[3].count", 0),
                    ("software.cases[0].segments[1].clean_count", 17),
                    ("software.cases[0].segments[1].clean_count", 15),
                    ("software.cases[0].segments[1].irq_entries", 1),
                    ("software.cases[0].segments[1].clean_sum_cycles", 0),
                    ("software.cases[0].segments[1].clean_min_cycles", 90)]
        for field, value in failures:
            result = complete(1)
            result[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                tool.validate_complete(result)
        result = complete(1)
        result["software.cases[0].segments[0].sum_cycles"] = 2401
        result["software.cases[0].segments[0].clean_sum_cycles"] = 2401
        with self.assertRaisesRegex(RuntimeError, "兼容"):
            tool.validate_complete(result)

    def test_mixed_and_fully_preempted_segments_preserve_clean_meaning(self):
        result = complete(1)
        prefix = "software.cases[0].segments[1]."
        result.update({prefix + field: value for field, value in {
            "clean_count": 12, "clean_min_cycles": 100, "clean_max_cycles": 180,
            "clean_sum_cycles": 1680, "irq_entries": 5}.items()})
        backends, comparison = tool.validate_complete(result)
        timing = backends["software"][0]["segments"][1]
        self.assertEqual(timing["clean"]["count"], 12)
        self.assertAlmostEqual(timing["clean"]["mean_us"], 140 / 170)
        self.assertEqual(timing["irq_entries"], 5)
        self.assertTrue(comparison[0]["segments"][1]["clean_mean_speedup"] > 0)
        result.update({prefix + field: value for field, value in {
            "clean_count": 0, "clean_min_cycles": 0, "clean_max_cycles": 0,
            "clean_sum_cycles": 0, "irq_entries": 16}.items()})
        backends, comparison = tool.validate_complete(result)
        self.assertIsNone(backends["software"][0]["segments"][1]["clean"]["mean_us"])
        self.assertIsNone(comparison[0]["segments"][1]["clean_mean_speedup"])

    def test_backend_pair_errors_must_match_declared_bounds(self):
        result = complete(1)
        result["cordic.cases[0].last_duty[0]"] = 0.500001
        with self.assertRaisesRegex(RuntimeError, "配对"):
            tool.validate_complete(result)
        result["max_duty_error"] = 1.1e-6
        tool.validate_complete(result)
        result["cordic.cases[0].last_phasor[0]"] = 3e-5
        with self.assertRaisesRegex(RuntimeError, "配对"):
            tool.validate_complete(result)

    def test_post_health_must_advance_and_preserve_request(self):
        for mode in ["guard", "request", "clock", "permit"]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, raw, mode=mode):
                if not tag.startswith("post-health"):
                    return
                if mode == "guard":
                    raw["guard_after"] = struct.pack("<I", 1)
                elif mode == "request":
                    raw["request"] = struct.pack("<I", 99)
                elif mode == "clock":
                    self.alter_publication(raw, "snapshot.control_time_ticks", 200)
                else:
                    self.alter_publication(raw, "compute_probe_permitted", 0)
            self.backend.mutate = mutate
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(len(self.backend.writes), 1)

    def test_write_ack_failure_is_not_retried_or_cleaned_up(self):
        self.backend.write_error = "应答丢失"
        with self.assertRaisesRegex(RuntimeError, "应答丢失"):
            self.measure()
        self.assertEqual(len(self.backend.writes), 1)
        self.assertNotIn("result-poll", self.backend.reads)

    def test_only_bounded_read_rechecks_for_publication_collision(self):
        for permanent in [False, True]:
            self.backend = FakeBackend(self.prepared)
            self.backend.mutate = lambda tag, raw, permanent=permanent: raw.update(pub_after=struct.pack("<I", 8)) if tag == "pre-health-1" or permanent and tag.startswith("pre-health-1-coherent-") else None
            if permanent:
                with self.assertRaisesRegex(RuntimeError, "八次只读"):
                    self.measure()
                self.assertEqual(self.backend.writes, [])
            else:
                self.measure()
                self.assertEqual(len(self.backend.writes), 1)
            self.assertEqual(sum(tag.startswith("pre-health-1") for tag in self.backend.reads), 8 if permanent else 2)

    def test_short_enums_derived_and_layout_corruption_rejected(self):
        raw = encode("result", tool.RESULT_FIELDS, complete(1), self.layout)
        self.assertEqual(tool.decode(raw, "result", tool.RESULT_FIELDS, self.layout)["state"], 2)
        for key, value in [("request", 0x1FFFFFFC), ("sequence_size", 8), ("result", self.layout["request"]),
                           ("bdtr", 0x20000000), ("case_count", 7), ("result.state.size", 3),
                           ("result.software.cases[0].count.offset", 0), ("result.software.cases[0].count.offset", 8192),
                           ("enum.MOTOR_STATE_IDLE", 2)]:
            broken = dict(self.layout)
            broken[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                tool.validate_layout(broken)

    def test_request_wrap_remains_nonzero_and_new(self):
        self.backend.request = self.backend.old_sequence = 0xFFFFFFFF
        report = self.measure()
        self.assertEqual(report["request_sequence"], 1)

    def create_artifacts(self):
        build = self.root / "build"
        build.mkdir()
        stem = build / "Observe"
        stem.with_suffix(".hex").write_text(":020000040800F2\n:0400000001020304F2\n:00000001FF\n", encoding="ascii")
        stem.with_suffix(".axf").write_bytes(elf_fixture(self.layout))
        mapping = "Image Entry point : 0x08000001\nLoad Region LR_IROM1 (Base: 0x08000000, Size: 0x00000004, Max: 0x20000)\n"
        mapping += "".join(f"{name} 0x{self.layout[key]:08x} Data {self.layout[key + '_size']} fixture.o(.data)\n" for key, name in tool.SYMBOLS.items())
        stem.with_suffix(".map").write_text(mapping, encoding="utf-8")
        return build, stem

    def test_real_elf_hex_map_identity_and_foreign_symbols(self):
        _, stem = self.create_artifacts()
        self.assertEqual(tool.verify_artifacts(stem.with_suffix(".hex"), stem.with_suffix(".axf"), stem.with_suffix(".map"), self.layout), self.prepared.image)
        for mode in ["load", "map", "symbol"]:
            saved = {suffix: stem.with_suffix(suffix).read_bytes() for suffix in [".hex", ".axf", ".map"]}
            if mode == "load":
                stem.with_suffix(".axf").write_bytes(elf_fixture(self.layout, b"\1\2\3\5"))
            elif mode == "map":
                stem.with_suffix(".map").write_text(stem.with_suffix(".map").read_text().replace("Size: 0x00000004", "Size: 0x00000005"))
            else:
                broken = dict(self.layout)
                broken["request"] += 16
                stem.with_suffix(".axf").write_bytes(elf_fixture(broken))
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                tool.verify_artifacts(stem.with_suffix(".hex"), stem.with_suffix(".axf"), stem.with_suffix(".map"), self.layout)
            for suffix, payload in saved.items():
                stem.with_suffix(suffix).write_bytes(payload)

    def test_prepare_freezes_artifacts_and_refuses_overwrite_or_wrong_hash(self):
        build, stem = self.create_artifacts()
        with patch.object(tool, "derive_layout", return_value=self.layout):
            prepared = tool.prepare_build(build, tool.digest(stem.with_suffix(".hex")), self.root / "new")
        self.assertEqual(prepared.image, self.prepared.image)
        self.assertTrue((self.root / "new/prepared.json").is_file())
        with self.assertRaisesRegex(RuntimeError, "覆盖"):
            tool.prepare_build(build, tool.digest(stem.with_suffix(".hex")), self.root / "new")
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            tool.prepare_build(build, "0" * 64, self.root / "wrong")
        self.assertFalse((self.root / "wrong").exists())

    def test_gdb_derives_same_image_without_device_commands(self):
        gdb = self.root / "fake-gdb.exe"
        gdb.write_bytes(b"never executed")
        stdout = "\n".join(f"${index} = {hex(self.layout[key])}" for index, key in enumerate(tool.layout_spec(), 1))
        process = type("Process", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
        with patch.object(tool.subprocess, "run", return_value=process) as run:
            result = tool.derive_layout(self.root / "same.axf", gdb, self.output)
        self.assertEqual(result, self.layout)
        arguments = run.call_args.args[0]
        self.assertIn("-x", arguments)
        commands = (self.output / "layout-gdb.commands").read_text(encoding="utf-8")
        self.assertIn("ObserveComputeComparisonResult", commands)
        self.assertIn('file "' + (self.root / "same.axf").as_posix() + '"', commands)
        self.assertNotIn("target remote", commands)

    def test_cli_default_has_no_backend_and_measure_requires_confirmation(self):
        arguments = ["--build-dir", str(self.root), "--expected-hex-sha256", "a" * 64, "--output", str(self.root / "new")]
        with patch.object(tool, "prepare_build", return_value=self.prepared), patch.object(tool, "CubeBackend") as backend, redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(arguments), 0)
            backend.assert_not_called()
        with patch.object(tool, "prepare_build") as prepare, patch.object(tool, "CubeBackend") as backend:
            with self.assertRaisesRegex(RuntimeError, "确认"):
                tool.main(arguments + ["--measure", "--vbus-dmm-v", "2.68"])
            prepare.assert_not_called()
            backend.assert_not_called()

    def test_reused_transport_only_allows_exact_one_request_write(self):
        cli = self.root / "fake-cli.exe"
        cli.write_bytes(b"never executed")
        backend = tool.CubeBackend(self.prepared, cli)
        process = type("Process", (), {"returncode": 0, "stdout": b"Device ID : 0x468\nCore is running\n", "stderr": b""})()
        for address, value in [(self.layout["request"] + 4, 1), (self.layout["request"], 0),
                               (self.layout["request"], 0x100000000), (self.layout["request"], 1.0)]:
            with self.subTest(address=address, value=value), self.assertRaises(RuntimeError):
                backend.write_request(address, value)
        with patch.object(tool.subprocess, "run", return_value=process) as run:
            backend.write_request(self.layout["request"], 1)
            with self.assertRaises(RuntimeError):
                backend.write_request(self.layout["request"], 2)
            for command in [["-rst"], ["-halt"], ["-run"], ["-w", "bad.hex"], ["-w32", hex(self.layout["request"]), "2"]]:
                with self.subTest(command=command), self.assertRaises(RuntimeError):
                    backend.execute(command, "forbidden", 1.0)
            self.assertEqual(run.call_count, 1)

    def test_shared_fast_transport_reads_full_comparison_without_upload_file_gaps(self):
        cli = self.root / "fake-cli.exe"
        cli.write_bytes(b"never executed")
        backend = tool.CubeBackend(self.prepared, cli)
        address, size = self.layout["result"], 4792
        values = [(index * 31337) & 0xFFFFFFFF for index in range(size // 4)]
        message = f"Device ID : 0x468\nCore is running\nReading 32-bit memory content\nSize : {size} Bytes\nAddress: : 0x{address:08X}\n"
        for index in range(0, len(values), 4):
            message += f"0x{address + index * 4:08X} : " + " ".join(f"{value:08X}" for value in values[index:index + 4]) + "\n"
        message += "Core is running\n"
        process = type("Process", (), {"returncode": 0, "stdout": message.encode(), "stderr": b""})()
        with patch.object(tool.subprocess, "run", return_value=process) as run:
            actual = backend.read([("result", address, size)], "large-comparison")
        expected = struct.pack("<" + "I" * len(values), *values)
        self.assertEqual(actual["result"], expected)
        self.assertEqual((self.output / "001-large-comparison-result.bin").read_bytes(), expected)
        self.assertEqual(run.call_args.args[0][-4:], ["-r32", hex(address), str(size), "-score"])
        self.assertFalse(backend.write_attempted)


if __name__ == "__main__":
    unittest.main()
