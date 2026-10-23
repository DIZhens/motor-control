"""真实ARM Server/Codec/Runtime交接，模拟Transport和实时业务边界；无硬件访问。"""
from pathlib import Path
import argparse
import binascii
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC

ROOT=Path(__file__).resolve().parents[3]
COMM=ROOT/'protocol/uart_v1'
PORTS=ROOT/'protocol/ports'
COMMON=ROOT/'protocol/common'
sys.path.insert(0,str(ROOT))
from host.protocol import codec as py
ARM=Path(os.environ.get('KEIL_ARMCC_BIN',r'D:\keil5\ARM\ARMCC\bin'))
groups=[]
def passed(name): groups.append(name);print('PASS:',name,flush=True)
def wire(message=2,session=1,seq=1,payload=b'\x01\x00',kind=1,version=1,flags=0):
    raw=struct.pack('<4B2H2I',version,kind,flags,16,len(payload),message,session,seq)+payload
    return py.cobs_encode(raw+struct.pack('<H',binascii.crc_hqx(raw,0xFFFF)))+b'\0'
def req(message=2,subject=1,session=1,seq=1,**value):
    return wire(message,session,seq,py.encode_request(dict(message_id=message,subject_id=subject,**value)))

class Model:
    data=[];symbols={}
    def __init__(self):
        self.cpu=Uc(UC_ARCH_ARM,UC_MODE_THUMB|UC_MODE_MCLASS);self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        self.cpu.mem_map(0x08000000,0x40000);self.cpu.mem_map(0x20000000,0x8000);self.cpu.mem_map(0xE000E000,0x1000)
        for a,b in self.data:self.cpu.mem_write(a,b)
        self.put(0xE000ED88,0xF00000);self.call('model_init')
    def put(self,name,value):
        self.cpu.mem_write(self.symbols[name] if isinstance(name,str) else name,struct.pack('<I',value&0xFFFFFFFF))
    def get(self,name):return struct.unpack('<I',self.cpu.mem_read(self.symbols[name],4))[0]
    def call(self,name,arg=0):
        self.cpu.reg_write(UC_ARM_REG_SP,0x20007FF0);self.cpu.reg_write(UC_ARM_REG_LR,0x0803F001);self.cpu.reg_write(UC_ARM_REG_R0,arg)
        self.cpu.emu_start(self.symbols[name]|1,0x0803F000,count=1000000)
        assert self.cpu.reg_read(UC_ARM_REG_PC)==0x0803F000,name
        return self.cpu.reg_read(UC_ARM_REG_R0)
    def feed(self,data):
        assert len(data)<=1024
        self.cpu.mem_write(self.symbols['model_input'],bytes(data));self.call('model_feed',len(data))
    def poll(self,now=None):
        if now is not None:self.put('model_now',now)
        self.call('model_poll')
    def take(self):
        self.call('model_flush')
        n=self.get('model_tx_length')
        if not n:return None
        data=bytes(self.cpu.mem_read(self.symbols['model_tx'],n));self.put('model_tx_length',0);self.put('model_tx_busy',0)
        status,f=py.decode_frame(data[:-1]);assert status==py.Status.OK
        return f,(py.decode_response(f.message_id,f.payload) if f.kind==2 else
                  py.decode_sample(f.payload) if f.kind==3 else py.decode_event(f.message_id,f.payload))
    def response(self,data,rt=False):
        self.feed(data)
        if rt:self.call('model_rt_step')
        self.poll();result=self.take();assert result is not None
        return result
    def open(self,session=1,seq=1):
        f,v=self.response(wire(1,0,seq,struct.pack('<I',session)))
        assert f.session_id==0 and v==dict(result_code=0,subject_id=0,accepted_session_id=session)
        return self

def tests():
    for mode in range(2):
        m=Model()
        assert m.call('model_binding_invalid_request',mode)==2 and m.get('model_submit_count')==0
    passed('真实Firmware绑定拒绝非法内部kind/command，不形成Runtime请求或Motor调用')
    m=Model();assert m.call('model_binding_complete',0)==1 and m.get('model_binding_output_ticket')==5
    for mode in range(1,5):
        m=Model();assert m.call('model_binding_complete',mode)==0
        assert m.get('model_failure')==6 and m.get('model_permitted')==0
        assert m.get('model_binding_output_ticket')==0x5A5A5A5A
    passed('真实Firmware绑定规范化合法Completion；非法kind/outcome/Motor/参数结果Fail-closed且不发布半值')
    m=Model();m.open();m.open(seq=2)
    assert m.get('model_submit_count')==0
    assert m.response(req(session=0))[1]['result_code']==py.Result.SESSION_REQUIRED
    assert m.response(req(session=9))[1]['result_code']==py.Result.SESSION_INVALID
    assert m.response(req(seq=1))[1]['result_code']==0
    m.open(2);assert m.response(req(session=1,seq=2))[1]['result_code']==py.Result.SESSION_INVALID
    assert m.response(req(session=2,seq=1))[1]['result_code']==0
    passed('新会话只重置协议状态；同ID幂等、旧Session拒绝且不改变新会话序号')
    m=Model().open();data=req(3,0x102,seq=1,value=dict(kp=.002,ki=.006))
    m.feed(data);m.feed(data);assert m.take()[1]['result_code']==py.Result.IN_PROGRESS
    assert m.get('model_submit_count')==1
    assert m.response(req(2,1,seq=2))[1]['result_code']==py.Result.BUSY
    assert m.response(wire(1,0,1,struct.pack('<I',2)))[1]['result_code']==py.Result.SESSION_BUSY
    m.call('model_rt_step');m.poll();original=m.take()
    assert original[1]['value']['generation']==2
    assert m.response(data)==original and m.get('model_submit_count')==1
    conflict=req(3,0x102,seq=1,value=dict(kp=.003,ki=.006))
    assert m.response(conflict)[1]['result_code']==py.Result.SEQUENCE_CONFLICT
    assert m.response(data)==original
    passed('处理中IN_PROGRESS、完成后原响应重放；相同身份不同内容冲突，不重复写参数')
    m=Model().open();bad=wire(3,1,1,b'\x02\x01\0')
    old=m.response(bad)
    assert old[1]['result_code']==py.Result.LENGTH_MISMATCH
    assert m.response(bad)==old
    assert m.response(req(3,0x102,seq=1,value=dict(kp=.002,ki=.004)))[1]['result_code']==py.Result.SEQUENCE_CONFLICT
    for seq in range(2,7):assert m.response(req(seq=seq))[1]['result_code']==0
    assert m.response(bad)[1]['result_code']==py.Result.SEQUENCE_INVALID
    for seq in [0,0xFFFFFFFF]:assert m.response(req(seq=seq))[1]['result_code']==py.Result.SEQUENCE_INVALID
    assert m.response(req(seq=0xFFFFFFFE))[1]['result_code']==0
    assert m.response(req(seq=7))[1]['result_code']==py.Result.SEQUENCE_INVALID
    passed('新拒绝退休并缓存；缓存淘汰旧序号拒绝，保留序号及不回绕规则')
    m=Model().open()
    for seq,obj in enumerate(py.READ_SIZES,1):
        response=m.response(req(subject=obj,seq=seq))[1]
        assert response['result_code']==0 and response['subject_id']==obj
    for seq,(obj,payload,result) in enumerate([(1,b'\0'*24,py.Result.READ_ONLY),(0x8888,b'',py.Result.UNKNOWN_OBJECT),
        (0x102,b'\0'*7,py.Result.LENGTH_MISMATCH),(0x102,struct.pack('<2f',float('nan'),.004),py.Result.BAD_VALUE)],20):
        assert m.response(wire(3,1,seq,struct.pack('<H',obj)+payload))[1]['result_code']==result
    m.put('model_healthy',0)
    assert m.response(req(3,0x102,seq=30,value=dict(kp=.002,ki=.006)),True)[1]['result_code']==py.Result.OBJECT_WRONG_STATE
    assert m.response(req(4,4,seq=31),True)[1]['motor_result']==1
    assert m.response(req(4,1,seq=32,target_velocity_rpm=60),True)[1]['motor_result']==2
    passed('七对象精确布局/只读/值域，写权限来自权威实时结果；Motor拒绝仍是协议OK')
    m=Model().open();m.put('model_start_ready',1)
    accepted=m.response(req(4,1,seq=1,target_velocity_rpm=60),True)[1]
    state=m.response(req(2,2,seq=2))[1]['value']['motor_state']
    assert accepted['result_code']==0 and accepted['motor_result']==1 and state==1
    m=Model().open();m.put('model_submit_override',5)
    precondition=m.response(req(4,1,seq=1,target_velocity_rpm=60))[1]
    assert precondition['result_code']==py.Result.PRECONDITION_FAILED and precondition['motor_result']==0xFFFF
    passed('真实Motor接受不等于RUN；交接前置失败与Motor业务结果保持独立')
    m=Model().open();ordinary=req(3,0x102,seq=1,value=dict(kp=.003,ki=.006));m.feed(ordinary)
    stop=req(4,3,session=0,seq=10);m.feed(stop)
    m.call('model_rt_step');m.poll()
    values=[m.take(),m.take()]
    assert values[0][1]['result_code']==py.Result.CANCELLED_BY_STOP
    assert values[1][1]['motor_result']==1 and values[1][0].session_id==0
    assert m.response(stop,True)==values[1] and m.get('model_submit_count')==3
    assert m.response(ordinary)==values[0]
    assert m.response(req(2,0x102,seq=2))[1]['value']['generation']==1
    passed('sessionless Stop优先取消未消费普通ticket；普通取消终态仍缓存，已完成Stop可再提交')
    m=Model().open();stop=req(4,3,session=0,seq=10)
    m.feed(stop);m.feed(stop)
    assert m.take()[1]['result_code']==py.Result.IN_PROGRESS and m.get('model_submit_count')==1
    assert m.call('model_current_session')==1 and m.call('model_highest_sequence')==0
    m.call('model_rt_step');m.feed(stop)
    assert m.take()[1]['result_code']==py.Result.IN_PROGRESS and m.get('model_submit_count')==1
    m.poll();first=m.take()
    assert first[1]['motor_result']==1 and m.call('model_motor_commands')==1
    assert m.response(stop,True)==first and m.get('model_submit_count')==2
    assert m.call('model_motor_commands')==2 and m.call('model_current_session')==1
    assert m.call('model_highest_sequence')==0
    assert m.response(req(seq=1))[1]['result_code']==0
    passed('无会话Stop在途仅提交一次；终态后相同帧再提交完成，不改变Session或普通序号水位')
    m=Model();stop=req(4,3,session=0,seq=10)
    first=m.response(stop,True)
    assert m.response(stop,True)==first and m.get('model_submit_count')==2
    assert m.call('model_motor_commands')==2 and m.call('model_current_session')==0
    passed('尚无Session时相同无会话Stop完成后仍可再执行，不因Session同为0进入完成缓存')
    m=Model().open();m.feed(req(4,3,session=0,seq=10));other=req(4,3,session=0,seq=11)
    assert m.response(other)[1]['result_code']==py.Result.BUSY and m.get('model_submit_count')==1
    m.call('model_rt_step');m.poll();assert m.take()[1]['motor_result']==1
    assert m.response(other,True)[1]['motor_result']==1 and m.get('model_submit_count')==2
    assert m.call('model_current_session')==1
    passed('其他无会话Stop在途返回BUSY且不缓存拒绝；原Stop完成后该帧可以提交')
    m=Model().open();stop=req(4,3,session=1,seq=1)
    m.feed(stop);m.feed(stop)
    assert m.take()[1]['result_code']==py.Result.IN_PROGRESS and m.get('model_submit_count')==1
    m.call('model_rt_step');m.poll();first=m.take()
    assert m.response(stop)==first and m.get('model_submit_count')==1
    assert m.call('model_motor_commands')==1
    assert m.response(wire(4,1,1,b'\x03\0',flags=1))[1]['result_code']==py.Result.SEQUENCE_CONFLICT
    passed('非0 Session的Stop继续在途去重及完成终态重放，只执行一次')
    stop=req(4,3,session=0,seq=10);damaged=bytearray(stop);damaged[-3]^=1
    invalid=[bytes(damaged),b'\xff\0',wire(4,0,10,b'\x03\0',version=2),
             wire(4,0,10,b'\x03\0',flags=1),wire(4,0,10,b'\x03\0',kind=2),
             wire(4,0,10,b'\x03\0\0')]
    for frame in invalid:
        m=Model().open();m.feed(frame);m.take()
        assert m.get('model_submit_count')==0 and m.call('model_motor_commands')==0
        assert m.call('model_current_session')==1
    passed('sessionless Stop仍逐层验证COBS/CRC/Header/Payload；错误帧不提交且不改变Session')
    m=Model().open()
    for communication,telemetry in [(0xFFFFFFFF,0xFFFFFFFF),(0,0),(0,0xFFFFFFFF),(438,254)]:
        m.put(m.symbols['g_system_runtime_status']+36,communication)
        m.put(m.symbols['g_system_runtime_status']+40,telemetry)
        m.call('model_mapping_read_stacks')
        assert m.get('model_mapping_communication_stack')==communication
        assert m.get('model_mapping_telemetry_stack')==telemetry
        assert m.get('model_mapping_unexpected_calls')==0
    passed('真实Target通信状态映射原样保留未测量0xFFFFFFFF、实测0及非零高水位')
    m=Model().open();m.feed(req(4,3,seq=1))
    assert m.response(req(4,3,seq=2))[1]['result_code']==py.Result.BUSY
    m.open(2);m.call('model_rt_step');m.poll()
    f,v=m.take();assert f.session_id==1 and v['motor_result']==1
    assert m.response(req(session=2,seq=1))[1]['result_code']==0
    passed('不同Stop不覆盖原Stop；会话替换保留已排队Stop身份，旧完成不污染新会话')
    m=Model().open();m.feed(req(3,0x102,seq=1,value=dict(kp=.002,ki=.006)))
    m.call('model_rt_step');m.feed(req(4,3,seq=2));m.call('model_rt_step');m.poll()
    a,b=m.take(),m.take();assert a[0].sequence==2 and a[1]['motor_result']==1
    assert b[0].sequence==1 and b[1]['result_code']==0
    passed('Stop不取消已消费并完成的历史请求；关键Stop终态先发送')
    m=Model().open();m.feed(req(4,3,seq=1));m.poll(19);assert m.get('model_failure')==0
    m.poll(20);assert m.get('model_failure')==8 and m.get('model_failure_count')==1
    assert m.take()[1]['result_code']==py.Result.SYSTEM_FAIL_CLOSED
    m.call('model_rt_step');m.poll(30);assert m.get('model_failure_count')==1
    assert m.response(wire(1,0,3,struct.pack('<I',9)))[1]['result_code']==py.Result.SYSTEM_FAIL_CLOSED
    m=Model().open();m.put('model_now',0xFFFFFFF8);m.feed(req(4,3,seq=1));m.poll(11);assert m.get('model_failure')==0
    m.poll(12);assert m.get('model_failure')==8
    m=Model().open();m.feed(req(4,3,seq=1));m.call('model_rt_step');m.poll(20);assert m.get('model_failure')==8
    passed('20ms完成截止及u32时钟回绕；超时先关断/撤会话，迟到结果不救活或重放')
    m=Model().open();m.call('model_unknown_completion');m.poll();assert m.get('model_failure')==6
    m=Model().open()
    for seq in range(1,6):m.feed(req(seq=seq))
    assert m.get('model_failure')==5 and m.get('model_failure_count')==1
    m=Model().open();m.feed(req(seq=1));m.put('model_tx_error',1);m.call('model_flush');assert m.get('model_failure')==7
    for result in [3,4,99]:
        m=Model().open();m.put('model_submit_override',result);m.feed(req(4,3,seq=1));assert m.get('model_failure')==6
    for corruption in [0,1,2,3,4]:
        m=Model().open();m.feed(req(4,1,seq=1,target_velocity_rpm=60))
        m.call('model_corrupt_state',corruption);m.poll();assert m.get('model_failure')==6
    passed('未知ticket、终态FIFO溢出及不可恢复Transport错误锁首因，不静默丢响应')
    m=Model().open();request=req(seq=1)
    for split in range(1,len(request)):
        m=Model().open();m.feed(request[:split]);assert m.take() is None
        m.feed(request[split:]);assert m.take()[1]['result_code']==0
    m=Model().open();damaged=bytearray(request);damaged[-3]^=1;m.feed(damaged);assert m.take() is None
    m.feed(request[:5]);m.poll(20);m.feed(request[5:]);assert m.take() is None
    assert m.response(request)[1]['result_code']==0
    m=Model().open();m.feed(request[:5]);m.call('model_discontinuity');m.feed(request[5:]);assert m.take() is None
    assert m.response(request)[1]['result_code']==0 and m.get('model_failure_count')==0
    passed('所有分片点、CRC/半帧超时/连续性破坏只恢复字节流，不派生Stop或故障')
    m=Model().open();m.feed(req(seq=1));m.put('model_tx_busy',1)
    m.call('model_flush');m.put('model_tx_busy',0);a=m.take();assert a[0].sequence==1
    assert m.take() is None
    for seq,(message,kind,version,flags,result) in enumerate([(0x7777,1,1,0,py.Result.UNKNOWN_MESSAGE),
        (2,1,2,0,py.Result.UNSUPPORTED_VERSION),(2,1,1,1,py.Result.UNSUPPORTED_FLAGS)],2):
        assert m.response(wire(message,1,seq,b'\1\0',kind,version,flags))[1]['result_code']==result
    m.feed(wire(2,1,5,b'\1\0',2));assert m.take() is None
    status=m.response(req(2,3,seq=6))[1]['value']
    assert status['message_errors']==3 and status['version_errors']==1
    passed('TX BUSY保留完整终态；未知消息/版本/flags明确拒绝，错误kind不执行')

    m=Model().open();m.call('model_produce',1)
    f,v=m.take();assert f.kind==3 and f.sequence==1 and v['sample_sequence']==4
    m.call('model_produce',2);m.put('model_tx_busy',1);m.call('model_flush')
    assert m.get('model_telemetry_pending')==1
    m.call('model_produce',4);m.put('model_tx_busy',0)
    assert m.take()[0].sequence==4 and m.get('model_telemetry_pending')==0
    m.call('model_produce',5);m.feed(req(seq=1));assert m.take()[0].kind==2
    assert m.take()[0].sequence==5
    passed('单槽遥测BUSY不取走；产生计数映射在线序号、覆盖表现为缺口；终态优先')

    m=Model().open();encodes=[]
    address=m.symbols['core_frame_encode'] & ~1
    m.cpu.hook_add(UC_HOOK_CODE,lambda cpu,pc,size,user:encodes.append(1) if pc==address else None)
    # OPEN已执行编码路径；清旧翻译块使新Hook真正覆盖该入口。
    m.cpu.ctl_flush_tb()
    m.call('model_produce',1);m.put('model_tx_busy',1)
    for _ in range(5):m.call('model_flush')
    assert len(encodes)==1 and m.get('model_telemetry_pending')==1,(len(encodes),m.get('model_telemetry_pending'))
    m.put('model_tx_busy',0);f,v=m.take()
    assert f.kind==3 and f.sequence==1 and v['sample_sequence']==4 and len(encodes)==1
    passed('真实ARM编码入口计数：同一Mailbox五次BUSY只编码一次，接受前不消费、不增加发送槽')

    m=Model().open();m.call('model_produce',1);m.put('model_tx_busy',1);m.call('model_flush')
    m.call('model_produce',3);m.call('model_flush');m.put('model_tx_busy',0)
    f,v=m.take();assert f.sequence==3 and v['sample_sequence']==12
    m.call('model_produce',4);m.put('model_tx_busy',1);m.call('model_flush')
    m.feed(req(seq=1));m.put('model_tx_busy',0)
    assert m.take()[0].kind==2
    f,v=m.take();assert f.kind==3 and f.sequence==4
    passed('覆盖后的最新产生身份重新编码；终态抢先复用wire后，旧遥测编码不能误发')

    m=Model().open();m.call('model_produce',1);m.put('model_tx_busy',1);m.call('model_flush')
    m.call('model_event_state',2);m.poll();m.put('model_tx_busy',0)
    assert m.take()[0].kind==4 and m.take()[0].kind==3
    m=Model().open();m.call('model_produce',1);m.put('model_tx_busy',1);m.call('model_flush')
    pending=req(3,0x102,seq=1,value=dict(kp=.002,ki=.006));m.feed(pending);m.feed(pending)
    m.put('model_tx_busy',0)
    f,v=m.take();assert f.kind==3 and f.sequence==1 and v['sample_sequence']==4
    passed('Event与IN_PROGRESS提示改写工作区会撤销遥测复用资格，优先级与帧内容保持正确')

    m=Model().open();m.call('model_produce',1);m.open(2)
    assert m.take() is None and m.get('model_telemetry_pending')==0
    m.call('model_produce',2);m.call('model_old_epoch');assert m.take() is None
    m.call('model_produce',1);assert m.take()[0].sequence==1
    m.call('model_produce',2)
    assert m.response(req(3,0x201,session=2,seq=1,value=dict(enabled=1,rate_hz=50)),True)[1]['result_code']==0
    assert m.take() is None
    m.call('model_produce',3);assert m.take()[0].sequence==3
    passed('新Session清序号且丢弃队列旧epoch和迟到旧值；配置切换丢旧generation并保持线上单调')

    m=Model().open();m.call('model_produce',1)
    m.call('model_event_state',2);m.poll()
    m.feed(req(seq=1));assert m.take()[0].kind==2
    f,v=m.take();assert f.kind==4 and f.message_id==0x2001 and f.sequence==1
    assert v['first']==1 and v['second']==2
    assert m.take()[0].kind==3
    m.call('model_event_state',3);m.poll()
    assert m.take()[0].message_id==0x2001
    f,v=m.take();assert f.message_id==0x2002 and v['first']==5 and v['second']==5
    m.poll();assert m.take() is None
    passed('有效快照变化生成STATE_CHANGED/FAULT_LATCHED，仅新锁存位；事件优先于遥测')

    m=Model().open()
    for i in range(12):m.call('model_event_state',2 if i%2==0 else 1);m.poll()
    status=m.response(req(subject=3))[1]['value']
    assert status['event_drop_count']==4 and status['event_fifo_high_water']==8
    assert [m.take()[0].sequence for _ in range(8)]==list(range(5,13))
    m.call('model_event_state',2);m.poll();m.open(2);assert m.take() is None
    assert m.get('model_failure')==0
    passed('事件FIFO8满丢最旧并计数；会话替换清旧事件且不故障')

    m=Model().open();m.call('model_exhaust_sequences');m.call('model_produce',0xFFFFFFFE)
    assert m.take()[0].sequence==0xFFFFFFFE
    m.call('model_produce',0xFFFFFFFF);assert m.take() is None
    m.call('model_event_state',2);m.poll();assert m.take()[0].sequence==0xFFFFFFFE
    m.call('model_event_state',1);m.poll();assert m.take() is None
    assert m.response(req(seq=1))[1]['result_code']==0
    m.open(2);m.call('model_produce',1);assert m.take()[0].sequence==1
    passed('Telemetry/Event独立耗尽不回绕、不改Session或Motor；新Session恢复独立序号域')

def host_tests():
    from host.protocol.client import Client, RequestTimeout, ConnectionLost, JsonlLogger
    class Clock:
        value=0.
        def __call__(self):return self.value
    class Stream:
        timeout=None
        write_timeout=None
        def __init__(self):
            self.model=Model();self.clock=Clock();self.writes=[];self.drop=0;self.buffer=b''
        def write(self,data):
            self.writes.append(data);self.model.put('model_now',int(self.clock()*1000))
            self.model.feed(data[:7]);self.model.feed(data[7:]);return len(data)
        def read(self,size):
            self.clock.value+=.001;self.model.put('model_now',int(self.clock()*1000))
            self.model.call('model_rt_step');self.model.poll()
            response=self.model.take()
            if response:
                if self.drop:self.drop-=1
                else:self.buffer+=py.encode_frame(response[0])
            data,self.buffer=self.buffer[:min(size,17)],self.buffer[min(size,17):]
            return data
    stream=Stream();client=Client(stream,clock=stream.clock)
    assert stream.timeout==.005 and stream.write_timeout==.100
    assert client.connect(123)['result_code']==0 and client.sequence==4
    assert set(client.last_recovery)=={'motor_snapshot','speed_pi_staged','speed_pi_active'}
    assert stream.model.get('model_submit_count')==0
    client.connect(123);assert client.sequence==7 and stream.model.get('model_submit_count')==0
    passed('Python显式连接只读Snapshot/Staged/Active；同会话不重置Sequence，不恢复运动')
    stream.drop=1;before=len(stream.writes)
    response=client.write_pi(.002,.006)
    assert response['result_code']==0 and response['value']['generation']==2
    assert stream.model.get('model_submit_count')==1 and client.retransmissions==1
    assert len(stream.writes)-before==2 and stream.writes[-2]==stream.writes[-1]
    passed('Python响应丢失后只重发完全相同线上身份，真实ARM参数只应用一次')
    stream.drop=3;before=len(stream.writes)
    try:client.execute(1,60)
    except RequestTimeout:pass
    else:raise AssertionError('expected request timeout')
    assert client.session==0 and len(stream.writes)-before==3 and len(set(stream.writes[before:]))==1
    calls=stream.model.get('model_submit_count')
    try:client.read_object(1)
    except ConnectionLost:pass
    else:raise AssertionError('must explicitly reconnect')
    client.connect(456);assert stream.model.get('model_submit_count')==calls
    passed('Python总超时结果未知并撤本地资格；新会话不重放旧Start，只重新读状态')
    # 旧Session和同身份但subject不符的响应不能误配到当前请求。
    future=client.sequence
    for session,subject in [(123,1),(456,0x101)]:
        value=dict(result_code=0,subject_id=subject,value=(dict(kp=.001,ki=.004) if subject==0x101 else
            dict(zip(py.DEVICE_FIELDS,[1,1,1,1,0x27,20000,1]))))
        stream.buffer+=py.encode_frame(py.Frame(2,2,session,future,py.encode_response(2,value)))
    assert client.read_object(1)['value']['firmware_build_id']==0x123456789ABCDEF0
    assert client.ignored_frames>=2
    client.stop_sequence=1001;stop=client.stop();assert stop['result_code']==0 and stop['motor_result']==1
    assert py.decode_frame(stream.writes[-1][:-1])[1].session_id==0
    passed('Python按message/Session/Sequence/subject关联，过滤旧响应；Stop无需会话')
    with tempfile.TemporaryDirectory(prefix='protocol-log-') as directory:
        path=Path(directory)/'capture.jsonl'
        with JsonlLogger(path) as logger:
            client.logger=logger;client.read_object(py.MOTOR_SNAPSHOT)
        rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        assert rows[0]['direction']=='tx' and rows[-1]['direction']=='rx'
        assert rows[-1]['value']['value']['id_a'] is None
        try:JsonlLogger(path)
        except FileExistsError:pass
        else:raise AssertionError('must not overwrite log')
    passed('JSONL保留线上身份/重试/解码值，无效浮点转null，已有记录拒绝覆盖')
    client.logger=None
    # 在一次read块内放入终态之后的遥测/事件，不能在匹配响应后提前return丢掉它们。
    stream=Stream();client=Client(stream,clock=stream.clock);client.connect(789)
    sample=dict(zip(py.SAMPLE_FIELDS,[0]*len(py.SAMPLE_FIELDS)))
    sample.update(valid_flags=0x301,telemetry_config_generation=1)
    def telemetry(sequence,generation=1,session=789):
        sample['telemetry_config_generation']=generation
        return py.encode_frame(py.Frame(3,0x1001,session,sequence,py.encode_sample(sample)))
    for seq in [1,3]:stream.buffer+=telemetry(seq)
    stream.buffer+=telemetry(77,session=123)
    stream.buffer+=py.encode_frame(py.Frame(4,0x2001,789,1,py.encode_event(0x2001,dict(first=1,second=2,control_time_ticks=100))))
    assert client.read_object(1)['result_code']==0
    domain=client.telemetry_domains[(789,1)]
    assert domain['received']==2 and domain['gaps']==1 and domain['order_errors']==0
    assert client.latest_event[1]['second']==2 and (123,1) not in client.telemetry_domains
    stream.buffer+=telemetry(4,2)
    while stream.buffer:client.poll()
    assert client.telemetry_domains[(789,2)]['gaps']==0
    passed('Python在RPC和空闲期间持续解码Telemetry/Event；按Session/config域计缺口，过滤旧会话')
    class DelayedStream:
        def __init__(self):self.clock=Clock();self.chunks=[]
        def read(self,size):
            delay,data=self.chunks.pop(0);self.clock.value+=delay;return data
    delayed=DelayedStream();client=Client(delayed,clock=delayed.clock);client.session=789
    data=telemetry(1)
    delayed.chunks=[(.001,data[:27]),(.050,data[27:])]
    client.poll();client.poll()
    assert client.parser.stats['timeouts']==0 and client.latest_telemetry[0].sequence==1
    delayed.chunks=[(.001,data[:27]),(.021,b''),(.001,data[27:]+telemetry(2))]
    client.poll();client.poll();client.poll()
    assert client.parser.stats['timeouts']==1 and client.latest_telemetry[0].sequence==2
    passed('Host调度停顿不伪装线上空闲；实际观察空读达到20ms仍丢半帧并恢复同步')
    delayed.chunks=[(.001,telemetry(0)),(.001,py.encode_frame(py.Frame(3,0x7777,789,3,b'')))]
    client.poll();client.poll();assert client.async_errors==2
    passed('非法异步序号和未知遥测计错，不能击穿客户端接收循环')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',type=Path);args=parser.parse_args()
    target=ROOT/'firmware/targets/BLDC_G431'
    sources=[ROOT/'firmware/app/communication/motor_node_binding.c',COMM/'core_server.c',COMM/'core_protocol.c',COMM/'core_protocol_values.c',Path(__file__).with_name('core_server_model.c'),
        Path(__file__).with_name('target_core_mapping_model.c'),target/'runtime/target_core_server.c',
        ROOT/'firmware/app/system_runtime/runtime_exchange.c',ROOT/'firmware/app/system_runtime/runtime_parameters.c',ROOT/'firmware/app/motor/motor.c']
    with tempfile.TemporaryDirectory(prefix='g431-core-server-') as directory:
        temp=Path(directory);scatter=temp/'model.sct'
        scatter.write_text('LR 0x08000000 0x40000 { ER 0x08000000 0x40000 { .ANY (+RO) } RW 0x20000000 0x8000 { .ANY (+RW +ZI) } }')
        objects=[]
        for source in sources:
            obj=temp/(source.stem+'.o');objects.append(obj)
            command=[ARM/'armcc.exe','--cpu=Cortex-M4.fp','--c99','-O3','-Otime','--split_sections','-g']
            # 纯协议源仅取得protocol头目录，不能借模型的Firmware include掩盖反向依赖。
            includes=[COMM,PORTS,COMMON]
            if not source.is_relative_to(ROOT/'protocol/uart_v1'):
                includes += [ROOT/'firmware/app/communication',ROOT/'firmware/app/motor',ROOT/'firmware/app/system_runtime']
            for include in includes:command+=['-I',include]
            if source.name in ('target_core_server.c','target_core_mapping_model.c'):
                command+=['-DBLDC_G431_FINITE_RUN','-DSTM32G431xx','-DUSE_HAL_DRIVER']
                for include in [target/'runtime',target/'Core/Inc',ROOT/'firmware/platform_contract',
                    target/'Drivers/CMSIS/Include',target/'Drivers/CMSIS/Device/ST/STM32G4xx/Include',
                    target/'Drivers/STM32G4xx_HAL_Driver/Inc']:
                    command+=['-I',include]
            if source.name=='target_core_server.c':
                # 单独保留真实对象映射，避免与Server业务替身端口同名；未使用装配路径由链接器移除。
                for name in ('read_object','permitted','fail_closed','submit','completion','telemetry_peek','telemetry_retire'):
                    command+=['-Dcore_server_port_'+name+'=mapping_core_server_port_'+name]
            result=subprocess.run([*map(str,command),'-c',str(source),'-o',str(obj)],capture_output=True)
            assert result.returncode==0 and b'warning' not in result.stdout.lower()+result.stderr.lower(),result.stdout+result.stderr
        axf=temp/'model.axf'
        result=subprocess.run([*map(str,[ARM/'armlink.exe','--cpu=Cortex-M4.fp','--scatter',scatter,'--entry=model_init','--keep=core_server_model.o(*)','--keep=target_core_mapping_model.o(*)','-o',axf,*objects])],capture_output=True)
        assert result.returncode==0,result.stdout+result.stderr
        with axf.open('rb') as f:
            elf=ELFFile(f);Model.data=[(s['sh_addr'],s.data()) for s in elf.iter_sections() if s['sh_flags']&2 and s['sh_type']=='SHT_PROGBITS']
            Model.symbols={s.name:s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
        tests();host_tests()
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(dict(status='PASS',groups=groups,hardware_access=False,
            execution='ARMCC Cortex-M4F + Unicorn; real Target object mapping with substituted read inputs; simulated Transport/real-time stepping and bytewise library copy substitute',
            source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),*sources,ROOT/'host/protocol/client.py',ROOT/'host/protocol/codec.py',
                *COMMON.glob('*.h'),*COMM.glob('*.h'),*PORTS.glob('*.h'),ROOT/'firmware/app/communication/motor_node_binding_target.h']}),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
