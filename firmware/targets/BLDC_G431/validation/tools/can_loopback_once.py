"""冻结内部回环镜像的无功率联调；默认仅离线核对，不烧录/复位/清故障。"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys

TARGET = Path(__file__).resolve().parents[2]
ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0,str(ROOT))
from communication.host.protocol import can_codec as can
from communication.host.protocol.profile import Result
from candidate_manifest import read_hex

FIELDS = '''version command_token acknowledged_token request_result request_id request_flags request_dlc
request_data0 request_data1 ready milliseconds service_count accepted_tx completed_tx rx_frames rx_lost
trace_count motion_count heartbeat_count mode_register test_register motor_state motor_faults guard_faults
internal_reason target_bits bus_bits moe six_pin_levels system_phase communication_stack telemetry_stack
session highest_sequence ordinary_ticket stop_ticket rx_irq tx_irq'''.split()
CLI = Path(r'D:\cube\Cubeprogrammer\bin\STM32_Programmer_CLI.exe')


def require(condition,message):
    if not condition:raise RuntimeError(message)


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def offline(candidate):
    prepared=json.loads(candidate.read_text(encoding='utf-8'));folder=candidate.parent
    require(prepared['status']=='PREPARED_NOT_HARDWARE_VALIDATED','Wrong candidate type')
    for name,digest in prepared['files'].items():
        require(hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest,'Changed artifact: '+name)
    for name,digest in prepared['identity']['source_sha256'].items():
        path=folder/'target_communication_config.h' if name.endswith('/target_communication_config.h') else ROOT/name
        data=path.read_bytes().removeprefix(b'\xef\xbb\xbf').replace(b'\r\n',b'\n')
        require(hashlib.sha256(data).hexdigest()==digest,'Changed candidate source: '+name)
    require(prepared['probe_size']==len(FIELDS)*4+128*24,'Unexpected diagnostic layout')
    return prepared


class Session:
    def __init__(self,args,prepared):
        self.args=args;self.prepared=prepared;self.number=0;self.history=[];self.snapshots=[];self.actions=[]
        self.last_trace=0;self.token=0;self.groups=[]
        args.output.mkdir(parents=True,exist_ok=False)
    def cli(self,tag,arguments):
        self.number+=1;stem=f'{self.number:03d}-{tag}'
        result=subprocess.run([str(self.args.cli),'-c','port=SWD','mode=HOTPLUG','freq=4000',*arguments,'-score'],capture_output=True,timeout=20)
        output=(result.stdout+result.stderr).decode('utf-8',errors='replace')
        (self.args.output/(stem+'.log')).write_text(output,encoding='utf-8')
        require(result.returncode==0 and 'Error:' not in output,stem+': CLI failed')
        require(re.search(r'Device ID\s*:\s*0x468\b',output) and 'Core is running' in output,stem+': wrong/stopped MCU')
        return stem
    def upload(self,tag,blocks):
        arguments=[];paths={}
        for name,address,size in blocks:
            path=self.args.output/f'{self.number+1:03d}-{tag}-{name}.bin';paths[name]=(path,size)
            arguments+=['-u',hex(address),str(size),str(path)]
        self.cli(tag,arguments)
        result={name:path.read_bytes() for name,(path,size) in paths.items()}
        require(all(len(result[name])==size for name,(path,size) in paths.items()),'Incomplete upload')
        return result
    def read(self,tag):
        data=self.upload(tag,[('probe',self.prepared['probe_address'],self.prepared['probe_size'])])['probe']
        header=dict(zip(FIELDS,struct.unpack_from('<'+str(len(FIELDS))+'I',data)))
        header['bus_v']=struct.unpack('<f',struct.pack('<I',header['bus_bits']))[0]
        require(header['version']==0x43414E31 and header['ready']==1 and header['system_phase']==3,'Probe not ready')
        require(header['motor_state']==1 and header['motor_faults']==header['guard_faults']==header['internal_reason']==0,'Non-idle/fault state')
        require(header['moe']==header['six_pin_levels']==header['target_bits']==header['rx_lost']==0,'Power/target/RX lost condition')
        require(0<=header['bus_v']<4 and header['mode_register']&0xA0==0xA0 and header['test_register']&16,'Not isolated low-bus loopback')
        for name,minimum in [('communication_stack',192),('telemetry_stack',96)]:
            require(header[name]!=0xFFFFFFFF and header[name]>=minimum,'Unmeasured/low stack: '+name)
        count=header['trace_count'];require(count>=self.last_trace and count-self.last_trace<=128,'Diagnostic trace overrun')
        for sequence in range(self.last_trace+1,count+1):
            values=struct.unpack_from('<6I',data,len(FIELDS)*4+((sequence-1)%128)*24)
            identifier,metadata,word0,word1,ms,actual=values
            require(actual==sequence,'Torn trace; no control action retried')
            frame=can.Frame(identifier,struct.pack('<II',word0,word1),bool(metadata&256),bool(metadata&512),metadata&255)
            record=dict(sequence=sequence,id=identifier,dlc=frame.dlc,extended=frame.extended,remote=frame.remote,data_hex=frame.data.hex(),ms=ms)
            if identifier in (0x181,0x201,0x281) and frame.dlc==8 and not(frame.extended or frame.remote):
                record['decoded']=can.decode_message(frame)
            self.history.append(record)
        self.last_trace=count;self.snapshots.append(dict(tag=tag,**header))
        return header
    def passed(self,name):
        self.groups.append(name);print('PASS:',name,flush=True)
    def send(self,name,frame,expected=None,filtered=False):
        before=self.read(name+'-before');start=len(self.history)
        for attempt in range(4):
            self.token+=1;base=self.prepared['probe_address']
            values=[frame.id,int(frame.extended)|(int(frame.remote)<<1),frame.dlc,*struct.unpack('<II',frame.data)]
            arguments=[]
            for index,value in enumerate(values):arguments+=['-w32',hex(base+(4+index)*4),hex(value)]
            self.cli(name+'-fields',arguments)
            self.cli(name+'-commit',['-w32',hex(base+4),hex(self.token)])
            after=self.read(name+'-after')
            require(after['acknowledged_token']==self.token,'Request token not acknowledged')
            if after['request_result']==0:break
            # BUSY明确表示未提交硬件；只重投同一无功率测试帧。任何其他失败均不重试。
            require(after['request_result']==2 and attempt<3,'Frame was not accepted')
        records=self.history[start:]
        require(any(r['id']==frame.id and r['dlc']==frame.dlc and r['extended']==frame.extended
            and r['remote']==frame.remote and (frame.dlc!=8 or r['data_hex']==frame.data.hex()) for r in records),'TX Message RAM mismatch')
        replies=[r['decoded'] for r in records if r.get('decoded',{}).get('kind')=='response']
        if expected is not None:
            session,sequence,result,motor=expected
            require(any((r['session'],r['sequence'],r['result'],r['motor'])==(session,sequence,int(result),motor) for r in replies),name+': terminal response mismatch')
        else:require(not replies,name+': unexpected response')
        require(after['rx_frames']-before['rx_frames']==(0 if filtered else 1),name+': RX/filter mismatch')
        self.actions.append(dict(name=name,frame=dict(id=frame.id,data_hex=frame.data.hex(),dlc=frame.dlc,extended=frame.extended,remote=frame.remote),expected=expected,
            before=before,after=after,trace=records))
        self.passed(name);return before,after


def execute(session):
    p=session.prepared;folder=session.args.candidate.parent
    image=read_hex(folder/'BLDC_G431_Run.hex');start,end=min(image),max(image)+1
    require(0x08000000<=start<end<=0x08020000,'Unexpected Flash range')
    # 禁止整块dump外设寄存器：读取FDCAN保留地址本身会触发ARA。只读明确有效的只读/配置字。
    blocks=session.upload('identity',[('flash',start,end-start),('nbtp',0x4000641C,4),('rxgfc',0x40006480,4),
        ('ils',0x40006458,4),('ile',0x4000645C,4),('filters',0x4000A400,12),('clock',p['system_clock_address'],4)])
    require(all(blocks['flash'][address-start]==byte for address,byte in image.items()),'Live Flash differs from frozen candidate')
    word=lambda name:struct.unpack('<I',blocks[name])[0]
    require(struct.unpack('<I',blocks['clock'])[0]==170000000,'Unexpected clock')
    require(word('nbtp')==0x00100E03 and word('rxgfc')==0x0003002B and word('ils')==0 and word('ile')==1,'CAN configuration mismatch')
    require(struct.unpack('<3I',blocks['filters'])==(0x880007FF,0x880107FF,0x890107FF),'Acceptance RAM mismatch')
    first=session.read('initial');require(first['session']==0 and first['command_token']==0,'Fresh test boot required; no automatic reset')
    session.passed('fresh Flash identity, 170MHz/500k timing, actual filter RAM, internal loopback and measured task stacks')
    req=lambda tag,seq,service,target=0:can.request(1,tag,seq,service,target)
    session.send('no-session',req(0,1,1,4000),(0,1,Result.SESSION_REQUIRED,0xFFFF))
    session.send('open',req(0x1234,0,0),(0x1234,0,Result.OK,0xFFFF))
    start_frame=req(0x1234,1,1,4000)
    _,after=session.send('start-not-ready',start_frame,(0x1234,1,Result.OK,2));ticket=after['ordinary_ticket'];require(ticket>0,'No runtime ticket')
    _,after=session.send('completed-duplicate',start_frame,(0x1234,1,Result.OK,2));require(after['ordinary_ticket']==ticket,'Duplicate re-submitted Runtime')
    session.send('sequence-conflict',req(0x1234,1,1,2000),(0x1234,1,Result.SEQUENCE_CONFLICT,0xFFFF))
    session.send('update-wrong-state',req(0x1234,2,2,4000),(0x1234,2,Result.OK,4))
    _,after=session.send('session-stop',req(0x1234,3,3),(0x1234,3,Result.OK,1));ticket=after['stop_ticket']
    _,after=session.send('session-stop-replay',req(0x1234,3,3),(0x1234,3,Result.OK,1));require(after['stop_ticket']==ticket,'Bound Stop replay changed ticket')
    for broadcast in [False,True]:
        frame=can.stop(1,0xFFFF if broadcast else 0x3344,broadcast)
        for repeat in range(2):
            before,after=session.send(('broadcast' if broadcast else 'directed')+f'-stop-{repeat}',frame,(0,0xFFFF if broadcast else 0x3344,Result.OK,1))
            require(after['stop_ticket']>before['stop_ticket'] and after['session']==0x1234 and after['highest_sequence']==3,'Sessionless Stop did not re-execute without changing Session')
    for name,data,result in [('flags',struct.pack('<HHBBh',0x1234,4,2,1,4000),Result.UNSUPPORTED_FLAGS),
                             ('service',struct.pack('<HHBBh',0x1234,5,99,0,0),Result.UNKNOWN_COMMAND),
                             ('target',struct.pack('<HHBBh',0x1234,6,1,0,8001),Result.BAD_VALUE)]:
        sequence=struct.unpack_from('<H',data,2)[0]
        session.send('bad-'+name,can.Frame(0x101,data),(0x1234,sequence,result,0xFFFF))
    session.send('foreign-session',req(0x4321,7,2,4000),(0x4321,7,Result.SESSION_INVALID,0xFFFF))
    payload=struct.pack('<HHBBh',0x1234,7,2,0,4000)
    for identifier in [0x041,0x07F,0x100,0x102]:
        session.send(f'filter-id-{identifier:x}',can.Frame(identifier,payload),filtered=True)
    session.send('filter-extended',can.Frame(0x101,payload,extended=True),filtered=True)
    session.send('filter-rtr',can.Frame(0x101,payload,remote=True),filtered=True)
    session.send('bad-dlc',can.Frame(0x101,payload,dlc=7))
    session.send('bad-stop-reserved',can.Frame(1,b'\x22\x11\x01'+b'\x00'*5))
    final=session.read('final')
    states=[r['decoded'] for r in session.history if r.get('decoded',{}).get('kind')=='state']
    motions=[r['decoded'] for r in session.history if r.get('decoded',{}).get('kind')=='motion']
    require(len(states)>=2 and all(s['motor_state']==1 and s['faults']==0 for s in states),'Missing/invalid heartbeat')
    require(motions and all(m['target_x100']==0 and m['flags']&1 for m in motions),'Missing/invalid compact telemetry')
    require(final['rx_irq']>0 and final['tx_irq']>0 and final['rx_lost']==0,'IRQ chain failed')
    require(final['completed_tx']+1>=final['accepted_tx'],'Outstanding/lost hardware TX')
    require(final['session']==0x1234 and final['highest_sequence']==6,'Rejected frames changed Session/sequence')
    session.passed('heartbeat, fixed-point motion, IRQ/Task/Runtime responses, no RX lost and final IDLE/zero/off')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',required=True,type=Path)
    parser.add_argument('--execute',action='store_true',help='现场纯逻辑条件确认后才使用；不烧录或复位')
    parser.add_argument('--output',type=Path);parser.add_argument('--cli',type=Path,default=CLI)
    args=parser.parse_args();prepared=offline(args.candidate)
    if not args.execute:
        print('PASS offline frozen loopback candidate:',prepared['identity']['firmware_build_id']);return
    require(args.output is not None,'--output required')
    session=Session(args,prepared)
    try:
        execute(session)
        save(args.output/'result.json',dict(status='PASS',groups=session.groups,identity=prepared['identity'],
            files=prepared['files'],snapshots=session.snapshots,actions=session.actions,tx_message_ram_trace=session.history,
            scope='CAN internal loopback only; frame injection via hardware TX, task-side TX Message RAM witness and hardware completion; actual Server/Runtime/Motor',
            not_proven=['external transceiver/CANH/CANL/ACK/arbitration','external Error Passive/Bus-off','two nodes/CAN motor motion']))
    except Exception as error:
        save(args.output/'failure.json',dict(status='FAIL',error=str(error),groups=session.groups,snapshots=session.snapshots,actions=session.actions,trace=session.history))
        raise


if __name__=='__main__':main()
