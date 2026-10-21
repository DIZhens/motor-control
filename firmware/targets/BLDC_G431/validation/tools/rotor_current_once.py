"""有限转子DQ电流试验；仅使用已审查映射候选，不安装标定或自动重试。"""
import argparse
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import time

import open_loop_once as loop

require = loop.require
FIELDS = dict.fromkeys(['sequence','request_sequence','active','result','sample_count','enabled_samples',
    'window_samples','voltage_limited_samples'], 'I')
FIELDS.update(dict.fromkeys(['iq_reference_a','electrical_offset_turns','mean_id_a','mean_iq_a',
    'peak_tracking_error_a','max_voltage_v','peak_abs_current_a','min_duty','max_duty',
    'start_position_turns','end_position_turns','displacement_turns','max_abs_velocity_rpm',
    'last_velocity_rpm','mean_velocity_rpm','max_position_age_s'], 'f'))
SIZE = 96
LIMITS = {'iq_max_nominal_a':0.2,'voltage_request_upper_bound_v':1.8,'phase_trip_nominal_a':0.65,
    'kp_v_per_a':0.5,'ki_v_per_a_s':300.0,'integral_limit_v':1.5,
    'velocity_limit_rpm':300.0,'displacement_limit_turns':2.0,'trajectory_s':1.0,
    'independent_deadline_s':1.5,'position_max_age_s':0.0015,'actuation_prediction_s':0.000125}
DEFAULT_MAPPING = Path(__file__).resolve().parents[1]/'evidence/current-survey/excitation-comparison.json'


def parameters(iq, offset):
    require(math.isfinite(iq) and 0.01 <= abs(iq) <= 0.2 and math.isfinite(offset) and 0 <= offset < 1,
        'Iq须为正负0.01～0.2名义A，零位须位于[0,1)')
    values=struct.unpack('<2f',struct.pack('<2f',iq,offset))
    require(0 < abs(values[0]) <= struct.unpack('<f',struct.pack('<f',0.2))[0] and 0 <= values[1] < 1,
        'float32目标或零位越界')
    return values


def mapping_candidate(evidence):
    result=evidence['trial']['mapping']
    require(evidence['trial']['combined_screen_passed'] and result['pole_pairs_confirmed']
        and result['nominal_pole_pairs']==7 and result['encoder_direction_candidate']==1
        and abs(result['return_error_mechanical_turns'])<=0.003
        and result['max_offset_residual_electrical_turns']<=0.035,
        '需要已通过7极对、正方向及原映射门限的实物候选')
    offset=parameters(0.1,result['offset_candidate_electrical_turns'])[1]
    return {'pole_pairs':7,'encoder_direction':1,'electrical_offset_turns':offset}


def prepare(build, output, gdb, mapping, expected=None):
    candidate=mapping_candidate(json.loads(mapping.read_text(encoding='utf-8')))
    report=loop.prepare(build,output,gdb,expected)
    specs={'config':'&g_target_validation_rotor_current_config','result':'&g_target_validation_rotor_current',
        'config_size':'sizeof(RotorCurrentConfig)','size':'sizeof(RotorCurrentSnapshot)',
        'command':'TARGET_VALIDATION_COMMAND_ROTOR_CURRENT',
        'config_iq':'&((RotorCurrentConfig*)0)->iq_reference_a',
        'config_offset':'&((RotorCurrentConfig*)0)->electrical_offset_turns'}
    specs.update({'field_'+n:'&((RotorCurrentSnapshot*)0)->'+n for n in FIELDS})
    command=[str(gdb),'-batch','-ex','file "'+(output/'artifacts'/loop.candidate.ARTIFACTS[1]).as_posix()+'"']
    for value in specs.values():command+=['-ex','p/x (unsigned int)'+value]
    proc=subprocess.run(command,capture_output=True,text=True,timeout=20)
    (output/'rotor-current-layout.log').write_text(proc.stdout+proc.stderr,encoding='utf-8')
    values=re.findall(r'^\$\d+ = (0x[0-9a-f]+|\d+)\s*$',proc.stdout,re.M)
    require(proc.returncode==0 and len(values)==len(specs),'转子DQ的DWARF不完整')
    layout=dict(zip(specs,(int(v,0) for v in values)))
    require(layout['size']==SIZE and layout['config_size']==8 and layout['command']==19
        and layout['config_iq']==0 and layout['config_offset']==4
        and [layout['field_'+n] for n in FIELDS]==list(range(0,SIZE,4)),'转子DQ的ABI改变')
    for name,size in [('config',8),('result',SIZE)]:
        require(0x20000000<=layout[name]<layout[name]+size<=0x20008000,'转子DQ地址越界')
    (output/'mapping-evidence.json').write_bytes(mapping.read_bytes())
    report.update(rotor_current_schema=1,rotor_current_layout=layout,rotor_current_limits=LIMITS,
        rotor_current_tool_sha256=loop.candidate.digest(Path(__file__)),
        open_loop_tool_sha256=loop.candidate.digest(Path(loop.__file__)),
        mapping_evidence_sha256=loop.candidate.digest(mapping),
        mapping_candidate=candidate)
    report['source_files']['validation/tools/rotor_current_once.py']=loop.candidate.digest(Path(__file__))
    loop.common.save(output/'prepared.json',report)
    return report


class Session(loop.Session):
    power_command, power_duration_us = 19, 1_500_000

    def __init__(self, output, prepared, cli, action, iq):
        frozen,_=loop.load_prepared(prepared)
        require(frozen.get('rotor_current_schema')==1 and frozen.get('rotor_current_limits')==LIMITS,
            '不是本版有限转子DQ候选')
        require(frozen['rotor_current_tool_sha256']==loop.candidate.digest(Path(__file__))
            and frozen['open_loop_tool_sha256']==loop.candidate.digest(Path(loop.__file__))
            and frozen['mapping_evidence_sha256']==loop.candidate.digest(prepared.parent/'mapping-evidence.json'),
            '工具或映射证据已改变，须重新冻结')
        candidate=frozen['mapping_candidate']
        require(candidate==mapping_candidate(json.loads((prepared.parent/'mapping-evidence.json').read_text(encoding='utf-8'))),
            '候选与冻结的实物映射证据不符')
        payload=parameters(iq,candidate['electrical_offset_turns'])
        super().__init__(output,prepared,cli,action,0.3,1.0)
        self.rotor_layout=frozen['rotor_current_layout']
        self.layout=dict(self.layout,config=self.rotor_layout['config'])
        self.payload_parameters=payload


def submit_power_once(session):
    # 复用既有写白名单和sequence提交规则，独立验证带符号Iq，避免误用电压参数校验。
    require(session.last_sequence is not None,'先读取本次邮箱')
    previous=session.last_sequence
    request=((previous+1)&0xffffffff) or 1
    session.last_sequence=request
    iq,offset=session.payload_parameters
    session.execute('rotor-payload',['-w32',hex(session.layout['config']),hex(loop.float_bits(iq)),
        hex(loop.float_bits(offset)),'-w32',hex(session.layout['request']+4),hex(19),hex(loop.KEY),
        hex(session.power_duration_us),hex(loop.float_bits(0.5))])
    raw=session.upload('rotor-payload-readback',[('config',session.layout['config'],8),('request',session.layout['request'],20)])
    require(raw['config']==struct.pack('<2f',iq,offset)
        and raw['request']==struct.pack('<IIIIf',previous,19,loop.KEY,session.power_duration_us,0.5),
        '转子DQ参数读回不符，禁止提交sequence')
    session.power_commit_ready=True
    session.execute('rotor-sequence',['-w32',hex(session.layout['request']),hex(request)])
    return request


def read_result(session, tag):
    address=session.rotor_layout['result']
    first=session.upload(tag+'-before',[('sequence',address,4)])['sequence']
    blocks=session.upload(tag,[('rotor',address,SIZE),('stop',session.layout['stop'],4),
        ('bdtr',0x40012c44,4),('dhcsr',0xe000edf0,4)])
    last=session.upload(tag+'-after',[('sequence',address,4)])['sequence']
    seq=loop.common.word(first);raw=blocks['rotor']
    result={'coherent':seq%2==0 and seq==loop.common.word(raw)==loop.common.word(last),
        'stop_reason':loop.common.word(blocks['stop']),'moe':bool(loop.common.word(blocks['bdtr'])&0x8000),
        'halted':bool(loop.common.word(blocks['dhcsr'])&((1<<17)|(1<<19)))}
    if result['coherent']:
        result.update({name:struct.unpack_from('<'+fmt,raw,session.rotor_layout['field_'+name])[0] for name,fmt in FIELDS.items()})
    loop.common.save(session.out/(tag+'.json'),result)
    return result


def analyze(observed, parameters):
    require(not observed['moe'] and not observed['halted'] and observed['active']==0
        and observed['stop_reason']==observed['result'] and observed['result'] in [1,23],
        '转子DQ未正常完成或触发预定运动边界；禁止自动重试')
    require(all(math.isfinite(observed[n]) for n,f in FIELDS.items() if f=='f'),'转子DQ结果包含坏数值')
    require(tuple(observed[n] for n in ['iq_reference_a','electrical_offset_turns'])==parameters,
        '转子DQ目标或零位不是本次候选')
    require(0<=observed['max_voltage_v']<=1.80001 and 0<=observed['peak_abs_current_a']<0.65
        and 0<=observed['max_abs_velocity_rpm']<=300.001 and abs(observed['displacement_turns'])<2.01
        and 0<=observed['max_position_age_s']<=0.001501
        and 0.1<=observed['min_duty']<=observed['max_duty']<=0.9,'转子DQ结果超出有限包络')
    complete=observed['result']==1
    if complete:
        require((observed['sample_count'],observed['enabled_samples'],observed['window_samples'])==(10002,10001,2000),
            '转子DQ计数或保持窗口不完整')
    window_complete=observed['window_samples']==2000
    error=math.hypot(observed['mean_id_a'],observed['mean_iq_a']-parameters[0]) if window_complete else None
    direction=observed['displacement_turns']*(1 if parameters[0]>0 else -1)
    return {'normal_completion':complete,'motion_boundary_reached':not complete,
        'tracking_window_complete':window_complete,'mean_dq_error_nominal_a':error,
        'tracking_screen_passed':(error<=0.01 and observed['peak_tracking_error_a']<=0.03) if window_complete else None,
        'motion_direction_screen_passed':direction>0.003,'current_gain_accuracy_confirmed':False,
        'calibration_applied':False,'rotor_foc_qualified':False}


def execute_once(session,anchor,vbus,clock=time.monotonic):
    first,before,probe=loop.preflight(session,'rotor-before',vbus)
    loop.check_zero_anchor(session,anchor,first,before,probe)
    request=submit_power_once(session)
    deadline=clock()+4;session.deadline=deadline;index=0
    while clock()<deadline:
        session.call_timeout_s=max(0.001,deadline-clock())
        result=read_result(session,'rotor-poll-'+str(index));index+=1
        if result['coherent'] and result['request_sequence']==request and not result['active']:break
    else:raise RuntimeError('转子DQ主机4秒期限到，仅允许一次关断')
    session.call_timeout_s,session.deadline=15.0,None
    analysis=analyze(result,session.payload_parameters)
    _,final,updated=loop.preflight(session,'rotor-final',vbus,stationary=False)
    require(final['command_sequence']==final['active_request_sequence']==request
        and final['result']==final['stop_reason']==result['result'] and updated==probe,'终态事务或保护改变')
    require(final['enable_attempts']-before['enable_attempts']==final['enable_successes']-before['enable_successes']==1,
        '不是唯一使能')
    stages=final['stage_attempts']-before['stage_attempts']
    require(stages==final['stage_successes']-before['stage_successes'] and 2<=stages<=10001
        and (result['result']!=1 or stages==10001),'提交次数或成功次数异常')
    return {'result':'ROTOR_CURRENT_FINISHED_OUTPUT_OFF','request_sequence':request,'zero_request_sequence':anchor['request_sequence'],
        'before':before,'final':final,'rotor_current':result,'analysis':analysis}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--action',choices=['prepare','zero','execute','stop'],default='prepare')
    parser.add_argument('--build-dir',type=Path,default=loop.common.DEFAULT_BUILD)
    parser.add_argument('--mapping-evidence',type=Path,default=DEFAULT_MAPPING)
    parser.add_argument('--prepared',type=Path);parser.add_argument('--zero-summary',type=Path)
    parser.add_argument('--gdb',type=Path,default=loop.common.DEFAULT_GDB)
    parser.add_argument('--cli',type=Path,default=loop.common.DEFAULT_CLI)
    parser.add_argument('--iq-a',type=float,default=0.1)
    parser.add_argument('--vbus-dmm-v',type=float)
    parser.add_argument('--bench-confirmed',action='store_true',help=loop.BENCH_TEXT)
    args=parser.parse_args(argv)
    parameters(args.iq_a,0)
    require(not args.output.exists(),'输出目录已存在，拒绝覆盖')
    if args.action=='prepare':
        require(args.prepared is None and args.zero_summary is None,'离线准备不接受运行锚')
        return prepare(args.build_dir,args.output,args.gdb,args.mapping_evidence)
    require(args.prepared is not None and (args.action=='execute')==(args.zero_summary is not None),'需要冻结候选；仅execute须零点锚')
    if args.action in ['zero','execute']:
        require(args.bench_confirmed and args.vbus_dmm_v is not None and math.isfinite(args.vbus_dmm_v)
            and 10<=args.vbus_dmm_v<=14,'需要现场接线确认和10..14V母线参考')
    anchor=json.loads(args.zero_summary.read_text(encoding='utf-8')) if args.zero_summary else None
    session=None
    try:
        session=Session(args.output,args.prepared,args.cli,args.action,args.iq_a)
        if args.action=='execute':report=execute_once(session,anchor,args.vbus_dmm_v)
        elif args.action=='zero':report=loop.zero(session,args.vbus_dmm_v)
        else:
            session.read('stop-before');request=loop.submit(session,1,'safe-off-once')
            final,_=session.read('stop-after');require(not final['moe'] and not final['guard_running'],'关断未确认')
            report={'result':'SAFE_OFF_SENT_ONCE_OUTPUT_OFF','request_sequence':request,'final':final}
    except Exception as error:
        report={'result':'FAILED_NO_RETRY','error':str(error),'action':args.action,
            'write_attempted':bool(session and session.write_attempted),'automatic_retry':False}
        if session and session.write_attempted and 1 not in session.writes:
            try:
                session.call_timeout_s,session.deadline=5.0,None
                report['cleanup_request_sequence']=loop.submit(session,1,'exception-safe-off-only')
                report['cleanup'],_=session.read('exception-after-safe-off')
            except Exception as cleanup:report['cleanup_error']=str(cleanup)
        if args.output.exists():loop.common.save(args.output/'summary.json',report)
        raise
    report.update(action=args.action,hex_sha256=session.hex_sha256,automatic_retry=False,
        calibration_applied=False,foc_qualified=False)
    loop.common.save(args.output/'summary.json',report)
    return report


if __name__=='__main__':
    print(json.dumps(main(),indent=2,allow_nan=False))
