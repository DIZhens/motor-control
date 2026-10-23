"""Timing-tool safety checks using synthetic memory/artifacts only; no SWD."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import timing_check as timing
import verify_candidate_tool as candidate_fixture

common = timing.common
LAYOUT = {"address": 0x20001000, "size": 200, "recovery": 0x200010C8}
REAL_SESSION = timing.Session


def healthy():
    block = bytearray(104)
    for offset, value in [(0, 0xA1), (24, 0x6868), (28, 0x7868), (32, 0x1555),
                          (44, 4250), (64, 4240), (68, 0x03003400)]:
        struct.pack_into("<I", block, offset, value)
    state = dict(status=1, started=1, state=0, halted=False, core_lockup=False,
        drive_running=True, adc_irq=True, moe=False, high_inputs=0, low_inputs=0,
        guard_running=False, guard_irq=False, invalid_current_count=0, legacy_invalid=0,
        moe_violations=0, bus_v=2.65, bus_valid=True, trip_armed=True,
        legacy_trip_valid=True, trip_valid=1, legacy_trip_active=False,
        legacy_trip_causes=0, bdtr=0x03003400, power_key=0, bie=True,
        break_flags=0, comp_mask=0, dac_codes=[2065, 345, 345], result=0,
        stop_reason=0, fast_tick_count=1000, uw_tick=50, mailbox_command=0,
        mailbox_duration_us=0, mailbox_duty_bits=0, pwm=timing.pwm.decode_pwm(block))
    state.update({field: 0 for field in timing.pwm.checks.COUNTERS})
    state.update({"current.acquisition_valid": True, "current.measured_phases": 6,
                  "trip.condition_active": False, "trip.causes": 0})
    return state


def capture(command, sequence=1):
    result = dict.fromkeys(timing.FIELDS, 0)
    before_delta = {10: 100, 11: 1200, 12: 2500, 13: 9000, 14: 100}[command]
    before_count = {10: 3900, 11: 2800, 12: 1500, 13: 3500, 14: 3900}[command]
    result.update(sequence=sequence * 2, request_sequence=sequence, case_id=command,
                  outcome=1, wait_iterations=50, elapsed_cycles=before_delta + 300,
                  stage_called=1, stage_accepted=int(command in [10, 11, 14]),
                  second_stage_called=int(command == 14))
    for point, delta, count in [("entry", 0, 4000), ("before", before_delta, before_count),
            ("after", before_delta + 100, before_count - 100),
            ("cleanup", before_delta + 300, before_count - 300)]:
        for key, value in zip(timing.REGISTERS, [100000 + delta, count, 0xB1, 0, 0x03003400,
                                                *timing.NEUTRAL]):
            result[point + "." + key] = value
    if command in [10, 11, 14]:
        for channel, value in enumerate(timing.REQUEST, 1):
            result["after.ccr" + str(channel)] = value
    if command == 14:
        for key, value in zip(timing.REGISTERS, [100300, 3700, 0xB1, 0, 0x03003400,
                                                *timing.NEUTRAL]):
            result["second_after." + key] = value
    return result


def packed(value):
    return struct.pack("<50I", *(value[key] for key in timing.FIELDS))


class FakeSession(REAL_SESSION):
    """Real command encoder; synthetic target transitions and block uploads."""
    submit_timing = REAL_SESSION.submit_timing

    def __init__(self, output):
        self.out = Path(output)
        self.out.mkdir(exist_ok=True)
        self.layout = {"request": 0x20000500}
        self.state = healthy()
        self.capture = dict.fromkeys(timing.FIELDS, 0)
        self.last_sequence = None
        self.write_attempted = False
        self.calls, self.reads, self.uploads = [], [], []
        self.recovery = 2
        self.read_change = lambda tag, value: None
        self.capture_change = lambda tag, value: None
        self.block_change = lambda tag, blocks: None

    def execute(self, tag, arguments):
        # Retain the real fixed-command whitelist while blocking its subprocess.
        with patch.object(common.Session, "execute") as transport:
            REAL_SESSION.execute(self, tag, arguments)
            transport.assert_called_once_with(tag, arguments)
        self.calls.append((tag, arguments))
        command, sequence = int(arguments[2], 0), int(arguments[8], 0)
        self.state.update(command_sequence=sequence, active_request_sequence=sequence,
            mailbox_sequence=sequence, mailbox_command=command, mailbox_duration_us=0,
            mailbox_duty_bits=0, result=1, stop_reason=1)
        if command == 1:
            self.state.update(moe=False, result=2, stop_reason=2)
        else:
            self.capture = capture(command, sequence)
            self.state["stage_attempts"] += 1 + int(command == 14)
            self.state["stage_successes"] += int(command in [10, 11, 14])

    def read(self, tag):
        self.reads.append(tag)
        self.state["fast_tick_count"] += 20
        self.state["uw_tick"] += 1
        result = deepcopy(self.state)
        self.read_change(tag, result)
        self.last_sequence = result["mailbox_sequence"]
        return result, {}

    def upload(self, tag, requests):
        self.uploads.append((tag, requests))
        value = deepcopy(self.capture)
        self.capture_change(tag, value)
        blocks = {"seq-before": struct.pack("<I", value["sequence"]), "timing": packed(value),
                  "seq-after": struct.pack("<I", value["sequence"]),
                  "recovery": struct.pack("<I", self.recovery)}
        self.block_change(tag, blocks)
        return blocks


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="g431-timing-test-")
        self.root = Path(self.temp.name)
        self.session = FakeSession(self.root / "session")
        # Any unmocked GDB or programmer invocation is a test failure.
        self.no_process = patch.object(timing.subprocess, "run", side_effect=AssertionError("Unexpected process access"))
        self.no_process.start()

    def tearDown(self):
        self.no_process.stop()
        self.temp.cleanup()

    def test_inspect_fresh_startup_never_writes(self):
        report = timing.run(self.session, LAYOUT, True)
        self.assertEqual(report["result"], "TIMING_READ_ONLY_PASS")
        self.assertEqual(self.session.calls, [])
        self.assertFalse(self.session.write_attempted)
        self.assertEqual(self.session.reads, ["startup-1", "startup-2"])

    def test_fixed_suite_order_counts_and_no_replay(self):
        report = timing.run(self.session, LAYOUT)
        self.assertEqual(report["result"], "TIMING_SUITE_PASS_OUTPUT_OFF")
        self.assertEqual([int(args[2], 0) for _, args in self.session.calls], [10, 11, 12, 14, 13])
        self.assertEqual([case["sequence"] for case in report["cases"]], [1, 2, 3, 4, 5])
        self.assertEqual(report["final"]["stage_attempts"], 6)
        self.assertEqual(report["final"]["stage_successes"], 3)
        self.assertEqual(report["power_enable_calls"], 0)
        self.assertFalse(report["electrical_update_boundary_qualified"])
        self.assertFalse(report["final"]["moe"])
        with self.assertRaisesRegex(RuntimeError, "untouched startup"):
            timing.run(self.session, LAYOUT)
        self.assertEqual(len(self.session.calls), 5)

    def test_dirty_startup_state_and_capture_rejected_without_writes(self):
        for field in ["command_sequence", "active_request_sequence", "result", "stop_reason",
                      "stage_attempts", "stage_successes"]:
            self.session.state = healthy()
            self.session.state[field] = 1
            if field == "command_sequence":
                self.session.state["mailbox_sequence"] = 1
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                timing.run(self.session, LAYOUT, True)
        self.session.state = healthy()
        for field in timing.FIELDS:
            self.session.capture = dict.fromkeys(timing.FIELDS, 0)
            self.session.capture[field] = 2
            with self.subTest(capture_field=field), self.assertRaises(RuntimeError):
                timing.run(self.session, LAYOUT, True)
        self.assertFalse(self.session.write_attempted)

    def test_bus_moe_enable_and_other_guard_failures(self):
        for field, value in [("bus_v", 4.0), ("bus_v", 12.0), ("bus_v", float("nan")),
                ("bus_valid", False), ("moe", True), ("enable_attempts", 1), ("enable_successes", 1),
                ("moe_violations", 1), ("power_key", 123), ("guard_irq", True),
                ("halted", True), ("adc_irq", False), ("high_inputs", 1), ("low_inputs", 1),
                ("break_flags", 0x80), ("trip_armed", False), ("probe_held", 1)]:
            self.session.state = healthy()
            self.session.state[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                timing.run(self.session, LAYOUT)
        self.assertFalse(self.session.calls)

    def test_clock_liveness_required(self):
        for field in ["fast_tick_count", "uw_tick"]:
            self.session.read_change = lambda tag, value, key=field: value.update({key: 10})
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "stopped"):
                timing.run(self.session, LAYOUT)
        self.assertFalse(self.session.calls)

    def test_publication_and_recovery(self):
        self.session.capture = capture(10)
        for recovery in [0, 1, 3]:
            self.session.recovery = recovery
            with self.subTest(recovery=recovery), self.assertRaisesRegex(RuntimeError, "recovered"):
                timing.read_timing(self.session, LAYOUT, "capture")
        self.session.recovery = 2
        for name, raw in [("seq-before", struct.pack("<I", 0)), ("seq-after", struct.pack("<I", 4)),
                          ("timing", b"\0" * 196), ("timing", packed({**capture(10), "sequence": 3}))]:
            self.session.block_change = lambda tag, blocks, key=name, data=raw: blocks.update({key: data})
            with self.subTest(block=name, data=raw[:4]), self.assertRaises(RuntimeError):
                timing.read_timing(self.session, LAYOUT, "capture")

    def test_capture_expected_fields_and_safety_observations(self):
        for command in timing.CASES:
            valid = capture(command)
            timing.check_capture(valid, command, 1)
            changes = [("sequence", 0), ("request_sequence", 0), ("case_id", 99), ("outcome", 2),
                ("stage_called", 0), ("stage_accepted", 1 - valid["stage_accepted"]),
                ("second_stage_called", 1 - valid["second_stage_called"]), ("second_stage_accepted", 1),
                ("elapsed_cycles", 0), ("elapsed_cycles", 17000), ("wait_iterations", 0),
                ("wait_iterations", 4097), ("before.cr1", 0xA1), ("cleanup.ccr1", 1275)]
            for point in ["entry", "before", "after", "cleanup"] + (["second_after"] if command == 14 else []):
                changes += [(point + ".bdtr", 0x0300B400), (point + ".sr", 0x80),
                            (point + ".sr", 0x100), (point + ".cr1", 0xB0),
                            (point + ".cr1", 0xD1), (point + ".cr1", 0xB3)]
            changes += [("after.ccr2", 999)]
            for key, value in changes:
                with self.subTest(command=command, field=key, value=value), self.assertRaises(RuntimeError):
                    timing.check_capture({**valid, key: value}, command, 1)

    def test_new_slot_configuration_rejected_before_any_write(self):
        self.session.state["pwm"].update(repetition=3, update_irq_enabled=True)
        with self.assertRaisesRegex(RuntimeError, "PWM timing configuration"):
            timing.run(self.session, LAYOUT)
        self.assertFalse(self.session.write_attempted)
        self.assertEqual(self.session.calls, [])

    def test_deadline_cross_cycle_and_cancel_evidence(self):
        for command, changes in [(10, {"before.cnt": 1700}), (10, {"before.cycles": 102300}),
                (11, {"before.cnt": 2199}), (11, {"before.cnt": 3001}), (12, {"before.cnt": 1601}),
                (13, {"before.cycles": 108499}), (13, {"before.cycles": 117000}),
                (13, {"before.cnt": 1700}), (13, {"before.cnt": 4001}),
                (14, {"second_after.ccr1": 1275}), (14, {"second_after.cnt": 1700}),
                (14, {"second_after.cr1": 0xA1}), (14, {"second_after.cycles": 102300})]:
            with self.subTest(command=command, changes=changes), self.assertRaises(RuntimeError):
                timing.check_capture({**capture(command), **changes}, command, 1)
        wrap = capture(13)
        wrap.update({"entry.cycles": 0xFFFFF000, "before.cycles": (0xFFFFF000 + 9000) & 0xFFFFFFFF})
        timing.check_capture(wrap, 13, 1)

    def test_stale_request_capture_rejected(self):
        self.session.capture_change = lambda tag, value: value.update(request_sequence=0) if tag != "startup-timing" else None
        with self.assertRaisesRegex(RuntimeError, "did not pass"):
            timing.run(self.session, LAYOUT)
        self.assertEqual(len(self.session.calls), 1)

    def test_post_command_state_payload_and_counters_rejected(self):
        for key, value in [("command_sequence", 2), ("active_request_sequence", 2), ("result", 0),
                ("stop_reason", 2), ("mailbox_command", 3), ("mailbox_duration_us", 1),
                ("mailbox_duty_bits", 1), ("stage_attempts", 2), ("stage_successes", 2),
                ("enable_attempts", 1), ("enable_successes", 1), ("moe", True), ("bus_v", 12.0)]:
            self.session = FakeSession(self.root / "session")
            self.session.read_change = lambda tag, state, k=key, v=value: state.update({k: v}) if tag.startswith("immediate-after") else None
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                timing.run(self.session, LAYOUT)
            self.assertEqual(len(self.session.calls), 1)

    def test_replay_final_capture_or_counters_rejected(self):
        self.session.capture_change = lambda tag, value: value.update(sequence=12) if tag == "final-timing" else None
        with self.assertRaisesRegex(RuntimeError, "replayed"):
            timing.run(self.session, LAYOUT)
        self.assertEqual(len(self.session.calls), 5)
        for field in ["stage_attempts", "stage_successes", "active_request_sequence", "result", "stop_reason"]:
            self.session = FakeSession(self.root / "session")
            self.session.read_change = lambda tag, value, key=field: value.update({key: value[key] + 1}) if tag.startswith("final-no-replay") else None
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "Transaction changed"):
                timing.run(self.session, LAYOUT)

    def test_fixed_command_encoder_and_whitelist(self):
        session = REAL_SESSION.__new__(REAL_SESSION)
        session.layout = {"request": 0x20000500}
        session.last_sequence = None
        with patch.object(common.Session, "execute") as transport:
            with self.assertRaises(RuntimeError):
                session.submit_timing(10, "not-read")
            transport.assert_not_called()
            session.last_sequence = 0xFFFFFFFF
            self.assertEqual(session.submit_timing(10, "wrap"), 1)
            valid = transport.call_args.args[1]
            self.assertEqual(valid, ["-w32", "0x20000504", "0xa", "0x0", "0x0", "0x0", "-w32", "0x20000500", "0x1"])
            for command in [0, 2, 3, 4, 5, 6, 7, 8, 9, 15, 0xFFFFFFFF]:
                with self.subTest(command=command), self.assertRaises(RuntimeError):
                    session.submit_timing(command, "forbidden")
            for index, value in [(0, "-d"), (1, "0x40012c44"), (2, "3"), (3, "1"),
                    (4, "1"), (5, "1"), (6, "-rst"), (7, "0x20000504"), (8, "0"), (8, "0x100000000")]:
                bad = valid.copy()
                bad[index] = value
                with self.subTest(index=index, value=value), self.assertRaises(RuntimeError):
                    session.execute("bad", bad)
            for bad in [[], valid + ["-rst"], ["-u", "0x20000000", "4", "file", "-rst"],
                        ["-u", "0x20000000", "4", "file", "-w32", "0x40012c44", "1", "x"]]:
                with self.subTest(arguments=bad), self.assertRaises(RuntimeError):
                    session.execute("bad", bad)
            session.execute("read", ["-u", "0x20000000", "4", "file"])
            self.assertEqual(transport.call_count, 2)

    def invoke_main(self, session, output, inspect=False):
        argv = ["timing_check.py", "--candidate-manifest", str(self.root / "candidate.json"),
                "--output", str(output)] + (["--inspect-only"] if inspect else [])
        session.out = output
        with patch.object(sys, "argv", argv), patch.object(common, "configure_candidate"), \
                patch.object(common, "verify_source_freeze"), patch.object(timing, "capture_layout", return_value=LAYOUT), \
                patch.object(timing, "Session", return_value=session), redirect_stdout(io.StringIO()):
            timing.main()

    def test_main_success_and_inspect_report_identity(self):
        for inspect in [True, False]:
            session = FakeSession(self.root / "session")
            output = self.root / str(inspect)
            self.invoke_main(session, output, inspect)
            report = json.loads((output / "summary.json").read_text())
            self.assertEqual(report["hex_sha256"], common.EXPECTED)
            self.assertFalse(report["programmed_this_run"])
            self.assertFalse(report["automatic_retry"])
            self.assertEqual(len(session.calls), 0 if inspect else 5)

    def test_main_failure_writes_only_safe_off_after_attempt(self):
        for when in ["startup", "immediate-after", "cancel-after", "final-no-replay"]:
            session = FakeSession(self.root / "session")
            session.read_change = lambda tag, state, point=when: state.update(moe=True) if tag.startswith(point) else None
            output = self.root / when
            with self.subTest(when=when), self.assertRaises(RuntimeError):
                self.invoke_main(session, output)
            report = json.loads((output / "summary.json").read_text())
            commands = [int(args[2], 0) for _, args in session.calls]
            expected = {"startup": [], "immediate-after": [10, 1], "cancel-after": [10, 11, 12, 14, 1],
                        "final-no-replay": [10, 11, 12, 14, 13, 1]}[when]
            self.assertEqual(commands, expected)
            self.assertEqual(report["result"], "FAILED_NO_RETRY")
            self.assertEqual(report["write_attempted"], when != "startup")
            if commands:
                self.assertFalse(report["cleanup"]["moe"])
                self.assertEqual(session.calls[-1][0], "exception-safe-off-only")

    def test_main_requires_explicit_candidate_before_session(self):
        with patch.object(sys, "argv", ["timing", "--output", str(self.root / "output")]), \
                patch.object(timing, "Session") as session, self.assertRaisesRegex(RuntimeError, "explicit candidate"):
            timing.main()
        session.assert_not_called()

    def test_ambiguous_write_failure_only_attempts_one_safe_off(self):
        for cleanup_fails in [False, True]:
            session = FakeSession(self.root / "session")
            original = session.execute
            def failing_execute(tag, arguments):
                # The target may have accepted the mailbox despite a lost reply.
                original(tag, arguments)
                if tag == "submit-immediate" or cleanup_fails:
                    raise RuntimeError("Synthetic transport reply lost")
            session.execute = failing_execute
            output = self.root / ("lost-reply-" + str(cleanup_fails))
            with self.subTest(cleanup_fails=cleanup_fails), self.assertRaisesRegex(RuntimeError, "reply lost"):
                self.invoke_main(session, output)
            report = json.loads((output / "summary.json").read_text())
            self.assertEqual([int(args[2], 0) for _, args in session.calls], [10, 1])
            self.assertTrue(report["write_attempted"])
            self.assertEqual("cleanup_error" in report, cleanup_fails)

    def test_offline_freeze_and_layout_precede_session(self):
        for failing in ["configure", "freeze", "layout"]:
            events = []
            def step(name, result=None):
                def action(*args):
                    events.append(name)
                    if name == failing:
                        raise RuntimeError("offline " + name + " mismatch")
                    return result
                return action
            argv = ["timing", "--output", str(self.root / failing), "--candidate-manifest", str(self.root / "candidate.json")]
            with patch.object(sys, "argv", argv), patch.object(common, "configure_candidate", side_effect=step("configure")), \
                    patch.object(common, "verify_source_freeze", side_effect=step("freeze")), \
                    patch.object(timing, "capture_layout", side_effect=step("layout", LAYOUT)), \
                    patch.object(timing, "Session") as session, self.assertRaises(RuntimeError):
                timing.main()
            self.assertEqual(events, ["configure", "freeze", "layout"][:["configure", "freeze", "layout"].index(failing) + 1])
            session.assert_not_called()

    def test_offline_layout_rejects_wrong_sizes_offsets_or_ram(self):
        values = [LAYOUT["address"], 200, LAYOUT["recovery"], 4]
        for index in range(50):
            values += [index * 4, 4]
        def process(numbers):
            return SimpleNamespace(returncode=0, stderr="", stdout="".join("$%d = 0x%x\n" % (i + 1, value) for i, value in enumerate(numbers)))
        with patch.object(timing.subprocess, "run", return_value=process(values)):
            self.assertEqual(timing.capture_layout(self.root), LAYOUT)
        for index, value in [(0, 0x08000000), (0, 0x20008000), (1, 196), (2, 0x20001004),
                             (2, 0x20008000), (3, 1), (4, 4), (5, 8), (102, 192), (103, 1)]:
            bad = values.copy()
            bad[index] = value
            with self.subTest(index=index, value=value), patch.object(timing.subprocess, "run", return_value=process(bad)), \
                    self.assertRaises(RuntimeError):
                timing.capture_layout(self.root)


class IdentityTests(unittest.TestCase):
    # Reuse synthetic ELF/HEX/MAP construction, not the other test cases.
    setUp = candidate_fixture.Tests.setUp
    tearDown = candidate_fixture.Tests.tearDown
    write = candidate_fixture.Tests.write
    configure = candidate_fixture.Tests.configure

    def test_timing_session_live_image_mismatch_cannot_write(self):
        self.configure()
        with patch.object(common, "offline_layout", return_value=self.layout), \
                patch.object(common.Session, "upload", return_value={"flash": b"\0" * 4}) as upload, \
                patch.object(common.Session, "execute") as execute, \
                self.assertRaisesRegex(RuntimeError, "Live Flash differs"):
            REAL_SESSION(self.root / "session")
        upload.assert_called_once_with("identity", [("flash", 0x08000000, 4)])
        execute.assert_not_called()

    def test_candidate_layout_mismatch_prevents_swd(self):
        self.configure()
        with patch.object(common, "offline_layout", return_value={**self.layout, "request": 0x20000054}), \
                patch.object(common.Session, "upload") as upload, \
                self.assertRaisesRegex(RuntimeError, "layout differs"):
            REAL_SESSION(self.root / "session")
        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
