"""真实G4 HAL与帧适配器的ARM模型；MMIO为替身，不代替控制器实测。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import struct
import subprocess
import tempfile
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_WRITE, UC_HOOK_CODE
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_BASEPRI, UC_ARM_REG_PRIMASK)

TARGET = Path(__file__).resolve().parents[2]
ROOT = TARGET.parents[2]
TESTS = Path(__file__).resolve().parent
HAL = TARGET/'Drivers/STM32G4xx_HAL_Driver'
ARM = Path(os.environ.get('KEIL_ARMCC_BIN', r'D:\keil5\ARM\ARMCC\bin'))
OK, EMPTY, BUSY, NOT_STARTED, HW_ERROR, BAD_ARGUMENT = range(6)
GROUPS = []


def passed(name):
    GROUPS.append(name); print('PASS:', name)


def checked(command):
    result = subprocess.run(list(map(str, command)), capture_output=True)
    assert result.returncode == 0 and b'warning' not in result.stdout.lower()+result.stderr.lower(), result.stdout+result.stderr


def build(temp, node):
    sources = [TARGET/'runtime/fdcan_frame_transport.c', HAL/'Src/stm32g4xx_hal_fdcan.c', TESTS/'fdcan_transport_model.c']
    scatter = temp/'model.sct'
    scatter.write_text('LR 0x08000000 0x40000 { ER 0x08000000 0x40000 { .ANY (+RO) } RW 0x20000000 0x8000 { .ANY (+RW +ZI) } }')
    command = [ARM/'armcc.exe', '--cpu=Cortex-M4.fp', '--c99', '-O2', '--split_sections', '-g',
        '-DSTM32G431xx', '-DUSE_HAL_DRIVER', '-DUSE_FULL_ASSERT', '-DBLDC_G431_COMMUNICATION=2', f'-DBLDC_G431_CAN_NODE_ID={node}']
    for include in [TARGET/'runtime', TARGET/'Core/Inc', ROOT/'protocol/ports', ROOT/'protocol/common', ROOT/'protocol/can_v1',
                    HAL/'Inc', TARGET/'Drivers/CMSIS/Include', TARGET/'Drivers/CMSIS/Device/ST/STM32G4xx/Include']:
        command += ['-I', include]
    objects = []
    for source in sources:
        obj = temp/(source.stem+'.o'); checked(command+['-c', source, '-o', obj]); objects.append(obj)
    axf = temp/'model.axf'
    checked([ARM/'armlink.exe', '--cpu=Cortex-M4.fp', '--scatter', scatter, '--entry=model_prepare',
        '--keep=fdcan_transport_model.o(*)', '--keep=fdcan_frame_transport.o(*)', '-o', axf, *objects])
    with axf.open('rb') as stream:
        elf = ELFFile(stream)
        Model.data = [(s['sh_addr'], s.data()) for s in elf.iter_sections() if s['sh_flags'] & 2 and s['sh_type'] == 'SHT_PROGBITS']
        Model.symbols = {s.name:s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
    return sources


class Model:
    def __init__(self, start=True):
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
        for base, size in [(0x08000000,0x40000),(0x20000000,0x8000),(0x40006000,0x1000),(0x4000A000,0x1000),(0xE000E000,0x1000)]:
            self.cpu.mem_map(base,size)
        for address, contents in self.data: self.cpu.mem_write(address,contents)
        self.put(0xE000ED88,0xF00000)
        raw = bytes(self.cpu.mem_read(self.symbols['model_layout'],37*4))
        layout = dict(zip(['BASE','RAM','IRQ','CCCR','PSR','IR','IE','ILS','ILE','RXGFC','RXF0S','RXF0A','TXFQS','TXBRP','TXBAR','TXBCR','TXBTO',
            'RF0N','RF0L','TC','BO','EW','EP','MRAF','FRAME_SIZE','DATA','rx_frames','tx_frames','rx_lost','tx_errors','bus_off_count',
            'started','tx_in_flight','error_warning','error_passive','bus_off','recovering'],struct.unpack('<37I',raw)))
        self.l = layout; self.base = layout['BASE']; self.ram = layout['RAM']; self.queue=[]; self.init_exits=0; self.ir_clear=None
        self.put(0xE000E100 + (layout['IRQ']//32)*4,1<<(layout['IRQ']%32))
        self.cpu.mem_write(0xE000E400+layout['IRQ'],b'\x50')
        self.cpu.hook_add(UC_HOOK_MEM_WRITE,self.write_hook)
        self.cpu.hook_add(UC_HOOK_CODE,self.code_hook)
        self.call('model_prepare')
        if start:
            assert self.call('model_init')==OK
            assert self.call('model_bind')==OK
            assert self.call('model_start')==OK
    def get(self,address):
        if isinstance(address,str): address=self.symbols[address]
        return struct.unpack('<I',self.cpu.mem_read(address,4))[0]
    def put(self,address,value):
        if isinstance(address,str): address=self.symbols[address]
        self.cpu.mem_write(address,struct.pack('<I',value&0xFFFFFFFF))
    def reg(self,name,value=None):
        address=self.base+self.l[name]
        if value is None:return self.get(address)
        self.put(address,value)
    def write_hook(self,cpu,access,address,size,value,user):
        if address==self.base+self.l['IR']: self.ir_clear=self.reg('IR')&~value
        elif address==self.base+self.l['TXBAR']:
            self.reg('TXBRP',self.reg('TXBRP')|value);self.reg('TXBTO',self.reg('TXBTO')&~value)
        elif address==self.base+self.l['TXBCR']: self.reg('TXBRP',self.reg('TXBRP')&~value)
        elif address==self.base+self.l['RXF0A']:
            if self.queue:self.queue.pop(0)
            self.load_rx()
        elif address==self.base+self.l['CCCR'] and not value&1 and self.reg('CCCR')&1:self.init_exits+=1
    def code_hook(self,cpu,address,size,user):
        if self.ir_clear is not None:
            value,self.ir_clear=self.ir_clear,None
            self.reg('IR',value)
    def call(self,name,value=0):
        self.cpu.reg_write(UC_ARM_REG_SP,0x20007FF0);self.cpu.reg_write(UC_ARM_REG_LR,0x0803F001)
        self.cpu.reg_write(UC_ARM_REG_R0,value)
        self.cpu.emu_start(self.symbols[name]|1,0x0803F000,count=400000)
        assert self.cpu.reg_read(UC_ARM_REG_PC)==0x0803F000,name
        assert self.get('model_asserts')==0,(name,'HAL assertion')
        return self.cpu.reg_read(UC_ARM_REG_R0)
    def status(self):
        result=self.call('model_status_read');base=self.symbols['model_status']
        fields={k:self.get(base+self.l[k]) for k in ['rx_frames','tx_frames','rx_lost','tx_errors','bus_off_count']}
        fields.update({k:bool(self.cpu.mem_read(base+self.l[k],1)[0]) for k in ['started','tx_in_flight','error_warning','error_passive','bus_off','recovering']})
        return result,fields
    def load_rx(self):
        self.reg('RXF0S',len(self.queue))
        if self.queue:self.cpu.mem_write(self.ram+176,self.queue[0])
    def receive(self,identifier=0x101,payload=bytes(range(8)),dlc=8,fd=False,rtr=False,extended=False):
        word0=(identifier if extended else identifier<<18)|(int(extended)<<30)|(int(rtr)<<29)
        self.queue.append(struct.pack('<II',word0,(dlc<<16)|(int(fd)<<21))+payload.ljust(64,b'\x00'))
        self.load_rx();self.reg('IR',self.reg('IR')|self.l['RF0N'])
    def finish_tx(self):
        pending=self.reg('TXBRP');self.reg('TXBRP',0);self.reg('TXBTO',pending)
        self.reg('IR',self.reg('IR')|self.l['TC']);self.call('model_irq')
    def busoff(self):
        self.reg('PSR',0x80);self.reg('CCCR',self.reg('CCCR')|1)
        self.reg('IR',self.reg('IR')|self.l['BO']);self.call('model_irq')


def exercise():
    m=Model(start=False);assert m.call('model_start')==NOT_STARTED
    assert m.call('model_init')==OK and m.call('model_init')==BUSY
    assert m.call('model_bind')==OK and m.call('model_bind')==BUSY
    assert m.call('model_start')==OK and m.call('model_start')==BUSY
    assert m.reg('ILE')==1 and m.reg('ILS')==0 and m.reg('IE')
    passed('初始化不开放RX；先绑定再启动，单次装配及真实HAL中断组/通知配置通过FULL_ASSERT')
    for kind in range(6):
        m=Model(start=False);m.call('model_bad_init',kind);assert m.call('model_init')==HW_ERROR
    m=Model(start=False);m.cpu.mem_write(0xE000E400+m.l['IRQ'],b'\x40');assert m.call('model_init')==HW_ERROR
    passed('错误位时序/模式/自动重传/过滤器容量/帧格式及非法IRQ优先级拒绝装配')
    m=Model();m.receive();m.call('model_irq');assert m.get('model_irq_events')==1
    assert m.call('model_pop')==OK and m.status()[1]['rx_frames']==1 and m.call('model_pop')==EMPTY
    assert bytes(m.cpu.mem_read(m.symbols['model_rx']+m.l['DATA'],8))==bytes(range(8))
    for dlc,fd in [(0,False),(7,False),(9,True),(15,True)]:
        m.receive(payload=bytes(range(64)),dlc=dlc,fd=fd)
        assert m.call('model_pop')==OK and m.cpu.mem_read(m.symbols['model_rx']+2,1)==b'\x00'
    passed('真实HAL从Message RAM取8字节；DLC0/7/12/64和FD不获有效长度，64字节缓冲不越界')
    m=Model();tx=m.symbols['model_tx'];payload=b'CAN-test'
    m.cpu.mem_write(tx+m.l['DATA'],payload);assert m.call('model_send')==OK
    assert m.reg('TXBRP')==1 and m.get(m.ram+636)==8<<16
    assert bytes(m.cpu.mem_read(m.ram+640,8))==payload
    m.cpu.mem_write(tx+m.l['DATA'],b'changed!');assert m.call('model_send')==BUSY
    assert bytes(m.cpu.mem_read(m.ram+640,8))==payload
    m.finish_tx();assert m.status()[1]['tx_frames']==1
    m.call('model_irq');assert m.status()[1]['tx_frames']==1 and m.call('model_send')==OK
    passed('真实HAL按原值DLC8写完整TX；单帧在途不覆盖，TX完成IRQ仅退休一次且支持下一帧')
    m=Model();m.reg('PSR',0x60);m.reg('IR',m.l['EW']|m.l['EP']);m.call('model_irq')
    ok,s=m.status();assert ok and s['error_warning'] and s['error_passive'] and not s['bus_off']
    m.reg('IR',m.l['RF0L']);m.call('model_irq');assert m.status()[1]['rx_lost']==1
    assert m.get('model_irq_events')==2
    passed('Warning/Passive只发布状态；FIFO lost由真实HAL IRQ报告一次，留给Server执行Fail-closed')
    m=Model();assert m.call('model_send')==OK;m.put('model_ms',100)
    assert m.status()[0]==0 and m.reg('IE')==0 and m.reg('ILE')==0
    assert m.status()[1]['tx_errors']==1 and m.call('model_send')==HW_ERROR
    m=Model();m.reg('IR',m.l['MRAF']);m.call('model_irq');assert not m.status()[0]
    passed('发送100ms截止与Message RAM内部错误停止通信；重复读取不重复锁故障或重试')
    m=Model();assert m.call('model_send')==OK;m.receive();m.busoff()
    assert m.status()[1]['bus_off_count']==1 and m.call('model_pop')==BUSY and m.call('model_send')==BUSY
    exits=m.init_exits;assert m.call('model_recover')==BUSY
    assert m.reg('TXBRP')==0 and not m.queue and m.init_exits==exits+1
    for _ in range(3):assert m.call('model_recover')==BUSY
    assert m.init_exits==exits+1
    m.reg('PSR',0);assert m.call('model_recover')==OK
    ok,s=m.status();assert ok and not s['bus_off'] and not s['recovering'] and not s['tx_in_flight']
    assert m.call('model_send')==OK
    passed('Bus-off锁存禁收发；恢复先撤旧TX/排旧RX，只退出INIT一次，硬件BO未解除时始终BUSY')
    m=Model();m.busoff();assert m.call('model_recover')==BUSY;m.put('model_ms',200)
    assert m.call('model_recover')==HW_ERROR and not m.status()[0]
    m=Model();m.cpu.reg_write(UC_ARM_REG_BASEPRI,0x30);m.cpu.reg_write(UC_ARM_REG_PRIMASK,1)
    m.status();assert m.cpu.reg_read(UC_ARM_REG_BASEPRI)==0x30 and m.cpu.reg_read(UC_ARM_REG_PRIMASK)==1
    m.put('model_ms',0xFFFFFFF0);assert m.call('model_send')==OK;m.put('model_ms',83);assert m.status()[0]
    m.put('model_ms',84);assert not m.status()[0]
    passed('恢复200ms有限截止、u32计时回绕及调用者BASEPRI/PRIMASK保留')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',type=Path);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='g431-fdcan-') as directory:
        temp=Path(directory)
        sources=build(temp,1);exercise()
        for node in [1,127]:
            if node!=1:build(temp,node)
            m=Model()
            words=[m.get(m.ram+4*i) for i in range(3)]
            # RM0440标准掩码过滤：SFT=10、SFEC=001、SFID1、SFID2=7FF。
            expected=[0x880007FF | (identifier<<16) for identifier in [0,node,0x100+node]]
            assert words==expected,(node,words,expected)
            # RRFE/RRFS和ANFE/ANFS均拒绝，LSS保留3。
            assert m.reg('RXGFC')&0x3F==0x2B,hex(m.reg('RXGFC'))
        passed('Node1及Node127的实际HAL过滤器RAM仅匹配0/node/0x100+node，拒绝扩展/RTR和所有非匹配帧')
    if args.report:
        paths=[*sources,Path(__file__),TARGET/'runtime/target_communication_config.h',HAL/'Inc/stm32g4xx_hal_fdcan.h',ROOT/'protocol/ports/can_frame_port.h']
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(dict(status='PASS',groups=GROUPS,hardware_access=False,
            execution='ARMCC Cortex-M4F + Unicorn; actual CubeG4 HAL and transport; MMIO/FIFO/TX completion/clock/C-library copy substituted',
            limitations='不证明FDCAN硬件、真实IRQ调度/位时序/ACK/仲裁/Bus-off，恢复BO解除由模型注入',
            source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()
