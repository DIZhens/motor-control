"""有限速度主机事务检查；外部CLI和内存替身，不连接设备。"""
from copy import deepcopy
import json
import math
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import rotor_speed_once as speed
import verify_rotor_current_tool as current
import verify_open_loop_tool as base

loop=speed.loop


class FakeSession(current.FakeSession):
    power_command,power_duration_us=20,2000000
    snapshot_size,result_fields=speed.SIZE,speed.FIELDS

    def __init__(self,output,rpm=60):
        super().__init__(output)
        self.payload_parameters=speed.parameters(rpm,0.125)
        self.rotor_layout={'result':0x20000700,'bus_failure':0x20000800,**{'field_'+n:4*i for i,n in enumerate(speed.FIELDS)}}
        self.raw_result=bytes(speed.SIZE)
        self.raw_bus=bytes(speed.BUS_SIZE)

    def upload(self,tag,blocks):
        if blocks[0][0]=='bus_sequence':return {'bus_sequence':self.raw_bus[:4]}
        if blocks[0][0]=='bus_failure':return {'bus_failure':self.raw_bus}
        return super().upload(tag,blocks)

    def execute(self,tag,args):
        result=super().execute(tag,args)
        if len(args)==3 and 20 in self.writes:
            self.state.update(active_request_sequence=self.last_sequence)
            for k,v in [('stage_attempts',15001),('stage_successes',15001),('enable_attempts',1),('enable_successes',1)]:self.state[k]+=v
            rpm,offset=self.payload_parameters;sign=1 if rpm>0 else -1
            values=[6,self.last_sequence,0,1,15002,15001,4000,0,rpm,offset,0,sign*0.03,0.02,0.4,0.045,0.45,0.55,
                0.25,(0.25+sign*1.2)%1,sign*1.2,65,sign*5,sign*59,0.001,sign*0.03,sign,5,sign*0.01,1500,0,400,0]
            self.raw_result=struct.pack('<8I20f4I',*values)
        return result


class Tests(unittest.TestCase):
    def test_default_offline(self):
        with TemporaryDirectory() as tmp,patch.object(speed,'prepare',return_value={}) as prepare,patch.object(speed,'Session') as access:
            speed.main(['--output',str(Path(tmp)/'new')]);prepare.assert_called_once();access.assert_not_called()

    def test_invalid_target_and_missing_confirmation_never_connect(self):
        for args in [['--velocity-rpm','0'],['--velocity-rpm','9'],['--velocity-rpm','80.1'],['--velocity-rpm','-80.1'],
            ['--velocity-rpm','nan'],['--action','zero','--prepared','x'],['--action','execute','--prepared','x','--bench-confirmed','--vbus-dmm-v','12']]:
            with TemporaryDirectory() as tmp,patch.object(speed,'Session') as access:
                with self.assertRaises(RuntimeError):speed.main(['--output',str(Path(tmp)/'new'),*args])
                access.assert_not_called()

    def test_old_candidate_never_reads_board(self):
        with patch.object(loop,'load_prepared',return_value=({},Path('x'))),patch.object(speed.rotor.Session,'__init__') as read:
            with self.assertRaises(RuntimeError):speed.Session(Path('out'),Path('old'),Path('cli'),'execute',60)
            read.assert_not_called()

    def test_signed_target_unique_commit_and_complete_results(self):
        for rpm in [60,-60]:
            with TemporaryDirectory() as tmp:
                s=FakeSession(tmp,rpm);anchor=loop.zero(s,12);s.action='execute'
                r=speed.execute_once(s,anchor,12,clock=base.Clock())
                self.assertEqual(s.writes,[4,20]);self.assertEqual([len(c) for c in s.calls],[9,10,3])
                self.assertEqual(struct.unpack('<2f',s.config),s.payload_parameters)
                for k in ['normal_completion','current_tracking_screen_passed','speed_tracking_screen_passed','motion_direction_screen_passed','deceleration_screen_passed']:
                    self.assertTrue(r['analysis'][k],k)
                self.assertFalse(r['final']['moe']);self.assertFalse(r['analysis']['calibration_applied'])

    def test_echo_mismatch_never_commits(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute';s.bad_echo=True
            with self.assertRaises(RuntimeError):speed.execute_once(s,anchor,12,clock=base.Clock())
            self.assertEqual(s.writes,[4])

    def test_wrong_zero_never_powers(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute';anchor['hex_sha256']='other'
            with self.assertRaises(RuntimeError):speed.execute_once(s,anchor,12,clock=base.Clock())
            self.assertEqual(s.writes,[4])

    def test_counts_and_bounds_rejected(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute'
            good=speed.execute_once(s,anchor,12,clock=base.Clock())['rotor_speed']
            for k,v in [('speed_update_count',1499),('speed_window_updates',399),('sample_count',10002),('window_samples',0),
                ('last_iq_reference_a',0.11),('max_voltage_v',1.81),('peak_abs_current_a',0.65),('mean_id_a',math.nan)]:
                with self.assertRaises(RuntimeError):speed.analyze(good|{k:v},s.payload_parameters)

    def test_speed_current_direction_and_empty_window_separate(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute'
            good=speed.execute_once(s,anchor,12,clock=base.Clock())['rotor_speed']
            self.assertFalse(speed.analyze(good|{'mean_speed_error_rpm':7},s.payload_parameters)['speed_tracking_screen_passed'])
            self.assertFalse(speed.analyze(good|{'mean_iq_a':0.05},s.payload_parameters)['current_tracking_screen_passed'])
            self.assertFalse(speed.analyze(good|{'displacement_turns':-1.2},s.payload_parameters)['motion_direction_screen_passed'])
            self.assertFalse(speed.analyze(good|{'last_velocity_rpm':20},s.payload_parameters)['deceleration_screen_passed'])
            partial=speed.analyze(good|{'result':23,'stop_reason':23,'window_samples':0,'speed_window_updates':0},s.payload_parameters)
            self.assertIsNone(partial['current_tracking_screen_passed']);self.assertIsNone(partial['speed_tracking_screen_passed'])
            self.assertIsNone(partial['mean_dq_error_nominal_a'])

    def test_failure_one_safe_off_only(self):
        with TemporaryDirectory() as tmp:
            p=Path(tmp);s=FakeSession(p/'live');anchor=loop.zero(s,12);s.action='execute';loop.common.save(p/'zero.json',anchor)
            with patch.object(speed,'Session',return_value=s),patch.object(speed.rotor,'read_result',side_effect=RuntimeError('timeout')):
                with self.assertRaises(RuntimeError):speed.main(['--output',str(p/'new'),'--action','execute',
                    '--prepared','x','--zero-summary',str(p/'zero.json'),'--bench-confirmed','--vbus-dmm-v','12'])
            self.assertEqual(s.writes,[4,20,1]);self.assertFalse(s.state['moe'])

    def test_bad_bus_terminal_result_preserved_before_safe_off(self):
        with TemporaryDirectory() as tmp:
            p=Path(tmp);s=FakeSession(p/'live');anchor=loop.zero(s,12);s.action='execute';loop.common.save(p/'zero.json',anchor)
            original=s.execute
            def bad_bus(tag,args):
                result=original(tag,args)
                if len(args)==3 and 20 in s.writes:
                    raw=bytearray(s.raw_result);struct.pack_into('<I',raw,12,7);s.raw_result=bytes(raw)
                    s.state.update(result=7,stop_reason=7)
                    raw=bytearray(speed.BUS_SIZE);struct.pack_into('<4I2Qf3IQ10I',raw,0,
                        2,2,2,1,1000,980,0,0,1,0,980,4,1,1,1400,1500,2,0,16,0,0)
                    s.raw_bus=bytes(raw)
                return result
            s.execute=bad_bus
            def connect(output,*args):
                output.mkdir()
                return s
            with patch.object(speed,'Session',side_effect=connect):
                with self.assertRaisesRegex(RuntimeError,'结果码=7'):
                    speed.main(['--output',str(p/'new'),'--action','execute','--prepared','x',
                        '--zero-summary',str(p/'zero.json'),'--bench-confirmed','--vbus-dmm-v','12'])
            report=json.loads((p/'new/summary.json').read_text(encoding='utf-8'))
            self.assertEqual(report['rotor_speed']['result'],7)
            self.assertEqual(report['rotor_speed']['stop_reason'],7)
            self.assertEqual(report['bus_failure']['failed_conditions'],['invalid'])
            self.assertEqual(report['bus_failure']['acquisition_cause'],'adc_error')
            self.assertTrue(report['bus_failure']['diagnostic_matches_publication'])
            self.assertTrue(report['bus_failure']['matches_terminal_request'])
            self.assertTrue(report['bus_failure']['vbus_raw_captured'])
            self.assertFalse(report['bus_failure']['vrefint_raw_captured'])
            self.assertEqual(report['cleanup']['stop_reason'],2)
            self.assertEqual(s.writes,[4,20,1]);self.assertFalse(s.state['moe'])

    def test_bus_nonfinite_voltage_keeps_raw_bits_and_unrecorded_is_explicit(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);self.assertFalse(speed.read_bus_failure(s,'empty')['recorded'])
            raw=bytearray(speed.BUS_SIZE);struct.pack_into('<4I',raw,0,2,2,4,1)
            struct.pack_into('<I',raw,32,0x7fc00000);s.raw_bus=bytes(raw)
            result=speed.read_bus_failure(s,'nonfinite')
            self.assertIsNone(result['voltage_v']);self.assertEqual(result['voltage_bits'],0x7fc00000)
            self.assertEqual(result['failed_conditions'],['nonfinite'])


if __name__=='__main__':unittest.main(verbosity=1)
