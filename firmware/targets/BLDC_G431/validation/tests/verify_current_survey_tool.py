"""有限电流往返主机工具检查；不连接设备，不证明实物收敛。"""
from copy import deepcopy
from pathlib import Path
import math
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import current_survey_once as survey
import verify_open_loop_tool as base
import verify_current_hold_tool as hold_base
import verify_angle_survey_tool as angle_base

loop, hold = survey.loop, survey.hold


class FakeSession(hold_base.FakeSession):
    power_command, power_duration_us = 18, 5_500_000

    def __init__(self, output):
        super().__init__(output)
        self.payload_parameters = survey.parameters(0.4)
        self.survey_layout = {"angle_survey":0x20000700,"windows_offset":24,"window_size":48,
            **{"window_"+k:v for k,v in angle_base.WINDOW_OFFSETS.items()}}
        self.layout = dict(self.layout, config=0x20000500)
        self.raw_survey = bytes(168)

    def execute(self, tag, arguments):
        result = base.FakeSession.execute(self, tag, arguments)
        if len(arguments) == 3 and 18 in self.writes:
            self.state["active_request_sequence"] = self.last_sequence
            for key, delta in [("enable_attempts", 1), ("enable_successes", 1),
                               ("stage_attempts", 50001), ("stage_successes", 50001)]:
                self.state[key] += delta
            stats = loop.current.decode_capture(self.raw_capture, base.CURRENT_LAYOUT, loop.common.word(self.raw_capture, 8))
            values = [6, self.last_sequence, 0, 1, 50002, 50001, 7500, 0,
                self.payload_parameters[0], 0, stats["phase_b.mean_a"], stats["phase_c.mean_a"],
                self.payload_parameters[0], 0, self.payload_parameters[0]+0.02, 1.4, 0.40, 0.60, 1.2, 0, 0.002]
            self.hold_result = struct.pack("<8I13f", *values)
            value = angle_base.observation((0.98, (0.98 + 1/7) % 1, 0.98))
            value.update(request_sequence=self.last_sequence, voltage_v=self.payload_parameters[0], excursion_turns=1.0)
            self.raw_survey = angle_base.encode(value)
        return result

    def upload(self, tag, blocks):
        if blocks[0][0] == "survey":
            return {"survey": self.raw_survey}
        return super().upload(tag, blocks)


class Tests(unittest.TestCase):
    def test_default_offline(self):
        with TemporaryDirectory() as tmp, patch.object(survey, "prepare", return_value={}) as prepare, patch.object(survey, "Session") as access:
            survey.main(["--output", str(Path(tmp) / "new")])
            prepare.assert_called_once(); access.assert_not_called()

    def test_bad_current_or_missing_confirmation_never_connects(self):
        for args in [["--current-reference-a", "0"], ["--current-reference-a", "-0.06"],
                     ["--current-reference-a", "0.501"], ["--current-reference-a", "nan"],
                     ["--action", "zero", "--prepared", "x"],
                     ["--action", "execute", "--prepared", "x", "--zero-summary", "x", "--bench-confirmed", "--vbus-dmm-v", "nan"]]:
            with TemporaryDirectory() as tmp, patch.object(survey, "Session") as access:
                with self.assertRaises(RuntimeError): survey.main(["--output", str(Path(tmp) / "new"), *args])
                access.assert_not_called()

    def test_old_candidate_never_reads_identity(self):
        with patch.object(loop, "load_prepared", return_value=({}, Path("x"))), patch.object(loop.Session, "__init__") as access:
            with self.assertRaises(RuntimeError): survey.Session(Path("out"), Path("old"), Path("cli"), "execute", 0.06)
            access.assert_not_called()

    def test_unique_current_request_and_three_windows(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp); anchor = loop.zero(session, 12); session.action = "execute"
            r = survey.execute_once(session, anchor, 12, clock=base.Clock())
            self.assertEqual(session.writes, [4, 18])
            self.assertTrue(r["combined_screen_passed"])
            self.assertTrue(r["tracking"]["tracking_screen_passed"])
            self.assertEqual(r["survey"]["current_reference_a"], session.payload_parameters[0])
            self.assertNotIn("voltage_v", r["survey"])
            self.assertFalse(r["mapping"]["calibration_applied"])
            self.assertFalse(r["final"]["moe"])

    def test_bad_echo_never_commits(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp); anchor = loop.zero(session, 12)
            session.action = "execute"; session.bad_echo = True
            with self.assertRaises(RuntimeError): survey.execute_once(session, anchor, 12, clock=base.Clock())
            self.assertEqual(session.writes, [4])

    def test_shape_tracking_and_mapping_are_independent(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp); anchor = loop.zero(session, 12); session.action = "execute"
            good = survey.execute_once(session, anchor, 12, clock=base.Clock())
            for field, value in [("sample_count", 30002), ("window_samples", 5000),
                                 ("max_voltage_v", 1.801), ("peak_abs_current_a", 0.65), ("mean_alpha_a", math.nan)]:
                bad = deepcopy(good["current_hold"]); bad[field] = value
                with self.assertRaises(RuntimeError):
                    hold.validate_result(bad, session, anchor["statistics"], counts=(50002, 50001, 7500), references=(session.payload_parameters[0], 0.0), voltage_limit_v=1.8, current_limit_a=0.65)
            bad = deepcopy(good["survey"]); bad["windows"][2]["mean_position_turns"] += 0.01
            self.assertFalse(survey.angle.analyze(bad)["pole_pairs_confirmed"])
            bad = deepcopy(good["current_hold"]); bad["mean_alpha_a"] = 0.03
            self.assertFalse(hold.validate_result(bad, session, anchor["statistics"], counts=(50002, 50001, 7500), references=(session.payload_parameters[0], 0.0), voltage_limit_v=1.8, current_limit_a=0.65)["tracking_screen_passed"])

    def test_tracking_screen_scales_before_hardware_trial(self):
        self.assertEqual(survey.tracking_limits(0.06), (0.01,0.03))
        self.assertAlmostEqual(survey.tracking_limits(0.4)[0],0.02)
        self.assertAlmostEqual(survey.tracking_limits(0.4)[1],0.05)
        with TemporaryDirectory() as tmp:
            session=FakeSession(tmp);anchor=loop.zero(session,12);session.action="execute"
            report=survey.execute_once(session,anchor,12,clock=base.Clock())
            good=deepcopy(report["current_hold"]);good["mean_alpha_a"]=0.382;good["peak_tracking_error_a"]=0.048
            def checked(v):
                return hold.validate_result(v,session,anchor["statistics"],counts=(50002,50001,7500),
                    references=(session.payload_parameters[0],0.0),voltage_limit_v=1.8,current_limit_a=0.65,
                    tracking_limits=survey.tracking_limits(session.payload_parameters[0]))["tracking_screen_passed"]
            self.assertTrue(checked(good))
            self.assertFalse(checked(good|{"mean_alpha_a":0.379}))
            self.assertFalse(checked(good|{"peak_tracking_error_a":0.051}))

    def test_failure_only_one_safe_off(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); session = FakeSession(root / "live"); anchor = loop.zero(session, 12)
            loop.common.save(root / "zero.json", anchor); session.action = "execute"
            with patch.object(survey, "Session", return_value=session), patch.object(hold, "read_result", side_effect=RuntimeError("timeout")):
                with self.assertRaises(RuntimeError):
                    survey.main(["--output", str(root / "new"), "--action", "execute", "--prepared", "x", "--zero-summary", str(root / "zero.json"), "--bench-confirmed", "--vbus-dmm-v", "12"])
            self.assertEqual(session.writes, [4, 18, 1])
            self.assertFalse(session.state["moe"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
