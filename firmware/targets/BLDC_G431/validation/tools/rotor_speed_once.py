"""有限速度闭环；复用转子DQ单次事务，不安装标定，不自动重试。"""
import argparse
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import time

import rotor_current_once as rotor

loop,require=rotor.loop,rotor.require
FIELDS=dict.fromkeys(['sequence','request_sequence','active','result','sample_count','enabled_samples','window_samples','voltage_limited_samples'],'I')
FIELDS.update(dict.fromkeys(['target_velocity_rpm','electrical_offset_turns','mean_id_a','mean_iq_a','peak_current_tracking_error_a',
    'max_voltage_v','peak_abs_current_a','min_duty','max_duty','start_position_turns','end_position_turns','displacement_turns',
    'max_abs_velocity_rpm','last_velocity_rpm','mean_velocity_rpm','max_position_age_s','mean_iq_reference_a',
    'mean_speed_error_rpm','peak_speed_error_rpm','last_iq_reference_a'],'f'))
FIELDS.update(dict.fromkeys(['speed_update_count','speed_current_limited_updates','speed_window_updates','current_integral_limited_samples'],'I'))
SIZE=128
BUS_FIELDS={'sequence':('I',0),'request_sequence':('I',4),'failure_mask':('I',8),'publication_available':('I',12),
    'sample_time':('Q',16),'observation_time':('Q',24),'voltage_v':('f',32),'acquisition_valid':('I',36),
    'diagnostic_available':('I',40),'reserved':('I',44),'acquisition.observation_time':('Q',48)}
BUS_FIELDS.update({'acquisition.'+name:('I',56+4*i) for i,name in enumerate(
    ['cause','hal_status','rank_count','vbus_raw','vrefint_raw','adc_error','adc_state','adc_isr','adc_ier','adc_cr'])})
BUS_SIZE=96
TRACE_HEADER_FIELDS=dict.fromkeys(['sequence','request_sequence','active','result','count','capacity','divider_updates','dropped'],'I')
TRACE_HEADER_FIELDS.update(speed_kp_a_per_rpm='f',speed_ki_a_per_rpm_s='f')
TRACE_SAMPLE_FIELDS={'elapsed_ticks':'I','position_age_ticks':'I','position_turns':'f','velocity_rpm':'f',
    'iq_reference_a':'f','iq_measured_a':'f'}
TRACE_HEADER_SIZE,TRACE_SAMPLE_SIZE,TRACE_CAPACITY=40,24,375
TRACE_SIZE=TRACE_HEADER_SIZE+TRACE_SAMPLE_SIZE*TRACE_CAPACITY
TIMING_FIELDS={n:('I',4*i) for i,n in enumerate(['sequence','reason','slot_sequence','callback_slot',
    'observed_cycles','slot_start_cycles','irq_entry_cycles','callback_start_cycles','consumer_entry_cycles',
    'consumer_exit_cycles','auxiliary_exit_cycles','reserved','tim_count','tim_status','tim_control','state_bits',
    'adc1_status','adc2_status','actuation_epoch','callback_epoch'])}
TIMING_FIELDS['observation_time']=('Q',80)
TIMING_SIZE=88
LIMITS={'target_max_rpm':80.0,'iq_limit_nominal_a':0.1,'speed_kp_a_per_rpm':0.001,'speed_ki_a_per_rpm_s':0.004,
    'kp_min_a_per_rpm':0.0005,'kp_max_a_per_rpm':0.005,'ki_min_a_per_rpm_s':0.0,'ki_max_a_per_rpm_s':0.02,
    'speed_tracking_per_s':20.0,'velocity_filter_s':0.005,'speed_loop_hz':1000,'current_loop_hz':10000,
    'trajectory_s':1.5,'independent_deadline_s':2.0,'voltage_request_max_v':1.8,'phase_trip_nominal_a':0.65,
    'velocity_withdrawal_rpm':300.0,'displacement_withdrawal_turns':2.0}


def parameters(rpm,offset,kp=0.001,ki=0.004):
    require(math.isfinite(rpm) and 10<=abs(rpm)<=80 and math.isfinite(offset) and 0<=offset<1,
        '速度目标须为正负10～80rpm，零位须位于[0,1)')
    require(math.isfinite(kp) and 0.0005<=kp<=0.005 and math.isfinite(ki) and 0<=ki<=0.02,'速度PI参数越界')
    values=struct.unpack('<4f',struct.pack('<4f',rpm,offset,kp,ki))
    require(10<=abs(values[0])<=80 and 0<=values[1]<1,'float32速度目标或零位越界')
    return values


def prepare(build,output,gdb,mapping):
    report=rotor.prepare(build,output,gdb,mapping)
    specs={'config':'&g_target_validation_rotor_speed_config','result':'&g_target_validation_rotor_speed',
        'size':'sizeof(RotorSpeedSnapshot)','config_size':'sizeof(RotorSpeedConfig)','command':'TARGET_VALIDATION_COMMAND_ROTOR_SPEED',
        'config_target':'&((RotorSpeedConfig*)0)->target_velocity_rpm','config_offset':'&((RotorSpeedConfig*)0)->electrical_offset_turns',
        'config_kp':'&((RotorSpeedConfig*)0)->speed_kp_a_per_rpm','config_ki':'&((RotorSpeedConfig*)0)->speed_ki_a_per_rpm_s',
        'trace':'&g_target_validation_rotor_speed_trace','trace_size':'sizeof(RotorSpeedTrace)',
        'trace_samples':'&((RotorSpeedTrace*)0)->samples','trace_sample_size':'sizeof(RotorSpeedTraceSample)'}
    specs.update({'trace_field_'+n:'&((RotorSpeedTrace*)0)->'+n for n in TRACE_HEADER_FIELDS})
    specs.update({'trace_sample_field_'+n:'&((RotorSpeedTraceSample*)0)->'+n for n in TRACE_SAMPLE_FIELDS})
    specs.update({'field_'+n:'&((RotorSpeedSnapshot*)0)->'+n for n in FIELDS})
    specs.update(bus_failure='&g_target_validation_bus_failure',bus_failure_size='sizeof(TargetValidationBusFailure)')
    specs.update({'bus_field_'+n:'&((TargetValidationBusFailure*)0)->'+n for n in BUS_FIELDS})
    specs.update(timing_failure='&g_g431_timing_failure',timing_failure_size='sizeof(G431TimingFailure)')
    specs.update({'timing_field_'+n:'&((G431TimingFailure*)0)->'+n for n in TIMING_FIELDS})
    command=[str(gdb),'-batch','-ex','file "'+(output/'artifacts'/loop.candidate.ARTIFACTS[1]).as_posix()+'"']
    for value in specs.values():command+=['-ex','p/x (unsigned int)'+value]
    proc=subprocess.run(command,capture_output=True,text=True,timeout=20)
    (output/'rotor-speed-layout.log').write_text(proc.stdout+proc.stderr,encoding='utf-8')
    values=re.findall(r'^\$\d+ = (0x[0-9a-f]+|\d+)\s*$',proc.stdout,re.M)
    require(proc.returncode==0 and len(values)==len(specs),'有限速度的DWARF不完整')
    layout=dict(zip(specs,(int(v,0) for v in values)))
    require(layout['size']==SIZE and layout['config_size']==16 and layout['command']==20
        and [layout['config_'+n] for n in ['target','offset','kp','ki']]==[0,4,8,12]
        and [layout['field_'+n] for n in FIELDS]==list(range(0,SIZE,4)),'有限速度的ABI改变')
    require(layout['trace_size']==TRACE_SIZE and layout['trace_samples']==TRACE_HEADER_SIZE
        and layout['trace_sample_size']==TRACE_SAMPLE_SIZE
        and [layout['trace_field_'+n] for n in TRACE_HEADER_FIELDS]==list(range(0,TRACE_HEADER_SIZE,4))
        and [layout['trace_sample_field_'+n] for n in TRACE_SAMPLE_FIELDS]==list(range(0,TRACE_SAMPLE_SIZE,4)),
        '速度曲线ABI改变')
    for name,size in [('config',16),('result',SIZE),('trace',TRACE_SIZE)]:require(0x20000000<=layout[name]<layout[name]+size<=0x20008000,'有限速度地址越界')
    require(layout['bus_failure_size']==BUS_SIZE and all(layout['bus_field_'+n]==offset for n,(_,offset) in BUS_FIELDS.items())
        and 0x20000000<=layout['bus_failure']<layout['bus_failure']+BUS_SIZE<=0x20008000,'母线失败锁存ABI改变')
    require(layout['timing_failure_size']==TIMING_SIZE
        and all(layout['timing_field_'+n]==offset for n,(_,offset) in TIMING_FIELDS.items())
        and 0x20000000<=layout['timing_failure']<layout['timing_failure']+TIMING_SIZE<=0x20008000,'时序首故障ABI改变')
    report.update(rotor_speed_schema=2,rotor_speed_layout=layout,rotor_speed_limits=LIMITS,
        bus_failure_schema=1,timing_failure_schema=1,rotor_speed_tool_sha256=loop.candidate.digest(Path(__file__).resolve().parent / 'rotor_speed_once.py'))
    report['source_files']['validation/tools/rotor_speed_once.py']=loop.candidate.digest(Path(__file__).resolve().parent / 'rotor_speed_once.py')
    loop.common.save(output/'prepared.json',report)
    return report


class Session(rotor.Session):
    power_command,power_duration_us=20,2000000
    result_fields,snapshot_size=FIELDS,SIZE

    def __init__(self,output,prepared,cli,action,rpm,kp=0.001,ki=0.004):
        frozen,_=loop.load_prepared(prepared)
        require(frozen.get('rotor_speed_schema')==2 and frozen.get('rotor_speed_limits')==LIMITS
            and frozen.get('bus_failure_schema')==1
            and frozen.get('timing_failure_schema')==1
            and frozen.get('rotor_speed_tool_sha256')==loop.candidate.digest(Path(__file__).resolve().parent / 'rotor_speed_once.py'),
            '不是本版有限速度候选，或工具/包络已改变')
        payload=parameters(rpm,frozen['mapping_candidate']['electrical_offset_turns'],kp,ki)
        super().__init__(output,prepared,cli,action,0.1)
        self.rotor_layout=frozen['rotor_speed_layout'];self.layout=dict(self.layout,config=self.rotor_layout['config'])
        self.payload_parameters=payload


def read_timing_failure(session,tag='timing-first-failure'):
    address=session.rotor_layout['timing_failure']
    before=session.upload(tag+'-before',[('timing_sequence',address,4)])['timing_sequence']
    raw=session.upload(tag,[('timing_failure',address,TIMING_SIZE)])['timing_failure']
    after=session.upload(tag+'-after',[('timing_sequence',address,4)])['timing_sequence']
    require(len(raw)==TIMING_SIZE and before==after==raw[:4] and loop.common.word(raw)%2==0,
        '时序首故障读回不一致，不重复执行功率命令')
    failure={n:struct.unpack_from('<'+f,raw,o)[0] for n,(f,o) in TIMING_FIELDS.items()}
    failure['recorded']=failure['sequence']!=0
    reason=failure['reason']
    require(not failure['recorded'] or (1<=reason<=7 and failure['reserved']==0),'时序首故障原因或保留位不符')
    failure['reason_name']={0:'none',1:'early_update',2:'update_state',3:'delivery',4:'arm_window',
        5:'adc_entry',6:'consumer_return',7:'irq_exit'}.get(reason,'unknown')
    def elapsed(end,start):
        value=(failure[end]-failure[start])&0xffffffff
        return value if value<0x80000000 else None
    active_irq=failure['recorded'] and (reason in [5,6,7] or bool(failure['state_bits']&2))
    callback_current=(failure['recorded'] and failure['callback_slot']!=0
        and failure['callback_slot']==failure['slot_sequence']
        and elapsed('callback_start_cycles','slot_start_cycles') is not None
        and elapsed('callback_start_cycles','slot_start_cycles')<17000)
    failure['irq_context_active']=active_irq
    failure['callback_anchor_current']=callback_current
    failure['slot_elapsed_cycles']=elapsed('observed_cycles','slot_start_cycles') if failure['recorded'] else None
    failure['irq_hook_to_detection_cycles']=elapsed('observed_cycles','irq_entry_cycles') if active_irq else None
    valid_phases=callback_current and reason in [6,7]
    failure['prefix_cycles']=elapsed('consumer_entry_cycles','irq_entry_cycles') if valid_phases else None
    failure['consumer_cycles']=elapsed('consumer_exit_cycles','consumer_entry_cycles') if valid_phases else None
    failure['post_consumer_cycles']=elapsed('observed_cycles','consumer_exit_cycles') if valid_phases else None
    failure['auxiliary_cycles']=elapsed('auxiliary_exit_cycles','consumer_exit_cycles') if valid_phases and reason==7 else None
    failure['hal_tail_to_detection_cycles']=elapsed('observed_cycles','auxiliary_exit_cycles') if valid_phases and reason==7 else None
    failure['unmodified_irq_wcet_proved']=False
    failure['exception_entry_return_and_latch_copy_included']=False
    loop.common.save(session.out/(tag+'.json'),failure)
    return failure


def read_speed_trace(session,terminal):
    """只在有限事务结束后上传RAM，运行中不轮询整段曲线。"""
    address=session.rotor_layout['trace']
    before=session.upload('speed-trace-before',[('header',address,TRACE_HEADER_SIZE)])['header']
    raw=session.upload('speed-trace',[('trace',address,TRACE_SIZE)])['trace']
    after=session.upload('speed-trace-after',[('header',address,TRACE_HEADER_SIZE)])['header']
    require(len(raw)==TRACE_SIZE and before==after==raw[:TRACE_HEADER_SIZE],'速度曲线读回不一致')
    header={n:struct.unpack_from('<'+f,raw,4*i)[0] for i,(n,f) in enumerate(TRACE_HEADER_FIELDS.items())}
    require(header['sequence']!=0 and header['sequence']%2==0 and not header['active']
        and header['request_sequence']==terminal['request_sequence'] and header['result']==terminal['result']
        and header['capacity']==TRACE_CAPACITY and header['divider_updates']==4
        and 0<=header['count']<=TRACE_CAPACITY and header['dropped']==0,'速度曲线事务或边界不符')
    require((header['speed_kp_a_per_rpm'],header['speed_ki_a_per_rpm_s'])==session.payload_parameters[2:],
        '速度曲线中的实际PI参数不符')
    if terminal['result']==1:require(header['count']==TRACE_CAPACITY,'正常完成的速度曲线不完整')
    samples=[]
    for i in range(header['count']):
        sample={n:struct.unpack_from('<'+f,raw,TRACE_HEADER_SIZE+TRACE_SAMPLE_SIZE*i+4*j)[0]
            for j,(n,f) in enumerate(TRACE_SAMPLE_FIELDS.items())}
        require(sample['elapsed_ticks']==2+80*i and sample['position_age_ticks']<=30
            and all(math.isfinite(sample[n]) for n,f in TRACE_SAMPLE_FIELDS.items() if f=='f')
            and 0<=sample['position_turns']<1 and abs(sample['velocity_rpm'])<=300.001
            and abs(sample['iq_reference_a'])<=0.100001,
            '速度曲线样本非法')
        sample['time_s']=sample['elapsed_ticks']/20000
        if samples:
            previous=samples[-1]
            dt=(sample['elapsed_ticks']-sample['position_age_ticks']
                -previous['elapsed_ticks']+previous['position_age_ticks'])/20000
            delta=(sample['position_turns']-previous['position_turns']+0.5)%1-0.5
            require(dt>0,'曲线编码器时间没有前进')
            sample['position_interval_velocity_rpm']=delta*60/dt
        else:sample['position_interval_velocity_rpm']=None
        samples.append(sample)
    window=[s for s in samples if 0.8<=s['time_s']<1.2]
    summary={'header':header,'sample_hz':250,'current_units':'nominal A','window_points':len(window),
        'window_velocity_rms_error_rpm':math.sqrt(sum((s['velocity_rpm']-session.payload_parameters[0])**2 for s in window)/len(window)) if window else None,
        'position_interval_velocity_is_4ms_average':True,'not_1khz_encoder_noise_capture':True}
    loop.common.save(session.out/'speed-trace.json',{'summary':summary,'samples':samples})
    return summary


def read_bus_failure(session,tag,request=None):
    address=session.rotor_layout['bus_failure']
    before=session.upload(tag+'-before',[('bus_sequence',address,4)])['bus_sequence']
    raw=session.upload(tag,[('bus_failure',address,BUS_SIZE)])['bus_failure']
    after=session.upload(tag+'-after',[('bus_sequence',address,4)])['bus_sequence']
    sequence=struct.unpack_from('<I',raw)[0]
    require(sequence%2==0 and before==after==raw[:4],'母线首故障读回不一致，不重复执行功率命令')
    result={name:struct.unpack_from('<'+fmt,raw,offset)[0] for name,(fmt,offset) in BUS_FIELDS.items()}
    result['recorded']=sequence!=0
    result['matches_terminal_request']=bool(result['recorded'] and request is not None and result['request_sequence']==request)
    result['voltage_bits']=struct.unpack_from('<I',raw,32)[0]
    if not math.isfinite(result['voltage_v']):result['voltage_v']=None
    result['failed_conditions']=[name for bit,name in [(1,'unavailable'),(2,'invalid'),(4,'nonfinite'),(8,'undervoltage'),
        (16,'overvoltage'),(32,'future_timestamp'),(64,'stale')] if result['failure_mask']&bit]
    result['acquisition_cause']={0:'ok',1:'start_failed',2:'rank_mismatch',3:'vref_zero',4:'adc_error',
        5:'dma_incomplete',6:'dma_error'}.get(result['acquisition.cause'],'unknown')
    result['diagnostic_matches_publication']=bool(result['publication_available'] and result['diagnostic_available']
        and result['observation_time']==result['acquisition.observation_time'])
    result['vbus_raw_captured']=bool(result['diagnostic_available'] and result['acquisition.rank_count']>=1)
    result['vrefint_raw_captured']=bool(result['diagnostic_available'] and result['acquisition.rank_count']>=2)
    loop.common.save(session.out/(tag+'.json'),result)
    return result


def analyze(o,payload):
    require(not o['moe'] and not o['halted'] and not o['active'] and o['result']==o['stop_reason'] and o['result'] in [1,23],
        '有限速度未正常完成或触发约定运动边界，固件结果码='+str(o['result']))
    require(all(math.isfinite(o[n]) for n,f in FIELDS.items() if f=='f'),'有限速度结果包含坏数值')
    require((o['target_velocity_rpm'],o['electrical_offset_turns'])==payload[:2],'速度目标或零位不符')
    require(0<=o['max_voltage_v']<=1.80001 and 0<=o['peak_abs_current_a']<0.65
        and 0<=o['max_abs_velocity_rpm']<=300.001 and abs(o['displacement_turns'])<2.01
        and abs(o['last_iq_reference_a'])<=0.100001 and abs(o['mean_iq_reference_a'])<=0.100001
        and 0<=o['max_position_age_s']<=0.001501 and 0.1<=o['min_duty']<=o['max_duty']<=0.9,
        '有限速度超出包络')
    complete=o['result']==1;window=o['window_samples']==4000 and o['speed_window_updates']==400
    if complete:
        require((o['sample_count'],o['enabled_samples'],o['window_samples'],o['speed_update_count'],o['speed_window_updates'])
            ==(15002,15001,4000,1500,400),'有限速度计数或1kHz分频异常')
    require(0<=o['speed_current_limited_updates']<=o['speed_update_count']<=1500
        and 0<=o['current_integral_limited_samples']<=15000,'速度/电流限幅计数异常')
    error=math.hypot(o['mean_id_a'],o['mean_iq_a']-o['mean_iq_reference_a']) if window else None
    return {'normal_completion':complete,'motion_boundary_reached':not complete,'tracking_window_complete':window,
        'mean_dq_error_nominal_a':error,'current_tracking_screen_passed':(error<=0.01 and o['peak_current_tracking_error_a']<=0.04) if window else None,
        'speed_tracking_screen_passed':(abs(o['mean_speed_error_rpm'])<=max(3,abs(payload[0])*0.1)
            and o['peak_speed_error_rpm']<=max(12,abs(payload[0])*0.25)) if window else None,
        'motion_direction_screen_passed':o['displacement_turns']*(1 if payload[0]>0 else -1)>0.003,
        'deceleration_screen_passed':abs(o['last_velocity_rpm'])<=15 if complete else None,
        'calibration_applied':False,'foc_qualified':False}


def execute_once(session,anchor,vbus,clock=time.monotonic):
    first,before,probe=loop.preflight(session,'speed-before',vbus)
    loop.check_zero_anchor(session,anchor,first,before,probe)
    request=rotor.submit_power_once(session);deadline=clock()+4;session.deadline=deadline;index=0
    while clock()<deadline:
        session.call_timeout_s=max(0.001,deadline-clock())
        result=rotor.read_result(session,'speed-poll-'+str(index));index+=1
        if result['coherent'] and result['request_sequence']==request and not result['active']:
            # 保留清理前的原始终态；一次SAFE_OFF会将通用stop_reason改为2。
            session.terminal_result=result
            break
    else:raise RuntimeError('有限速度主机4秒期限到，只允许一次关断')
    session.call_timeout_s,session.deadline=15.0,None
    if result['result']==7 and not result['moe'] and not result['halted']:
        session.bus_failure=read_bus_failure(session,'bus-first-failure',request)
    if not result['moe'] and not result['halted']:
        session.timing_failure=read_timing_failure(session)
    session.trace_summary=read_speed_trace(session,result)
    analysis=analyze(result,session.payload_parameters)
    _,final,updated=loop.preflight(session,'speed-final',vbus,stationary=False)
    require(final['command_sequence']==final['active_request_sequence']==request
        and final['result']==final['stop_reason']==result['result'] and updated==probe,'速度终态事务或保护改变')
    require(final['enable_attempts']-before['enable_attempts']==final['enable_successes']-before['enable_successes']==1,'不是唯一速度使能')
    stages=final['stage_attempts']-before['stage_attempts']
    require(stages==final['stage_successes']-before['stage_successes'] and 2<=stages<=15001
        and (result['result']!=1 or stages==15001),'速度提交计数异常')
    return {'result':'ROTOR_SPEED_FINISHED_OUTPUT_OFF','request_sequence':request,'zero_request_sequence':anchor['request_sequence'],
        'before':before,'final':final,'rotor_speed':result,'analysis':analysis,'trace':session.trace_summary,
        'timing_failure':session.timing_failure,
        'controller_gains':{'kp_a_per_rpm':session.payload_parameters[2],'ki_a_per_rpm_s':session.payload_parameters[3]}}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--action',choices=['prepare','zero','execute','stop'],default='prepare')
    parser.add_argument('--build-dir',type=Path,default=loop.common.DEFAULT_BUILD)
    parser.add_argument('--mapping-evidence',type=Path,default=rotor.DEFAULT_MAPPING)
    parser.add_argument('--prepared',type=Path);parser.add_argument('--zero-summary',type=Path)
    parser.add_argument('--gdb',type=Path,default=loop.common.DEFAULT_GDB);parser.add_argument('--cli',type=Path,default=loop.common.DEFAULT_CLI)
    parser.add_argument('--velocity-rpm',type=float,default=60);parser.add_argument('--vbus-dmm-v',type=float)
    parser.add_argument('--kp',type=float,default=0.001);parser.add_argument('--ki',type=float,default=0.004)
    parser.add_argument('--bench-confirmed',action='store_true',help=loop.BENCH_TEXT)
    args=parser.parse_args(argv);parameters(args.velocity_rpm,0,args.kp,args.ki)
    require(not args.output.exists(),'输出目录已存在，拒绝覆盖')
    if args.action=='prepare':
        require(args.prepared is None and args.zero_summary is None,'离线准备不接受运行锚')
        return prepare(args.build_dir,args.output,args.gdb,args.mapping_evidence)
    require(args.prepared is not None and (args.action=='execute')==(args.zero_summary is not None),'需要冻结速度候选，仅execute须零点锚')
    if args.action in ['zero','execute']:
        require(args.bench_confirmed and args.vbus_dmm_v is not None and math.isfinite(args.vbus_dmm_v) and 10<=args.vbus_dmm_v<=14,
            '需要现场接线确认和10..14V母线参考')
    anchor=json.loads(args.zero_summary.read_text(encoding='utf-8')) if args.zero_summary else None;session=None
    try:
        session=Session(args.output,args.prepared,args.cli,args.action,args.velocity_rpm,args.kp,args.ki)
        if args.action=='execute':report=execute_once(session,anchor,args.vbus_dmm_v)
        elif args.action=='zero':report=loop.zero(session,args.vbus_dmm_v)
        else:
            session.read('stop-before');request=loop.submit(session,1,'safe-off-once');final,_=session.read('stop-after')
            require(not final['moe'] and not final['guard_running'],'关断未确认')
            report={'result':'SAFE_OFF_SENT_ONCE_OUTPUT_OFF','request_sequence':request,'final':final}
    except Exception as error:
        report={'result':'FAILED_NO_RETRY','error':str(error),'action':args.action,'write_attempted':bool(session and session.write_attempted),'automatic_retry':False}
        if session and hasattr(session,'terminal_result'):report['rotor_speed']=session.terminal_result
        if session and hasattr(session,'bus_failure'):report['bus_failure']=session.bus_failure
        if session and hasattr(session,'timing_failure'):report['timing_failure']=session.timing_failure
        if session and hasattr(session,'trace_summary'):report['trace']=session.trace_summary
        if session and session.write_attempted and 1 not in session.writes:
            try:
                session.call_timeout_s,session.deadline=5.0,None
                report['cleanup_request_sequence']=loop.submit(session,1,'exception-safe-off-only');report['cleanup'],_=session.read('exception-after-safe-off')
            except Exception as cleanup:report['cleanup_error']=str(cleanup)
        if args.output.exists():loop.common.save(args.output/'summary.json',report)
        raise
    report.update(action=args.action,hex_sha256=session.hex_sha256,automatic_retry=False,calibration_applied=False,foc_qualified=False)
    loop.common.save(args.output/'summary.json',report);return report


if __name__=='__main__':print(json.dumps(main(),indent=2,allow_nan=False))
