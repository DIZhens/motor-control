"""静态电角往返的主机替身检查；所有外部设备访问均替换。"""
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
import angle_survey_once as survey
import verify_open_loop_tool as baseline

loop = survey.loop
WINDOW_OFFSETS = dict(zip(survey.WINDOW_FIELDS, [0, 8, 16, 24, 28, 32, 36, 40, 44]))


def observation(positions=(0.99, 0.954285714, 0.99), mask=7):
    return {"sequence": 4, "request_sequence": 2, "result": 1, "stable_window_mask": mask,
        "excursion_turns": 0.25, "voltage_v": loop.parameters(0.3, 2)[0],
        "windows": [{"sample_count": 250, "first_position_time": start, "last_position_time": start+4980,
            "mean_position_turns": p, "span_turns": 0.0002, "first_position_turns": p,
            "last_position_turns": p, "mean_nominal_b_a": 0.01, "mean_nominal_c_a": -0.02}
            for p, start in zip(positions, [25000, 55000, 85000])]}


def encode(value):
    raw = bytearray(168)
    struct.pack_into("<4I2f", raw, 0, *[value[k] for k in ["sequence", "request_sequence", "result",
        "stable_window_mask", "excursion_turns", "voltage_v"]])
    for i, window in enumerate(value["windows"]):
        for name, fmt in survey.WINDOW_FIELDS.items():
            struct.pack_into("<"+fmt, raw, 24+48*i+WINDOW_OFFSETS[name], window[name])
    return bytes(raw)


class FakeSession(baseline.FakeSession):
    power_command, power_duration_us = 16, 5500000
    power_wait_s, sample_count_max = 8.0, 60000

    def __init__(self, output):
        super().__init__(output)
        self.payload_parameters = survey.parameters(0.3, 0.25)
        self.parameters = (self.payload_parameters[0], 0.0)
        self.survey_layout = {"angle_survey": 0x20000500, "windows_offset": 24, "window_size": 48,
            **{"window_"+k:v for k,v in WINDOW_OFFSETS.items()}}
        self.raw_survey = encode(observation())
        self.corrupt_survey = False

    def execute(self, tag, arguments):
        result = super().execute(tag, arguments)
        if len(arguments) == 3 and 16 in self.writes:
            value = observation()
            value["request_sequence"] = self.last_sequence
            self.raw_survey = encode(value)
        return result

    def upload(self, tag, blocks):
        if blocks[0][0] == "survey":
            raw = self.raw_survey
            if self.corrupt_survey and tag.endswith("2"):
                raw = b"\x05"+raw[1:]
            return {"survey":raw}
        return super().upload(tag, blocks)


class Tests(unittest.TestCase):
    def test_default_prepare_has_no_hardware_access(self):
        with TemporaryDirectory() as tmp, patch.object(survey, "prepare", return_value={}) as prepare, patch.object(survey, "Session") as access:
            survey.main(["--output", str(Path(tmp)/"new")])
            prepare.assert_called_once()
            access.assert_not_called()

    def test_parameters_and_confirmation_reject_before_access(self):
        for args in [["--excursion-turns", "0"], ["--excursion-turns", "0.251"],
                     ["--excursion-turns", "nan"], ["--voltage-v", "0.501"],
                     ["--action", "execute", "--prepared", "x", "--zero-summary", "x"],
                     ["--action", "zero", "--prepared", "x", "--bench-confirmed", "--vbus-dmm-v", "nan"]]:
            with self.subTest(args=args), TemporaryDirectory() as tmp, patch.object(survey, "Session") as access:
                with self.assertRaises(RuntimeError):
                    survey.main(["--output", str(Path(tmp)/"new"), *args])
                access.assert_not_called()

    def test_old_prepared_rejected_before_identity_read(self):
        with patch.object(loop, "load_prepared", return_value=({}, Path("x"))), patch.object(loop.Session, "__init__") as access:
            with self.assertRaisesRegex(RuntimeError, "没有静态"):
                survey.Session(Path("out"), Path("old.json"), Path("cli"), "execute", 0.3, 0.25)
            access.assert_not_called()

    def test_zero_then_one_static_request_with_readback(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp)
            anchor = loop.zero(session, 12)
            session.action = "execute"
            with patch.object(loop.time, "monotonic", baseline.Clock()):
                report = survey.execute_once(session, anchor, 12)
            self.assertEqual(session.writes, [4, 16])
            self.assertEqual([len(c) for c in session.calls], [9, 10, 3])
            self.assertEqual(loop.common.word(session.mailbox, 12), 5500000)
            self.assertEqual(struct.unpack("<2f", session.config), session.payload_parameters)
            self.assertEqual(report["result"], "ANGLE_SURVEY_COMPLETE_OUTPUT_OFF")
            self.assertTrue(report["mapping"]["mapping_candidate_consistent"])
            self.assertFalse(report["mapping"]["electrical_calibration_confirmed"])
            self.assertFalse(report["mapping"]["calibration_applied"])
            with self.assertRaises(RuntimeError):
                loop.submit(session, 16, "no-retry")
            with self.assertRaises(RuntimeError):
                loop.submit(session, 15, "no-rotation-command")

    def test_changed_payload_never_commits_sequence(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp)
            anchor = loop.zero(session, 12)
            session.action, session.bad_echo = "execute", True
            with self.assertRaisesRegex(RuntimeError, "读回"):
                survey.execute_once(session, anchor, 12)
            self.assertEqual(session.writes, [4])

    def test_frozen_result_requires_same_even_version_and_request(self):
        for mutation in ["changed", "odd", "wrong-request", "bad-count", "short-window", "nan", "span", "window-order"]:
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                session = FakeSession(tmp)
                value = observation()
                if mutation == "changed": session.corrupt_survey = True
                elif mutation == "odd": value["sequence"] = 3
                elif mutation == "wrong-request": value["request_sequence"] = 3
                elif mutation == "bad-count": value["windows"][0]["sample_count"] = 251
                elif mutation == "short-window": value["windows"][0]["last_position_time"] -= 1000
                elif mutation == "nan": value["windows"][0]["mean_position_turns"] = float("nan")
                elif mutation == "window-order":
                    value["windows"][1]["first_position_time"] = value["windows"][0]["first_position_time"]
                    value["windows"][1]["last_position_time"] = value["windows"][0]["last_position_time"]
                else: value["windows"][0]["span_turns"] = 0.003
                session.raw_survey = encode(value)
                with self.assertRaises(RuntimeError): survey.read_survey(session, 2)
                self.assertEqual(session.writes, [])

    def test_mapping_sign_return_and_static_no_motion_cannot_qualify(self):
        for positions, mask in [((0.2,0.2,0.2),7), ((0.2,0.235714,0.2),7),
                                 ((0.2,0.164286,0.21),7), ((0.2,0.164286,0.2),3)]:
            with self.subTest(positions=positions, mask=mask):
                result = survey.analyze(observation(positions, mask))
                self.assertFalse(result["mapping_candidate_consistent"])
                self.assertFalse(result["pole_pairs_confirmed"])
                self.assertFalse(result["calibration_applied"])

    def test_circular_offsets_use_explicit_direction_and_nominal_poles(self):
        result = survey.analyze(observation((0.01, (0.01-0.25/7)%1, 0.01)))
        self.assertTrue(result["mapping_candidate_consistent"])
        self.assertAlmostEqual(result["outward_mechanical_turns"], -0.25/7)
        self.assertAlmostEqual(result["offset_candidate_electrical_turns"], 0.93)
        self.assertLess(result["max_offset_residual_electrical_turns"], 1e-6)
        self.assertFalse(result["pole_pairs_confirmed"])

    def test_failure_cleanup_only_once(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); session=FakeSession(root/"live")
            anchor=loop.zero(session,12); session.action="execute"
            zero=root/"zero.json"; zero.write_text(json.dumps(anchor),encoding="utf-8")
            def fail(s,a,v):
                loop.submit(s,16,"power-once")
                raise RuntimeError("lost result")
            with patch.object(survey,"Session",return_value=session), patch.object(survey,"execute_once",side_effect=fail):
                with self.assertRaisesRegex(RuntimeError,"lost result"):
                    survey.main(["--output",str(root/"new"),"--action","execute","--prepared","x",
                        "--zero-summary",str(zero),"--bench-confirmed","--vbus-dmm-v","12"])
            self.assertEqual(session.writes,[4,16,1])
            self.assertFalse(session.state["moe"])

    def test_eight_second_timeout_cannot_repeat_power(self):
        with TemporaryDirectory() as tmp:
            session=FakeSession(tmp)
            anchor=loop.zero(session,12)
            session.action,session.timeout="execute",True
            with self.assertRaisesRegex(RuntimeError,"8 秒"):
                loop.execute_once(session,anchor,12,baseline.Clock())
            self.assertEqual(session.writes,[4,16])


if __name__ == "__main__":
    unittest.main()
