"""真实ARM UART DMA适配器；MMIO模拟DMA推进/W1C，覆盖复制期间并发和所有权故障。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import random
import struct
import subprocess
import tempfile

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_WRITE, UC_HOOK_CODE
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_BASEPRI, UC_ARM_REG_PRIMASK

TARGET = Path(__file__).resolve().parents[2]
ROOT = TARGET.parents[2]
TESTS = Path(__file__).resolve().parent
COMPILER = Path(os.environ.get('KEIL_ARMCC_BIN', r'D:\keil5\ARM\ARMCC\bin'))
DMA, RX, TX, UART, DWT = 0x40020000, 0x4002001C, 0x40020030, 0x40013800, 0xE0001004
EN, TCIE, HTIE, TEIE, DIR, CIRC, MINC = 1, 2, 4, 8, 16, 32, 128
HT2, TC2, TE2, TC3, TE3 = 64, 32, 128, 512, 2048
IDLE, TC = 16, 64
OK, EMPTY, BUSY, TOO_LARGE, NOT_STARTED, HW_ERROR, DISCONTINUITY, BAD_ARGUMENT = range(8)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', type=Path)
args = parser.parse_args()
groups = []


def passed(name):
    groups.append(name); print('PASS:', name)


def checked(command, cwd):
    result = subprocess.run([str(x) for x in command], cwd=cwd, capture_output=True)
    output = (result.stdout + result.stderr).decode(errors='replace')
    assert result.returncode == 0 and 'warning' not in output.lower(), output


sources = [TARGET/'runtime/uart_dma_transport.c', TESTS/'uart_dma_model.c']
with tempfile.TemporaryDirectory(prefix='g431-uart-dma-') as directory:
    temp = Path(directory)
    scatter = temp/'model.sct'
    scatter.write_text('LR 0x08000000 0x40000 { ER 0x08000000 0x40000 { .ANY (+RO) } '
                       'RW 0x20000000 0x8000 { .ANY (+RW +ZI) } }')
    command = [COMPILER/'armcc.exe', '--cpu=Cortex-M4.fp', '--c99', '-O2', '--split_sections', '-g', '-DSTM32G431xx', '-DUSE_HAL_DRIVER', '-DBLDC_G431_COMMUNICATION=1']
    for include in [TARGET/'runtime', TARGET/'Core/Inc', ROOT/'protocol/ports',
                    TARGET/'Drivers/STM32G4xx_HAL_Driver/Inc', TARGET/'Drivers/CMSIS/Include',
                    TARGET/'Drivers/CMSIS/Device/ST/STM32G4xx/Include']:
        command += ['-I', include]
    objects = []
    for source in sources:
        obj = temp/(source.stem+'.o'); checked(command+['-c', source, '-o', obj], temp); objects.append(obj)
    axf = temp/'model.axf'
    checked([COMPILER/'armlink.exe','--cpu=Cortex-M4.fp','--scatter',scatter,'--entry=model_init',
             '--keep=uart_dma_model.o(*)','--keep=uart_dma_transport.o(*)','-o',axf,*objects], temp)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        (args.report.parent/'uart-model-disassembly.txt').write_bytes(subprocess.check_output([str(COMPILER/'fromelf.exe'),'--text','-c',str(axf)]))
    with axf.open('rb') as stream:
        elf = ELFFile(stream)
        data = [(s['sh_addr'],s.data()) for s in elf.iter_sections()
                if s['sh_flags']&2 and s['sh_type']=='SHT_PROGBITS']
        symbols = {s.name:s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}

    class Model:
        def __init__(self, start=True):
            self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
            for base,size in [(0x08000000,0x40000),(0x20000000,0x8000),(0x40013000,0x1000),
                              (DMA,0x1000),(0xE0001000,0x1000),(0xE000E000,0x1000)]:
                self.cpu.mem_map(base,size)
            for address,contents in data: self.cpu.mem_write(address,contents)
            self.put(0xE000ED88,0xF00000)
            self.put(0xE0001000,1)
            self.put(0xE000E100,(1<<12)|(1<<13)); self.put(0xE000E104,1<<5)
            for irq in [12,13,37]: self.cpu.mem_write(0xE000E400+irq,bytes([0x50]))
            self.put(RX,CIRC|MINC); self.put(TX,DIR|MINC)
            self.put(DMA+0x804,24); self.put(DMA+0x808,25)
            self.put(UART,13); self.put(UART+12,369)
            self.after_copy = None; self.pending_copy = False; self.position=0
            self.cpu.hook_add(UC_HOOK_MEM_WRITE,self.write_hook)
            self.cpu.hook_add(UC_HOOK_CODE,self.code_hook)
            if start:
                assert self.call('model_init')==OK
                assert self.call('model_bind')==OK
                assert self.call('model_start')==OK

        def get(self,address):
            if isinstance(address,str): address=symbols[address]
            return struct.unpack('<I',self.cpu.mem_read(address,4))[0]
        def put(self,address,value):
            if isinstance(address,str): address=symbols[address]
            self.cpu.mem_write(address,struct.pack('<I',value&0xFFFFFFFF))
        def write_hook(self,cpu,access,address,size,value,user):
            if address==DMA+4:
                flags=self.get(DMA)
                for n in range(7):
                    mask=0xF<<(4*n)
                    if value&(1<<(4*n)): flags&=~mask
                    else: flags&=~(value&mask)
                self.put(DMA,flags)
            elif address==UART+32: self.put(UART+28,self.get(UART+28)&~value)
            elif symbols['model_destination']<=address<symbols['model_destination']+1024:
                if self.after_copy: self.pending_copy=True
        def code_hook(self,cpu,address,size,user):
            if self.pending_copy:
                self.pending_copy=False
                action,self.after_copy=self.after_copy,None
                action(self)
        def call(self,name,*values):
            self.cpu.reg_write(UC_ARM_REG_SP,0x20007FF0)
            self.cpu.reg_write(UC_ARM_REG_LR,0x0803F001)
            for reg,value in zip([UC_ARM_REG_R0],values):self.cpu.reg_write(reg,value)
            self.cpu.emu_start(symbols[name]|1,0x0803F000,count=200000)
            assert self.cpu.reg_read(UC_ARM_REG_PC)==0x0803F000,name
            return self.cpu.reg_read(UC_ARM_REG_R0)
        def advance(self,cycles):
            self.put(DWT,self.get(DWT)+cycles)
            self.put('model_ms',self.get(DWT)//170000)
        def receive(self,contents,irq=False):
            for byte in contents:
                self.cpu.mem_write(symbols['s_rx']+self.position,bytes([byte]))
                self.position=(self.position+1)&511
                if self.position==256:self.put(DMA,self.get(DMA)|HT2)
                if self.position==0:self.put(DMA,self.get(DMA)|TC2)
            self.put(RX+4,512-self.position)
            self.advance(len(contents)*3690)
            if irq:self.call('g431_uart_rx_dma_irq')
        def read(self,capacity=128):
            result=self.call('model_read',capacity)
            count=self.get('model_count')
            return result,bytes(self.cpu.mem_read(symbols['model_destination'],count)),self.get('model_epoch')
        def stats(self):
            assert self.call('model_read_stats')==1
            raw=bytes(self.cpu.mem_read(symbols['model_stats'],56))
            names=['rx_bytes','tx_bytes','rx_discontinuities','buffer_overruns','parity_errors',
                   'framing_errors','noise_errors','hardware_rx_overruns','tx_busy_count','tx_errors',
                   'produced_bytes','consumed_bytes','continuity_epoch']
            return dict(zip(names,struct.unpack('<13I',raw[:52])),rx_started=bool(raw[52]),tx_in_flight=bool(raw[53]))
        def tx_dma_complete(self):
            self.put(TX+4,0);self.put(DMA,self.get(DMA)|TC3);self.call('g431_uart_tx_dma_irq')
        def tx_wire_complete(self):
            self.put(UART+28,self.get(UART+28)|TC);self.call('g431_uart_irq')

    m=Model(False)
    actual_layout=struct.unpack('<16I',m.cpu.mem_read(symbols['model_stats_layout'],64))
    assert actual_layout==tuple([56,*range(0,52,4),52,53]),actual_layout
    assert m.call('model_start')==NOT_STARTED and m.call('model_send',1)==NOT_STARTED
    assert m.call('model_init')==OK and not m.get(RX)&EN
    assert m.call('model_start')==NOT_STARTED
    assert m.call('model_bind')==OK and m.call('model_bind')==BUSY
    assert m.call('model_start')==OK and m.call('model_start')==BUSY
    assert m.call('model_send',0)==BAD_ARGUMENT and m.call('model_send',131)==TOO_LARGE
    assert m.read()[0]==EMPTY
    passed('初始化不开放RX；绑定后才启动，重复/非法调用不破坏所有权')
    for address,value in [(UART+12,1476),(RX,MINC),(TX,CIRC|DIR|MINC),(DMA+0x804,25),(0xE000E40C,0)]:
        m=Model(False)
        if address==0xE000E40C:m.cpu.mem_write(address,b'\0')
        else:m.put(address,value)
        assert m.call('model_init')==HW_ERROR
    passed('真实硬件分频、DMA模式/请求和NVIC不符合合同则拒绝装配')
    m=Model();expected=bytes(range(256))*16;received=bytearray()
    rng=random.Random(431)
    for begin in range(0,len(expected),47):
        part=expected[begin:begin+47];m.receive(part,irq=rng.choice([False,True]))
        m.put(UART+28,IDLE);m.call('g431_uart_irq');m.call('g431_uart_rx_dma_irq')
        while True:
            result,chunk,epoch=m.read(rng.randint(1,130))
            if result==EMPTY:break
            assert result==OK and epoch==0
            assert chunk==expected[len(received):len(received)+len(chunk)], (begin,len(received),chunk.hex(),
                expected[len(received):len(received)+len(chunk)].hex(),m.stats(),m.get('s_origin'),
                bytes(m.cpu.mem_read(symbols['s_rx']+(len(received)&511),len(chunk))).hex())
            received+=chunk
    assert received==expected and m.stats()['rx_discontinuities']==0, (len(received),len(expected),
        next(((i,a,b) for i,(a,b) in enumerate(zip(received,expected)) if a!=b),None),m.stats())
    passed('4096字节随机批量读取及多圈回绕；重复IDLE/DMA通知不重复交付')
    m=Model();m.receive(bytes(range(256))+bytes(range(64)))
    # 一次同时处理HT，随后未读数据跨TC，再统一读取；任务延迟但IRQ仍采集进度。
    m.call('g431_uart_rx_dma_irq');m.receive(bytes(range(256)),irq=True)
    assert m.stats()['buffer_overruns']==1 and m.read()[0]==DISCONTINUITY
    m.receive(b'after-overrun');assert m.read()[1]==b'after-overrun'
    passed('已知生产超过512即明确覆盖；丢弃旧段并在新epoch恢复')
    m=Model();m.receive(bytes(range(256))+bytes(range(100)))
    # 从位置200出发，一次HT/TC合并，可唯一解释为跨两个边界。
    m=Model();m.receive(bytes(range(200)));assert m.read(256)[1]==bytes(range(200))
    part=bytes(i&255 for i in range(360));m.receive(part,irq=True)
    assert m.read(512)[1]==part and m.stats()['rx_discontinuities']==0
    passed('HT/TC同时挂起时从NDTR和边界旗标还原跨圈增量')
    m=Model();m.receive(bytes(range(256))*3,irq=True)
    assert m.read()[0]==DISCONTINUITY and m.stats()['rx_discontinuities']>=1
    m.receive(b'new');assert m.read()[1]==b'new'
    m=Model();m.receive(b'a'*20);m.advance(9*170000);assert m.read()[0]==DISCONTINUITY
    passed('无法排除整圈覆盖或观测间隔超界时不猜测字节，明确报告连续性丢失')
    m=Model();m.receive(b'a'*100)
    m.after_copy=lambda model:model.receive(b'b'*450)
    assert m.read(100)[0]==DISCONTINUITY and m.get('model_count')==0
    passed('复制期间DMA覆盖会在复制后复核中撤销整份结果')
    for bit,name in [(1,'parity_errors'),(2,'framing_errors'),(4,'noise_errors'),(8,'hardware_rx_overruns')]:
        m=Model();m.receive(b'bad-prefix');m.put(UART+28,bit);m.call('g431_uart_irq')
        assert m.get('model_failure_count')==0 and m.stats()[name]==1 and m.read()[0]==DISCONTINUITY
        m.position=0;m.receive(b'clean-next-frame');assert m.read()[1]==b'clean-next-frame'
    passed('PE/FE/NE/ORE使旧半帧失效并有界重开DMA，不触发内部关断或自动运动')
    m=Model();payload=bytes(range(130));m.cpu.mem_write(symbols['model_source'],payload)
    assert m.call('model_send',130)==OK
    m.cpu.mem_write(symbols['model_source'],b'X'*130)
    assert bytes(m.cpu.mem_read(m.get(TX+12),130))==payload and m.call('model_send',1)==BUSY
    m.tx_dma_complete();assert m.stats()['tx_in_flight'] and m.call('model_send',1)==BUSY
    m.tx_wire_complete();assert not m.stats()['tx_in_flight'] and m.stats()['tx_bytes']==130
    m.call('g431_uart_tx_dma_irq');m.call('g431_uart_irq');assert m.stats()['tx_bytes']==130
    passed('完整TX内部复制，DMA TC至USART TC期间仍BUSY；重复IRQ不重复完成')
    m=Model();assert m.call('model_send',1)==OK
    m.advance(5*170000);assert m.read()[0]==EMPTY
    m.advance(5*170000-1);assert m.read()[0]==EMPTY
    m.advance(1);assert m.read()[0]==HW_ERROR and m.get('model_failure')==2
    assert m.call('model_send',1)==HW_ERROR and m.stats()['tx_errors']==1
    def assert_failed_quiet(model):
        assert model.get(RX)&15==0 and model.get(TX)&15==0
        assert model.get(UART)&((1<<4)|(1<<6)|(1<<8))==0
        assert model.get(UART+8)&((1<<0)|(1<<6)|(1<<7))==0
        model.put(DMA,HT2|TC2);model.put(UART+28,IDLE|2|TC)
        model.call('g431_uart_rx_dma_irq');model.call('g431_uart_tx_dma_irq');model.call('g431_uart_irq')
        assert model.get('model_failure_count')==1 and model.stats()['tx_errors']==1
    assert_failed_quiet(m)
    for channel,flag,entry in [(TX,TE3,'g431_uart_tx_dma_irq'),(RX,TE2,'g431_uart_rx_dma_irq')]:
        m=Model();m.put(DMA,flag);m.call(entry)
        assert m.get('model_failure')==1 and m.read()[0]==HW_ERROR
        assert_failed_quiet(m)
    for stage in ['dma', 'uart']:
        m=Model();assert m.call('model_send',1)==OK
        if stage=='uart':m.tx_dma_complete()
        m.advance(10*170000)
        if stage=='dma':m.tx_dma_complete()
        else:m.tx_wire_complete()
        assert m.get('model_failure')==2 and m.stats()['tx_bytes']==0
        assert_failed_quiet(m)
    passed('10ms发送截止不被迟到TC解除；内部失败只锁一次并关闭RX/TX中断源，不覆盖或重试')
    m=Model();m.cpu.reg_write(UC_ARM_REG_BASEPRI,0x30);m.cpu.reg_write(UC_ARM_REG_PRIMASK,1)
    m.read();assert m.cpu.reg_read(UC_ARM_REG_BASEPRI)==0x30 and m.cpu.reg_read(UC_ARM_REG_PRIMASK)==1
    m=Model();m.put('s_stats',0)  # 后面分别设置生产/消费接近u32回绕。
    stats=symbols['s_stats'];m.put(stats+40,0xFFFFFFF0);m.put(stats+44,0xFFFFFFF0);m.put('s_origin',0xFFFFFFF0)
    m.receive(b'wrap'*12,irq=True);assert m.read()[1]==b'wrap'*12 and m.stats()['produced_bytes']==32
    passed('临界区保留调用者屏蔽状态，逻辑生产/消费u32回绕仍按ring origin读取')

if args.report:
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(dict(status='PASS',groups=groups,hardware_access=False,
        execution='ARMCC Cortex-M4F + Unicorn; simulated DMA/NVIC/W1C and bytewise C-library copy substitute; not real UART timing',
        source_sha256={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in [Path(__file__),*sources,TARGET/'runtime/target_communication_config.h']}),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
