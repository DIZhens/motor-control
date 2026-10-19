"""无功率 stage 工具的主机替身验证；不调用设备或外部进程。"""

from contextlib import redirect_stdout
from copy import deepcopy
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
import stage_budget as tool
import test_compute_probe_tool as shared


def fixture_layout():
    layout = {key: 0 for key in tool.layout_spec()}
    for key, address, size in [("request", 0x20000018, 4), ("sequence", 0x2000001C, 4),
                               ("active", 0x20000020, 4), ("guard", 0x20000008, 4),
                               ("publication_sequence", 0x2000000C, 4),
                               ("publication", 0x20000300, 64), ("result", 0x20004000, 180)]:
        layout[key], layout[key + "_size"] = address, size
    layout["bdtr"], layout["row_count"] = tool.TIM1_BASE + 68, 6
    for prefix, fields in [("result", tool.RESULT_FIELDS), ("publication", tool.PUBLICATION_FIELDS)]:
        offset = 0
        for field, fmt in fields.items():
            size = 1 if prefix == "publication" and field == "snapshot.state" else struct.calcsize("<" + fmt)
            layout[prefix + "." + field + ".offset"] = offset
            layout[prefix + "." + field + ".size"] = size
            offset += size
    layout.update({"enum." + name: value for name, value in tool.EXPECTED_ENUMS.items()})
    tool.validate_layout(layout)
    return layout


def encode(prefix, fields, values, layout):
    payload = bytearray(layout[prefix + "_size"])
    for field, fmt in fields.items():
        if prefix == "publication" and field == "snapshot.state":
            fmt = {1: "B", 2: "H", 4: "I"}[layout[prefix + "." + field + ".size"]]
        struct.pack_into("<" + fmt, payload, layout[prefix + "." + field + ".offset"], values.get(field, 0))
    return bytes(payload)


def complete(request):
    result = {"schema_version": 1, "request_sequence": request, "state": 3, "failure": 0,
              "completed_samples": 80, "stage_calls": 80, "stage_accepted": 80, "enable_attempts": 0,
              "observation_refreshes": 4, "speed_updates": 8, "min_remaining_counts": 200,
              "last_shadow_faults": 0, "limited_samples": 40, "fast_samples": 72, "slow_samples": 8,
              "last_elapsed_cycles": 1800, "last_step_cycles": 900, "last_timer_count": 2000,
              "last_entry_count": 4200, "last_cr1": 0x11, "last_bdtr": 0}
    for index, count in enumerate([80, 80, 8, 4, 72, 8]):
        scale = 0.5 if index == 1 else 1.0
        result.update({f"rows[{index}].{field}": int(value) for field, value in {
            "count": count, "min_cycles": 1000 * scale, "max_cycles": 2000 * scale,
            "sum_cycles": count * 1500 * scale}.items()})
    return result


def failed(request):
    result = {field: 0 for field in tool.RESULT_FIELDS}
    result.update(schema_version=1, request_sequence=request, state=4, failure=6, stage_calls=1,
                  last_elapsed_cycles=10000, last_step_cycles=9700, last_timer_count=500,
                  last_entry_count=4200, last_cr1=0x11, last_bdtr=0, last_shadow_faults=4)
    return result


class FakeBackend:
    def __init__(self, prepared):
        self.prepared, self.layout = prepared, prepared.layout
        self.request, self.old_request = 0, 0
        self.write_attempted, self.write_error = False, None
        self.reads, self.writes = [], []
        self.mutate = lambda tag, raw: None
        self.result_mutate = lambda tag, result: None
        self.forever, self.old_forever, self.terminal_failed = False, False, False

    def read(self, blocks, tag, timeout_s=20.0):
        assert timeout_s > 0
        self.reads.append(tag)
        if tag == "flash-identity":
            flash = bytearray(b"\xff" * tool.FLASH_SIZE)
            for address, value in self.prepared.image.items():
                flash[address - tool.FLASH_START] = value
            raw = {"flash": bytes(flash)}
        elif tag.startswith(("pre-result", "result-", "failure-result")):
            result = {"schema_version": 1, "request_sequence": self.old_request, "state": 0}
            if not tag.startswith("pre-result") and not self.old_forever:
                result = failed(self.request) if self.terminal_failed else complete(self.request)
                if self.forever:
                    result = {"schema_version": 1, "request_sequence": self.request, "state": 1}
            self.result_mutate(tag, result)
            raw = {"seq_before": struct.pack("<I", 2), "seq_after": struct.pack("<I", 2),
                   "result": encode("result", tool.RESULT_FIELDS, result, self.layout)}
        else:
            base = tag.split("-coherent-")[0]
            publication = {"snapshot.control_time_ticks": {"pre-health-1": 100, "pre-health-2": 200,
                                                           "post-health-1": 300, "post-health-2": 400}[base],
                           "snapshot.control_time_ticks_per_second": 20000, "snapshot.state": 1,
                           "snapshot.faults": 0, "snapshot.bus_voltage_v": 2.7,
                           "snapshot.status_valid": 1, "snapshot.control_time_valid": 1,
                           "snapshot.bus_voltage_valid": 1, "has_sample": 1, "stage_probe_permitted": 1}
            raw = {name: struct.pack("<I", 0) for name in
                   ["guard_before", "guard_after", "bdtr_before", "bdtr_after", "cpu_before", "cpu_after"]}
            raw.update(pub_before=struct.pack("<I", 4), pub_after=struct.pack("<I", 4),
                       request=struct.pack("<I", self.request), active=struct.pack("<I", int(self.forever)),
                       publication=encode("publication", tool.PUBLICATION_FIELDS, publication, self.layout))
        self.mutate(tag, raw)
        for name, address, size in blocks:
            assert len(raw[name]) == size
            if name in ["result", "publication", "request", "active"]:
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


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="stage-budget-host-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "evidence"
        self.output.mkdir()
        self.layout = fixture_layout()
        self.prepared = tool.PreparedBuild({tool.FLASH_START + index: value for index, value in enumerate(b"\1\2\3\4")},
                                          self.layout, {"Observe.hex": "a" * 64}, self.output)
        self.backend, self.clock = FakeBackend(self.prepared), shared.Clock()
        self.conditions = tool.compute_probe.Conditions(2.7, True)
        self.process_guard = patch.object(tool.subprocess, "run", side_effect=AssertionError("测试禁止外部进程"))
        self.process_guard.start()

    def tearDown(self):
        self.process_guard.stop()
        self.temporary.cleanup()

    def measure(self):
        return tool.run_measurement(self.backend, self.prepared, self.conditions, self.clock.now, self.clock.sleep)

    def alter_publication(self, raw, field, value):
        data = bytearray(raw["publication"])
        fmt = "B" if field == "snapshot.state" else tool.PUBLICATION_FIELDS[field]
        struct.pack_into("<" + fmt, data, self.layout["publication." + field + ".offset"], value)
        raw["publication"] = bytes(data)

    def test_complete_exactly_once_with_limits_and_no_compute_permission(self):
        self.assertNotIn("compute_probe_permitted", tool.PUBLICATION_FIELDS)
        report = self.measure()
        self.assertTrue(report["success"])
        self.assertEqual(self.backend.writes, [(self.layout["request"], 1)])
        self.assertEqual([row["count"] for row in report["rows"]], [80, 80, 8, 4, 72, 8])
        self.assertTrue(report["result_reread_unchanged"])
        self.assertFalse(report["hardware_qualified"] or report["foc_qualified"] or report["full_isr_wcet"])

    def test_missing_conditions_or_bad_layout_cause_zero_reads_or_writes(self):
        for conditions in [tool.compute_probe.Conditions(), tool.compute_probe.Conditions(2.7),
                           tool.compute_probe.Conditions(None, True), tool.compute_probe.Conditions(math.nan, True),
                           tool.compute_probe.Conditions(-1, True), tool.compute_probe.Conditions(3.61, True)]:
            self.conditions = conditions
            with self.subTest(conditions=conditions), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(self.backend.reads, [])
            self.assertEqual(self.backend.writes, [])

    def test_busy_pending_bad_schema_or_image_cause_zero_write(self):
        for field, value in [("state", 1), ("state", 2), ("state", 5), ("failure", 9), ("schema_version", 2)]:
            self.backend = FakeBackend(self.prepared)
            self.backend.result_mutate = lambda tag, result, field=field, value=value: result.update({field: value}) if tag == "pre-result" else None
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(self.backend.writes, [])
        self.backend = FakeBackend(self.prepared)
        self.backend.request = 9
        with self.assertRaisesRegex(RuntimeError, "未消费"):
            self.measure()
        self.assertEqual(self.backend.writes, [])
        self.backend = FakeBackend(self.prepared)
        self.backend.mutate = lambda tag, raw: raw.update(flash=b"\0" * tool.FLASH_SIZE) if tag == "flash-identity" else None
        with self.assertRaisesRegex(RuntimeError, "Flash"):
            self.measure()
        self.assertEqual(self.backend.writes, [])

    def test_pre_health_hardware_failures_cause_zero_write(self):
        for name, value in [("guard_after", 1), ("bdtr_before", 0x8000), ("cpu_after", 1 << 17), ("active", 1)]:
            self.backend = FakeBackend(self.prepared)
            self.backend.mutate = lambda tag, raw, name=name, value=value: raw.update({name: struct.pack("<I", value)}) if tag.startswith("pre-health") else None
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(self.backend.writes, [])

    def test_pre_publication_requires_true_stage_permission_and_progress(self):
        for field, value in [("snapshot.state", 2), ("snapshot.faults", 1), ("stage_probe_permitted", 0),
                             ("snapshot.control_time_valid", 0), ("has_sample", 0), ("snapshot.status_valid", 0),
                             ("snapshot.bus_voltage_valid", 0), ("snapshot.bus_voltage_v", 3.1),
                             ("snapshot.bus_voltage_v", math.nan), ("snapshot.control_time_ticks_per_second", 1000),
                             ("snapshot.control_time_ticks", 100)]:
            self.backend = FakeBackend(self.prepared)
            self.backend.mutate = lambda tag, raw, field=field, value=value: self.alter_publication(raw, field, value) if tag.startswith("pre-health") else None
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.measure()
            self.assertEqual(self.backend.writes, [])

    def test_failed_stage_keeps_full_gap_and_first_reason_after_guard_fault(self):
        self.backend.terminal_failed = True
        self.backend.mutate = lambda tag, raw: raw.update(guard_after=struct.pack("<I", 8)) if tag.startswith("post-health") else None
        report = self.measure()
        self.assertFalse(report["success"])
        self.assertEqual(report["primary_failure"], "firmware:STAGE")
        self.assertEqual(report["result"], failed(1))
        self.assertEqual(report["result"]["last_elapsed_cycles"], 10000)
        self.assertEqual(report["post-health-2"]["words"]["guard_after"], 8)
        self.assertEqual(len(self.backend.writes), 1)
        self.assertIn("result-confirm", self.backend.reads)
        tool.save(self.output / "failed.json", report)

    def test_complete_counts_limit_remaining_last_facts_and_rows_checked(self):
        failures = [(field, 0) for field in ["completed_samples", "stage_calls", "stage_accepted", "speed_updates",
                    "observation_refreshes", "fast_samples", "slow_samples", "min_remaining_counts", "limited_samples"]]
        failures += [("limited_samples", 80), ("enable_attempts", 1), ("last_shadow_faults", 1),
                     ("last_timer_count", 1700), ("last_cr1", 1), ("last_bdtr", 0x8000),
                     ("last_step_cycles", 0), ("last_elapsed_cycles", 4251), ("rows[4].count", 71),
                     ("rows[2].sum_cycles", 1), ("rows[0].max_cycles", 4251), ("rows[3].min_cycles", 0)]
        for field, value in failures:
            result = complete(1)
            result[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                tool.validate_complete(result)

    def test_partial_foreign_or_invalid_result_returns_failure_without_resend(self):
        for values in [{"request_sequence": 99}, {"schema_version": 0}, {"state": 9}, {"failure": 10},
                       {"completed_samples": 79}, {"enable_attempts": 1}, {"state": 4, "failure": 0}]:
            self.backend = FakeBackend(self.prepared)
            self.backend.result_mutate = lambda tag, result, values=values: result.update(values) if tag == "result-poll" else None
            with self.subTest(values=values):
                report = self.measure()
                self.assertFalse(report["success"])
                self.assertEqual(len(self.backend.writes), 1)
                self.assertIn("post-health-2", report)

    def test_active_and_old_timeouts_never_resend(self):
        for mode in ["forever", "old_forever"]:
            self.backend, self.clock = FakeBackend(self.prepared), shared.Clock()
            if mode == "forever":
                self.backend.result_mutate = lambda tag, result: result.update(state=1) if tag.startswith("result-poll") else None
            else:
                self.backend.old_forever = True
            report = self.measure()
            self.assertFalse(report["success"])
            self.assertRegex(report["primary_failure"], "2 秒|次数")
            self.assertEqual(len(self.backend.writes), 1)
            self.assertLessEqual(self.clock.value, 2.040001)

    def test_freeze_change_or_permanent_conflict_is_not_accepted(self):
        for mode in ["freeze", "conflict"]:
            self.backend = FakeBackend(self.prepared)
            if mode == "freeze":
                self.backend.result_mutate = lambda tag, result: result.update(last_timer_count=2001) if tag == "result-confirm" else None
            else:
                self.backend.mutate = lambda tag, raw: raw.update(seq_after=struct.pack("<I", 4)) if tag.startswith("result-poll") else None
            report = self.measure()
            self.assertFalse(report["success"])
            self.assertEqual(len(self.backend.writes), 1)
            if mode == "conflict":
                self.assertEqual(sum(tag.startswith("result-poll") for tag in self.backend.reads), 3)

    def test_post_health_failure_or_stagnation_cannot_make_complete_pass(self):
        for mode in ["guard", "request", "clock", "permit", "nan", "read_error"]:
            self.backend = FakeBackend(self.prepared)
            def mutate(tag, raw, mode=mode):
                if not tag.startswith("post-health"):
                    return
                if mode == "guard":
                    raw["guard_before"] = struct.pack("<I", 1)
                elif mode == "request":
                    raw["request"] = struct.pack("<I", 9)
                elif mode == "clock":
                    self.alter_publication(raw, "snapshot.control_time_ticks", 100)
                elif mode == "permit":
                    self.alter_publication(raw, "stage_probe_permitted", 0)
                elif mode == "nan":
                    self.alter_publication(raw, "snapshot.bus_voltage_v", math.nan)
                else:
                    raise RuntimeError("后健康传输失败")
            self.backend.mutate = mutate
            report = self.measure()
            with self.subTest(mode=mode):
                self.assertFalse(report["success"])
                self.assertEqual(report["result"]["state"], 3)
                self.assertEqual(len(self.backend.writes), 1)
            if mode == "nan":
                tool.save(self.output / "nan-failure.json", report)

    def test_write_ack_loss_only_reads_and_preserves_first_cause(self):
        self.backend.write_error = "应答丢失"
        report = self.measure()
        self.assertFalse(report["success"])
        self.assertEqual(report["primary_failure"], "应答丢失")
        self.assertEqual(len(self.backend.writes), 1)
        self.assertIn("failure-result", self.backend.reads)
        self.assertNotIn("result-poll", self.backend.reads)

    def test_request_wrap_is_nonzero_and_new(self):
        self.backend.request = self.backend.old_request = 0xFFFFFFFF
        report = self.measure()
        self.assertTrue(report["success"])
        self.assertEqual(report["request_sequence"], 1)

    def test_layout_rejects_old_width_overlap_size_or_wrong_symbols(self):
        for key, value in [("result_size", 176), ("row_count", 5), ("request", 0x1FFFFFFC),
                           ("sequence_size", 8), ("result", self.layout["request"]),
                           ("result.last_bdtr.offset", 0), ("result.rows[0].count.size", 1),
                           ("publication.stage_probe_permitted.size", 4), ("enum.G431_STAGE_BUDGET_COMPLETE", 2)]:
            layout = dict(self.layout)
            layout[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                tool.validate_layout(layout)

    def test_only_bounded_coherent_health_rechecks(self):
        for permanent in [False, True]:
            self.backend = FakeBackend(self.prepared)
            self.backend.mutate = lambda tag, raw, permanent=permanent: raw.update(pub_after=struct.pack("<I", 8)) if tag == "pre-health-1" or permanent and tag.startswith("pre-health-1-coherent-") else None
            if permanent:
                with self.assertRaises(RuntimeError):
                    self.measure()
                self.assertEqual(self.backend.writes, [])
            else:
                self.assertTrue(self.measure()["success"])
            self.assertEqual(sum(tag.startswith("pre-health-1") for tag in self.backend.reads), 8 if permanent else 2)

    def test_stage_gdb_batch_is_offline_and_separate(self):
        gdb = self.root / "gdb.exe"
        gdb.write_bytes(b"never executed")
        process = type("Process", (), {"returncode": 0, "stderr": "", "stdout": "\n".join(
            f"${index} = {hex(self.layout[key])}" for index, key in enumerate(tool.layout_spec(), 1))})()
        with patch.object(tool.subprocess, "run", return_value=process):
            layout = tool.derive_layout(self.root / "same.axf", gdb, self.output)
        self.assertEqual(layout, self.layout)
        commands = (self.output / "stage-layout-gdb.commands").read_text(encoding="utf-8")
        self.assertIn("G431StageBudgetResult", commands)
        self.assertIn("stage_probe_permitted", commands)
        self.assertNotIn("target remote", commands)
        self.assertFalse((self.output / "layout-gdb.commands").exists())

    def test_prepare_reuses_exact_original_freeze_without_layout_pollution(self):
        output = self.root / "new"
        original = tool.PreparedBuild(self.prepared.image, shared.fixture_layout(), self.prepared.hashes, output)
        def freeze(*args):
            artifacts = output / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "Observe.axf").write_bytes(b"frozen")
            (artifacts / "Observe.map").write_text("frozen", encoding="utf-8")
            return original
        with patch.object(tool, "EVIDENCE_ROOT", self.root), patch.object(tool.compute_probe, "prepare_build", side_effect=freeze) as prepare, \
                patch.object(tool, "derive_layout", return_value=self.layout), patch.object(tool, "verify_symbols"):
            actual = tool.prepare_build(self.root / "build", "a" * 64, output)
        self.assertEqual(actual.layout, self.layout)
        self.assertEqual(original.layout, shared.fixture_layout())
        self.assertNotEqual(actual.layout["request"], original.layout["request"])
        self.assertEqual(prepare.call_count, 1)
        self.assertTrue((output / "stage-prepared.json").is_file())
        with self.assertRaisesRegex(RuntimeError, "local-evidence"):
            tool.prepare_build(self.root / "build", "a" * 64, self.root / "outside")

    def test_extra_symbols_are_bound_to_same_elf_map_and_separate_mailbox(self):
        axf = self.root / "same.axf"
        with patch.object(shared.tool, "SYMBOLS", tool.SYMBOLS):
            axf.write_bytes(shared.elf_fixture(self.layout))
        mapping = "".join(f"{name} 0x{self.layout[key]:08x} Data {self.layout[key + '_size']} fixture.o(.data)\n"
                          for key, name in tool.SYMBOLS.items())
        original = shared.fixture_layout()
        tool.verify_symbols(axf, mapping, self.layout, original)
        with self.assertRaisesRegex(RuntimeError, "MAP"):
            tool.verify_symbols(axf, mapping.replace("g_target_runtime_stage_request", "wrong_symbol"), self.layout, original)
        original["request"] = self.layout["request"]
        with self.assertRaisesRegex(RuntimeError, "重叠"):
            tool.verify_symbols(axf, mapping, self.layout, original)

    def test_cli_default_is_offline_and_failed_terminal_returns_nonzero(self):
        args = ["--build-dir", str(self.root), "--expected-hex-sha256", "a" * 64, "--output", str(self.root / "new")]
        with patch.object(tool, "prepare_build", return_value=self.prepared), patch.object(tool, "CubeBackend") as backend, redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(args), 0)
            backend.assert_not_called()
        with patch.object(tool, "prepare_build") as prepare, patch.object(tool, "CubeBackend") as backend:
            with self.assertRaises(RuntimeError):
                tool.main(args + ["--measure", "--vbus-dmm-v", "2.7"])
            prepare.assert_not_called()
            backend.assert_not_called()
        self.backend.terminal_failed = True
        with patch.object(tool, "prepare_build", return_value=self.prepared), patch.object(tool, "CubeBackend", return_value=self.backend), redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(args + ["--measure", "--logic-only-confirmed", "--vbus-dmm-v", "2.7"]), 1)
        self.assertTrue((self.output / "result.json").is_file())
        self.assertFalse((self.output / "failure.json").exists())

    def test_shared_transport_restricts_write_to_stage_request(self):
        cli = self.root / "cli.exe"
        cli.write_bytes(b"never executed")
        backend = tool.CubeBackend(self.prepared, cli)
        message = type("Process", (), {"returncode": 0, "stdout": b"Device ID : 0x468\nCore is running\n", "stderr": b""})()
        with self.assertRaises(RuntimeError):
            backend.write_request(shared.fixture_layout()["request"], 1)
        with patch.object(tool.subprocess, "run", return_value=message) as run:
            backend.write_request(self.layout["request"], 1)
            with self.assertRaises(RuntimeError):
                backend.write_request(self.layout["request"], 2)
            self.assertEqual(run.call_count, 1)
            self.assertIn(hex(self.layout["request"]), run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
