"""Observe 电流工具的纯主机替身测试；不启动构建、不运行调试器、不访问串口或 SWD。"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import math
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parents[1] / "tools/current_capture.py"
spec = importlib.util.spec_from_file_location("observe_current_capture_tool", MODULE)
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool
spec.loader.exec_module(tool)


def layout_fixture():
    layout = {key: 0 for key in tool.layout_spec()}
    for key, address, size in [
        ("request", 0x20000000, 4), ("sequence", 0x20000004, 4),
        ("guard", 0x20000008, 4), ("publication_sequence", 0x2000000C, 4),
        ("capture", 0x20000100, 80), ("publication", 0x20000200, 64),
        ("runtime", 0x20000400, 256), ("zero", 0x20000420, 80),
    ]:
        layout[key], layout[key + "_size"] = address, size
    layout["bdtr"] = tool.TIM1_BASE + 68
    for prefix, fields in [("capture", tool.RESULT_FIELDS), ("publication", tool.PUBLICATION_FIELDS), ("zero", tool.ZERO_FIELDS)]:
        offset = 0
        for field, fmt in fields.items():
            size = struct.calcsize("<" + fmt)
            layout[prefix + "." + field + ".offset"] = offset
            layout[prefix + "." + field + ".size"] = size
            offset += size
    for name, value in [("MOTOR_STATE_IDLE", 1), ("CURRENT_MEASUREMENT_READY", 3),
                        ("CURRENT_MEASUREMENT_PHASE_BC", 6), ("OBSERVE_CURRENT_CAPTURE_IDLE", 0),
                        ("OBSERVE_CURRENT_CAPTURE_ACTIVE", 1), ("OBSERVE_CURRENT_CAPTURE_COMPLETE", 2),
                        ("OBSERVE_CURRENT_CAPTURE_FAILED", 3)]:
        layout["enum." + name] = value
    tool.validate_layout(layout)
    return layout


def encode(prefix, fields, values, layout):
    data = bytearray(layout[prefix + "_size"])
    for field, fmt in fields.items():
        struct.pack_into("<" + fmt, data, layout[prefix + "." + field + ".offset"], values.get(field, 0))
    return bytes(data)


def packed_word(value):
    return struct.pack("<I", value)


def complete_capture(sequence=1):
    result = {"request_sequence": sequence, "state": 2, "failure": 0, "sample_count": 2048,
              "first_sample_time": 201, "last_sample_time": 4295,
              "offset_b_a": 0.01, "offset_c_a": -0.002}
    for phase, mean in [("b", 0.1), ("c", -0.002)]:
        result.update({f"phase_{phase}.mean_a": mean, f"phase_{phase}.min_a": mean - 0.01,
                       f"phase_{phase}.max_a": mean + 0.01, f"phase_{phase}.variance_a2": 0.000001})
    return result


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, interval):
        self.value += interval


class FakeBackend:
    def __init__(self, prepared):
        self.prepared = prepared
        self.layout = prepared.layout
        self.request = 0
        self.writes = []
        self.reads = []
        self.write_attempted = False
        self.mutate = lambda tag, blocks: None
        self.capture_mutate = lambda tag, capture: None
        self.active_forever = False
        self.write_error = None

    def read(self, blocks, tag, timeout_s=20.0):
        self.reads.append((tag, timeout_s))
        layout = self.layout
        if tag == "flash-identity":
            flash = bytearray([0xFF] * tool.FLASH_SIZE)
            for address, value in self.prepared.image.items():
                flash[address - tool.FLASH_START] = value
            result = {"flash": bytes(flash)}
        elif tag in ["pre-result", "capture-poll", "capture-confirm"]:
            capture = {field: 0 for field in tool.RESULT_FIELDS}
            if tag != "pre-result":
                capture = complete_capture(self.request)
                if self.active_forever:
                    capture.update(state=1, sample_count=0, first_sample_time=0, last_sample_time=0)
            self.capture_mutate(tag, capture)
            result = {"seq_before": packed_word(4 if tag == "pre-result" else 6),
                      "seq_after": packed_word(4 if tag == "pre-result" else 6),
                      "capture": encode("capture", tool.RESULT_FIELDS, capture, layout)}
        else:
            base_tag = tag.split("-coherent-")[0].split("-catchup-")[0]
            publication = {"snapshot.control_time_ticks": {"pre-health-1": 100, "pre-health-2": 200, "post-health": 5000}[base_tag],
                           "snapshot.control_time_ticks_per_second": 20000,
                           "snapshot.state": 1, "snapshot.faults": 0, "snapshot.bus_voltage_v": 2.68,
                           "snapshot.status_valid": 1, "snapshot.control_time_valid": 1,
                           "snapshot.bus_voltage_valid": 1, "has_sample": 1}
            zero = {"state": 3, "failure": 0, "sample_count": 2048,
                    "offset_a[1]": 0.01, "offset_a[2]": -0.002,
                    "config.calibration_sample_count": 2048, "config.measured_phases": 6}
            zero_raw = encode("zero", tool.ZERO_FIELDS, zero, layout)
            result = {"guard_before": packed_word(0), "guard_after": packed_word(0),
                      "bdtr_before": packed_word(0), "bdtr_after": packed_word(0),
                      "cpu_before": packed_word(0), "cpu_after": packed_word(0),
                      "pub_before": packed_word(8), "pub_after": packed_word(8),
                      "publication": encode("publication", tool.PUBLICATION_FIELDS, publication, layout),
                      "zero_before": zero_raw, "zero_after": zero_raw, "request": packed_word(self.request)}
        self.mutate(tag, result)
        self.assert_addresses(blocks, tag)
        return result

    def assert_addresses(self, blocks, tag):
        expected = {"flash": (tool.FLASH_START, tool.FLASH_SIZE),
                    "seq_before": (self.layout["sequence"], 4), "seq_after": (self.layout["sequence"], 4),
                    "capture": (self.layout["capture"], self.layout["capture_size"])}
        for name, address, size in blocks:
            if name in expected:
                assert (address, size) == expected[name]

    def write_request(self, address, value, timeout_s=2.0):
        assert not self.write_attempted and address == self.layout["request"] and 0 < value <= 0xFFFFFFFF
        self.write_attempted = True
        self.writes.append((address, struct.pack("<I", value), timeout_s))
        self.request = value
        if self.write_error:
            raise RuntimeError(self.write_error)


def elf_fixture(layout, payload=b"\x01\x02\x03\x04"):
    """提供真实最小 ELF32/ARM，验证加载字节和符号绑定，不需要编译器。"""
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
        self.temporary = TemporaryDirectory(prefix="observe-current-host-test-")
        self.root = Path(self.temporary.name)
        self.layout = layout_fixture()
        self.output = self.root / "evidence"
        self.output.mkdir()
        self.prepared = tool.PreparedBuild({tool.FLASH_START + index: value for index, value in enumerate(b"\1\2\3\4")},
                                          self.layout, {"Observe.hex": "a" * 64}, self.output)
        self.backend = FakeBackend(self.prepared)
        self.reference = tool.Reference("B", 0.09, 2.68, True)
        self.clock = Clock()
        self.no_process = patch.object(tool.subprocess, "run", side_effect=AssertionError("替身测试禁止启动进程"))
        self.no_process.start()

    def tearDown(self):
        self.no_process.stop()
        self.temporary.cleanup()

    def acquire(self):
        return tool.run_acquisition(self.backend, self.prepared, self.reference, self.clock.now, self.clock.sleep)

    def alter_field(self, blocks, prefix, field, value, names):
        for name in names:
            raw = bytearray(blocks[name])
            fields = tool.PUBLICATION_FIELDS if prefix == "publication" else tool.ZERO_FIELDS
            struct.pack_into("<" + fields[field], raw, self.layout[prefix + "." + field + ".offset"], value)
            blocks[name] = bytes(raw)

    def test_complete_once_and_raw_corrected_statistics(self):
        report = self.acquire()
        self.assertEqual(len(self.backend.writes), 1)
        address, data, timeout = self.backend.writes[0]
        self.assertEqual((address, data), (self.layout["request"], packed_word(1)))
        self.assertTrue(0.0 < timeout <= 2.0)
        self.assertAlmostEqual(report["phases"]["B"]["raw"]["mean_a"], 0.1, places=6)
        self.assertAlmostEqual(report["phases"]["B"]["corrected"]["mean_a"], 0.09, places=6)
        self.assertAlmostEqual(report["phases"]["C"]["corrected"]["mean_a"], 0.0, places=6)
        self.assertEqual(report["phases"]["B"]["raw"]["variance_a2"], report["phases"]["B"]["corrected"]["variance_a2"])
        self.assertFalse(report["gain_applied"] or report["motor_or_foc_qualified"] or report["automatic_retry"])
        self.assertEqual(report["reference_source"], "dmm")
        self.assertEqual(report["reference_quality"], "dmm-reading")

    def test_reference_default_preserves_existing_positional_arguments(self):
        for reference in [tool.Reference("B", 0.1, 2.68), tool.Reference("C", -0.1, 2.68, True)]:
            self.assertEqual(reference.source, "dmm")
            reference.validate()
        self.assertTrue(tool.Reference("C", -0.1, 2.68, True).logic_only_confirmed)

    def test_supply_display_reference_records_coarse_quality_without_changing_acquisition(self):
        self.reference = tool.Reference("B", 0.05, 2.68, True, source="supply-display")
        report = self.acquire()
        self.assertEqual(report["reference_source"], "supply-display")
        self.assertEqual(report["reference_current_a"], 0.05)
        self.assertEqual(report["reference_quality"], "coarse-comparison")
        self.assertIn("不证明绝对精度", report["reference_limitations"])
        self.assertIn("不是万用表电流测量", report["reference_limitations"])
        self.assertEqual(report["vbus_dmm_v"], 2.68)
        self.assertEqual(report["write_count"], len(self.backend.writes))
        self.assertEqual(len(self.backend.writes), 1)
        self.assertFalse(report["gain_applied"] or report["motor_or_foc_qualified"] or report["automatic_retry"])

    def test_invalid_reference_source_and_supply_current_fail_before_any_device_access(self):
        for source in ["", "supply", "DMM", None]:
            self.reference = tool.Reference("B", 0.05, 2.68, True, source=source)
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, "来源"):
                self.acquire()
        self.reference = tool.Reference("B", 0.25001, 2.68, True, source="supply-display")
        with self.assertRaisesRegex(RuntimeError, "电源实际显示的有符号参考电流"):
            self.acquire()
        self.reference = tool.Reference("B", 0.05, math.nan, True, source="supply-display")
        with self.assertRaisesRegex(RuntimeError, "万用表 VBUS"):
            self.acquire()
        self.assertEqual(self.backend.reads, [])
        self.assertEqual(self.backend.writes, [])

    def test_supply_reference_does_not_bypass_power_preflight(self):
        self.reference = tool.Reference("B", 0.05, 2.68, True, source="supply-display")
        self.backend.mutate = lambda tag, blocks: blocks.update(bdtr_after=packed_word(0x8000)) if tag == "pre-health-1" else None
        with self.assertRaisesRegex(RuntimeError, "MOE"):
            self.acquire()
        self.assertEqual(self.backend.writes, [])

    def test_reference_limits_and_confirmation_fail_before_any_read_or_write(self):
        for reference in [tool.Reference("A", 0.1, 2.68, True), tool.Reference("B", 0.25001, 2.68, True),
                          tool.Reference("B", -0.25001, 2.68, True), tool.Reference("B", math.nan, 2.68, True),
                          tool.Reference("B", 0.1, 3.6001, True), tool.Reference("B", 0.1, -0.001, True),
                          tool.Reference("B", 0.1, math.inf, True), tool.Reference("B", 0.1, 2.68, False)]:
            self.reference = reference
            with self.subTest(reference=reference), self.assertRaises(RuntimeError):
                self.acquire()
            self.assertEqual(self.backend.reads, [])
            self.assertEqual(self.backend.writes, [])
        for current in [-0.25, 0.0, 0.25]:
            tool.Reference("C", current, 3.6).validate()

    def test_live_flash_mismatch_is_read_only(self):
        self.backend.mutate = lambda tag, blocks: blocks.update(flash=b"\0" * tool.FLASH_SIZE) if tag == "flash-identity" else None
        with self.assertRaisesRegex(RuntimeError, "Flash"):
            self.acquire()
        self.assertEqual(self.backend.writes, [])

    def test_busy_or_pending_request_cannot_write(self):
        self.backend.capture_mutate = lambda tag, result: result.update(state=1) if tag == "pre-result" else None
        with self.assertRaisesRegex(RuntimeError, "活动采集"):
            self.acquire()
        self.assertEqual(self.backend.writes, [])
        self.backend.capture_mutate = lambda tag, result: None
        self.backend.request = 7
        with self.assertRaisesRegex(RuntimeError, "未消费"):
            self.acquire()
        self.assertEqual(self.backend.writes, [])

    def test_guard_moe_cpu_or_partial_read_preflight_is_read_only(self):
        for name, value in [("guard_before", 1), ("guard_after", 1), ("bdtr_before", 0x8000),
                            ("bdtr_after", 0x8000), ("cpu_before", 1 << 17), ("cpu_after", 1 << 19),
                            ("zero_before", None), ("publication", None)]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, blocks, name=name, value=value):
                if tag == "pre-health-1":
                    blocks[name] = packed_word(value) if value is not None else blocks[name][:-1]
            self.backend.mutate = mutate
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.acquire()
            self.assertEqual(self.backend.writes, [])

    def test_motor_bus_and_zero_preflight_each_fail_without_writes(self):
        for prefix, field, value in [
            ("publication", "snapshot.state", 2), ("publication", "snapshot.faults", 1),
            ("publication", "snapshot.status_valid", 0), ("publication", "snapshot.control_time_valid", 0),
            ("publication", "snapshot.bus_voltage_valid", 0), ("publication", "has_sample", 0),
            ("publication", "snapshot.bus_voltage_v", 3.7), ("publication", "snapshot.bus_voltage_v", -0.01),
            ("publication", "snapshot.bus_voltage_v", 2.0), ("publication", "snapshot.bus_voltage_v", math.nan),
            ("zero", "state", 2), ("zero", "failure", 1), ("zero", "sample_count", 2047),
            ("zero", "config.calibration_sample_count", 2047), ("zero", "config.measured_phases", 7),
            ("zero", "offset_a[1]", math.inf),
        ]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, blocks, prefix=prefix, field=field, value=value):
                if tag == "pre-health-1":
                    self.alter_field(blocks, prefix, field, value,
                                     ["publication"] if prefix == "publication" else ["zero_before", "zero_after"])
            self.backend.mutate = mutate
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                self.acquire()
            self.assertEqual(self.backend.writes, [])

    def test_preflight_coherence_clock_and_frozen_offset_required(self):
        for mutation in ["odd-publication", "changed-publication", "stopped-time", "zero-torn", "offset-changed"]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, blocks, mode=mutation):
                if tag.startswith("pre-health-1") and mode == "odd-publication":
                    blocks.update(pub_before=packed_word(7), pub_after=packed_word(7))
                if tag.startswith("pre-health-1") and mode == "changed-publication":
                    blocks["pub_after"] = packed_word(10)
                if tag == "pre-health-2" and mode == "stopped-time":
                    self.alter_field(blocks, "publication", "snapshot.control_time_ticks", 100, ["publication"])
                if tag == "pre-health-1" and mode == "zero-torn":
                    self.alter_field(blocks, "zero", "offset_a[1]", 0.02, ["zero_after"])
                if tag == "pre-health-2" and mode == "offset-changed":
                    self.alter_field(blocks, "zero", "offset_a[1]", 0.02, ["zero_before", "zero_after"])
            self.backend.mutate = mutate
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                self.acquire()
            self.assertEqual(self.backend.writes, [])

    def test_partial_failed_wrong_identity_or_bad_statistics_never_repeat_request(self):
        mutations = [{"sample_count": 2047}, {"last_sample_time": 4294}, {"state": 3, "failure": 3},
                     {"first_sample_time": 200, "last_sample_time": 4295},
                     {"request_sequence": 22}, {"phase_b.mean_a": math.nan}, {"phase_c.min_a": math.inf},
                     {"phase_b.mean_a": 0.2}, {"phase_c.variance_a2": -0.01}, {"offset_b_a": 0.02},
                     {"phase_b.min_a": 0.1, "phase_b.max_a": 0.1}]
        for changes in mutations:
            self.backend = FakeBackend(self.prepared)
            self.backend.capture_mutate = lambda tag, result, changes=changes: result.update(changes) if tag != "pre-result" else None
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.acquire()
            self.assertEqual(len(self.backend.writes), 1)

    def test_result_seqlock_and_second_identical_read_required(self):
        for mutation in ["odd", "changed", "partial", "second-change", "second-sequence"]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, blocks, mode=mutation):
                if tag == "capture-poll":
                    if mode == "odd":
                        blocks.update(seq_before=packed_word(7), seq_after=packed_word(7))
                    if mode == "changed":
                        blocks["seq_after"] = packed_word(8)
                    if mode == "partial":
                        blocks["capture"] = blocks["capture"][:-1]
                if tag == "capture-confirm":
                    if mode == "second-change":
                        capture = complete_capture(self.backend.request)
                        capture["phase_b.mean_a"] = 0.101
                        blocks["capture"] = encode("capture", tool.RESULT_FIELDS, capture, self.layout)
                    if mode == "second-sequence":
                        blocks.update(seq_before=packed_word(8), seq_after=packed_word(8))
            self.backend.mutate = mutate
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                self.acquire()
            self.assertEqual(len(self.backend.writes), 1)

    def test_active_timeout_and_partial_active_publication_do_not_resend(self):
        self.backend.active_forever = True
        with self.assertRaisesRegex(RuntimeError, "2 秒"):
            self.acquire()
        self.assertEqual(len(self.backend.writes), 1)
        self.assertLessEqual(self.clock.value, 2.020001)
        self.backend = FakeBackend(self.prepared)
        self.backend.active_forever = True
        self.backend.capture_mutate = lambda tag, result: result.update(sample_count=1) if tag == "capture-poll" else None
        with self.assertRaisesRegex(RuntimeError, "部分窗口"):
            self.acquire()
        self.assertEqual(len(self.backend.writes), 1)

    def test_post_capture_fault_offset_clock_or_request_change_is_rejected(self):
        for mutation in ["fault", "guard", "moe", "offset", "request", "clock"]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, blocks, mode=mutation):
                if not tag.startswith("post-health"):
                    return
                if mode == "fault":
                    self.alter_field(blocks, "publication", "snapshot.faults", 1, ["publication"])
                if mode == "guard":
                    blocks["guard_after"] = packed_word(1)
                if mode == "moe":
                    blocks["bdtr_after"] = packed_word(0x8000)
                if mode == "offset":
                    self.alter_field(blocks, "zero", "offset_a[1]", 0.02, ["zero_before", "zero_after"])
                if mode == "request":
                    blocks["request"] = packed_word(22)
                if mode == "clock":
                    self.alter_field(blocks, "publication", "snapshot.control_time_ticks", 4000, ["publication"])
            self.backend.mutate = mutate
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                self.acquire()
            self.assertEqual(len(self.backend.writes), 1)

    def test_ambiguous_write_failure_is_not_retried_or_cleaned_up(self):
        self.backend.write_error = "写入应答丢失"
        with self.assertRaisesRegex(RuntimeError, "应答丢失"):
            self.acquire()
        self.assertEqual(len(self.backend.writes), 1)
        self.assertNotIn("capture-poll", [tag for tag, _ in self.backend.reads])

    def test_publication_collision_allows_only_bounded_read_only_rechecks(self):
        for permanent in [False, True]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, blocks, permanent=permanent):
                if tag == "pre-health-1" or (permanent and tag.startswith("pre-health-1-coherent-")):
                    blocks["pub_after"] = packed_word(10)
            self.backend.mutate = mutate
            if permanent:
                with self.assertRaisesRegex(RuntimeError, "三次只读"):
                    self.acquire()
                self.assertEqual(self.backend.writes, [])
                self.assertEqual(sum(tag.startswith("pre-health-1") for tag, _ in self.backend.reads), 3)
            else:
                self.acquire()
                self.assertEqual(len(self.backend.writes), 1)
                self.assertEqual(sum(tag.startswith("pre-health-1") for tag, _ in self.backend.reads), 2)

    def test_post_publication_can_catch_up_without_a_second_capture_request(self):
        self.backend.mutate = lambda tag, blocks: self.alter_field(blocks, "publication", "snapshot.control_time_ticks", 4290,
                                                                   ["publication"]) if tag == "post-health" else None
        report = self.acquire()
        self.assertGreaterEqual(report["after"]["time"], report["capture"]["last_sample_time"])
        self.assertEqual(len(self.backend.writes), 1)
        self.assertIn("post-health-catchup-2", [tag for tag, _ in self.backend.reads])

    def test_short_armcc_enums_use_derived_width_not_fixed_offsets(self):
        layout = deepcopy(self.layout)
        for field in tool.ENUM_FIELDS:
            layout[field + ".size"] = 1
        tool.validate_layout(layout)
        capture = complete_capture()
        raw = bytearray(layout["capture_size"])
        for field, fmt in tool.RESULT_FIELDS.items():
            if "capture." + field in tool.ENUM_FIELDS:
                fmt = "B"
            struct.pack_into("<" + fmt, raw, layout["capture." + field + ".offset"], capture[field])
        decoded = tool.decode(bytes(raw), "capture", tool.RESULT_FIELDS, layout)
        self.assertEqual(decoded["state"], 2)
        self.assertEqual(decoded["failure"], 0)
        tool.validate_complete(decoded)

    def test_sequence_wrap_is_new_nonzero_and_exact_one_word(self):
        self.backend.request = 0xFFFFFFFF
        self.backend.capture_mutate = lambda tag, result: result.update(request_sequence=0xFFFFFFFF) if tag == "pre-result" else None
        report = self.acquire()
        self.assertEqual(report["request_sequence"], 1)
        self.assertEqual(self.backend.writes[0][1], b"\1\0\0\0")

    def test_layout_bounds_width_overlap_and_enum_are_checked_without_device(self):
        for key, value in [("request", 0x1FFFFFFC), ("request_size", 8), ("capture", self.layout["request"]),
                           ("zero", self.layout["runtime"] - 4), ("bdtr", 0x20000000),
                           ("capture.phase_b.mean_a.size", 8), ("capture.phase_b.mean_a.offset", 80),
                           ("capture.phase_b.mean_a.offset", 0), ("enum.MOTOR_STATE_IDLE", 2)]:
            broken = dict(self.layout)
            broken[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                tool.validate_layout(broken)
        self.assertEqual(self.backend.reads, [])

    def create_artifacts(self):
        build = self.root / "build"
        build.mkdir()
        stem = build / "Observe"
        stem.with_suffix(".hex").write_text(":020000040800F2\n:0400000001020304F2\n:00000001FF\n", encoding="ascii")
        stem.with_suffix(".axf").write_bytes(elf_fixture(self.layout))
        mapping = "Image Entry point : 0x08000001\nLoad Region LR_IROM1 (Base: 0x08000000, Size: 0x00000004, Max: 0x20000)\n"
        mapping += "".join(f"{name} 0x{self.layout[key]:08x} Data {self.layout[key + '_size']} fixture.o(.data)\n"
                           for key, name in tool.SYMBOLS.items())
        stem.with_suffix(".map").write_text(mapping, encoding="utf-8")
        return build, stem

    def test_offline_exact_hex_axf_map_binding_and_foreign_symbols_rejected(self):
        build, stem = self.create_artifacts()
        self.assertEqual(tool.verify_artifacts(stem.with_suffix(".hex"), stem.with_suffix(".axf"),
                                              stem.with_suffix(".map"), self.layout), self.prepared.image)
        for corruption in ["load", "map", "symbol"]:
            original = {suffix: stem.with_suffix(suffix).read_bytes() for suffix in [".hex", ".axf", ".map"]}
            if corruption == "load":
                stem.with_suffix(".axf").write_bytes(elf_fixture(self.layout, b"\1\2\3\5"))
            elif corruption == "map":
                stem.with_suffix(".map").write_text(stem.with_suffix(".map").read_text().replace("Size: 0x00000004", "Size: 0x00000005"))
            else:
                wrong = dict(self.layout)
                wrong["request"] += 16
                stem.with_suffix(".axf").write_bytes(elf_fixture(wrong))
            with self.subTest(corruption=corruption), self.assertRaises(RuntimeError):
                tool.verify_artifacts(stem.with_suffix(".hex"), stem.with_suffix(".axf"), stem.with_suffix(".map"), self.layout)
            for suffix, payload in original.items():
                stem.with_suffix(suffix).write_bytes(payload)

    def test_map_physical_load_length_supports_only_explicit_compression(self):
        build, stem = self.create_artifacts()
        original = stem.with_suffix(".map").read_text(encoding="utf-8")
        compressed = original.replace("Size: 0x00000004, Max: 0x20000)",
                                      "Size: 0x00000008, Max: 0x20000, ABSOLUTE, COMPRESSED[0x00000004])")
        stem.with_suffix(".map").write_text(compressed, encoding="utf-8")
        self.assertEqual(tool.verify_artifacts(stem.with_suffix(".hex"), stem.with_suffix(".axf"),
                                              stem.with_suffix(".map"), self.layout), self.prepared.image)
        invalid = [compressed.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000003]"),
                   compressed.replace(", COMPRESSED[0x00000004]", ""),
                   compressed.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000000]"),
                   compressed.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000009]"),
                   compressed.replace("COMPRESSED[0x00000004]", "COMPRESSED[4]"),
                   compressed.replace("COMPRESSED[0x00000004]", "COMPRESSED[0x00000004], COMPRESSED[0x00000004]"),
                   compressed.replace("Base: 0x08000000", "Base: 0x08000004"),
                   compressed.replace("Max: 0x20000", "Max: 0x00000007"),
                   compressed + "Load Region LR_OTHER (Base: 0x08000000, Size: 0x4)\n"]
        for mapping in invalid:
            stem.with_suffix(".map").write_text(mapping, encoding="utf-8")
            with self.subTest(mapping=mapping[:160]), self.assertRaises(RuntimeError):
                tool.verify_artifacts(stem.with_suffix(".hex"), stem.with_suffix(".axf"),
                                      stem.with_suffix(".map"), self.layout)
        with self.assertRaisesRegex(RuntimeError, "物理字节"):
            tool.validate_map_load_region("Load Region LR_IROM1 (Base: 0x08000000, Size: 0x3)",
                                          {0x08000000: 1, 0x08000002: 2, 0x08000003: 3})

    def test_prepare_freezes_new_files_and_never_overwrites(self):
        build, stem = self.create_artifacts()
        output = self.root / "prepared"
        with patch.object(tool, "derive_layout", return_value=self.layout):
            prepared = tool.prepare_build(build, tool.digest(stem.with_suffix(".hex")), output)
        self.assertTrue((output / "prepared.json").is_file())
        self.assertEqual(prepared.image, self.prepared.image)
        self.assertEqual((output / "artifacts/Observe.axf").read_bytes(), stem.with_suffix(".axf").read_bytes())
        with self.assertRaisesRegex(RuntimeError, "覆盖"):
            tool.prepare_build(build, tool.digest(stem.with_suffix(".hex")), output)
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            tool.prepare_build(build, "0" * 64, self.root / "wrong-hash")
        self.assertFalse((self.root / "wrong-hash").exists())

    def test_gdb_layout_uses_same_axf_symbol_types_without_hardware(self):
        gdb = self.root / "fake-gdb.exe"
        gdb.write_bytes(b"never executed")
        stdout = "\n".join(f"${index} = {hex(self.layout[key])}" for index, key in enumerate(tool.layout_spec(), 1))
        process = type("Process", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
        with patch.object(tool.subprocess, "run", return_value=process) as run:
            actual = tool.derive_layout(self.root / "same.axf", gdb, self.output)
        self.assertEqual(actual, self.layout)
        arguments = run.call_args.args[0]
        self.assertIn('file "' + (self.root / "same.axf").as_posix() + '"', arguments)
        self.assertTrue(any("ObserveCurrentCaptureResult" in item for item in arguments))
        self.assertFalse(any("target remote" in item or "-c" == item for item in arguments))

    def test_cli_default_only_prepares_and_confirmation_precedes_device(self):
        arguments = ["--build-dir", str(self.root), "--expected-hex-sha256", "a" * 64,
                     "--output", str(self.root / "new"), "--reference-phase", "B",
                     "--reference-current-a", "0.1", "--vbus-dmm-v", "2.68"]
        with patch.object(tool, "prepare_build", return_value=self.prepared), \
             patch.object(tool, "CubeBackend") as backend, redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(arguments), 0)
            backend.assert_not_called()
        with patch.object(tool, "prepare_build") as prepare, patch.object(tool, "CubeBackend") as backend:
            with self.assertRaisesRegex(RuntimeError, "确认"):
                tool.main(arguments + ["--acquire"])
            prepare.assert_not_called()
            backend.assert_not_called()

    def test_cli_reference_source_defaults_and_choices_are_checked_before_device(self):
        arguments = ["--build-dir", str(self.root), "--expected-hex-sha256", "a" * 64,
                     "--output", str(self.root / "new"), "--reference-phase", "B",
                     "--reference-current-a", "0.05", "--vbus-dmm-v", "2.68"]
        for extra, source in [([], "dmm"), (["--reference-source", "supply-display"], "supply-display")]:
            with self.subTest(source=source), \
                 patch.object(tool, "Reference", wraps=tool.Reference) as reference, \
                 patch.object(tool, "prepare_build", return_value=self.prepared), \
                 patch.object(tool, "CubeBackend") as backend, redirect_stdout(io.StringIO()):
                self.assertEqual(tool.main(arguments + extra), 0)
                self.assertEqual(reference.call_args.kwargs["source"], source)
                self.assertEqual(reference.call_args.args[2], 2.68)
                backend.assert_not_called()
        with patch.object(tool, "prepare_build") as prepare, patch.object(tool, "CubeBackend") as backend, \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            tool.main(arguments + ["--reference-source", "unknown"])
        self.assertEqual(error.exception.code, 2)
        prepare.assert_not_called()
        backend.assert_not_called()

    def test_backend_exact_write_address_width_once_and_no_other_commands(self):
        cli = self.root / "fake-cli.exe"
        cli.write_bytes(b"never executed")
        backend = tool.CubeBackend(self.prepared, cli)
        process = type("Process", (), {"returncode": 0, "stdout": b"Device ID : 0x468\nCore is running\n", "stderr": b""})()
        for address, value in [(self.layout["request"] + 4, 1), (self.layout["request"], 0),
                               (self.layout["request"], 0x100000000), (self.layout["request"], 1.0)]:
            with self.subTest(address=address, value=value), self.assertRaises(RuntimeError):
                backend.write_request(address, value)
        with patch.object(tool.subprocess, "run", return_value=process) as run:
            backend.write_request(self.layout["request"], 9)
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[-4:], ["-w32", hex(self.layout["request"]), "0x9", "-score"])
            with self.assertRaises(RuntimeError):
                backend.write_request(self.layout["request"], 10)
            for command in [["-rst"], ["-halt"], ["-run"], ["-w", "image.hex"], ["-w32", hex(self.layout["request"]), "10"]]:
                with self.subTest(command=command), self.assertRaises(RuntimeError):
                    backend.execute(command, "forbidden", 1.0)
            self.assertEqual(run.call_count, 1)

    def fast_read_fixture(self):
        blocks = [("before", self.layout["publication_sequence"], 4),
                  ("publication", self.layout["publication"], 20),
                  ("after", self.layout["publication_sequence"], 4)]
        words = [[0x8136], [0x003275F8, 0, 0x4E20, 1, 0x4029774A], [0x8136]]
        message = "Device ID : 0x468\r\r\nCore is running\r\r\n"
        for (_, address, size), values in zip(blocks, words):
            message += f"Reading 32-bit memory content\r\r\n  Size : {size} Bytes\r\r\n  Address: : 0x{address:08X}\r\r\n"
            for offset in range(0, len(values), 4):
                message += f"0x{address + offset * 4:08X} : " + " ".join(f"{word:08X}" for word in values[offset:offset + 4]) + "\r\r\n"
        message += "Core is running\r\r\n"
        return blocks, words, message

    def test_fast_read32_parses_ordered_words_and_preserves_raw_files(self):
        blocks, words, message = self.fast_read_fixture()
        cli = self.root / "fake-cli.exe"
        cli.write_bytes(b"never executed")
        backend = tool.CubeBackend(self.prepared, cli)
        process = type("Process", (), {"returncode": 0, "stdout": message.encode(), "stderr": b""})()
        with patch.object(tool.subprocess, "run", return_value=process) as run:
            result = backend.read(blocks, "coherent-fast")
        expected = {name: struct.pack("<" + "I" * len(values), *values) for (name, _, _), values in zip(blocks, words)}
        self.assertEqual(result, expected)
        arguments = run.call_args.args[0]
        self.assertNotIn("-u", arguments)
        self.assertEqual(arguments.count("-r32"), 3)
        for name, payload in expected.items():
            self.assertEqual((self.output / f"001-coherent-fast-{name}.bin").read_bytes(), payload)
        self.assertEqual(backend.write_attempted, False)
        self.assertTrue((self.output / "001-coherent-fast.log").is_file())

    def test_fast_read32_rejects_missing_extra_wrong_size_address_and_words(self):
        blocks, _, message = self.fast_read_fixture()
        bad = [message.replace("Size : 20 Bytes", "Size : 24 Bytes"),
               message.replace(f"Address: : 0x{blocks[1][1]:08X}", "Address: : 0x2000FFFF"),
               message.replace(f"0x{blocks[1][1] + 16:08X} : 4029774A", ""),
               message.replace(f"0x{blocks[1][1] + 16:08X}", f"0x{blocks[1][1] + 12:08X}"),
               message.replace("00008136", "00008136 00000000", 1),
               message.replace("4029774A", "4029774Z"),
               message.replace("Size : 20 Bytes", "Size : 20 Bytes\nSize : missing"),
               message.replace(f"Address: : 0x{blocks[1][1]:08X}", f"Address: : 0x{blocks[1][1]:08X}\nAddress: missing"),
               message + message[message.index("Reading 32-bit memory content"):],
               message.replace("Reading 32-bit memory content", "", 1),
               "0x20000000 : 00000000\n" + message]
        for index, altered in enumerate(bad):
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                tool.CubeBackend.parse_read32(altered, blocks)

    def test_fast_read32_transport_rejects_flash_unaligned_extra_flags_and_errors(self):
        cli = self.root / "fake-cli.exe"
        cli.write_bytes(b"never executed")
        backend = tool.CubeBackend(self.prepared, cli)
        commands = [["-r32", hex(tool.FLASH_START), "4"], ["-r32", hex(tool.RAM_START + 1), "4"],
                    ["-r32", hex(tool.RAM_START), "3"], ["-r32", hex(tool.RAM_START), "0"],
                    ["-r32", hex(tool.DHCSR), "8"], ["-r32", hex(tool.RAM_START), "4", "-rst"],
                    ["-r32", hex(tool.RAM_START), "4", "-u", hex(tool.RAM_START), "4"],
                    ["-r32", "not-an-address", "4"], ["-r32", hex(tool.RAM_START), "4;reset"]]
        commands += [["-u", hex(tool.FLASH_START), "4", "-rst"],
                     ["-u", hex(tool.FLASH_START), "4", str(self.root / "outside.bin")]]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                backend.execute(command, "forbidden-fast", 1.0)
        with self.assertRaises(RuntimeError):
            backend.read32([("flash", tool.FLASH_START, 4)], "flash-forbidden")
        for index, (code, text) in enumerate([(1, "Device ID : 0x468\nCore is running"),
                                               (0, "Device ID : 0x468\nCore is running\nError : failure"),
                                               (0, "Device ID : 0x469\nCore is running"),
                                               (0, "Device ID : 0x468\nCore is halted")]):
            process = type("Process", (), {"returncode": code, "stdout": text.encode(), "stderr": b""})()
            with patch.object(tool.subprocess, "run", return_value=process), self.subTest(index=index), self.assertRaises(RuntimeError):
                backend.execute(["-r32", hex(tool.RAM_START), "4"], f"bad-response-{index}", 1.0)
        self.assertFalse(backend.write_attempted or backend.transport_write_attempted)

    def test_flash_read_keeps_upload_and_never_uses_read32(self):
        cli = self.root / "fake-cli.exe"
        cli.write_bytes(b"never executed")
        backend = tool.CubeBackend(self.prepared, cli)
        def uploaded(arguments, tag, timeout):
            self.assertEqual(arguments[0], "-u")
            self.assertNotIn("-r32", arguments)
            Path(arguments[3]).write_bytes(b"\x12\x34\x56\x78")
            return ""
        with patch.object(backend, "execute", side_effect=uploaded):
            self.assertEqual(backend.read([("flash", tool.FLASH_START, 4)], "flash-upload"),
                             {"flash": b"\x12\x34\x56\x78"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
