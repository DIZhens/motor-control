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
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'tools'))
import rotor_speed_once as speed
import verify_rotor_current_tool as current
import verify_open_loop_tool as base

loop=speed.loop


class FakeSession(current.FakeSession):
    power_command,power_duration_us=20,2000000
    snapshot_size,result_fields=speed.SIZE,speed.FIELDS

    def __init__(self,output,rpm=60,kp=0.001,ki=0.004):
        super().__init__(output)
        self.payload_parameters=speed.parameters(rpm,0.125,kp,ki)
        self.rotor_layout={'result':0x20000700,'bus_failure':0x20000800,'timing_failure':0x20000900,'trace':0x20001000,
            **{'field_'+n:4*i for i,n in enumerate(speed.FIELDS)}}
        self.raw_result=bytes(speed.SIZE)
        self.raw_bus=bytes(speed.BUS_SIZE)
        self.raw_timing=bytes(speed.TIMING_SIZE)
        self.raw_trace=bytes(speed.TRACE_SIZE)

    def upload(self,tag,blocks):
        if blocks[0][0]=='timing_sequence':return {'timing_sequence':self.raw_timing[:4]}
        if blocks[0][0]=='timing_failure':return {'timing_failure':self.raw_timing}
        if blocks[0][0]=='header':return {'header':self.raw_trace[:speed.TRACE_HEADER_SIZE]}
        if blocks[0][0]=='trace':return {'trace':self.raw_trace}
        if blocks[0][0]=='bus_sequence':return {'bus_sequence':self.raw_bus[:4]}
        if blocks[0][0]=='bus_failure':return {'bus_failure':self.raw_bus}
        return super().upload(tag,blocks)

    def execute(self,tag,args):
        result=super().execute(tag,args)
        if len(args)==3 and 20 in self.writes:
            self.state.update(active_request_sequence=self.last_sequence)
            for k,v in [('stage_attempts',15001),('stage_successes',15001),('enable_attempts',1),('enable_successes',1)]:self.state[k]+=v
            rpm,offset,kp,ki=self.payload_parameters;sign=1 if rpm>0 else -1
            values=[6,self.last_sequence,0,1,15002,15001,4000,0,rpm,offset,0,sign*0.03,0.02,0.4,0.045,0.45,0.55,
                0.25,(0.25+sign*1.2)%1,sign*1.2,65,sign*5,sign*59,0.001,sign*0.03,sign,5,sign*0.01,1500,0,400,0]
            self.raw_result=struct.pack('<8I20f4I',*values)
            raw=bytearray(speed.TRACE_SIZE)
            struct.pack_into('<8I2f',raw,0,2,self.last_sequence,0,1,speed.TRACE_CAPACITY,speed.TRACE_CAPACITY,4,0,kp,ki)
            for i in range(speed.TRACE_CAPACITY):
                struct.pack_into('<2I4f',raw,speed.TRACE_HEADER_SIZE+i*speed.TRACE_SAMPLE_SIZE,
                    2+80*i,2,(0.25+sign*i*0.004)%1,sign*59,sign*0.03,sign*0.03)
            self.raw_trace=bytes(raw)
        return result


class Tests(unittest.TestCase):
    def test_default_offline(self):
        with TemporaryDirectory() as tmp,patch.object(speed,'prepare',return_value={}) as prepare,patch.object(speed,'Session') as access:
            speed.main(['--output',str(Path(tmp)/'new')]);prepare.assert_called_once();access.assert_not_called()

    def test_invalid_target_and_missing_confirmation_never_connect(self):
        for args in [['--velocity-rpm','0'],['--velocity-rpm','9'],['--velocity-rpm','80.1'],['--velocity-rpm','-80.1'],
            ['--kp','0.00049'],['--kp','0.00501'],['--ki','-0.001'],['--ki','0.02001'],['--kp','nan'],
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
                self.assertEqual(s.writes,[4,20]);self.assertEqual([len(c) for c in s.calls],[9,12,3])
                self.assertEqual(struct.unpack('<4f',s.config),s.payload_parameters)
                self.assertEqual(r['trace']['header']['count'],375)
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
                    trace=bytearray(s.raw_trace);struct.pack_into('<I',trace,12,7);s.raw_trace=bytes(trace)
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

    def test_dma_failure_cause_decode_is_read_only(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp)
            for cause,label in [(5,'dma_incomplete'),(6,'dma_error')]:
                raw=bytearray(speed.BUS_SIZE);struct.pack_into('<4I',raw,0,2,2,2,1)
                struct.pack_into('<I',raw,40,1);struct.pack_into('<I',raw,56,cause);s.raw_bus=bytes(raw)
                result=speed.read_bus_failure(s,label,2)
                self.assertEqual(result['acquisition_cause'],label)
                self.assertTrue(result['matches_terminal_request']);self.assertFalse(result['vbus_raw_captured'])
            self.assertEqual(s.writes,[])

    def test_bounded_gains_are_echoed_and_legacy_payload_cannot_commit(self):
        for kp,ki in [(0.0005,0),(0.005,0.02),(0.002,0.004)]:
            with TemporaryDirectory() as tmp:
                s=FakeSession(tmp,kp=kp,ki=ki);anchor=loop.zero(s,12);s.action='execute'
                r=speed.execute_once(s,anchor,12,clock=base.Clock())
                self.assertEqual(s.payload_parameters[2:],tuple(r['controller_gains'].values()))
                self.assertEqual(s.writes,[4,20])
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);s.action='execute'
            with self.assertRaises(RuntimeError):s.execute('old-payload',['-w32',hex(s.layout['config']),'0','0',
                '-w32',hex(s.layout['request']+4),'20',hex(loop.KEY),'2000000',hex(loop.float_bits(0.5))])
            self.assertFalse(s.write_attempted)

    def test_trace_rejects_torn_wrong_request_capacity_gain_and_bad_samples_without_writes(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);anchor=loop.zero(s,12);s.action='execute'
            terminal=speed.execute_once(s,anchor,12,clock=base.Clock())['rotor_speed'];good=s.raw_trace
            mutations=[(0,'I',3),(4,'I',9),(8,'I',1),(16,'I',376),(20,'I',374),(24,'I',5),(28,'I',1),
                (32,'f',0.003),(40,'I',4),(44,'I',31),(48,'f',math.nan),(52,'f',math.inf),(56,'f',0.101)]
            writes=list(s.writes)
            for offset,fmt,value in mutations:
                raw=bytearray(good);struct.pack_into('<'+fmt,raw,offset,value);s.raw_trace=bytes(raw)
                with self.assertRaises(RuntimeError):speed.read_speed_trace(s,terminal)
                self.assertEqual(s.writes,writes)

    def test_timing_latch_is_read_only_and_cycle_wrap_is_explicit(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp)
            self.assertFalse(speed.read_timing_failure(s)['recorded'])
            fields={'sequence':2,'reason':7,'slot_sequence':10,'callback_slot':10,
                'slot_start_cycles':0xffffc000,'irq_entry_cycles':0xffffd000,'callback_start_cycles':0xffffd100,
                'consumer_entry_cycles':0xffffd200,'consumer_exit_cycles':0xfffff500,
                'auxiliary_exit_cycles':0xfffffc00,'observed_cycles':0x100,'state_bits':0xfef,'observation_time':123456}
            raw=bytearray(speed.TIMING_SIZE)
            for n,v in fields.items():
                fmt,offset=speed.TIMING_FIELDS[n];struct.pack_into('<'+fmt,raw,offset,v)
            s.raw_timing=bytes(raw);r=speed.read_timing_failure(s)
            self.assertTrue(r['callback_anchor_current']);self.assertTrue(r['irq_context_active'])
            self.assertEqual(r['reason_name'],'irq_exit');self.assertEqual(r['slot_elapsed_cycles'],0x4100)
            self.assertEqual(r['prefix_cycles'],0x200);self.assertEqual(r['consumer_cycles'],0x2300)
            self.assertEqual(r['auxiliary_cycles'],0x700);self.assertEqual(r['hal_tail_to_detection_cycles'],0x500)
            self.assertEqual(s.writes,[])
            for offset,value in [(0,3),(4,8),(44,1)]:
                bad=bytearray(raw);struct.pack_into('<I',bad,offset,value);s.raw_timing=bytes(bad)
                with self.assertRaises(RuntimeError):speed.read_timing_failure(s)
            self.assertEqual(s.writes,[])

    def test_timing_fault_before_callback_does_not_report_stale_phase_durations(self):
        with TemporaryDirectory() as tmp:
            s=FakeSession(tmp);raw=bytearray(speed.TIMING_SIZE)
            struct.pack_into('<4I',raw,0,2,2,99,98);s.raw_timing=bytes(raw)
            r=speed.read_timing_failure(s)
            self.assertFalse(r['callback_anchor_current']);self.assertIsNone(r['consumer_cycles'])
            self.assertIsNone(r['irq_hook_to_detection_cycles']);self.assertEqual(s.writes,[])

    def test_bad_current_keeps_timing_cause_before_single_safe_off(self):
        with TemporaryDirectory() as tmp:
            p=Path(tmp);s=FakeSession(p/'live');anchor=loop.zero(s,12);s.action='execute';loop.common.save(p/'zero.json',anchor)
            original=s.execute
            def bad_current(tag,args):
                result=original(tag,args)
                if len(args)==3 and 20 in s.writes:
                    raw=bytearray(s.raw_result);struct.pack_into('<I',raw,12,6);s.raw_result=bytes(raw)
                    trace=bytearray(s.raw_trace);struct.pack_into('<I',trace,12,6);s.raw_trace=bytes(trace)
                    timing=bytearray(speed.TIMING_SIZE);struct.pack_into('<4I',timing,0,2,7,9,9);s.raw_timing=bytes(timing)
                    s.state.update(result=6,stop_reason=6)
                return result
            s.execute=bad_current
            def connect(output,*args):output.mkdir();return s
            with patch.object(speed,'Session',side_effect=connect):
                with self.assertRaisesRegex(RuntimeError,'结果码=6'):
                    speed.main(['--output',str(p/'new'),'--action','execute','--prepared','x',
                        '--zero-summary',str(p/'zero.json'),'--bench-confirmed','--vbus-dmm-v','12'])
            report=json.loads((p/'new/summary.json').read_text(encoding='utf-8'))
            self.assertEqual(report['rotor_speed']['result'],6)
            self.assertEqual(report['timing_failure']['reason_name'],'irq_exit')
            self.assertEqual(s.writes,[4,20,1]);self.assertFalse(s.state['moe'])


if __name__=='__main__':unittest.main(verbosity=1)
