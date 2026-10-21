"""有限开环主机工具的纯替身验证；不连接 ST-Link。"""
from copy import deepcopy
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import open_loop_once as loop
REAL_SESSION = loop.Session


CURRENT_LAYOUT = dict(zip(loop.current.FIELDS, [0, 8, 12, 16, 20, 24, 32, 40, 48, 52, 56, 60, 64, 68, 72, 76]))


def healthy():
    block = bytearray(104)
    for offset, value in [(0, 0xA1), (12, 1), (48, 3), (24, 0x6868), (28, 0x7868), (32, 0x1555),
                          (44, 4250), (64, 4240), (68, 0x03003400)]:
        struct.pack_into("<I", block, offset, value)
    state = dict(status=1, started=1, state=0, halted=False, core_lockup=False,
        drive_running=True, adc_irq=True, moe=False, high_inputs=0, low_inputs=0,
        guard_running=False, guard_irq=False, invalid_current_count=0, legacy_invalid=0,
        moe_violations=0, phase_b_a=-0.012, phase_c_a=0.0, bus_v=12.0, bus_valid=True,
        trip_armed=True, legacy_trip_valid=True, trip_valid=1, legacy_trip_active=False,
        legacy_trip_causes=0, bdtr=0x03003400, af1=9, af2=23, power_key=0,
        bie=True, break_flags=0, comp_mask=0, dac_codes=[2065, 345, 345],
        comp_csr=[0x41, 0x41, 0x51, 0x41], dac_cr=[1, 0x10001], dac_mcr=[3, 0x30003],
        result=0, stop_reason=0, fast_tick_count=1000, uw_tick=50, position_valid=True,
        position_turns=0.25, position_samples=50, mailbox_command=0, mailbox_duration_us=2000,
        mailbox_duty_bits=0x3F000000, pwm=loop.pwm.decode_pwm(block))
    state.update(dict.fromkeys(loop.checks.COUNTERS, 0))
    state.update({"current.acquisition_valid": True, "current.measured_phases": 6,
        "trip.condition_active": False, "trip.causes": 0, "bus.acquisition_valid": True,
        "bus.voltage_v": 12.0, "current.observation_time": 1000, "bus.observation_time": 990})
    return state


def capture(request=1, changes=None):
    fields = dict(zip(loop.current.FIELDS, [4, request, 1, 4096, 16900, 17100, 100, 8290,
        -0.012, -0.02, 0, 1e-5, -0.001, -0.01, 0.01, 1e-5]))
    fields.update(changes or {})
    raw = bytearray(80)
    for key, value in fields.items():
        struct.pack_into("<" + loop.current.FIELDS[key], raw, CURRENT_LAYOUT[key], value)
    return bytes(raw)


class FakeSession:
    """让真实白名单检查处理全部写入，只替换外部 CLI 和内存读取。"""
    def __init__(self, output):
        self.out = Path(output)
        self.out.mkdir(exist_ok=True)
        self.state, self.hex_sha256 = healthy(), "frozen-test-image"
        self.layout = {"request": 0x20000000, "config": 0x20000100, "capture": 0x20000200,
            "open_loop": 0x20000300, "stop": 0x20000400, "current_fields": CURRENT_LAYOUT,
            "result_fields": dict(zip(loop.RESULT_FIELDS, range(0, 72, 4)))}
        self.probe = dict.fromkeys(loop.checks.PROBE_FIELDS, 0)
        self.raw_capture, self.config = bytes(80), bytes(8)
        self.mailbox, self.result = bytearray(20), bytes(72)
        self.last_sequence, self.write_attempted, self.writes = 0, False, []
        self.call_timeout_s, self.deadline = 15, None
        self.power_payload_attempted, self.power_commit_ready = False, False
        self.parameters = loop.parameters(0.3, 2)
        self.action, self.cli, self.calls = "zero", "fake-cli", []
        self.timeout, self.bad_echo, self.motion = False, False, False
        self.control_stalled, self.history = False, {}

    def read(self, tag):
        for key, delta in [("uw_tick", 50), ("fast_tick_count", 500), ("current.observation_time", 1000),
                           ("bus.observation_time", 1000), ("position_samples", 50)]:
            if self.control_stalled and key in ["current.observation_time", "bus.observation_time"]:
                continue
            self.state[key] += delta
        if self.motion:
            self.state["position_turns"] += 0.01
        self.last_sequence = self.state["mailbox_sequence"]
        result = deepcopy(self.state), {"probe": struct.pack("<17I", *self.probe.values()), "capture": self.raw_capture}
        self.history[tag] = deepcopy(result)
        return result

    def execute(self, tag, arguments):
        def cli(command, stdout, stderr, timeout):
            self.calls.append(arguments)
            stdout.write(b"Device ID : 0x468\nCore is running\n")
            if len(arguments) in [10, 12]:
                marker = len(arguments) - 6
                self.config = struct.pack("<" + str(marker - 2) + "I", *(int(v, 0) for v in arguments[2:marker]))
                self.mailbox[4:] = struct.pack("<4I", *(int(v, 0) for v in arguments[marker + 2:]))
            else:
                if len(arguments) == 9:
                    self.mailbox[4:] = struct.pack("<4I", *(int(v, 0) for v in arguments[2:6]))
                    sequence = int(arguments[8], 0)
                else:
                    sequence = int(arguments[2], 0)
                struct.pack_into("<I", self.mailbox, 0, sequence)
                command_id = loop.common.word(self.mailbox, 4)
                self.state.update(command_sequence=sequence, mailbox_sequence=sequence,
                    mailbox_command=command_id, mailbox_duration_us=loop.common.word(self.mailbox, 12),
                    mailbox_duty_bits=loop.common.word(self.mailbox, 16), result=1, stop_reason=1)
                if command_id == 4:
                    self.raw_capture = capture(sequence)
                    self.state.update(active_request_sequence=sequence, guard_irq_count=self.state["guard_irq_count"] + 4096)
                elif command_id in [15, 16]:
                    samples = 30000 if command_id == 15 else 50000
                    self.state.update(active_request_sequence=sequence)
                    for key, delta in [("enable_attempts", 1), ("enable_successes", 1),
                                       ("stage_attempts", samples), ("stage_successes", samples)]:
                        self.state[key] += delta
                    stats = loop.current.decode_capture(self.raw_capture, CURRENT_LAYOUT, loop.common.word(self.raw_capture, 8))
                    values = [6, sequence, int(self.timeout), 0 if self.timeout else 1, 4 if self.timeout else 6,
                        0, samples+1, samples, *self.parameters, stats["phase_b.mean_a"], stats["phase_c.mean_a"],
                        0.12, 0.25, 0.5, 0.25, 0.47, 0.53]
                    self.result = struct.pack("<8I10f", *values)
                elif command_id == 1:
                    self.state.update(moe=False, guard_running=False, result=2, stop_reason=2)
            return type("Completed", (), {"returncode": 0})()
        with patch.object(loop.subprocess, "run", side_effect=cli):
            return REAL_SESSION.execute(self, tag, arguments)

    def upload(self, tag, blocks):
        result = {}
        for name, address, size in blocks:
            data = {"config": self.config, "request": bytes(self.mailbox), "capture": self.raw_capture,
                "open-loop": self.result, "sequence": self.result[:4],
                "stop": struct.pack("<I", self.state["stop_reason"]),
                "bdtr": struct.pack("<I", 0x8000 if self.state["moe"] else 0), "dhcsr": bytes(4)}[name]
            if self.bad_echo and name == "config":
                data = bytes(8)
            self.out.joinpath(tag + "-" + name + ".bin").write_bytes(data)
            result[name] = data
        return result


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        self.now += 0.25
        return self.now


class Tests(unittest.TestCase):
    def test_default_prepare_never_constructs_session(self):
        with TemporaryDirectory() as tmp, patch.object(loop, "Session") as access, patch.object(loop, "prepare", return_value={}) as prepare:
            loop.main(["--output", str(Path(tmp) / "new")])
            prepare.assert_called_once()
            access.assert_not_called()

    def test_inspect_is_read_only_and_stop_writes_only_once(self):
        for action in ["inspect", "stop"]:
            with self.subTest(action=action), TemporaryDirectory() as tmp:
                sessions = []
                def factory(output, prepared, cli, mode, voltage, frequency):
                    session = FakeSession(output)
                    session.action = mode
                    if mode == "stop":
                        session.state.update(moe=True, guard_running=True)
                    sessions.append(session)
                    return session
                with patch.object(loop, "Session", side_effect=factory):
                    result = loop.main(["--output", str(Path(tmp) / "new"), "--action", action,
                                        "--prepared", "offline.json"])
                self.assertEqual(sessions[0].writes, [] if action == "inspect" else [1])
                self.assertFalse(result["motor_qualified"])
                self.assertFalse(result["foc_qualified"])
                self.assertFalse(result["automatic_retry"])

    def test_external_timeout_cannot_replay_committed_sequence(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp)
            session.action, session.last_sequence = "execute", 1
            session.power_payload_attempted = session.power_commit_ready = True
            arguments = ["-w32", hex(session.layout["request"]), "0x1"]
            with patch.object(loop.subprocess, "run", side_effect=loop.subprocess.TimeoutExpired("fake", 0.1)):
                with self.assertRaises(loop.subprocess.TimeoutExpired):
                    REAL_SESSION.execute(session, "external-timeout", arguments)
            self.assertEqual(session.writes, [15])
            with self.assertRaises(RuntimeError):
                REAL_SESSION.execute(session, "forbidden-retry", arguments)

    def test_parameter_caps_before_hardware(self):
        for voltage, frequency in [(0, 2), (0.501, 2), (float("nan"), 2), (float("inf"), 2),
                                   (0.3, 0), (0.3, -5.1), (0.3, float("nan")), (1e-99, 2)]:
            with self.subTest(voltage=voltage, frequency=frequency), TemporaryDirectory() as tmp, patch.object(loop, "Session") as access:
                with self.assertRaises(RuntimeError):
                    loop.main(["--output", str(Path(tmp) / "new"), "--voltage-v", str(voltage),
                               "--electrical-frequency-hz", str(frequency)])
                access.assert_not_called()
        self.assertEqual(loop.parameters(0.5, -5), (0.5, -5))

    def test_bench_and_vbus_required_before_hardware(self):
        for extras in [[], ["--bench-confirmed"], ["--bench-confirmed", "--vbus-dmm-v", "9.9"],
                       ["--bench-confirmed", "--vbus-dmm-v", "nan"]]:
            with self.subTest(extras=extras), TemporaryDirectory() as tmp, patch.object(loop, "Session") as access:
                with self.assertRaises(RuntimeError):
                    loop.main(["--output", str(Path(tmp) / "new"), "--action", "zero", "--prepared", "fake.json", *extras])
                access.assert_not_called()

    def make_zero(self, folder):
        session = FakeSession(folder)
        with patch.object(loop.time, "sleep"):
            report = loop.zero(session, 12)
        session.action = "execute"
        return session, report

    def test_zero_raw_and_complete_statistics(self):
        with TemporaryDirectory() as tmp:
            session, report = self.make_zero(tmp)
            self.assertEqual(report["sample_count"], 4096)
            self.assertEqual(bytes.fromhex(report["zero_capture_hex"]), session.raw_capture)
            self.assertEqual(session.writes, [4])
            self.assertEqual(session.state["enable_successes"], 0)
            self.assertEqual(len(list(Path(tmp).glob("zero-capture-*.bin"))), 2)

    def test_one_shot_write_readback_sequence_order(self):
        with TemporaryDirectory() as tmp:
            session, report = self.make_zero(tmp)
            result = loop.execute_once(session, report, 12, Clock())
            self.assertEqual(result["result"], "OPEN_LOOP_COMPLETE_OUTPUT_OFF")
            self.assertEqual(session.writes, [4, 15])
            self.assertEqual([len(call) for call in session.calls], [9, 10, 3])
            self.assertEqual(int(session.calls[-2][6], 0), 15)
            self.assertEqual(session.calls[-1], ["-w32", hex(session.layout["request"]), "0x2"])
            self.assertTrue(session.out.joinpath("submit-open-loop-once-payload-readback-config.bin").exists())
            flattened = [arg for call in session.calls for arg in call]
            self.assertFalse(set(flattened) & {"-rst", "-halt", "-d", "-e", "-run"})
            with self.assertRaises(RuntimeError):
                loop.submit(session, 15, "second-power-forbidden")

    def test_bad_readback_never_commits_power(self):
        with TemporaryDirectory() as tmp:
            session, report = self.make_zero(tmp)
            session.bad_echo = True
            with self.assertRaisesRegex(RuntimeError, "读回"):
                loop.execute_once(session, report, 12, Clock())
            self.assertEqual(session.writes, [4])
            self.assertEqual([len(call) for call in session.calls], [9, 10])

    def test_health_refusal_no_write(self):
        for changes in [{"moe": True}, {"halted": True}, {"bus_v": 11.6}, {"position_valid": False},
                        {"trip.causes": 2}, {"enable_attempts": 1}, {"guard_running": True}, {"adc_irq": False}]:
            with self.subTest(changes=changes), TemporaryDirectory() as tmp:
                session = FakeSession(tmp)
                session.state.update(changes)
                with self.assertRaises(RuntimeError):
                    loop.zero(session, 12)
                self.assertEqual(session.calls, [])

    def test_motion_refusal_no_write(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp)
            session.motion = True
            with self.assertRaisesRegex(RuntimeError, "静止"):
                loop.zero(session, 12)
            self.assertEqual(session.calls, [])

    def test_transient_uie_zero_passes_but_control_time_stall_rejected(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp)
            session.state["pwm"]["update_irq_enabled"] = False
            loop.preflight(session, "uie-transient", 12)
            self.assertEqual(session.calls, [])
            self.assertFalse(session.state["pwm"]["update_irq_enabled"])
            session.control_stalled = True
            with self.assertRaisesRegex(RuntimeError, "ControlTime"):
                loop.preflight(session, "control-stopped", 12)
            self.assertEqual(session.calls, [])

    def recovery_fixture(self, root):
        original, anchor = self.make_zero(root / "original")
        original.history["zero-after-2"][0]["pwm"]["update_irq_enabled"] = False
        original.action = "zero"
        loop.submit(original, 1, "exception-safe-off-only")
        cleanup, _ = original.read("exception-after-safe-off")
        failure = {"result": "FAILED_NO_RETRY", "action": "zero", "error": "Unexpected PWM update IRQ ownership",
            "write_attempted": True, "automatic_retry": False, "cleanup_request_sequence": 2, "cleanup": cleanup}
        path = original.out / "summary.json"
        path.write_text(json.dumps(failure), encoding="utf-8")
        (original.out / "identity.json").write_text(json.dumps({"hex_sha256": original.hex_sha256}), encoding="utf-8")
        for name, command, sequence, duration in [("submit-zero-once.log", 4, 1, 2000),
                                                ("exception-safe-off-only.log", 1, 2, 0)]:
            (original.out / name).write_text("w @0x20000004: 0x%08x 0x00000000 0x%08x 0x3f000000\nw @0x20000000: 0x%08x\n" %
                (command, duration, sequence), encoding="utf-8")
        live = FakeSession(root / "live")
        live.state, live.raw_capture, live.mailbox = deepcopy(original.state), original.raw_capture, deepcopy(original.mailbox)
        live.last_sequence, live.action = 2, "execute"
        return original, live, path

    def test_recover_zero_read_only_then_real_safe_off_anchor_executes_once(self):
        with TemporaryDirectory() as tmp:
            original, live, path = self.recovery_fixture(Path(tmp))
            def read_archived(reader, tag):
                return deepcopy(original.history[tag.removeprefix("recovery-original-")])
            with patch.object(REAL_SESSION, "read", read_archived):
                recovered = loop.recover_zero(live, path, 12)
            self.assertEqual(live.calls, [])
            self.assertEqual(live.writes, [])
            self.assertEqual(recovered["request_sequence"], 1)
            self.assertEqual(recovered["final"]["mailbox_command"], 1)
            self.assertEqual(recovered["final"]["command_sequence"], 2)
            self.assertEqual(recovered["final"]["active_request_sequence"], 1)
            self.assertEqual(recovered["final"]["result"], 2)
            result = loop.execute_once(live, recovered, 12, Clock())
            self.assertEqual(result["request_sequence"], 3)
            self.assertEqual(result["zero_request_sequence"], 1)
            self.assertEqual(live.writes, [15])

    def test_recovery_rejects_unrelated_failure_or_changed_evidence_without_write(self):
        for mutation in ["failure", "original-stage", "capture", "extra-write", "live-counter", "live-capture", "live-time"]:
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                original, live, path = self.recovery_fixture(Path(tmp))
                if mutation == "failure":
                    failure = json.loads(path.read_text(encoding="utf-8"))
                    failure["error"] = "different error"
                    path.write_text(json.dumps(failure), encoding="utf-8")
                elif mutation == "original-stage":
                    original.history["zero-after-1"][0]["stage_attempts"] = 1
                elif mutation == "capture":
                    original.history["zero-after-2"][1]["capture"] = bytes(80)
                elif mutation == "extra-write":
                    (original.out / "another.log").write_text("w @0x20000000: 0x00000003\n", encoding="utf-8")
                elif mutation == "live-counter":
                    live.state["guard_irq_count"] += 1
                elif mutation == "live-capture":
                    live.raw_capture = bytes(80)
                else:
                    live.control_stalled = True
                def read_archived(reader, tag):
                    return deepcopy(original.history[tag.removeprefix("recovery-original-")])
                with patch.object(REAL_SESSION, "read", read_archived), self.assertRaises(RuntimeError):
                    loop.recover_zero(live, path, 12)
                self.assertEqual(live.calls, [])
                self.assertEqual(live.writes, [])

    def test_zero_limits_and_partial_windows(self):
        for changes in [{"statistics.phase_b.mean_a": 0.101, "statistics.phase_b.max_a": 0.11},
                        {"statistics.phase_c.min_a": -0.051}, {"statistics.sample_count": 4095},
                        {"statistics.last_sample_time": 8291}, {"sequence": 3}]:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                stats = loop.current.decode_capture(capture(changes=changes), CURRENT_LAYOUT, 1)
                loop.zero_bounds(stats)

    def test_anchor_rejects_changed_boot_request_offset_or_raw(self):
        for mutation in ["request", "raw", "statistics", "hex", "time", "counter"]:
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                session, report = self.make_zero(tmp)
                if mutation == "request":
                    session.state["mailbox_sequence"] += 1
                elif mutation == "raw":
                    session.raw_capture = capture(changes={"statistics.phase_b.mean_a": -0.013})
                elif mutation == "statistics":
                    report["statistics"]["phase_b.mean_a"] = 0
                elif mutation == "hex":
                    report["hex_sha256"] = "different"
                elif mutation == "time":
                    session.state["uw_tick"] = 0
                else:
                    session.state["stage_attempts"] += 1
                with self.assertRaises(RuntimeError):
                    loop.execute_once(session, report, 12, Clock())
                self.assertEqual(session.writes, [4])

    def test_timeout_does_not_repeat_power(self):
        with TemporaryDirectory() as tmp:
            session, report = self.make_zero(tmp)
            session.timeout = True
            with self.assertRaisesRegex(RuntimeError, "6 秒"):
                loop.execute_once(session, report, 12, Clock())
            self.assertEqual(session.writes, [4, 15])
            self.assertEqual([len(call) for call in session.calls], [9, 10, 3])

    def test_failure_main_cleanup_once_and_no_replay(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            session, anchor = self.make_zero(folder / "fake")
            zero_path = folder / "zero.json"
            zero_path.write_text(json.dumps(anchor), encoding="utf-8")
            def failed(s, a, v):
                loop.submit(s, 15, "power")
                raise RuntimeError("host timeout")
            with patch.object(loop, "Session", return_value=session), patch.object(loop, "execute_once", side_effect=failed):
                with self.assertRaisesRegex(RuntimeError, "host timeout"):
                    loop.main(["--output", str(folder / "new"), "--action", "execute", "--prepared", "fake.json",
                        "--zero-summary", str(zero_path), "--vbus-dmm-v", "12", "--bench-confirmed"])
            self.assertEqual(session.writes, [4, 15, 1])
            self.assertEqual(sum(len(call) == 3 for call in session.calls), 1)

    def test_write_whitelist_blocks_other_commands_and_direct_sequence(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp)
            session.action = "execute"
            for command in [2, 3, 5, 6, 7, 8, 9, 10, 14, 15]:
                with self.subTest(command=command), self.assertRaises(RuntimeError):
                    session.execute("bad", ["-w32", hex(session.layout["request"] + 4), hex(command), "0x0",
                        "0x0", hex(loop.float_bits(0.5)), "-w32", hex(session.layout["request"]), "0x1"])
            with self.assertRaises(RuntimeError):
                session.execute("commit-without-readback", ["-w32", hex(session.layout["request"]), "0x1"])
            self.assertEqual(session.calls, [])

    def test_snapshot_odd_and_changed_sequence_not_coherent(self):
        with TemporaryDirectory() as tmp:
            session, report = self.make_zero(tmp)
            loop.execute_once(session, report, 12, Clock())
            raw = bytearray(session.result)
            struct.pack_into("<I", raw, 0, 7)
            session.result = bytes(raw)
            self.assertFalse(loop.read_result(session, "odd")["coherent"])
            struct.pack_into("<I", raw, 0, 8)
            session.result = bytes(raw)
            real_upload = session.upload
            def changed(tag, blocks):
                result = real_upload(tag, blocks)
                if tag.endswith("sequence-after"):
                    result["sequence"] = struct.pack("<I", 10)
                return result
            with patch.object(session, "upload", side_effect=changed):
                self.assertFalse(loop.read_result(session, "changed")["coherent"])


if __name__ == "__main__":
    unittest.main()
