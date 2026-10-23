"""有限转子DQ主机工具检查；真实写白名单配合CLI/目标内存替身。"""
from copy import deepcopy
import math
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'tools'))
import rotor_current_once as rotor
import verify_open_loop_tool as base

loop=rotor.loop


class FakeSession(base.FakeSession):
    power_command,power_duration_us=19,1500000

    def __init__(self,output,iq=0.1):
        super().__init__(output)
        self.payload_parameters=rotor.parameters(iq,0.125)
        self.rotor_layout={'result':0x20000600,**{'field_'+n:4*i for i,n in enumerate(rotor.FIELDS)}}
        self.raw_result=bytes(rotor.SIZE)

    def execute(self,tag,args):
        result=super().execute(tag,args)
        if len(args)==3 and 19 in self.writes:
            self.state.update(active_request_sequence=self.last_sequence)
            for k,v in [('stage_attempts',10001),('stage_successes',10001),('enable_attempts',1),('enable_successes',1)]:
                self.state[k]+=v
            iq,offset=self.payload_parameters;sign=1 if iq>0 else -1
            values=[6,self.last_sequence,0,1,10002,10001,2000,0,iq,offset,0,iq,0.02,0.8,0.13,0.45,0.55,
                0.25,(0.25+sign*0.4)%1,sign*0.4,60,sign*60,sign*60,0.001]
            self.raw_result=struct.pack('<8I16f',*values)
        return result

    def upload(self,tag,blocks):
        if blocks[0][0]=='rotor':
            return {'rotor':self.raw_result,'stop':struct.pack('<I',self.state['stop_reason']),'bdtr':bytes(4),'dhcsr':bytes(4)}
        if blocks[0][0]=='sequence':return {'sequence':self.raw_result[:4]}
        return super().upload(tag,blocks)


class Tests(unittest.TestCase):
    def test_default_offline(self):
        with TemporaryDirectory() as tmp,patch.object(rotor,'Session') as access,patch.object(rotor,'prepare',return_value={}) as prepare:
            rotor.main(['--output',str(Path(tmp)/'new')]);prepare.assert_called_once();access.assert_not_called()

    def test_bad_parameters_or_missing_confirmation_never_connect(self):
        for args in [['--iq-a','0'],['--iq-a','0.201'],['--iq-a','-0.201'],['--iq-a','nan'],
            ['--action','zero','--prepared','x'],['--action','execute','--prepared','x','--bench-confirmed','--vbus-dmm-v','12']]:
            with TemporaryDirectory() as tmp,patch.object(rotor,'Session') as access:
                with self.assertRaises(RuntimeError):rotor.main(['--output',str(Path(tmp)/'new'),*args])
                access.assert_not_called()
        for offset in [-0.1,1.0,math.nan]:
            with self.assertRaises(RuntimeError):rotor.parameters(0.1,offset)

    def test_mapping_must_pass_original_screens(self):
        good={'trial':{'combined_screen_passed':True,'mapping':{'pole_pairs_confirmed':True,'nominal_pole_pairs':7,
            'encoder_direction_candidate':1,'return_error_mechanical_turns':0.001,
            'max_offset_residual_electrical_turns':0.01,'offset_candidate_electrical_turns':0.125}}}
        self.assertEqual(rotor.mapping_candidate(good),{'pole_pairs':7,'encoder_direction':1,'electrical_offset_turns':0.125})
        for key,value in [('nominal_pole_pairs',6),('encoder_direction_candidate',-1),('pole_pairs_confirmed',False),
            ('return_error_mechanical_turns',0.004),('max_offset_residual_electrical_turns',0.04)]:
            bad=deepcopy(good);bad['trial']['mapping'][key]=value
            with self.assertRaises(RuntimeError):rotor.mapping_candidate(bad)

    def test_old_candidate_never_reads_board(self):
        with patch.object(loop,'load_prepared',return_value=({},Path('x'))),patch.object(loop.Session,'__init__') as read:
            with self.assertRaises(RuntimeError):rotor.Session(Path('out'),Path('old'),Path('cli'),'execute',0.1)
            read.assert_not_called()

    def test_positive_and_negative_are_unique_signed_requests(self):
        for iq in [0.1,-0.1]:
            with TemporaryDirectory() as tmp:
                s=FakeSession(tmp,iq);anchor=loop.zero(s,12);s.action='execute'
                r=rotor.execute_once(s,anchor,12,clock=base.Clock())
                self.assertEqual(s.writes,[4,19]);self.assertEqual([len(c) for c in s.calls],[9,10,3])
                self.assertEqual(struct.unpack('<2f',s.config),s.payload_parameters)
                self.assertTrue(r['analysis']['tracking_screen_passed']);self.assertTrue(r['analysis']['motion_direction_screen_passed'])
                self.assertFalse(r['final']['moe']);self.assertFalse(r['analysis']['calibration_applied'])

    def test_echo_mismatch_never_commits(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute';s.bad_echo=True
            with self.assertRaises(RuntimeError):rotor.execute_once(s,anchor,12,clock=base.Clock())
            self.assertEqual(s.writes,[4])

    def test_old_zero_anchor_is_rejected_before_power(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute'
            anchor['hex_sha256']='other'
            with self.assertRaises(RuntimeError):rotor.execute_once(s,anchor,12,clock=base.Clock())
            self.assertEqual(s.writes,[4])

    def test_limits_and_tracking_and_direction_are_separate(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute'
            good=rotor.execute_once(s,anchor,12,clock=base.Clock())['rotor_current']
            for key,value in [('window_samples',1999),('max_voltage_v',1.81),('peak_abs_current_a',0.65),
                ('max_abs_velocity_rpm',301),('max_position_age_s',0.002),('mean_id_a',math.nan),('electrical_offset_turns',0.2)]:
                with self.assertRaises(RuntimeError):rotor.analyze(good|{key:value},s.payload_parameters)
            self.assertFalse(rotor.analyze(good|{'mean_iq_a':0.05},s.payload_parameters)['tracking_screen_passed'])
            self.assertFalse(rotor.analyze(good|{'displacement_turns':-0.4},s.payload_parameters)['motion_direction_screen_passed'])
            boundary=rotor.analyze(good|{'result':23,'stop_reason':23,'window_samples':0},s.payload_parameters)
            self.assertTrue(boundary['motion_boundary_reached'])
            self.assertFalse(boundary['tracking_window_complete'])
            self.assertIsNone(boundary['mean_dq_error_nominal_a'])
            self.assertIsNone(boundary['tracking_screen_passed'])

    def test_failure_sends_only_one_safe_off(self):
        with TemporaryDirectory() as tmp:
            p=Path(tmp);s=FakeSession(p/'live');anchor=loop.zero(s,12);s.action='execute'
            loop.common.save(p/'zero.json',anchor)
            with patch.object(rotor,'Session',return_value=s),patch.object(rotor,'read_result',side_effect=RuntimeError('timeout')):
                with self.assertRaises(RuntimeError):rotor.main(['--output',str(p/'new'),'--action','execute',
                    '--prepared','x','--zero-summary',str(p/'zero.json'),'--bench-confirmed','--vbus-dmm-v','12'])
            self.assertEqual(s.writes,[4,19,1]);self.assertFalse(s.state['moe'])


if __name__=='__main__':unittest.main(verbosity=1)
