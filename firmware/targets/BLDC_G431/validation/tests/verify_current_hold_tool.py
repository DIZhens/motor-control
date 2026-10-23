"""电流环主机白名单、先读回后提交和失败关断的替身检查；不连接设备。"""
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
import current_hold_once as hold
import verify_open_loop_tool as baseline

loop = hold.loop


class FakeSession(baseline.FakeSession):
    power_command, power_duration_us = 17, 3500000

    def __init__(self, output):
        super().__init__(output)
        self.payload_parameters = hold.parameters(0.06, 0)
        self.hold_layout = {"result":0x20000600, **{"field_"+n:4*i for i,n in enumerate(hold.FIELDS)}}
        self.hold_result = bytes(hold.SIZE)

    def execute(self, tag, arguments):
        result = super().execute(tag, arguments)
        if len(arguments) == 3 and 17 in self.writes:
            self.state.update(active_request_sequence=self.last_sequence)
            for key,delta in [("enable_attempts",1),("enable_successes",1),("stage_attempts",30001),("stage_successes",30001)]:
                self.state[key] += delta
            stats = loop.current.decode_capture(self.raw_capture, baseline.CURRENT_LAYOUT, loop.common.word(self.raw_capture,8))
            values = [6,self.last_sequence,0,1,30002,30001,5000,0,*self.payload_parameters,
                stats["phase_b.mean_a"],stats["phase_c.mean_a"],*self.payload_parameters,0.08,0.3,0.48,0.52,0.2,0.,0.002]
            self.hold_result = struct.pack("<8I13f",*values)
        return result

    def upload(self, tag, blocks):
        if blocks[0][0] == "hold":
            return {"hold":self.hold_result,"stop":struct.pack("<I",self.state["stop_reason"]),"bdtr":bytes(4),"dhcsr":bytes(4)}
        if blocks[0][0] == "sequence":
            return {"sequence":self.hold_result[:4]}
        return super().upload(tag, blocks)


class Tests(unittest.TestCase):
    def test_default_is_offline(self):
        with TemporaryDirectory() as tmp, patch.object(hold,"prepare",return_value={}) as prepare, patch.object(hold,"Session") as access:
            hold.main(["--output",str(Path(tmp)/"new")])
            prepare.assert_called_once(); access.assert_not_called()

    def test_bad_parameters_and_missing_confirmation_reject_before_connection(self):
        for args in [["--alpha-reference-a","nan"],["--alpha-reference-a","0"],
            ["--alpha-reference-a","0.081"],["--beta-reference-a","0.07"],
            ["--action","zero","--prepared","x"],
            ["--action","execute","--prepared","x","--zero-summary","x","--bench-confirmed","--vbus-dmm-v","nan"]]:
            with TemporaryDirectory() as tmp, patch.object(hold,"Session") as access:
                with self.assertRaises(RuntimeError): hold.main(["--output",str(Path(tmp)/"new"),*args])
                access.assert_not_called()

    def test_old_image_rejected_before_identity(self):
        with patch.object(loop,"load_prepared",return_value=({},Path("x"))),patch.object(loop.Session,"__init__") as access:
            with self.assertRaises(RuntimeError): hold.Session(Path("o"),Path("old"),Path("cli"),"execute",0.06,0)
            access.assert_not_called()

    def test_zero_then_unique_current_request(self):
        with TemporaryDirectory() as tmp:
            session = FakeSession(tmp)
            anchor = loop.zero(session,12)
            session.action="execute"
            report = hold.execute_once(session,anchor,12,clock=baseline.Clock())
            self.assertEqual(session.writes,[4,17])
            self.assertEqual([len(c) for c in session.calls],[9,10,3])
            self.assertTrue(report["tracking"]["tracking_screen_passed"])
            self.assertFalse(report["tracking"]["current_gain_accuracy_confirmed"])
            self.assertFalse(report["final"]["moe"])

    def test_bad_echo_never_commits_power(self):
        with TemporaryDirectory() as tmp:
            session=FakeSession(tmp); anchor=loop.zero(session,12)
            session.action="execute"; session.bad_echo=True
            with self.assertRaises(RuntimeError): hold.execute_once(session,anchor,12,clock=baseline.Clock())
            self.assertEqual(session.writes,[4])

    def test_frozen_result_counts_parameters_voltage_and_zero_are_checked(self):
        with TemporaryDirectory() as tmp:
            session=FakeSession(tmp); anchor=loop.zero(session,12); session.action="execute"
            report=hold.execute_once(session,anchor,12,clock=baseline.Clock())
            good=report["current_hold"]; stats=anchor["statistics"]
            for key,value in [("window_samples",4999),("sample_count",30001),("enabled_samples",30000),
                ("moe",True),("halted",True),("result",18),("max_voltage_v",0.41),
                ("peak_abs_current_a",0.25),("offset_b_a",0),("alpha_reference_a",0.061),
                ("mean_beta_a",math.nan)]:
                bad=deepcopy(good); bad[key]=value
                with self.assertRaises(RuntimeError): hold.validate_result(bad,session,stats)
            bad=deepcopy(good); bad["mean_alpha_a"]=0.03
            self.assertFalse(hold.validate_result(bad,session,stats)["tracking_screen_passed"])

    def test_error_after_commit_only_sends_safe_off(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); session=FakeSession(root/"live"); anchor=loop.zero(session,12)
            loop.common.save(root/"zero.json",anchor)
            session.action="execute"
            with patch.object(hold,"Session",return_value=session),patch.object(hold,"read_result",side_effect=RuntimeError("read timeout")):
                with self.assertRaises(RuntimeError):
                    hold.main(["--output",str(root/"new"),"--action","execute","--prepared","x",
                        "--zero-summary",str(root/"zero.json"),"--bench-confirmed","--vbus-dmm-v","12"])
            self.assertEqual(session.writes,[4,17,1])
            self.assertFalse(session.state["moe"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
